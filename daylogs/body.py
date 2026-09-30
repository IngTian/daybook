"""Weight, food and activity: reads, writes, trend windows, and the calorie arithmetic.

Activity and the burn it scales arrived after this line first read "weight and food",
and BMR is only the first half of that arithmetic — resting expenditure, an activity
factor, the day's TDEE, and the per-day net series built from them all live here.

Every write validates before it touches SQLite, so a bad prompt never
produces a half-valid row. Deletes return the removed row so the caller can
push it onto the undo stack.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from zoneinfo import ZoneInfo

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SOURCES = frozenset({"labeled", "estimated"})
_MAX_KG = 500.0


class BodyError(ValueError):
    pass


def _check_date(date: str) -> str:
    if not _DATE_RE.match(date):
        raise BodyError(f"date {date!r} must be YYYY-MM-DD")
    try:
        dt.date.fromisoformat(date)
    except ValueError as e:
        raise BodyError(f"date {date!r} is not a real date") from e
    return date


def _window_start(end_date: str, days: int) -> str:
    return (dt.date.fromisoformat(end_date) - dt.timedelta(days=days - 1)).isoformat()


# ── weight ───────────────────────────────────────────────────────────────
def add_weight(conn, *, kg: float, date: str, at: int, note: str | None = None) -> int:
    if not 0 < float(kg) <= _MAX_KG:
        raise BodyError(f"{kg} kg is not a plausible weight")
    cur = conn.execute(
        "INSERT INTO weight (date, measured_at, kg, note) VALUES (?, ?, ?, ?)",
        (_check_date(date), int(at), float(kg), note or None),
    )
    return int(cur.lastrowid)


def list_weight(
    conn, *, since: str | None = None, until: str | None = None, limit: int = 200,
    sort: str = "date", desc: bool = True,
) -> list[sqlite3.Row]:
    """Whole rows, newest first, optionally bounded at either end.

    Both bounds, not just `since`: the Body table is filtered by the tab's span, and a
    span has a right edge too. With a lower bound alone, viewing an older day listed
    readings that had not happened yet — the same class of wrongness as a header
    naming a window the query ignores.
    """
    sql = "SELECT * FROM weight WHERE 1=1"
    args: list = []
    if since:
        sql += " AND date >= ?"
        args.append(_check_date(since))
    if until:
        sql += " AND date <= ?"
        args.append(_check_date(until))
    # Newest first throughout — the same order `_day_or_window` returns, so the three
    # tables on the Body tab agree. Within a day that puts the *latest* reading on top,
    # which is the one the WEIGHT header states as the headline; `morning_weight`'s reading
    # sits below it and is what the trend, the deltas and the digest take. `value` sorts
    # by kg, through the same helper the other two tables use.
    sql += f" {_order_by(stamp='measured_at', value='kg', sort=sort, desc=desc)} LIMIT ?"
    args.append(int(limit))
    return list(conn.execute(sql, args))


def latest_weight(conn, *, on_or_before: str | None = None) -> sqlite3.Row | None:
    sql = "SELECT * FROM weight"
    args: list = []
    if on_or_before:
        sql += " WHERE date <= ?"
        args.append(_check_date(on_or_before))
    sql += " ORDER BY date DESC, measured_at DESC LIMIT 1"
    return conn.execute(sql, args).fetchone()


def morning_weight(conn, *, on_or_before: str | None = None) -> sqlite3.Row | None:
    """The **first** reading of the most recent day that has one — the day's comparable
    weight.

    The counterpart to `latest_weight`, and the app needs both. "What do I weigh now" is
    the latest reading and belongs in the header. "What did this day weigh" is the first
    one: taken fasted, before food and water, so it is the only reading that compares
    across days. The trend, the 7d/30d deltas and the digest all want the second.

    They were the same function, and it was `latest_weight`. On real data every day with
    two readings was weighed early and again mid-morning, so taking the later one took the
    low end of every day — not a neutral sample of it. It also made the digest prompt
    false: it states `weight_kg` is "the weigh-in on the *morning* of target_date, before
    any of the food listed" while being handed the mid-morning one.

    Same staleness rule as `latest_weight`: a week-old reading is the best available
    answer, not a reason to show nothing.
    """
    sql = "SELECT * FROM weight"
    args: list = []
    if on_or_before:
        sql += " WHERE date <= ?"
        args.append(_check_date(on_or_before))
    # Latest *day*, then earliest reading within it. One ORDER BY cannot express that.
    sql += " ORDER BY date DESC, measured_at ASC LIMIT 1"
    return conn.execute(sql, args).fetchone()


def weight_series_between(
    conn, *, start: str | None, end: str
) -> list[tuple[str, float, int]]:
    """One point per day between `start` and `end` inclusive, ascending — each day's
    **first** reading.

    `start=None` means unbounded. This is now the only implementation of the collapse;
    `weight_delta` takes its day-count window through here too. Collapsing at all is so
    that a morning weigh-in plus a curious evening re-check does not become two points.
    Keeping the *first* is so that what survives is comparable: the fasted reading, before
    food and water. Latest-wins took the low end of every multi-reading day and hid the high
    one entirely once the window passed `3d`.

    Returns `(date, kg, measured_at)`. The timestamp comes along so the chart can
    place a day's point at the hour it was taken rather than at midnight — the
    collapse is about how many points there are, not about pretending they all
    happened at once. Use `weight_points_between` when every reading is wanted.
    """
    sql = """
        SELECT date, kg, measured_at FROM weight w
        WHERE date <= ?
          AND measured_at = (
              SELECT MIN(measured_at) FROM weight w2 WHERE w2.date = w.date
          )
    """
    args: list = [_check_date(end)]
    if start is not None:
        sql += " AND date >= ?"
        args.append(_check_date(start))
    sql += " GROUP BY date ORDER BY date ASC"
    return [(r["date"], r["kg"], r["measured_at"]) for r in conn.execute(sql, args)]


def weight_points_between(conn, *, start: str | None, end: str) -> list[tuple[int, float]]:
    """*Every* reading between `start` and `end` inclusive, ascending by time.

    The counterpart to `weight_series_between`, for windows short enough that the
    time of day is a visible axis position (`horizon.HOURLY_MAX_DAYS`). Over a month
    the per-day collapse is what keeps the trend readable — weight swings a kilo
    within a day — but across three days that same collapse hides the thing you
    zoomed in to see.

    Returns `(measured_at, kg)` and no date: at this resolution the timestamp *is*
    the position.
    """
    sql = "SELECT measured_at, kg FROM weight WHERE date <= ?"
    args: list = [_check_date(end)]
    if start is not None:
        sql += " AND date >= ?"
        args.append(_check_date(start))
    sql += " ORDER BY measured_at ASC"
    return [(r["measured_at"], r["kg"]) for r in conn.execute(sql, args)]


def kcal_series_between(conn, *, start: str | None, end: str) -> list[tuple[str, int]]:
    """Daily calorie totals between `start` and `end`, ascending. Days with no
    entries are absent rather than zero — a logging gap is not a fast."""
    sql = "SELECT date, SUM(kcal) AS total FROM food WHERE date <= ?"
    args: list = [_check_date(end)]
    if start is not None:
        sql += " AND date >= ?"
        args.append(_check_date(start))
    sql += " GROUP BY date ORDER BY date ASC"
    return [(r["date"], int(r["total"])) for r in conn.execute(sql, args)]


def kcal_average(conn, *, start: str | None, end: str) -> int | None:
    """Mean intake over the days that actually have entries."""
    series = kcal_series_between(conn, start=start, end=end)
    if not series:
        return None
    return round(sum(v for _, v in series) / len(series))


def weight_delta(conn, *, end_date: str, days: int) -> float | None:
    """Change across the window, first day's reading to last day's.

    Goes through `weight_series_between` rather than a day-count helper of its own. There used
    to be one — a `weight_series` taking `days` instead of a start date and not returning the
    timestamp — and it was a second copy of the same `MIN(measured_at)` collapse. Equivalence
    was checked before deleting it, over two readings on one day, an exact `measured_at` tie,
    and rows just outside both bounds; `test_weight_delta_excludes_rows_outside_the_window`
    is the part of that which a test can still re-derive, and it is the only assertion
    anywhere that this day-count lower bound excludes rows.
    """
    series = weight_series_between(
        conn, start=_window_start(_check_date(end_date), days), end=end_date
    )
    if len(series) < 2:
        return None
    return round(series[-1][1] - series[0][1], 2)


def update_weight(conn, id: int, **fields) -> bool:
    return _update(conn, "weight", id, fields, allowed={"kg", "date", "measured_at", "note"})


def delete_weight(conn, id: int) -> dict | None:
    return _delete(conn, "weight", id)


# ── food ─────────────────────────────────────────────────────────────────
def add_food(conn, *, description: str, kcal: int, source: str, date: str, at: int) -> int:
    if source not in _SOURCES:
        raise BodyError(f"source must be one of {sorted(_SOURCES)}")
    if not description.strip():
        raise BodyError("description must be non-empty")
    if int(kcal) < 0:
        raise BodyError("kcal must be >= 0")
    cur = conn.execute(
        "INSERT INTO food (date, ate_at, description, kcal, source) VALUES (?, ?, ?, ?, ?)",
        (_check_date(date), int(at), description.strip(), int(kcal), source),
    )
    return int(cur.lastrowid)


SORTS = ("date", "value")


def _order_by(*, stamp: str, value: str, sort: str, desc: bool) -> str:
    """A window's ORDER BY. One rule for all three Body tables, so the sub-views agree.

    `date` is day then clock. `value` is the table's own number — kg, kcal, factor — with
    date and clock kept as the tiebreak, so rows sharing a number stay in a stable and
    readable order instead of shuffling. Column names are literals from this module and
    never user input; the sort *name* is checked because it arrives from a keypress.

    On `value` ascending, activity's NULL factors sort to the top — SQLite puts NULLs
    first — which is the useful direction anyway: those are the inferences that never
    landed, and they are the rows worth fixing.
    """
    if sort not in SORTS:
        raise BodyError(f"sort must be one of {SORTS}")
    d = "DESC" if desc else "ASC"
    if sort == "value":
        return f"ORDER BY {value} {d}, date DESC, {stamp} DESC"
    return f"ORDER BY date {d}, {stamp} {d}, id {d}"


def _day_or_window(
    conn, table: str, *, stamp: str, value: str, date, since, until, limit,
    sort: str = "date", desc: bool = True,
) -> list[sqlite3.Row]:
    """A single day in the order it happened, or a window newest-first throughout.

    One function, two questions, two orders. `date=` serves the digest and the Day tab,
    which read a day out loud and want breakfast before dinner. The window serves the Body
    tables, which are a log you scroll: **newest first all the way down**, within a day as
    well as across days.

    The window used to be `date DESC, stamp ASC` — newest day on top, each day then read
    forwards — so that a `1d` window matched the digest's order exactly. That equivalence
    was the stated reason, and it turned out to be worth less than it cost: the same table
    changed direction halfway down, which reads as a bug when you scroll it. Nobody
    compares the table against the digest row by row; prose wants chronology and a log
    wants recency, and they are allowed to differ. `1d` still selects exactly the rows the
    per-day view did, just newest-first like every other horizon.

    Both bounds for the window, for the reason `list_weight` documents: with a lower
    bound alone, viewing an older day listed rows that had not happened yet.

    Passing both a date and a bound is refused rather than resolved. They are different
    questions with different orders, and picking one silently would make a call site's
    intent unreadable.
    """
    if date is not None and (since is not None or until is not None):
        raise BodyError(f"list a {table} by date or by window, not both")
    if date is not None:
        return list(
            conn.execute(
                f"SELECT * FROM {table} WHERE date = ? ORDER BY {stamp} ASC, id ASC",
                (_check_date(date),),
            )
        )
    sql = f"SELECT * FROM {table} WHERE 1=1"
    args: list = []
    if since:
        sql += " AND date >= ?"
        args.append(_check_date(since))
    if until:
        sql += " AND date <= ?"
        args.append(_check_date(until))
    sql += f" {_order_by(stamp=stamp, value=value, sort=sort, desc=desc)} LIMIT ?"
    args.append(int(limit))
    return list(conn.execute(sql, args))


def list_food(
    conn, *, date: str | None = None, since: str | None = None,
    until: str | None = None, limit: int = 2000,
    sort: str = "date", desc: bool = True,
) -> list[sqlite3.Row]:
    """A day's meals, or a window's. See `_day_or_window`. `value` sorts by kcal."""
    return _day_or_window(
        conn, "food", stamp="ate_at", value="kcal", date=date, since=since, until=until,
        limit=limit, sort=sort, desc=desc,
    )


def day_kcal(conn, *, date: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(kcal), 0) AS total FROM food WHERE date = ?",
        (_check_date(date),),
    ).fetchone()
    return int(row["total"])


def update_food(conn, id: int, **fields) -> bool:
    if "source" in fields and fields["source"] not in _SOURCES:
        raise BodyError(f"source must be one of {sorted(_SOURCES)}")
    return _update(
        conn,
        "food",
        id,
        fields,
        allowed={"description", "kcal", "source", "date", "ate_at"},
    )


def restamp(at: int, *, date: str, hhmm: str, tz: str) -> int | None:
    """The new epoch-second timestamp for a row whose date or clock time was edited, or
    `None` when neither moved.

    Stored timestamps carry seconds; the grammar's only time token is `HH:MM`. So
    re-deriving the timestamp on every edit would quietly shave the seconds off a
    row whose time nobody touched — and for weight those seconds are the
    tie-breaker `weight_series_between` uses to pick a day's reading. Returning `None`
    means "leave the column alone", which is what the caller wants far more often
    than a rewrite.

    The guard compares the whole local **minute**, date included. It compared only
    `%H:%M`, which made an edit that moved a row to another day at the same clock time
    look like nothing had changed — so the caller wrote the new `date` beside the old
    day's timestamp. For weight that inverts the two named concepts: an after-dinner
    reading moved onto the next day became that day's `morning_weight` while the fasted
    reading became its `latest_weight`, so the trend, the 7d/30d deltas and the digest's
    `weight_kg` all took the wrong one. For activity it lets a moved row win a
    `resolved_factor` tie it should lose, rescaling every calorie figure for that day.
    Both were reproduced before this line changed.

    `tz` has to be the zone the line was *rendered* in. Comparing in a different one
    makes every edit look like a time change, so it rewrites the tie-breaker column on
    an edit that only touched a description.
    """
    zone = ZoneInfo(tz)
    if dt.datetime.fromtimestamp(int(at), zone).strftime("%Y-%m-%d %H:%M") == f"{date} {hhmm}":
        return None
    hh, mm = (int(part) for part in hhmm.split(":"))
    return int(
        dt.datetime.combine(
            dt.date.fromisoformat(date), dt.time(hh, mm), tzinfo=zone
        ).timestamp()
    )


def delete_food(conn, id: int) -> dict | None:
    return _delete(conn, "food", id)


# ── BMR ──────────────────────────────────────────────────────────────────
def age_from_birthday(birthday: str | None, today: str | None = None) -> int | None:
    if not birthday:
        return None
    try:
        b = dt.date.fromisoformat(birthday)
    except ValueError:
        return None
    t = dt.date.fromisoformat(today) if today else dt.date.today()
    return t.year - b.year - ((t.month, t.day) < (b.month, b.day))


def compute_bmr(cfg, kg: float | None, today: str | None = None) -> int | None:
    """Mifflin-St Jeor. None whenever an input is missing — a calorie total with
    no maintenance baseline is better shown bare than shown against a guess."""
    if kg is None or cfg.height_cm is None or not cfg.sex:
        return None
    age = age_from_birthday(cfg.birthday, today)
    if age is None:
        return None
    base = 10 * float(kg) + 6.25 * float(cfg.height_cm) - 5 * age
    return round(base + (5 if cfg.sex == "male" else -161))


# ── shared row helpers ───────────────────────────────────────────────────
def _update(conn, table: str, id: int, fields: dict, *, allowed: set[str]) -> bool:
    fields = {k: v for k, v in fields.items() if v is not None}
    unknown = set(fields) - allowed
    if unknown:
        raise BodyError(f"cannot update {sorted(unknown)} on {table}")
    if not fields:
        return False
    if "date" in fields:
        _check_date(fields["date"])
    sets = ", ".join(f"{k} = ?" for k in fields)
    cur = conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*fields.values(), int(id)))
    return cur.rowcount > 0


def _delete(conn, table: str, id: int) -> dict | None:
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (int(id),)).fetchone()
    if row is None:
        return None
    conn.execute(f"DELETE FROM {table} WHERE id = ?", (int(id),))
    return dict(row)


# ── activity: what a day cost, above resting ─────────────────────────────
# Standard PAL multipliers, *re-described*. The textbook labels bake habitual
# exercise into the band ("moderate: exercise 3-5 days a week"), and using those as
# a baseline while also logging gym days would count the exercise twice. So a level
# here means a day with **no logged activity**, and the wording describes
# occupational movement instead. Anyone comparing against an online TDEE calculator
# will get a different answer for the same word; double-counting exercise is the
# worse error. 1.9 is omitted deliberately — that band is athletic training, which is
# a logged activity, not an ordinary day.
ACTIVITY_LEVELS: dict[str, float] = {
    "desk": 1.2,
    "light": 1.375,
    "active": 1.55,
    "heavy": 1.725,
}

# The physiological range a whole-day PAL can occupy. Enforced on anything inferred,
# because a wrong factor does not misreport one row — it silently rescales every
# calorie judgement for that day, and a hallucinated 4.0 would triple the baseline.
FACTOR_MIN, FACTOR_MAX = 1.2, 1.9


def baseline_factor(cfg) -> float | None:
    """The profile's ordinary-day multiplier, or None if it is unset.

    Deliberately not defaulted. Assuming `desk` for everyone would raise maintenance
    by 20% and silently restate every number already on screen and in every past
    digest; an absent setting is shown as absent, the way a missing height is.

    An unrecognised keyword also returns None rather than raising: `config.toml` is
    hand-edited, and a typo there must not stop the app.
    """
    return ACTIVITY_LEVELS.get(getattr(cfg, "activity", None) or "")


def add_activity(
    conn, *, description: str, date: str, at: int, factor: float | None, source: str
) -> int:
    """Log one activity. `factor` is the whole day's PAL as inferred for this entry."""
    if not description.strip():
        raise BodyError("say what you did")
    if factor is not None and not (FACTOR_MIN <= factor <= FACTOR_MAX):
        raise BodyError(f"an activity factor must be between {FACTOR_MIN} and {FACTOR_MAX}")
    cur = conn.execute(
        "INSERT INTO activity (date, logged_at, description, factor, source)"
        " VALUES (?, ?, ?, ?, ?)",
        (_check_date(date), int(at), description.strip(), factor, source),
    )
    return int(cur.lastrowid)


def update_activity(conn, id: int, **fields) -> bool:
    """Edit a logged activity. Only the columns the table displays are reachable.

    `source` is deliberately absent: it is provenance the digest reads to know whether
    a number was inferred, not something editing a description should rewrite — the
    same stance `update_food` takes.

    A factor is not clearable here. `_update` drops None values, so a line that names
    no `=factor` leaves the stored one alone rather than blanking it. That is on
    purpose: a row with no factor is an inference that never landed, not a state
    anyone would choose, and it is also what keeps such a row editable at all — its
    rendered line carries no `=`, so treating that as "clear it" would make its
    description unfixable. `x` is how a row goes away.
    """
    if fields.get("factor") is not None and not (
        FACTOR_MIN <= fields["factor"] <= FACTOR_MAX
    ):
        raise BodyError(f"an activity factor must be between {FACTOR_MIN} and {FACTOR_MAX}")
    if "description" in fields and not (fields["description"] or "").strip():
        raise BodyError("say what you did")
    return _update(
        conn,
        "activity",
        id,
        fields,
        allowed={"description", "factor", "date", "logged_at"},
    )


def delete_activity(conn, id: int) -> dict | None:
    return _delete(conn, "activity", id)


def list_activity(
    conn, *, date: str | None = None, since: str | None = None,
    until: str | None = None, limit: int = 2000,
    sort: str = "date", desc: bool = True,
) -> list[sqlite3.Row]:
    """A day's activities oldest first, or a window's newest first. See `_day_or_window`.

    `value` sorts by factor."""
    return _day_or_window(
        conn, "activity", stamp="logged_at", value="factor", date=date, since=since,
        until=until, limit=limit, sort=sort, desc=desc,
    )


def resolved_factor(conn, cfg, *, date: str) -> tuple[float | None, str | None]:
    """`(multiplier, where it came from)` for `date` — `"logged"` or `"profile"`.

    Three steps, each a real state:

    1. the latest activity row carrying a factor — an inference over what you did;
    2. otherwise the profile baseline, which is the common case and needs no input;
    3. otherwise `(None, None)`, and `net` sits against resting BMR as it did before.

    Latest-wins rather than first: re-logging supersedes, so correcting a day's factor
    means logging it again. Note this is the *opposite* of the weight series, which
    collapses a day to its **first** reading — `weight_series_between` and `morning_weight` both
    take `MIN(measured_at)`, because a fasted reading is the only comparable one. Two rules,
    for two different questions. A row whose factor is NULL —
    an inference that never landed — falls through to the baseline rather than
    poisoning the day with nothing, and reports the baseline's origin, not the log's.

    The origin comes back because it reaches the screen. A factor rescales every
    calorie judgement for its day, and a multiplier with nothing to make you doubt it
    quietly becomes the baseline for everything.
    """
    row = conn.execute(
        "SELECT factor FROM activity WHERE date = ? AND factor IS NOT NULL"
        " ORDER BY logged_at DESC LIMIT 1",
        (_check_date(date),),
    ).fetchone()
    if row is not None:
        return float(row["factor"]), "logged"
    base = baseline_factor(cfg)
    return (base, "profile") if base is not None else (None, None)


def day_factor(conn, cfg, *, date: str) -> float | None:
    """The multiplier to apply to BMR for `date`, or None if there is nothing to say.

    One resolution, and this is the half of it that drops the origin — for the two
    callers, `day_tdee` and `day_baseline`, that only need the number. The surfaces that
    *do* want the origin call `resolved_factor` directly: the ENERGY panel, which shows
    it, and `summary.build_payload`, which files it as `activity_source`. Resolving the
    factor twice is how a header and a panel start disagreeing about the same day.
    """
    return resolved_factor(conn, cfg, date=date)[0]


def compute_tdee(bmr: int | None, factor: float | None) -> int | None:
    """What the day actually cost: resting expenditure scaled by activity.

    None whenever either input is missing, for the same reason `compute_bmr` returns
    None on a missing weight — a maintenance figure resting on a guess is worse than
    no figure, because it looks equally authoritative.
    """
    if bmr is None or factor is None:
        return None
    return round(bmr * factor)


def day_tdee(conn, cfg, *, date: str) -> int | None:
    """What `date` cost: that day's resting BMR, scaled by that day's factor.

    The one place that composition lives. Four surfaces read it — the ENERGY panel, the
    Day tab's BODY block, the digest payload, and the activity write toast — and four
    separate compositions is four chances for one panel to measure against two different
    baselines. The FOOD header was on that list and is not any more: it describes the
    window of rows beneath it, and the day's own balance is stated once, in the panel.

    The weight is the latest on or before `date`, the rule every other reader
    follows: a week-old weigh-in is the best available answer. A day before the first
    weigh-in has no BMR and therefore no burn.
    """
    latest = latest_weight(conn, on_or_before=date)
    bmr = compute_bmr(cfg, latest["kg"] if latest else None, today=date)
    return compute_tdee(bmr, day_factor(conn, cfg, date=date))


def day_baseline(conn, cfg, *, date: str) -> int | None:
    """What `date` is measured against: its burn if there is a factor, else resting BMR.

    Anything that states a calorie figure has to ask this, and two of them had drifted
    — a count is not given here because the last one went stale. The food toast asked
    `compute_bmr` while the FOOD header one line above it asked `day_tdee`, so the
    header said net against `burn` and the toast said "+X vs BMR" in the same instant.
    And `net_series_between` keyed on the *factor*, so `c` -> net drew an empty chart for
    any profile without a level — which is the default, because the level is deliberately
    never defaulted.

    "No factor" is a real state in which every calorie figure sits against resting BMR
    exactly as it did before, so a baseline still exists. That is the distinction: ask for
    the baseline, not for the factor. `None` means there is genuinely nothing to measure
    against — no weigh-in, or no profile — and then a figure is shown bare.
    """
    latest = latest_weight(conn, on_or_before=date)
    bmr = compute_bmr(cfg, latest["kg"] if latest else None, today=date)
    if bmr is None:
        return None
    burn = compute_tdee(bmr, day_factor(conn, cfg, date=date))
    # Explicit `is not None` rather than `or`: a truthiness test would also swallow a
    # burn of 0, which is a real number here and not an absent one.
    return burn if burn is not None else bmr


def net_series_between(
    conn, cfg, *, start: str | None, end: str
) -> list[tuple[str, int]]:
    """Daily `intake − that day's baseline`, ascending, for the days that have both.

    Per day, not "the window's average intake minus today's burn": a factor describes
    one day, so a single gym session must not restate a month of net. Days with no
    food are absent — a logging gap is not a fast, the same rule
    `kcal_series_between` follows — and so are days with no *baseline*, because showing
    intake as if it were net reads as an enormous surplus.

    Baseline, not burn: keyed on the factor this returned nothing at all for a profile
    with no level, so the chart was empty beside an ENERGY panel showing a live net.
    """
    return [
        (date, kcal - baseline)
        for date, kcal in kcal_series_between(conn, start=start, end=end)
        if (baseline := day_baseline(conn, cfg, date=date)) is not None
    ]


def net_average(conn, cfg, *, start: str | None, end: str) -> int | None:
    """Mean net over the days that have both an intake and a burn."""
    series = net_series_between(conn, cfg, start=start, end=end)
    if not series:
        return None
    return round(sum(v for _, v in series) / len(series))


def bmi(cfg, kg: float | None) -> float | None:
    """Weight over height squared, to one decimal. None without both.

    Returned as a bare number: no band, no colour. "over" / "obese" is a judgement
    this app does not otherwise make, and it is a restatement of weight rather than a
    second fact — which is also why there is no BMI chart. It would be the weight
    curve times a constant.
    """
    if kg is None or not cfg.height_cm:
        return None
    metres = float(cfg.height_cm) / 100
    return round(float(kg) / (metres * metres), 1)
