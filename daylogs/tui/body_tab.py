"""Tab 2 — weight and food for one day.

Holds no arithmetic. Weight deltas come from body.weight_delta, the day's calorie
total from body.day_kcal, BMR from body.compute_bmr, and the plot from chart.py.
What lives here is key handling, the estimate review flow, and rendering.
"""

from __future__ import annotations

import datetime as dt

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Static

from daylogs import body, estimate, parse, photo, sigil
from daylogs import horizon as hz
from daylogs.config import load_config, update_config
from daylogs.fmt import hhmm, human_date, wall
from daylogs.parse import (
    ParseError,
    parse_activity,
    parse_food,
    parse_profile,
    parse_weigh,
)
from daylogs.tui import chart
from daylogs.tui.common import PanelTab
from daylogs.tui.widgets import (
    FAINT,
    arrow,
    burn_bar,
    mark,
    sparkline,
    trend_style,
    view_row,
)

_CHART_H = 8
_YLABEL_W = 7
# Deliberately not horizon.DEFAULT ("MTD"). A weight trend reads better over a
# rolling month than over a month-to-date window that is one day wide on the 1st;
# Money wants MTD because a budget is a calendar-month thing. The horizon *list* is
# shared, the starting point is per-tab.
_DEFAULT_HORIZON = "1m"
# What the in-progress popup says while each call is in flight. The words used to be a
# suffix on the FOOD header and in the footer's state row — which fixed the original
# defect (a 3-second toast against a 60-second call) but kept the indicator on the tab
# that started the work, so pressing `3` erased every trace of a running estimate. The
# popup is app-level and keyed, so the two can also run at once; the keys are the worker
# groups they belong to.
_WORK_FOOD = ("food", "estimating calories")
_WORK_ACTIVITY = ("activity", "estimating the day's activity factor")
# The sub-views `tab` walks, in the order the strip draws them. Named so the header,
# the strip and the toggle cannot disagree about what exists. Each name is also its
# table's name, which is what lets the row lookup, the edit prefill and the delete
# handler stay one branch each instead of three.
_VIEWS = ("weight", "food", "activity")
# The series the TREND panel plots, in the order `c` walks them. One panel rather than
# a fourth one: side by side, three panels do not fit 110 columns, and the axis, the
# horizon and the width arithmetic already live here. Weight stays in the cycle because
# it is the same question about the same window.
_CHARTS = ("weight", "intake", "net")
# A safety net on the three table queries, not an advertised number. All three are
# filtered by the tab's span now, so each header states the count it actually got; this
# only bounds a pathological "all time". Matches money's `_LIMIT_CAP` in spirit — and if
# it ever bites, the header says so rather than silently showing a prefix.
_ROW_CAP = 2000
# The number each sub-view sorts by under `k`, and the word the state row shows for it.
# One per table, which is why `k` is a single key rather than three.
_VALUE_LABEL = {"weight": "kg", "food": "kcal", "activity": "factor"}
_ARROW = {True: "↓", False: "↑"}


class BodyTab(PanelTab):
    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.viewing_date = ""
        self.table_mode = "food"
        self.horizon = _DEFAULT_HORIZON
        # Which series the TREND panel plots. Independent of `horizon` and of
        # `table_mode` on purpose: "what am I looking at" and "over what window" are
        # two questions, and zooming must not reset the answer to the first.
        self.chart_mode = "weight"
        self._pending: estimate.Estimate | None = None
        self._pending_photo: tuple[object, bool] | None = None
        self._ids: list[int] = []
        # The row an open edit prompt belongs to. InlinePrompt carries a label and
        # text but no id, so the tab holds the identity between open and submit.
        self._editing: tuple[str, int] | None = None
        # An estimate is running. Set at the top of the worker and cleared on the two
        # paths that end one — deliberately NOT in a `finally`, which is the tempting
        # shape and the wrong one here. `@work(exclusive=True)` only ever cancels this
        # worker because a replacement is starting, and the replacement sets the flag
        # itself; a `finally` would clear it in the gap between the two and blink the
        # indicator off mid-estimate. Same shape as summary_tab's `busy`.
        self._estimating = False
        # An activity factor is being inferred. A separate flag *and* a separate worker
        # group from the food estimate: the two are unrelated questions, and sharing
        # the exclusive group would mean logging a gym session silently killed a meal
        # estimate you were still waiting on.
        self._inferring = False
        # The inferred factor waiting to be confirmed, so the confirm line can be
        # submitted unchanged and still land a number.
        self._pending_activity: estimate.Effort | None = None
        # The FOOD header line, assigned in whichever branch of `reload` built it.
        self._food_head = ""
        # The kcal of the food rows the table actually listed, so the header's window
        # total is the sum of what is on screen rather than a second query that could
        # disagree with it — including when the row cap bites.
        self._kcal_shown: list[int] = []
        # Sort state, deliberately not per sub-view: `tab` keeps the sort, the way Money
        # keeps it across panes. The same two attributes `MoneyView` carries.
        self.sort_field = "date"
        self.sort_desc = True

    def compose(self) -> ComposeResult:
        yield Static(id="weight-head", classes="pane-title")
        # Two panels side by side: the trend, and the energy balance. v2 left the
        # right two-thirds of the screen empty.
        with Horizontal(classes="panel-row"):
            with Vertical(classes="panel", id="panel-trend"):
                yield Static(id="trend-title", classes="panel-title")
                yield Static(id="weight-chart", classes="chart")
            with Vertical(classes="panel", id="panel-energy"):
                yield Static("ENERGY", classes="panel-title")
                yield Static(id="energy-body", classes="panel-body")
        yield Static(id="food-head", classes="pane-title")
        # Same position and class as Money's pane strip, so both tabs answer
        # "which view am I on" in the same place on screen.
        yield Static(id="body-views", classes="muted")
        yield DataTable(id="body-table", cursor_type="row")
        yield Static(id="inbox-line", classes="muted")

    def on_mount(self) -> None:
        self.viewing_date = self.app.today()

    def focus_default(self) -> None:
        self.query_one("#body-table", DataTable).focus()

    def cancel_editing(self) -> None:
        """Drop everything an abandoned prompt was carrying.

        Not just the edit id. `_pending_photo` says "this inbox file belongs to the
        row about to be written", and it is consumed — the file is *moved* into
        `processed/` — by whichever `confirm food` submission comes next. Leave it
        armed after someone escapes the confirm prompt and the next typed meal, with
        nothing to do with the photo, eats it: no food row for it, and it vanishes
        from the inbox count with no message. `_pending` is dropped for the same
        reason, so a stale estimate cannot supply calories to an unrelated line.
        """
        self._editing = None
        self._pending = None
        self._pending_photo = None
        self._pending_activity = None

    def span(self) -> hz.Span:
        return hz.resolve(self.horizon, anchor=self.viewing_date or self.app.today())

    def status_hint(self) -> str:
        """The tab's state: which day, which horizon, which sort. Not what is running — the
        popup says that, in one place, for whichever tab you are looking at.

        The sort shows both fields with the active one marked, the same shape Money uses:
        seeing the alternative beside the choice says what else `d`/`k` offer without
        pressing `?`. The value field is named for the table on screen — `kcal` on food,
        `kg` on weight, `factor` on activity — because that is the word the keymap's static
        label cannot carry.
        """
        date = self.viewing_date or self.app.today()
        parts = [self.horizon]
        if date != self.app.today():
            parts.insert(0, human_date(date))
        fields = " ".join(
            mark(f"{_ARROW[self.sort_desc]}{label}", "bold")
            if field == self.sort_field
            else mark(label, FAINT)
            for field, label in (("date", "date"), ("value", _VALUE_LABEL[self.table_mode]))
        )
        return f"{' · '.join(parts)}    {mark('sort', FAINT)} {fields}"

    # ── rendering ────────────────────────────────────────────────────────
    def reload(self) -> None:
        conn, cfg = self.app.conn, self.app.cfg
        date = self.viewing_date or self.app.today()

        latest = body.latest_weight(conn, on_or_before=date)
        kg = latest["kg"] if latest else None
        if kg is None:
            head = "WEIGHT   no weigh-in yet — press w"
        else:
            d7 = body.weight_delta(conn, end_date=date, days=7)
            d30 = body.weight_delta(conn, end_date=date, days=30)
            head = f"WEIGHT   {kg:g} kg  ({hhmm(latest['measured_at'], cfg.timezone)})"
            # A bare number, next to the weight it restates. No band and no colour:
            # "overweight" is a judgement this app does not otherwise make, and there
            # is no BMI chart for the same reason — it is the weight curve times a
            # constant.
            index = body.bmi(cfg, kg)
            if index is not None:
                head += f"   BMI {index:.1f}"
            head += f"   {_delta(d7, '7d')}   {_delta(d30, '30d')}"
            # The reading is the latest *on or before* the viewed day, which can
            # be much older. Showing a stale number as today's is the kind of
            # quiet wrongness that makes a tracker untrustworthy.
            if latest["date"] != date:
                head += f"   (last weighed {human_date(latest['date'])})"
        span = self.span()
        head += f"   ·  {span.label}"
        self.query_one("#weight-head", Static).update(head)

        self.query_one("#trend-title", Static).update(
            "TREND   " + view_row(_CHARTS, self.chart_mode)
        )
        self.query_one("#weight-chart", Static).update(
            "\n".join(self._chart_rows(conn, cfg, span))
        )

        kcal = body.day_kcal(conn, date=date)
        bmr = body.compute_bmr(cfg, kg, today=date)
        # Resolved through the data layer rather than multiplied here: four surfaces
        # read the same burn, and four compositions is four chances for one panel to
        # measure the same day against two different baselines.
        factor, origin = body.resolved_factor(conn, cfg, date=date)
        burn = body.day_tdee(conn, cfg, date=date)
        self.query_one("#energy-body", Static).update(
            self._energy_panel(
                conn, cfg, kcal=kcal, bmr=bmr,
                burn=burn, factor=factor, origin=origin, span=span,
            )
        )

        # Filled before the header is composed, so the weight header can state the
        # count the query actually returned rather than a number it hopes is right.
        self._fill_table(span)

        # The header describes the table directly beneath it, so it follows table_mode —
        # it used to read "FOOD … kcal in / BMR → net" even while the table listed
        # weigh-ins, and a label that is wrong is worse than one that is missing.
        #
        # Three tables over one window, so three headers of one shape: what you are
        # looking at, the window, and how many rows are in it. The day's own energy
        # balance moved out rather than being duplicated per view — the ENERGY panel
        # already carried every figure of it, and CLAUDE.md warns that each extra surface
        # reading `burn` is another chance for two panels to disagree about a day.
        self._food_head = self._table_head(span)
        self.query_one("#food-head", Static).update(self._food_head)
        self.query_one("#body-views", Static).update(view_row(_VIEWS, self.table_mode))

        pending = photo.pending_count(cfg.inbox_dir)
        line = self.query_one("#inbox-line", Static)
        line.update(f"{pending} photo{'s' if pending != 1 else ''} in inbox — press p")
        line.display = pending > 0

    def _local(self, epoch: int) -> dt.datetime:
        """An epoch as a wall-clock time in the configured zone.

        The conversion belongs here rather than in `horizon`, which is pure and has
        no business knowing about `cfg`. A UTC reading at 02:00 Toronto is the
        previous evening locally, and plotting it on the wrong day is the same class
        of bug as the date traps this repo keeps hitting.
        """
        return wall(epoch, self.app.cfg.timezone)

    def _chart_rows(self, conn, cfg, span) -> list[str]:
        """The TREND panel's plot, for whichever series is selected.

        All three share the axis, the horizon and the panel width; what differs is the
        data and whether zero belongs in the vertical extent. Weight fits itself —
        anchored at zero a 70-75 kg series is a flat line at the top of the panel —
        while a calorie series is a magnitude from zero, and a signed net is unreadable
        without knowing which side of zero it sits on.
        """
        # Width from the panel, not a constant: a hardcoded width wider than the panel
        # makes every chart row wrap, doubling its height and looking broken.
        common = dict(
            width=self._chart_width(), height=_CHART_H, ylabel_width=_YLABEL_W, unit=""
        )
        if self.chart_mode == "weight":
            # Plot against real time, not point index. Two readings a day apart were
            # being spread across a month-wide panel as a smooth climb; at their true
            # positions they sit together at the right edge with the unweighed weeks
            # visibly empty, which is the honest picture.
            #
            # How many points depends on how wide the window is. Over a month the
            # per-day collapse keeps the trend readable, because weight swings a kilo
            # inside a day. Over one to three days that collapse hides exactly what you
            # zoomed in for, so every reading is plotted.
            if span.hourly:
                moments = body.weight_points_between(conn, start=span.start, end=span.end)
            else:
                moments = [
                    (at, kg_) for _, kg_, at in
                    body.weight_series_between(conn, start=span.start, end=span.end)
                ]
            when = [self._local(at) for at, _ in moments]
            ax = hz.axis(span, [w.date().isoformat() for w in when])
            # The extent comes from every reading in the window, not from the points
            # drawn. Over a month the line is one point per day, so fitting it to those
            # labelled the top of a week as 81.85 while a reading of 82.65 sat inside it —
            # a range the window does not have. On the hourly views the two sets are the
            # same, so this changes nothing there.
            everything = [
                kg for _, kg in body.weight_points_between(
                    conn, start=span.start, end=span.end
                )
            ]
            return chart.frame_chart(
                [v for _, v in moments],
                x_labels=ax.labels(),
                positions=ax.fractions_at(when),
                low=min(everything) if everything else None,
                high=max(everything) if everything else None,
                **common,
            )

        if self.chart_mode == "intake":
            series = body.kcal_series_between(conn, start=span.start, end=span.end)
        else:
            # Each day against its own burn. `body.net_series_between` skips days with
            # no food and days with no resolvable burn, so a gap stays a gap rather
            # than plotting as a day of nothing eaten.
            series = body.net_series_between(conn, cfg, start=span.start, end=span.end)
        # A day is a day here, however wide the window: calories are totalled per day,
        # so there is no finer resolution for an hourly horizon to reveal.
        ax = hz.axis(span, [d for d, _ in series])
        return chart.frame_chart(
            [float(v) for _, v in series],
            x_labels=ax.labels(),
            positions=ax.fractions([d for d, _ in series]),
            include_zero=True,
            **common,
        )

    def _chart_width(self) -> int:
        """Braille cells inside the trend panel, after the y labels and the axis."""
        return max(16, self.panel_width("#panel-trend", minimum=24) - _YLABEL_W - 1)

    # (name, key that adds one, singular, plural) per sub-view. As data, so the three
    # headers cannot drift into three shapes again.
    _HEADS = {
        "weight": ("WEIGHT", "w", "weigh-in", "weigh-ins"),
        "activity": ("ACTIVITY", "a", "activity", "activities"),
        "food": ("FOOD", "f", "meal", "meals"),
    }

    def _table_head(self, span) -> str:
        """`NAME   window   how much is in it`, plus the key that fills an empty one.

        A bare `0` for an empty window would be "true, useless, and reads as stale data" —
        the same reason Money's empty state names `r`. Each names both the key that adds a
        row and that `-` widens the window, because at `1d` on a day you did not log, the
        rows very likely exist one day over.
        """
        name, key, singular, plural = self._HEADS[self.table_mode]
        n = len(self._ids)
        if n == 0:
            return f"{name}   {span.label}   nothing logged — press {key}, or - to widen"
        # The cap is a safety net on a pathological "all time", so the number shown is
        # always the number of rows actually listed — and it says when it bit.
        capped = "  (capped)" if n >= _ROW_CAP else ""
        head = f"{name}   {span.label}   {n} {singular if n == 1 else plural}{capped}"
        if self.table_mode == "food":
            # The window's intake: the sum of the rows on screen, so it agrees with them
            # even when the cap bit. Deliberately not a window `net` — a factor describes
            # one day, so net over a window is computed per day (`net_average`, in the
            # ENERGY panel) and never as one subtraction.
            head += f"   {sum(self._kcal_shown):,} kcal in"
        return head

    def _energy_panel(
        self, conn, cfg, *, kcal, bmr, burn, factor, origin, span
    ) -> str:
        """Intake against maintenance for the day, then the same over the horizon.

        A calorie count means nothing without a baseline, which is why the baseline
        sits next to it rather than on its own line elsewhere.

        With an activity factor the baseline is `burn` — what the day actually cost —
        and the two rows that derive it stay on screen. That is deliberate: a factor
        rescales every calorie judgement for its day, so an inferred number with
        nothing to make you doubt it would quietly become the baseline for everything.
        Without a factor every line here is exactly what it was before.
        """
        lines: list[str] = []
        if bmr is None:
            lines.append(f"  in        {kcal:>7,} kcal")
            # Name the key, not the file. Telling someone to edit config.toml is
            # how this panel stayed empty: the fix was real but nobody was going
            # to leave the app to apply it.
            lines.append("  BMR             —   press h to set height,")
            lines.append("                      sex and birthday")
        else:
            # What `net` is measured against, and the one value the bar and the
            # percentage may use — naming it once is what keeps this panel from
            # measuring the same day against two different baselines.
            against = bmr if burn is None else burn
            lines.append(f"  in        {kcal:>7,} kcal")
            if burn is None:
                lines.append(f"  BMR      −{bmr:>7,}")
                # An empty state names the fix, and this is where the fallback actually
                # happens: with no factor the day is measured against *resting* BMR, and
                # the panel used to show that without a word about it. The guidance lived
                # in the ACTIVITY header, which now describes a window rather than a day.
                # Named as a key, not a file — telling someone to edit config.toml is how
                # this panel stayed empty before.
                # Two lines, like the BMR guidance above: one line of 52 cells clipped to
                # "press h to set an ordin…" in a 46-column panel, and `.panel-body` uses
                # `text-overflow: ellipsis`, so it truncated silently while a substring
                # assertion on its prefix went on passing.
                lines.append("  activity        —   press h to set an")
                lines.append("                      ordinary day")
            else:
                # BMR and activity derive `burn`; `in`, `burn` and `net` are the sum.
                # So BMR drops its minus sign: it is no longer a term of `net`, and a
                # column of signs that does not add up is worse than no signs.
                lines.append(f"  BMR       {bmr:>7,}")
                lines.append(f"  activity  {f'×{factor:g}':>7}  {origin}")
                lines.append(f"  burn     −{against:>7,}")
            lines.append("            ─────────")
            lines.append(f"  net       {kcal - against:>+7,}")
            if kcal:
                lines.append("")
                bar_w = max(10, self.panel_width("#panel-energy", minimum=24) - 6)
                lines.append(f"  {burn_bar(kcal, against, width=bar_w, marker_frac=None)}")
                lines.append(f"  {round(kcal / against * 100)}% of maintenance")

        avg = body.kcal_average(conn, start=span.start, end=span.end)
        logged = body.kcal_series_between(conn, start=span.start, end=span.end)
        lines.append("")
        lines.append(f"  over {span.horizon}")
        if avg is None:
            lines.append("    no food logged in this window")
        else:
            lines.append(f"    avg in    {avg:>7,} kcal  ({len(logged)} days logged)")
            # Per day, not this average minus today's burn: a factor describes one
            # day, so a single gym session must not restate the whole window.
            avg_net = body.net_average(conn, cfg, start=span.start, end=span.end)
            if avg_net is not None:
                lines.append(f"    avg net   {avg_net:>+7,}")
            spark_w = max(10, self.panel_width("#panel-energy", minimum=24) - 6)
            lines.append(
                # Calories are a magnitude from zero, so scale from zero: a run of
                # similar days is a plateau, not a floor.
                f"    {sparkline([float(v) for _, v in logged], width=spark_w, from_zero=True)}"
            )
        return "\n".join(lines)

    def _fill_table(self, span) -> None:
        """All three tables over the tab's one window.

        The window is the only thing they filter by — the viewed date reaches them solely as
        the span's right-hand edge, which is why this takes no date. `+`/`-` used to move
        the chart and the weight table and do nothing at all to the other two — one row at
        `1d` and the same one row at `all` — while the chart above the food table went on
        plotting weight. `1d` selects exactly the rows the old per-day view did, because
        `horizon.resolve` gives it `start == end == the anchor` — newest-first, like every
        other horizon.

        Every row carries its date as well as its clock time: over a window, two `08:00`
        rows five days apart are otherwise the same morning. `span.start` is None for "all
        time", which the queries read as no lower bound.
        """
        table = self.query_one("#body-table", DataTable)
        table.clear(columns=True)
        self._ids = []
        self._kcal_shown = []
        tz = self.app.cfg.timezone
        bounds = dict(since=span.start, until=span.end, limit=_ROW_CAP,
                      sort=self.sort_field, desc=self.sort_desc)
        if self.table_mode == "weight":
            table.add_columns("date", "time", "kg", "note")
            for r in body.list_weight(self.app.conn, **bounds):
                # The time is here because `measured_at` decides which of a day's
                # readings the trend uses and which the headline shows. Two weigh-ins on
                # one day were two rows with the same date and no way to tell them apart.
                table.add_row(
                    r["date"], hhmm(r["measured_at"], tz), f"{r['kg']:g}",
                    Text(r["note"] or ""),
                )
                self._ids.append(r["id"])
        elif self.table_mode == "activity":
            table.add_columns("date", "time", "description", "factor", "src")
            for r in body.list_activity(self.app.conn, **bounds):
                table.add_row(
                    r["date"],
                    hhmm(r["logged_at"], tz),
                    Text(r["description"]),
                    # A dash, not a blank: an inference that never landed is a state
                    # worth seeing, because the day quietly used the baseline instead.
                    "—" if r["factor"] is None else f"×{r['factor']:g}",
                    "lab" if r["source"] == "labeled" else "est",
                )
                self._ids.append(r["id"])
        else:
            table.add_columns("date", "time", "description", "kcal", "src")
            for r in body.list_food(self.app.conn, **bounds):
                table.add_row(
                    r["date"],
                    hhmm(r["ate_at"], tz),
                    # Text, not str: a `str` cell is parsed as markup, so a description
                    # containing `[/b]` raised out of the render and one containing
                    # `[work]` quietly lost the word.
                    Text(r["description"]),
                    f"{r['kcal']:,}",
                    "lab" if r["source"] == "labeled" else "est",
                )
                self._ids.append(r["id"])
                self._kcal_shown.append(int(r["kcal"]))

    def _selected_row(self):
        table = self.query_one("#body-table", DataTable)
        row = table.cursor_row
        if row is None or not (0 <= row < len(self._ids)):
            return None
        row_id = self._ids[row]
        # The view name is the table name, which is why this is not a three-way branch.
        which = self.table_mode
        return self.app.conn.execute(
            f"SELECT * FROM {which} WHERE id = ?", (row_id,)
        ).fetchone()

    # ── keys ─────────────────────────────────────────────────────────────
    def key_weigh(self) -> None:
        self.app.prompt.open("weigh")

    def key_profile(self) -> None:
        cfg = self.app.cfg
        current = " ".join(
            str(v)
            for v in (
                f"{cfg.height_cm:g}" if cfg.height_cm else "",
                cfg.sex or "",
                cfg.birthday or "",
                cfg.activity or "",
                cfg.timezone,
            )
            if v
        )
        self.app.prompt.open("profile", prefill=current)

    def key_food(self) -> None:
        self.app.prompt.open("food")

    def key_activity(self) -> None:
        self.app.prompt.open("activity")

    def key_activate(self) -> None:
        """enter edits the row under the cursor.

        Body had no key_activate at all, so enter did nothing here — the edit path
        was specified in the original design and never wired up.
        """
        row = self._selected_row()
        if row is None:
            self.app.notify("no row selected", timeout=3)
            return
        if self.table_mode == "weight":
            self._editing = ("weight", row["id"])
            self.app.prompt.open(
                "weigh", prefill=parse.render_weigh(row, self.app.cfg.timezone)
            )
        elif self.table_mode == "activity":
            self._editing = ("activity", row["id"])
            self.app.prompt.open(
                "activity", prefill=parse.render_activity(row, self.app.cfg.timezone)
            )
        else:
            self._editing = ("food", row["id"])
            self.app.prompt.open(
                "food", prefill=parse.render_food(row, self.app.cfg.timezone)
            )

    def key_next_subview(self) -> None:
        self._step_subview(1)

    def key_prev_subview(self) -> None:
        self._step_subview(-1)

    def _step_subview(self, delta: int) -> None:
        """Walk the strip. Directional now that there are three views — while there
        were two, `prev` was an alias for `next` and shift+tab happened to be right."""
        self.table_mode = _VIEWS[(_VIEWS.index(self.table_mode) + delta) % len(_VIEWS)]
        self.reload()

    def key_prev_period(self) -> None:
        self._shift(-1)

    def key_next_period(self) -> None:
        self._shift(1)

    def _shift(self, delta: int) -> None:
        """Step the anchor by one whole horizon, the way Money always has.

        It stepped one calendar day, which was the right size while the food and activity
        tables were per-day. Against a windowed tab it means thirty presses to page back a
        month — and at `all` it moved the right edge of a window whose header says ALL TIME,
        so the list silently stopped including today while claiming to show everything.
        `horizon.shift` makes `all` a no-op, because `_STEP["all"]` is `(0, 0)`.
        """
        anchor = self.viewing_date or self.app.today()
        self.viewing_date = hz.shift(self.horizon, anchor, delta)
        self.reload()

    def key_jump_now(self) -> None:
        self.viewing_date = self.app.today()
        self.reload()

    def _sort(self, field: str) -> None:
        """Same field flips direction; a different field switches and resets to descending.
        The rule `MoneyView.set_sort` already uses — four lines, kept beside the keys that
        call it rather than shared, because the two tabs hold their state differently."""
        if self.sort_field == field:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_field = field
            self.sort_desc = True
        self.reload()

    def key_sort_date(self) -> None:
        self._sort("date")

    def key_sort_value(self) -> None:
        self._sort("value")

    def key_next_chart(self) -> None:
        """Walk the TREND panel's series. One key rather than three, and cycling
        rather than a prompt, because it is the same shape as `tab` on the sub-views."""
        self.chart_mode = _CHARTS[(_CHARTS.index(self.chart_mode) + 1) % len(_CHARTS)]
        self.reload()

    def key_zoom_in(self) -> None:
        # HORIZONS runs short -> long, so magnifying is a step *back* along it.
        self.horizon = hz.next_horizon(self.horizon, -1)
        self.reload()

    def key_zoom_out(self) -> None:
        self.horizon = hz.next_horizon(self.horizon, 1)
        self.reload()

    def key_delete(self) -> None:
        row = self._selected_row()
        if row is None:
            self.app.notify("nothing selected", timeout=3)
            return
        mode = self.table_mode
        if mode == "weight":
            label = f"{row['kg']:g} kg on {row['date']}"
        elif mode == "activity":
            factor = "no factor" if row["factor"] is None else f"×{row['factor']:g}"
            label = f"{row['description']} ({factor})"
        else:
            label = f"{row['description']} ({row['kcal']:,} kcal)"
        self.app.ask_confirm(
            f"delete {label}?   y/n", lambda: self._do_delete(mode, row["id"])
        )

    def _do_delete(self, mode: str, row_id: int) -> None:
        table = mode
        fn = {
            "weight": body.delete_weight,
            "food": body.delete_food,
            "activity": body.delete_activity,
        }[mode]
        row = fn(self.app.conn, row_id)
        if row is None:
            return
        self.app.undo_stack.push(table, row)
        self.app.notify("deleted · u to undo", timeout=4)
        self.reload()

    def key_photo(self) -> None:
        cfg = self.app.cfg
        clip = photo.clipboard_image(cfg.root / ".tmp")
        if clip is not None:
            self._estimate_photo(clip, from_inbox=False)
            return
        nxt = photo.next_inbox_image(cfg.inbox_dir)
        if nxt is not None:
            self._estimate_photo(nxt, from_inbox=True)
            return
        self.app.prompt.open("photo path")

    def _estimate_photo(self, path, *, from_inbox: bool) -> None:
        self._pending_photo = (path, from_inbox)
        self._run_image_estimate(path)

    # ── workers ──────────────────────────────────────────────────────────
    def _set_estimating(self, running: bool) -> None:
        """Flip the flag and tell the popup.

        Deliberately still not `reload()`: that calls `_fill_table`, which does
        `table.clear(columns=True)` and so resets the DataTable cursor to row 0 —
        starting an estimate would throw away whichever food row you had selected. The
        popup is a sibling widget that repaints itself, so there is nothing else to
        touch, which is the whole reason the indicator moved off the header.
        """
        self._estimating = running
        if running:
            self.app.begin_work(*_WORK_FOOD, self.app.cfg.estimate_timeout_sec)
        else:
            self.app.end_work(_WORK_FOOD[0])

    def _set_inferring(self, running: bool) -> None:
        """The activity-factor counterpart. Its own popup key, matching its own worker
        group: logging a gym session must not look like it cancelled a meal estimate."""
        self._inferring = running
        if running:
            self.app.begin_work(*_WORK_ACTIVITY, self.app.cfg.estimate_timeout_sec)
        else:
            self.app.end_work(_WORK_ACTIVITY[0])

    @work(exclusive=True)
    async def _run_image_estimate(self, path) -> None:
        self._set_estimating(True)
        try:
            est = await estimate.from_image(
                image_path=path,
                note=None,
                runner=self.app.runner_image,
                timeout_sec=self.app.cfg.estimate_timeout_sec,
                model=self.app.cfg.claude_model,
            )
        except Exception as e:  # surfaced to the user, not swallowed
            self._pending_photo = None
            self._set_estimating(False)
            self.app.notify_error(f"photo estimate failed: {e}")
            return
        self._set_estimating(False)
        self._offer(est)

    @work(exclusive=True)
    async def _run_text_estimate(self, r) -> None:
        # A typed estimate supersedes any photo waiting to be confirmed — it shares
        # the exclusive group, so starting this cancels the image worker, and a
        # cancelled worker never reaches the `except` that would have released the
        # photo. Left armed, the file would be consumed by THIS estimate's confirm.
        self._pending_photo = None
        self._set_estimating(True)
        try:
            est = await estimate.from_text(
                description=r.description,
                runner=self.app.runner_json,
                timeout_sec=self.app.cfg.estimate_timeout_sec,
                model=self.app.cfg.claude_model,
            )
        except Exception as e:  # surfaced to the user, not swallowed
            self._set_estimating(False)
            self.app.notify_error(f"estimate failed: {e}")
            return
        self._set_estimating(False)
        self._offer(est, when=parse.When(date=r.date, at=r.at))

    def _offer(self, est: estimate.Estimate, *, when: parse.When | None = None) -> None:
        """Show the estimate as an editable line. Correcting it goes through the
        same grammar as typing it, so there is one code path, not two.

        Which is exactly why the `@` has to be *on* the line. The confirm line is
        re-parsed, and the grammar resolves an absent `@` to now — so `f pizza and salad
        @09-04/19:30` asked for yesterday's dinner and wrote today's, at whatever the clock
        said when Claude answered, with nothing on screen saying so. Two days' net figures
        were then wrong. The failure path never had this bug, because it writes the parsed
        row directly: the same keypress was right when Claude was down and wrong when it
        answered.

        `when` is optional because `_run_image_estimate` has no typed line to preserve — a
        photo is being estimated now, and today is the right answer there.
        """
        self._pending = est
        line = f"{sigil.escape(est.description)} ={est.kcal}"
        if when is not None:
            line += f" @{when.date}/{hhmm(when.at, self.app.cfg.timezone)}"
        # `owner=self.id`, unlike every other prompt here: this one is opened by a worker
        # up to a minute after the keypress, so the active tab is whatever the user wandered
        # to while waiting — and the answer belongs to Body regardless.
        self.app.prompt.open("confirm food", line, owner=self.id)

    # ── prompt handling ──────────────────────────────────────────────────
    def handle_prompt(self, label: str, value: str) -> None:
        """Malformed input raises; the app catches it, keeps the text, and shows
        why. Catching it here would discard the line the user typed."""
        if not value:
            self._pending = None
            self._pending_photo = None
            return
        if label == "weigh":
            self._submit_weigh(value)
        elif label == "food":
            self._submit_food(value)
        elif label == "confirm food":
            self._submit_confirmed_food(value)
        elif label == "activity":
            self._submit_activity(value)
        elif label == "confirm activity":
            self._submit_confirmed_activity(value)
        elif label == "photo path":
            self._estimate_photo(photo.resolve_path(value), from_inbox=False)
        elif label == "go to date":
            self._submit_goto(value)
        elif label == "profile":
            self._submit_profile(value)

    def _take_editing(self, which: str) -> int | None:
        """The id an open edit prompt belongs to, consumed once.

        Cleared on read so a stale id cannot be reused by the next submission — the
        row may have been deleted in between.

        A *rejected* submission does not lose it, though: `App.on_input_submitted` puts the
        id back before re-opening the prompt, because the read is not the end of the edit.
        `_submit_food` and `update_recurring` both raise after it, and the retry INSERTed.
        """
        if self._editing is None or self._editing[0] != which:
            return None
        _, row_id = self._editing
        self._editing = None
        return row_id


    def _submit_profile(self, value: str) -> None:
        """Persist to config.toml and reload, so BMR appears on this keystroke.

        `load_config` is re-read from the app's own root rather than the default,
        or a DAYLOGS_HOME-based run would write one file and read another.
        """
        profile = parse_profile(value)
        update_config(self.app.cfg.root / "config.toml", profile.fields())
        self.app.cfg = load_config(self.app.cfg.root)
        cfg = self.app.cfg
        if body.compute_bmr(cfg, 70.0, today=self.app.today()) is None:
            missing = ", ".join(
                name
                for name, ok in (
                    ("height", cfg.height_cm),
                    ("sex", cfg.sex),
                    ("birthday", cfg.birthday),
                )
                if not ok
            )
            self.app.notify(f"saved — still need {missing} for BMR", timeout=5)
        elif body.baseline_factor(cfg) is None:
            # An empty state names the fix. Resting BMR is not maintenance, and
            # nothing else on screen would tell you that a level is what turns one
            # into the other.
            self.app.notify(
                "profile saved — add desk, light, active or heavy for maintenance",
                timeout=6,
            )
        else:
            self.app.notify("profile saved", timeout=3)
        self.app.refresh_tabs()

    def _submit_goto(self, value: str) -> None:
        # One shared rule. This used to build a throwaway MoneyView as a parser and
        # then re-derive the date itself, which broke the day the month branch
        # started returning the month's last day.
        self.viewing_date = hz.resolve_goto(value)
        self.reload()

    def _submit_weigh(self, value: str) -> None:
        r = parse_weigh(value, now=self.app.now())
        row_id = self._take_editing("weight")
        if row_id is None:
            body.add_weight(self.app.conn, kg=r.kg, date=r.date, at=r.at, note=r.note)
            self.viewing_date = r.date
            self.reload()
            d7 = body.weight_delta(self.app.conn, end_date=r.date, days=7)
            trend = "" if d7 is None else f" · {arrow(d7)}{abs(d7):g} vs 7d"
            self.app.notify(f"{r.kg:g} kg logged{trend}", timeout=4)
            return
        before = self.app.conn.execute(
            "SELECT * FROM weight WHERE id = ?", (row_id,)
        ).fetchone()
        if before is None:
            self.app.notify("that row is gone", timeout=3)
            return
        # `render_weigh` emits the time now, because the table shows it — so an edit can
        # move it, and has to be able to. Restamped only when the minute actually changed:
        # `measured_at` carries seconds, the grammar's only time token is HH:MM, and those
        # seconds are the tie-breaker `weight_series_between` uses to pick a day's reading. That is
        # precisely the case `body.restamp` was written for.
        at = self._restamp_for(
            before["measured_at"], parsed_at=r.at, date=r.date, line=value
        )
        body.update_weight(
            self.app.conn, row_id, kg=r.kg, date=r.date, note=r.note or "", measured_at=at
        )
        self.app.undo_stack.push("weight", dict(before))
        self.app.notify(f"{r.kg:g} kg on {r.date} · u to undo", timeout=4)
        self.reload()

    def _line_sets_a_time(self, value: str) -> bool:
        r"""Whether the line actually carried an @time.

        Not `"@" in value`: an escaped `\@` inside a description is a plain word, and
        reading raw text for a sigil is the scavenging this grammar exists to remove.

        Every `/`-separated part is checked, not just the last. `parse.resolve_when` is
        deliberately order-free, so `@09:30/2026-09-04` means exactly what
        `@2026-09-04/09:30` means — and matching only the tail read the reversed form as
        "no time given", so an edit that plainly set one silently kept the old stamp.
        """
        for tok in sigil.tokenize(value):
            if tok.sigil == "@" and any(
                parse.TIME_RE.match(part) for part in tok.value.split("/")
            ):
                return True
        return False

    def _restamp_for(self, stored: int, *, parsed_at: int, date: str, line: str) -> int:
        """The timestamp an edited row should carry.

        One rule for every case, because the three that matter each went wrong on their own
        before: a line naming both a date and a time restamps to it; a line naming only a
        time keeps the anchor's date; a line naming only a **date** keeps the row's own
        clock time and moves it to that date — the grammar resolves a missing time to *now*,
        so restamping from the parsed instant would move a 07:05 reading to whenever the
        edit happened, and keeping the stored stamp instead left it on the old day, which
        inverts `morning_weight` and `latest_weight`. A line naming neither changes nothing,
        because `restamp` then compares equal and returns None.
        """
        tz = self.app.cfg.timezone
        clock = hhmm(parsed_at if self._line_sets_a_time(line) else stored, tz)
        moved = body.restamp(stored, date=date, hhmm=clock, tz=tz)
        return stored if moved is None else moved

    def _submit_food(self, value: str) -> None:
        r = parse_food(value, now=self.app.now())
        row_id = self._take_editing("food")
        if row_id is None:
            if r.kcal is None:
                self._run_text_estimate(r)
                return
            self._write_food(r, source="labeled")
            return
        before = self.app.conn.execute(
            "SELECT * FROM food WHERE id = ?", (row_id,)
        ).fetchone()
        if before is None:
            self.app.notify("that row is gone", timeout=3)
            return
        # The shared rule — see `_restamp_for`. This hand-rolled its own version and had no
        # date-only case, so an edit naming a date and no time moved `date` while leaving
        # `ate_at` on the old day; the row then sorted within its new day by a timestamp
        # belonging to the old one.
        at = self._restamp_for(before["ate_at"], parsed_at=r.at, date=r.date, line=value)
        # `source` is deliberately absent from the grammar: it is provenance
        # (labelled vs estimated) that the digest reads, not something an edit of the
        # description should rewrite. Pre-image after the write.
        if r.kcal is None:
            raise ParseError("kcal is required — give =kcal or leave the row unchanged")
        body.update_food(
            self.app.conn, row_id, description=r.description, kcal=r.kcal,
            date=r.date, ate_at=at
        )
        self.app.undo_stack.push("food", dict(before))
        self.app.notify(f"{r.description} · {r.kcal:,} kcal · u to undo", timeout=4)
        self.reload()

    def _submit_confirmed_food(self, value: str) -> None:
        r = parse_food(value, now=self.app.now())
        if r.kcal is None and self._pending is not None:
            r = type(r)(
                description=r.description,
                kcal=self._pending.kcal,
                date=r.date,
                at=r.at,
            )
        self._write_food(r, source="estimated")
        self._pending = None
        # Only consume the inbox file once the row is actually written. A failed
        # estimate must leave the photo pending, not silently eat it.
        if self._pending_photo is not None:
            path, from_inbox = self._pending_photo
            if from_inbox:
                photo.mark_processed(path, self.app.cfg.inbox_dir)
            self._pending_photo = None

    def _submit_activity(self, value: str) -> None:
        """Entry or edit, on one line. An edit updates; entry with no `=` asks Claude.

        The two paths diverge on the missing factor deliberately. On entry, no number
        means "work one out". On an edit it means "leave the stored one alone" — fixing
        a typo in a description must not silently re-roll the number the day is
        measured against, and it is also what keeps a factorless row editable, since
        its rendered line carries no `=` to begin with.
        """
        r = parse_activity(value, now=self.app.now())
        row_id = self._take_editing("activity")
        if row_id is not None:
            before = self.app.conn.execute(
                "SELECT * FROM activity WHERE id = ?", (row_id,)
            ).fetchone()
            if before is None:
                self.app.notify("that row is gone", timeout=3)
                return
            # The shared rule — see `_restamp_for`. This restamped from the *parsed*
            # instant, and the grammar fills a missing time from `now`, so an edit naming
            # only a date moved `logged_at` to whenever the edit happened. That column is
            # the tie-breaker `resolved_factor` uses to pick a day's latest inference, so a
            # description fix could hand the day a different factor.
            at = self._restamp_for(
                before["logged_at"], parsed_at=r.at, date=r.date, line=value
            )
            body.update_activity(
                self.app.conn, row_id,
                description=r.description, factor=r.factor, date=r.date,
                logged_at=at,
            )
            self.app.undo_stack.push("activity", dict(before))
            self.app.notify(f"{r.description} · u to undo", timeout=4)
            self.reload()
            return
        if r.factor is None:
            self._run_factor_estimate(r)
            return
        self._write_activity(r, source="labeled")

    def _submit_confirmed_activity(self, value: str) -> None:
        r = parse_activity(value, now=self.app.now())
        if r.factor is None and self._pending_activity is not None:
            r = type(r)(
                description=r.description,
                factor=self._pending_activity.factor,
                date=r.date,
                at=r.at,
            )
        self._pending_activity = None
        self._write_activity(r, source="estimated")

    def _write_activity(self, r, *, source: str) -> None:
        body.add_activity(
            self.app.conn,
            description=r.description,
            date=r.date,
            at=r.at,
            factor=r.factor,
            source=source,
        )
        self.viewing_date = r.date
        self.reload()
        # The useful answer is not "saved" but what the day now costs. When there is no
        # BMR the factor has nothing to scale, and saying so names the fix rather than
        # letting the entry look like it did nothing.
        burn = body.day_tdee(self.app.conn, self.app.cfg, date=r.date)
        shown = "no factor" if r.factor is None else f"×{r.factor:g}"
        if burn is None:
            self.app.notify(
                f"{r.description} · {shown} — press h for a height, sex and birthday"
                " to turn it into a burn figure",
                timeout=6,
            )
        else:
            self.app.notify(f"{r.description} · {shown} · {burn:,} burn", timeout=4)

    @work(exclusive=True, group="activity")
    async def _run_factor_estimate(self, r) -> None:
        """Infer the day's factor, then offer it for review.

        Its own worker group, not the food estimate's: they are unrelated questions,
        and sharing a group would mean logging a gym session cancelled a meal estimate
        that was still running.

        A failed inference still records what you did, with a NULL factor — the state
        the schema and `resolved_factor` already handle, where the day falls back to
        the profile baseline. The description is the user's data; the factor is a
        guess, and losing the first because the second failed is the worse outcome.
        """
        self._set_inferring(True)
        logged = [
            row["description"]
            for row in body.list_activity(self.app.conn, date=r.date)
        ]
        try:
            effort = await estimate.factor_from_text(
                # The day, not the entry: a PAL is not additive, so two sessions and
                # one session are different days.
                activities=[*logged, r.description],
                baseline=self.app.cfg.activity,
                runner=self.app.runner_json,
                timeout_sec=self.app.cfg.estimate_timeout_sec,
                model=self.app.cfg.claude_model,
            )
        except Exception as e:  # surfaced to the user, not swallowed
            self._set_inferring(False)
            self._write_activity(r, source="estimated")
            self.app.notify_error(
                f"factor estimate failed: {e} — logged with no factor, so the day"
                " uses your ordinary-day level"
            )
            return
        self._set_inferring(False)
        self._pending_activity = effort
        # Worker-opened, like `confirm food` — see the note there, including why the `@` is
        # on the line: the confirm is re-parsed, and an absent `@` means now.
        self.app.prompt.open(
            "confirm activity",
            f"{sigil.escape(r.description)} ={effort.factor:g}"
            f" @{r.date}/{hhmm(r.at, self.app.cfg.timezone)}",
            owner=self.id,
        )

    def _write_food(self, r, *, source: str) -> None:
        body.add_food(
            self.app.conn,
            description=r.description,
            kcal=r.kcal or 0,
            source=source,
            date=r.date,
            at=r.at,
        )
        self.viewing_date = r.date
        self.reload()
        total = body.day_kcal(self.app.conn, date=r.date)
        # The same question the header one line above asks. It used to ask `compute_bmr`
        # directly, so with a factor set the header said net against `burn` and this said
        # "vs BMR" in the same instant, about the same day.
        baseline = body.day_baseline(self.app.conn, self.app.cfg, date=r.date)
        # And named the same way. The number is burn only when a factor applied; without
        # one it is resting BMR, and calling that "burn" would be the same drift running
        # the other way. The name is display copy, so it is decided here rather than
        # returned from the data layer.
        factor, _ = body.resolved_factor(self.app.conn, self.app.cfg, date=r.date)
        # The useful answer is not "saved" but where the day now stands.
        if baseline is None:
            against = f"{total:,} today"
        else:
            against = f"{total - baseline:+,} vs {'burn' if factor is not None else 'BMR'}"
        self.app.notify(f"{r.description} · {r.kcal or 0:,} kcal · {against}", timeout=4)


def _delta(value: float | None, label: str) -> str:
    """A weight change, coloured down-is-good.

    That direction is an assumption, and the only one available: daylogs stores no
    goal weight, so "good" has to come from somewhere. Losing reads as progress
    for the person who built a weight tracker. The arrow carries the direction
    regardless, so the colour adds emphasis rather than being the only signal —
    which matters if the assumption is ever wrong for you.
    """
    if value is None:
        return f"— vs {label}"
    return mark(f"{arrow(value)} {abs(value):g} vs {label}", trend_style(value))


