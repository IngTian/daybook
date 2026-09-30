"""Tab 1, the Day tab — today's figures above the daily read.

Two panels of today's numbers, BODY beside MONEY, over the generated summary,
which is browsable and regeneratable. Every figure comes from body.py or
money.py; the panels lay out and never compute. The two halves carry different
dates on purpose — the figures are always today, the read is dated by the day it
describes — which is why each has its own header.

The class is still `SummaryTab` and the widget id still `#summary`: the tab id is
what the keymap's `show_summary` action and the `summary` key scope are named
after, so renaming it is a rename across the keymap, the footer and the tests
rather than a docstring fix.

Generation runs in a worker because `claude -p` takes seconds; everything else
in the app is microseconds and runs on the event loop.
"""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Markdown, Static

from daylogs import body, money, summary
from daylogs import horizon as hz
from daylogs.fmt import hhmm, human_date
from daylogs.markup import to_markdown
from daylogs.tui.common import PanelTab
from daylogs.tui.widgets import BAD, WARN, arrow, mark, trend_style

_EMPTY = "no summary yet — press r"
# The in-progress popup's key and label for this tab's one call. `busy` still drives the
# empty pane — while a first report is being written there is nothing to show and
# "no summary yet — press r" would be inviting a keypress that is already running — but
# the *indicator* lives in the popup now, app-level, so switching to Body mid-generation
# no longer hides the fact that something is in flight.
_WORK = ("summary", "writing the daily read")


class SummaryTab(PanelTab):
    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.viewing_date: str | None = None
        self.busy = False

    def compose(self) -> ComposeResult:
        yield Static(id="day-head", classes="pane-title")
        with Horizontal(classes="panel-row"):
            with Vertical(classes="panel", id="panel-day-body"):
                yield Static("BODY", classes="panel-title")
                yield Static(id="day-body-body", classes="panel-body")
            with Vertical(classes="panel", id="panel-day-money"):
                yield Static("MONEY", classes="panel-title")
                yield Static(id="day-money-body", classes="panel-body")
        yield Static(id="summary-head", classes="pane-title")
        with VerticalScroll(id="summary-scroll"):
            # A real Markdown widget, so `## Body` renders as a heading instead of
            # showing the hashes. v1 fed markdown to a Static, which rendered the
            # source text verbatim.
            yield Markdown(id="summary-body")
        yield Static(id="summary-empty", classes="muted")

    def focus_default(self) -> None:
        """The scroller — so the read pages, and so the arrows keep going through
        `show_scope`.

        With nothing focused on Day, `AUTO_FOCUS = "*"` landed on TabbedContent's own
        `ContentTabs`, whose `left`/`right` bindings are not priority either and so beat
        the App's. Measured: `→` moved `active_tab_id` to `tab-body` while `focused`
        stayed on `ContentTabs`, so `show_scope` never ran — the footer went on
        describing Day (`Wed Aug 26 · r regenerate`) with Body on screen, and Body's
        table was never focused, leaving row navigation and `enter` dead. Pressing `1`
        did not clear it, because this method did nothing; only a `2`/`3` did, which is
        why every test in tests/test_tui_nav.py — each of which presses a digit first —
        missed it entirely.

        `up`/`down`/`pageup`/`pagedown`/`home`/`end` come from ScrollableContainer's own
        bindings, so no keymap entry is needed or wanted: declaring them in `KEYMAP`
        would have the keymap describe keys `app_bindings()` does not bind, which is the
        drift the keymap exists to prevent. `left`/`right` still walk the tabs, because
        `VerticalScroll` sets `overflow-x: hidden` itself — `allow_horizontal_scroll` is
        therefore always false and `action_scroll_left`/`_right` raise `SkipAction`
        unconditionally, which is the same fall-through the `DataTable` rule in app.tcss
        buys by hand, here for free.
        """
        self.query_one("#summary-scroll", VerticalScroll).focus()

    def _body_panel(self, conn, cfg, *, date: str) -> str:
        """Today's body figures. Every value comes from body.py — this only lays out.

        Aligned in a value column like the Body tab's ENERGY panel, so the two read
        as the same app.
        """
        lines: list[str] = []
        latest = body.latest_weight(conn, on_or_before=date)
        if latest is None:
            # Name the key, not the file: an empty panel with no way out is how the
            # Body tab's energy block stayed blank.
            lines.append("  weight          —   press w on Body")
            kg = None
        else:
            kg = latest["kg"]
            d7 = body.weight_delta(conn, end_date=date, days=7)
            # Coloured with the same rule as the Body tab, via the same function.
            # Marked after the width arithmetic, per mark()'s contract — the arrow
            # and sign carry the direction, so colour only emphasises.
            trend = ""
            if d7 is not None:
                trend = "  " + mark(f"{arrow(d7)}{abs(d7):g} vs 7d", trend_style(d7))
            lines.append(f"  weight    {kg:>7,.1f} kg{trend}")

        # A bare number, no band and no colour — the same stance the Body tab takes.
        # None without a height, which is the only state worth distinguishing.
        index = body.bmi(cfg, kg)
        if index is not None:
            lines.append(f"  BMI       {index:>7.1f}")

        kcal = body.day_kcal(conn, date=date)
        bmr = body.compute_bmr(cfg, kg, today=date)
        # The same call the Body tab's ENERGY panel makes. Two panels resolving
        # maintenance separately is how they start disagreeing about one day.
        burn = body.day_tdee(conn, cfg, date=date)
        if bmr is None:
            lines.append(f"  in        {kcal:>7,} kcal")
            lines.append("  BMR             —   press h on Body")
        else:
            # With a factor the baseline is what the day cost; without one it is
            # resting expenditure, and the label has to say which.
            against = bmr if burn is None else burn
            net = kcal - against
            lines.append(
                f"  in        {kcal:>7,} / {against:,} {'BMR' if burn is None else 'burn'}"
            )
            lines.append(f"  net       {mark(f'{net:>+7,}', trend_style(net))} kcal")

        meals = len(body.list_food(conn, date=date))
        lines.append(f"  logged    {meals:>7} meal{'' if meals == 1 else 's'}")
        return "\n".join(lines)

    def _money_panel(self, conn, cfg, *, date: str) -> str:
        """This month's spend against its budget. One summarize_month call.

        The burn line only means something for a month in progress — 84% on day 27
        of 31 is fine and the same number on day 12 is not — so it is omitted for a
        past month rather than shown misleadingly.
        """
        month = date[:7]
        s = money.summarize_month(conn, month=month, today=date)
        lines: list[str] = [f"  spent   {s.total_spent:>10,.2f}"]

        pending, _ = money.pending_roll(conn, month=month)
        if s.total_budget <= 0:
            # Name the key that fixes it. A zero budget is true and useless.
            if pending == 0:
                lines.append("  budget          —   press b on Money")
            else:
                lines.append(f"  budget          —   press r on Money ({pending} to roll)")
        else:
            lines[0] = f"  spent   {s.total_spent:>10,.2f} of {s.total_budget:,.2f}"
            # More left is better, so the sign convention flips relative to weight
            # and calories — hence the keyword rather than a second rule.
            left = mark(f"{s.remaining:>10,.2f}", trend_style(s.remaining, falling_is_good=False))
            lines.append(f"  left    {left}")
            if month == self.app.today()[:7]:
                pct = round(s.total_spent / s.total_budget * 100)
                # Amber against *elapsed days*, not a flat threshold: 84% on day 27
                # of 31 is fine and the same number on day 12 is not. This is the
                # rule the Money tab's burn bar already draws with its `┃` marker.
                elapsed = s.day_of_month / s.days_in_month * 100
                burn = f"{pct:>9}% on day {s.day_of_month}/{s.days_in_month}"
                lines.append(f"  burn    {mark(burn, WARN if pct > elapsed else '')}")

        if s.over_budget:
            names = ", ".join(c.category for c in s.over_budget[:2])
            lines.append(f"  over      {mark(f'{names}  ⚠', BAD)}")
        return "\n".join(lines)

    # ── rendering ────────────────────────────────────────────────────────
    def reload(self) -> None:
        conn = self.app.conn
        today = self.app.today()
        self.query_one("#day-head", Static).update(f"TODAY   {human_date(today)}")
        self.query_one("#day-body-body", Static).update(
            self._body_panel(self.app.conn, self.app.cfg, date=today)
        )
        self.query_one("#day-money-body", Static).update(
            self._money_panel(self.app.conn, self.app.cfg, date=today)
        )
        row = (
            summary.get_report(conn, self.viewing_date)
            if self.viewing_date
            else summary.latest_report(conn)
        )
        head = self.query_one("#summary-head", Static)
        body = self.query_one("#summary-body", Markdown)
        empty = self.query_one("#summary-empty", Static)

        if row is None:
            self.viewing_date = None
            head.update("SUMMARY")
            body.update("")
            body.display = False
            empty.update("" if self.busy else _EMPTY)
            empty.display = not self.busy
            return

        self.viewing_date = row["date"]
        stamp = hhmm(row["generated_at"], self.app.cfg.timezone)
        head.update(f"SUMMARY   {human_date(row['date'])}   generated {stamp}")
        body.display = True
        empty.display = False
        body.update(to_markdown(row["content"]))

    def status_hint(self) -> str:
        """Which report is on screen. Not whether one is being written — the popup says
        that, from whichever tab you happen to be looking at."""
        return human_date(self.viewing_date) if self.viewing_date else "no summary"

    # ── keys ─────────────────────────────────────────────────────────────
    def key_generate(self) -> None:
        target = self.viewing_date or summary.target_date(self.app.today())
        self.generate(target)

    def key_prev_period(self) -> None:
        current = self.viewing_date or self.app.today()
        earlier = summary.prev_report_date(self.app.conn, current)
        if earlier is None:
            self.app.notify("no earlier summary", timeout=3)
            return
        self.viewing_date = earlier
        self.reload()

    def key_next_period(self) -> None:
        if self.viewing_date is None:
            return
        later = summary.next_report_date(self.app.conn, self.viewing_date)
        if later is None:
            self.app.notify("no later summary", timeout=3)
            return
        self.viewing_date = later
        self.reload()

    def key_jump_now(self) -> None:
        """Back to the newest report — the one you almost always want."""
        newest = summary.latest_report(self.app.conn)
        self.viewing_date = newest["date"] if newest else None
        self.reload()

    def handle_prompt(self, label: str, value: str) -> None:
        if label == "go to date" and value:
            wanted = hz.resolve_goto(value)
            if summary.get_report(self.app.conn, wanted) is None:
                self.app.notify(f"no summary for {wanted}", timeout=4)
                return
            self.viewing_date = wanted
            self.reload()

    # ── worker ───────────────────────────────────────────────────────────
    def _set_busy(self, running: bool) -> None:
        """Flip the flag and tell the popup, in one place.

        Two callers times two exit paths is four chances for a flag to move without the
        indicator following it — which is how a stuck "generating…" would look, and it
        would look like the app had hung on a call that finished.
        """
        self.busy = running
        if running:
            self.app.begin_work(*_WORK, self.app.cfg.summary_timeout_sec)
        else:
            self.app.end_work(_WORK[0])

    @work(exclusive=True)
    async def generate(self, date: str) -> None:
        self._set_busy(True)
        self.reload()
        try:
            await summary.generate(
                self.app.conn,
                self.app.cfg,
                date=date,
                runner=self.app.runner_text,
                # Restart the popup's clock per attempt: each one is allowed the full
                # timeout, so carrying the first attempt's elapsed into the second shows a
                # figure larger than the budget it is displayed against.
                on_attempt=lambda: self._set_busy(True),
            )
        except Exception as e:  # surfaced to the user, not swallowed
            self._set_busy(False)
            self.reload()
            self.app.notify_error(f"summary failed: {e}")
            return
        self._set_busy(False)
        self.viewing_date = date
        self.reload()


