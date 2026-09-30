from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from daylogs.body import (
    BodyError,
    add_activity,
    add_food,
    add_weight,
    age_from_birthday,
    compute_bmr,
    day_kcal,
    delete_food,
    delete_weight,
    latest_weight,
    list_activity,
    list_food,
    list_weight,
    morning_weight,
    restamp,
    update_food,
    update_weight,
    weight_delta,
    weight_points_between,
    weight_series_between,
)
from daylogs.config import Config


def _cfg(tmp_path, **kw):
    return Config(
        root=tmp_path,
        db_path=tmp_path / "d.db",
        inbox_dir=tmp_path / "inbox",
        memory_path=tmp_path / "memory.md",
        **kw,
    )


# ── weight ───────────────────────────────────────────────────────────────
def test_add_and_read_weight(db):
    wid = add_weight(db, kg=78.2, date="2026-08-27", at=1000, note="post-run")
    row = latest_weight(db)
    assert row["id"] == wid
    assert row["kg"] == 78.2
    assert row["note"] == "post-run"


def test_latest_weight_respects_cutoff(db):
    add_weight(db, kg=79.0, date="2026-08-20", at=1)
    add_weight(db, kg=78.2, date="2026-08-27", at=2)
    assert latest_weight(db, on_or_before="2026-08-24")["kg"] == 79.0


def test_latest_weight_none_on_empty(db):
    assert latest_weight(db) is None


def test_list_weight_newest_first_and_since_filter(db):
    add_weight(db, kg=79.0, date="2026-08-20", at=1)
    add_weight(db, kg=78.2, date="2026-08-27", at=2)
    assert [r["kg"] for r in list_weight(db)] == [78.2, 79.0]
    assert [r["kg"] for r in list_weight(db, since="2026-08-25")] == [78.2]


def test_one_point_per_day_and_the_first_reading_wins(db):
    """Collapsing at all keeps a curious re-check from becoming a second point. Keeping
    the *first* is what makes the survivor comparable across days: the fasted reading,
    before food and water. See tests/test_weight_of_a_day.py for why it changed."""
    add_weight(db, kg=79.0, date="2026-08-25", at=10)
    add_weight(db, kg=78.6, date="2026-08-25", at=20)
    add_weight(db, kg=78.2, date="2026-08-27", at=30)
    series = weight_series_between(db, start="2026-08-21", end="2026-08-27")
    assert [(d, kg) for d, kg, _ in series] == [("2026-08-25", 79.0), ("2026-08-27", 78.2)]


def test_weight_delta_excludes_rows_outside_the_window(db):
    """Retargeted from `weight_series_between`, which this used to cover directly. It is the only
    assertion anywhere that the day-count *lower* bound excludes rows: every other
    `weight_delta` test seeds its rows entirely inside the window, so dropping the bound
    would leave the suite green — the "passes while asserting nothing" shape."""
    add_weight(db, kg=90.0, date="2026-07-01", at=1)
    add_weight(db, kg=78.2, date="2026-08-27", at=2)
    assert weight_delta(db, end_date="2026-08-27", days=7) is None, "the July row is outside"
    assert weight_delta(db, end_date="2026-08-27", days=90) == -11.8, "and inside at 90d"


def test_the_series_is_empty_when_no_data(db):
    assert weight_series_between(db, start="2026-07-29", end="2026-08-27") == []


def test_weight_delta_uses_oldest_in_window(db):
    add_weight(db, kg=78.6, date="2026-08-21", at=1)
    add_weight(db, kg=78.2, date="2026-08-27", at=2)
    assert weight_delta(db, end_date="2026-08-27", days=7) == pytest.approx(-0.4)


def test_weight_delta_none_with_single_point(db):
    add_weight(db, kg=78.2, date="2026-08-27", at=1)
    assert weight_delta(db, end_date="2026-08-27", days=7) is None


def test_weight_delta_none_when_empty(db):
    assert weight_delta(db, end_date="2026-08-27", days=7) is None


def test_delete_weight_returns_row_for_undo(db):
    wid = add_weight(db, kg=78.2, date="2026-08-27", at=1)
    row = delete_weight(db, wid)
    assert row["kg"] == 78.2
    assert latest_weight(db) is None
    assert delete_weight(db, wid) is None


def test_update_weight(db):
    wid = add_weight(db, kg=78.2, date="2026-08-27", at=1)
    assert update_weight(db, wid, kg=79.0) is True
    assert latest_weight(db)["kg"] == 79.0


def test_update_weight_rejects_unknown_field(db):
    wid = add_weight(db, kg=78.2, date="2026-08-27", at=1)
    with pytest.raises(BodyError):
        update_weight(db, wid, bogus=1)


def test_add_weight_rejects_bad_values(db):
    with pytest.raises(BodyError):
        add_weight(db, kg=0, date="2026-08-27", at=1)
    with pytest.raises(BodyError):
        add_weight(db, kg=78.2, date="27-08-2026", at=1)
    with pytest.raises(BodyError):
        add_weight(db, kg=78.2, date="2026-02-30", at=1)


# ── food ─────────────────────────────────────────────────────────────────
def test_food_day_totals_and_order(db):
    add_food(db, description="yogurt", kcal=320, source="estimated", date="2026-08-27", at=30)
    add_food(db, description="salad", kcal=610, source="labeled", date="2026-08-27", at=10)
    add_food(db, description="other day", kcal=999, source="labeled", date="2026-08-26", at=5)
    rows = list_food(db, date="2026-08-27")
    assert [r["description"] for r in rows] == ["salad", "yogurt"]
    assert day_kcal(db, date="2026-08-27") == 930


def test_day_kcal_zero_when_empty(db):
    assert day_kcal(db, date="2026-08-27") == 0


def test_add_food_rejects_unknown_source(db):
    with pytest.raises(BodyError):
        add_food(db, description="x", kcal=10, source="guessed", date="2026-08-27", at=1)


def test_add_food_rejects_blank_description_and_negative_kcal(db):
    with pytest.raises(BodyError):
        add_food(db, description="   ", kcal=10, source="labeled", date="2026-08-27", at=1)
    with pytest.raises(BodyError):
        add_food(db, description="x", kcal=-1, source="labeled", date="2026-08-27", at=1)


def test_update_food_changes_only_given_fields(db):
    fid = add_food(
        db, description="salad", kcal=610, source="estimated", date="2026-08-27", at=1
    )
    assert update_food(db, fid, kcal=700, source="labeled") is True
    row = list_food(db, date="2026-08-27")[0]
    assert (row["kcal"], row["source"], row["description"]) == (700, "labeled", "salad")


def test_update_food_rejects_bad_source(db):
    fid = add_food(db, description="x", kcal=1, source="labeled", date="2026-08-27", at=1)
    with pytest.raises(BodyError):
        update_food(db, fid, source="guessed")


def test_update_food_unknown_id_returns_false(db):
    assert update_food(db, 999, kcal=1) is False


def test_delete_food_returns_row(db):
    fid = add_food(db, description="salad", kcal=610, source="labeled", date="2026-08-27", at=1)
    assert delete_food(db, fid)["kcal"] == 610
    assert list_food(db, date="2026-08-27") == []


# ── BMR (Mifflin-St Jeor) ───────────────────────────────────────────────
def test_bmr_male(tmp_path):
    cfg = _cfg(tmp_path, height_cm=180, sex="male", birthday="1996-08-27")
    # 10*80 + 6.25*180 - 5*30 + 5 = 800 + 1125 - 150 + 5 = 1780
    assert compute_bmr(cfg, 80.0, today="2026-08-27") == 1780


def test_bmr_female(tmp_path):
    cfg = _cfg(tmp_path, height_cm=165, sex="female", birthday="1996-08-27")
    # 10*60 + 6.25*165 - 5*30 - 161 = 600 + 1031.25 - 150 - 161 = 1320.25 -> 1320
    assert compute_bmr(cfg, 60.0, today="2026-08-27") == 1320


def test_bmr_none_without_weight_or_profile(tmp_path):
    full = _cfg(tmp_path, height_cm=180, sex="male", birthday="1996-08-27")
    assert compute_bmr(full, None) is None
    assert compute_bmr(_cfg(tmp_path), 80.0) is None
    assert compute_bmr(_cfg(tmp_path, height_cm=180, sex="male"), 80.0) is None
    assert compute_bmr(_cfg(tmp_path, height_cm=180, birthday="1996-08-27"), 80.0) is None


def test_age_from_birthday_handles_pre_birthday():
    assert age_from_birthday("1996-08-28", today="2026-08-27") == 29
    assert age_from_birthday("1996-08-27", today="2026-08-27") == 30
    assert age_from_birthday(None, today="2026-08-27") is None
    assert age_from_birthday("garbage", today="2026-08-27") is None


# ── restamp ──────────────────────────────────────────────────────────────
# An explicit zone throughout, on both sides. `restamp` takes one now, and these used
# naive system-local datetimes — which agreed with the old implementation only because
# both happened to read the machine. Pinning a zone makes them say what they mean and
# stop depending on where the suite runs.
RE_TZ = "America/Toronto"
_RZ = ZoneInfo(RE_TZ)


def _stamp(*parts) -> int:
    return int(datetime(*parts, tzinfo=_RZ).timestamp())


def _wall(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, _RZ)


def test_restamp_returns_none_when_the_minute_is_unchanged():
    """Stored timestamps carry seconds and the grammar only has HH:MM, so
    re-deriving unconditionally would shave the seconds off every edit."""
    at = _stamp(2026, 8, 20, 7, 5, 43)
    assert restamp(at, date="2026-08-20", hhmm="07:05", tz=RE_TZ) is None


def test_restamp_returns_a_new_timestamp_when_the_minute_changed():
    at = _stamp(2026, 8, 20, 7, 5, 43)
    out = restamp(at, date="2026-08-20", hhmm="08:30", tz=RE_TZ)
    assert out is not None
    assert _wall(out) == datetime(2026, 8, 20, 8, 30, tzinfo=_RZ)


def test_restamp_uses_the_given_date_not_the_stored_one():
    at = _stamp(2026, 8, 20, 7, 5, 43)
    out = restamp(at, date="2026-07-01", hhmm="09:15", tz=RE_TZ)
    assert _wall(out) == datetime(2026, 7, 1, 9, 15, tzinfo=_RZ)


def test_restamp_drops_the_seconds_only_when_it_actually_rewrites():
    at = _stamp(2026, 8, 20, 7, 5, 43)
    out = restamp(at, date="2026-08-20", hhmm="07:06", tz=RE_TZ)
    assert _wall(out).second == 0


def test_restamp_handles_midnight():
    at = _stamp(2026, 8, 20, 23, 59, 12)
    out = restamp(at, date="2026-08-20", hhmm="00:00", tz=RE_TZ)
    assert _wall(out) == datetime(2026, 8, 20, 0, 0, tzinfo=_RZ)


# ── sub-day weight points (the 1d / 3d views) ────────────────────────────


def test_weight_series_between_reports_when_each_daily_point_was_taken(db):
    """One point per day is kept for long horizons, but the chart needs to know the
    hour so it can place that point where it happened rather than at midnight."""
    add_weight(db, kg=80.0, date="2026-08-27", at=1000, note="")
    add_weight(db, kg=79.5, date="2026-08-27", at=2000, note="")
    rows = weight_series_between(db, start="2026-08-27", end="2026-08-27")
    assert len(rows) == 1, "the per-day collapse must survive"
    date, kg, at = rows[0]
    assert (date, kg, at) == ("2026-08-27", 80.0, 1000), "the first reading wins"


def test_weight_points_between_keeps_every_reading(db):
    """The 1d and 3d views want each weigh-in, not one per day — otherwise there is
    nothing for hour positions to separate."""
    add_weight(db, kg=80.0, date="2026-08-27", at=1000, note="")
    add_weight(db, kg=79.5, date="2026-08-27", at=2000, note="")
    add_weight(db, kg=79.0, date="2026-08-28", at=3000, note="")
    rows = weight_points_between(db, start="2026-08-27", end="2026-08-28")
    assert [(at, kg) for at, kg in rows] == [(1000, 80.0), (2000, 79.5), (3000, 79.0)]


def test_weight_points_between_respects_the_range(db):
    add_weight(db, kg=90.0, date="2026-08-01", at=1, note="")
    add_weight(db, kg=80.0, date="2026-08-27", at=2, note="")
    rows = weight_points_between(db, start="2026-08-27", end="2026-08-28")
    assert [kg for _, kg in rows] == [80.0]


def test_weight_points_between_unbounded_start(db):
    add_weight(db, kg=90.0, date="2026-08-01", at=1, note="")
    add_weight(db, kg=80.0, date="2026-08-27", at=2, note="")
    rows = weight_points_between(db, start=None, end="2026-08-28")
    assert [kg for _, kg in rows] == [90.0, 80.0]


# ── one window: the food and activity logs take the same bounds as weight ──
# `+`/`-` moved the chart and the weight table and did nothing at all to the food or
# activity tables — 1 row at `1d` and 1 row at `all`. The window is one concept
# (`horizon.py`), so every table on the tab answers to it.
#
# `date=` stays, and stays chronological: the digest and the Day tab want "the day, in
# the order it happened", which is a different question from "the log, newest first".


def _seed_days(db, n=5, start="2026-08-25"):
    import datetime as dt

    d0 = dt.date.fromisoformat(start)
    for i in range(n):
        d = (d0 + dt.timedelta(days=i)).isoformat()
        at = int(dt.datetime.fromisoformat(f"{d}T08:00").timestamp())
        add_food(db, description=f"meal {i}", kcal=500 + i, source="labeled", date=d, at=at)
        add_activity(db, description=f"walk {i}", factor=1.4, date=d, at=at + 3600,
                     source="labeled")


def test_list_food_bounded_at_both_ends(db):
    """Both bounds, for the reason `list_weight` documents: with a lower bound alone,
    viewing an older day listed meals that had not been eaten yet."""
    _seed_days(db)
    rows = list_food(db, since="2026-08-26", until="2026-08-28")
    assert [r["date"] for r in rows] == ["2026-08-28", "2026-08-27", "2026-08-26"]


def test_list_food_windowed_is_newest_first(db):
    """The opposite of the `date=` order, deliberately: a log you scroll wants today at
    the top, a day you read wants breakfast first."""
    _seed_days(db)
    rows = list_food(db, since="2026-08-25", until="2026-08-29")
    assert [r["date"] for r in rows] == sorted([r["date"] for r in rows], reverse=True)


def test_list_food_by_date_is_unchanged_and_chronological(db):
    """`summary.build_payload` and the Day tab read this. A digest that lists dinner
    before breakfast is a different digest."""
    import datetime as dt

    base = int(dt.datetime.fromisoformat("2026-08-25T07:00").timestamp())
    add_food(db, description="late", kcal=700, source="labeled", date="2026-08-25",
             at=base + 40000)
    add_food(db, description="early", kcal=300, source="labeled", date="2026-08-25", at=base)
    assert [r["description"] for r in list_food(db, date="2026-08-25")] == ["early", "late"]


def test_list_food_with_no_bounds_is_every_row(db):
    """`all` resolves to `start=None`, which has to mean no lower bound rather than
    no rows."""
    _seed_days(db)
    assert len(list_food(db, until="2026-08-29")) == 5


def test_list_food_respects_its_limit(db):
    _seed_days(db, n=5)
    assert len(list_food(db, until="2026-08-29", limit=2)) == 2


def test_list_activity_bounded_at_both_ends(db):
    _seed_days(db)
    rows = list_activity(db, since="2026-08-26", until="2026-08-27")
    assert [r["date"] for r in rows] == ["2026-08-27", "2026-08-26"]


def test_list_activity_by_date_is_unchanged_and_chronological(db):
    """`resolved_factor` picks a day's latest inference and the digest lists the day."""
    import datetime as dt

    base = int(dt.datetime.fromisoformat("2026-08-25T07:00").timestamp())
    add_activity(db, description="evening", factor=1.6, date="2026-08-25", at=base + 40000,
                 source="labeled")
    add_activity(db, description="morning", factor=1.4, date="2026-08-25", at=base,
                 source="labeled")
    assert [r["description"] for r in list_activity(db, date="2026-08-25")] == [
        "morning", "evening",
    ]


def test_asking_for_both_a_date_and_a_window_is_refused(db):
    """Two different questions with two different orders. Silently preferring one would
    make the caller's intent unknowable from the call."""
    import pytest

    with pytest.raises(BodyError):
        list_food(db, date="2026-08-25", since="2026-08-01")
    with pytest.raises(BodyError):
        list_activity(db, date="2026-08-25", until="2026-08-30")


def test_list_weight_respects_its_limit(db):
    """The cap is a safety net on a pathological "all time", and the header says
    "(capped)" off the back of it — so it has to actually bound the query."""
    import datetime as dt

    for i in range(6):
        d = (dt.date(2026, 8, 20) + dt.timedelta(days=i)).isoformat()
        at = int(dt.datetime.fromisoformat(f"{d}T07:00").timestamp())
        add_weight(db, kg=80.0 - i, date=d, at=at)
    assert len(list_weight(db, limit=3)) == 3
    assert len(list_weight(db, since="2026-08-20", until="2026-08-25", limit=2)) == 2


def test_restamp_follows_a_changed_date_even_when_the_minute_is_the_same(db):
    """The guard compared only `%H:%M`, so it answered "nothing changed" for an edit that
    moved the row to a different day at the same clock time — and the caller then wrote the
    new `date` beside the old day's timestamp.

    That inverts the two named weight concepts. Reproduced: an after-dinner 82.0 on the 4th
    and a fasted 80.0 on the 5th; move the evening row to the 5th and `morning_weight`
    returns 82.0 while `latest_weight` returns 80.0 — exactly backwards, so the trend, the
    7d/30d deltas and the digest's `weight_kg` (documented as the reading "before any of the
    food listed") all take the after-dinner one.
    """
    at = _stamp(2026, 8, 20, 21, 0, 17)
    out = restamp(at, date="2026-08-21", hhmm="21:00", tz=RE_TZ)
    assert out is not None, "a date-only move left the stamp on the old day"
    assert _wall(out) == datetime(2026, 8, 21, 21, 0, tzinfo=_RZ)


def test_restamp_still_returns_none_when_neither_half_moved(db):
    """The whole reason `None` exists: the stamp carries seconds the grammar cannot express,
    and those seconds are the tie-breaker `weight_series_between` uses to pick a day's reading."""
    assert restamp(_stamp(2026, 8, 20, 7, 5, 43), date="2026-08-20", hhmm="07:05",
                   tz=RE_TZ) is None


def test_a_date_only_weight_edit_keeps_the_two_concepts_straight(db):
    """The end-to-end shape of it, through the data layer the UI calls."""
    evening = _stamp(2026, 9, 4, 21, 0, 17)
    morning = _stamp(2026, 9, 5, 6, 30, 41)
    rid = add_weight(db, kg=82.0, date="2026-09-04", at=evening, note="after dinner")
    add_weight(db, kg=80.0, date="2026-09-05", at=morning)

    moved = restamp(evening, date="2026-09-05", hhmm="21:00", tz=RE_TZ)
    update_weight(db, rid, kg=82.0, date="2026-09-05", note="after dinner",
                  measured_at=moved if moved is not None else evening)

    assert morning_weight(db, on_or_before="2026-09-05")["kg"] == 80.0, (
        "the fasted reading is no longer the day's first"
    )
    assert latest_weight(db, on_or_before="2026-09-05")["kg"] == 82.0, (
        "the after-dinner reading is no longer the day's last"
    )


def test_a_date_only_activity_edit_moves_the_days_factor_with_it(db, tmp_path):
    """`resolved_factor` picks a day's *latest* inference by `logged_at`. A row whose date
    moved but whose stamp did not is counted for the new day at the old day's position, so
    it can win a tie it should lose — rescaling every calorie figure for that day."""
    from daylogs.body import add_activity, resolved_factor, update_activity

    add_activity(db, description="walk", factor=1.3, date="2026-08-25",
                 at=_stamp(2026, 8, 25, 8, 0), source="labeled")
    add_activity(db, description="gym", factor=1.8, date="2026-08-25",
                 at=_stamp(2026, 8, 25, 18, 0), source="labeled")
    stretch = _stamp(2026, 8, 27, 7, 5)
    sid = add_activity(db, description="stretch", factor=1.6, date="2026-08-27",
                       at=stretch, source="labeled")
    cfg = _cfg(tmp_path, timezone=RE_TZ)
    assert resolved_factor(db, cfg, date="2026-08-25") == (1.8, "logged")

    moved = restamp(stretch, date="2026-08-25", hhmm="07:05", tz=RE_TZ)
    update_activity(db, sid, description="stretch", date="2026-08-25",
                    **({} if moved is None else {"logged_at": moved}))
    assert resolved_factor(db, cfg, date="2026-08-25") == (1.8, "logged"), (
        "the moved row won a tie it should lose — its stamp stayed on the old day"
    )


def test_the_two_modes_select_the_same_rows_in_opposite_orders(db):
    """`1d` selects exactly what the per-day view did — and lists it newest-first, because
    the window is a log and `date=` is prose.

    The window was `stamp ASC` for a while so that a `1d` window matched the digest's order
    exactly. That equivalence cost more than it bought: the table changed direction halfway
    down, which reads as a bug when you scroll it. The rows are what have to agree; the
    order is allowed to differ, and does.
    """
    for hh, mm, name in ((8, 5, "breakfast"), (12, 40, "lunch"), (19, 20, "dinner")):
        add_food(db, description=name, kcal=500, source="labeled", date="2026-09-04",
                 at=_stamp(2026, 9, 4, hh, mm))
    by_date = [r["description"] for r in list_food(db, date="2026-09-04")]
    windowed = [r["description"] for r in list_food(db, since="2026-09-04", until="2026-09-04")]
    assert by_date == ["breakfast", "lunch", "dinner"], "the digest reads a day forwards"
    assert windowed == ["dinner", "lunch", "breakfast"], "the table reads newest first"
    assert set(windowed) == set(by_date), "the two modes must select the same rows"


def test_a_multi_day_window_is_newest_first_all_the_way_down(db):
    """One direction for the whole table. Chronological within a day made the list change
    direction halfway down — today's dinner above today's breakfast is what you expect from
    a log, and reading a day forwards *underneath* a descending date column does not."""
    for d in ("2026-09-03", "2026-09-04"):
        for hh, name in ((8, "breakfast"), (19, "dinner")):
            add_food(db, description=f"{name} {d[-2:]}", kcal=500, source="labeled", date=d,
                     at=_stamp(2026, int(d[5:7]), int(d[8:]), hh, 0))
    got = [r["description"] for r in list_food(db, since="2026-09-01", until="2026-09-04")]
    assert got == ["dinner 04", "breakfast 04", "dinner 03", "breakfast 03"], got


def test_a_days_weigh_ins_are_listed_latest_reading_first(db):
    """Same order the food and activity windows use, so the three Body tables agree — and
    within a day it puts `latest_weight`'s reading on top, which is the one the WEIGHT
    header states. `morning_weight`'s sits below it and is what the trend, the 7d/30d
    deltas and the digest take."""
    add_weight(db, kg=80.5, date="2026-09-04", at=_stamp(2026, 9, 4, 10, 40))
    add_weight(db, kg=80.0, date="2026-09-04", at=_stamp(2026, 9, 4, 7, 5))
    add_weight(db, kg=81.0, date="2026-09-03", at=_stamp(2026, 9, 3, 7, 30))
    rows = list_weight(db, since="2026-09-01", until="2026-09-04")
    assert [(r["date"], r["kg"]) for r in rows] == [
        ("2026-09-04", 80.5), ("2026-09-04", 80.0), ("2026-09-03", 81.0),
    ], "newest first, within a day as well as across days"
    assert rows[0]["kg"] == latest_weight(db, on_or_before="2026-09-04")["kg"], (
        "the top row of a day should be the reading the header states"
    )
    assert rows[1]["kg"] == morning_weight(db, on_or_before="2026-09-04")["kg"], (
        "the trend's reading is still there, one row down"
    )


# ── sorting a window ─────────────────────────────────────────────────────
def test_value_sort_orders_by_the_tables_own_number(db):
    """`k` on Body sorts by the one number each table has — kcal here. The date and clock
    stay as the tiebreak, so rows sharing a number keep a stable order rather than
    shuffling between reloads."""
    for kc, hh in ((500, 8), (900, 12), (200, 19)):
        add_food(db, description=f"meal {kc}", kcal=kc, source="labeled", date="2026-09-04",
                 at=_stamp(2026, 9, 4, hh, 0))
    win = dict(since="2026-09-01", until="2026-09-04")
    assert [r["kcal"] for r in list_food(db, **win, sort="value")] == [900, 500, 200]
    assert [r["kcal"] for r in list_food(db, **win, sort="value", desc=False)] == [200, 500, 900]
    # The default is unchanged: date sort, newest first.
    assert [r["kcal"] for r in list_food(db, **win)] == [200, 900, 500]


def test_date_sort_ascending_reads_the_window_forwards(db):
    """The other direction of the default field — pressing `d` twice. Oldest day first and
    each day forwards, which is the order the digest reads and the only way to get it in
    the table."""
    for d, hh in (("2026-09-03", 8), ("2026-09-04", 8), ("2026-09-04", 19)):
        add_food(db, description=f"{d} {hh}", kcal=500, source="labeled", date=d,
                 at=_stamp(2026, 9, int(d[8:]), hh, 0))
    got = [r["description"] for r in list_food(
        db, since="2026-09-01", until="2026-09-04", sort="date", desc=False
    )]
    assert got == ["2026-09-03 8", "2026-09-04 8", "2026-09-04 19"], got


def test_value_sort_applies_to_weight_and_activity_too(db):
    """One key, three tables: the number is kg on weight and factor on activity. They go
    through the same `_order_by`, so the sub-views cannot drift apart."""
    add_weight(db, kg=80.0, date="2026-09-04", at=_stamp(2026, 9, 4, 7, 0))
    add_weight(db, kg=81.5, date="2026-09-03", at=_stamp(2026, 9, 3, 7, 0))
    add_activity(db, description="gym", factor=1.6, source="labeled", date="2026-09-04",
                 at=_stamp(2026, 9, 4, 18, 0))
    add_activity(db, description="walk", factor=1.2, source="labeled", date="2026-09-03",
                 at=_stamp(2026, 9, 3, 18, 0))
    win = dict(since="2026-09-01", until="2026-09-04")
    assert [r["kg"] for r in list_weight(db, **win, sort="value")] == [81.5, 80.0]
    assert [r["factor"] for r in list_activity(db, **win, sort="value")] == [1.6, 1.2]


def test_a_null_factor_sorts_to_the_top_ascending(db):
    """An inference that never landed has no number, and SQLite puts NULLs first ascending.
    That is the useful direction rather than a wart: those are the rows worth fixing, and
    `resolved_factor` silently falls back to the baseline for them."""
    add_activity(db, description="landed", factor=1.6, source="estimated", date="2026-09-04",
                 at=_stamp(2026, 9, 4, 8, 0))
    add_activity(db, description="failed", factor=None, source="estimated", date="2026-09-04",
                 at=_stamp(2026, 9, 4, 9, 0))
    got = [r["description"] for r in list_activity(
        db, since="2026-09-01", until="2026-09-04", sort="value", desc=False
    )]
    assert got[0] == "failed", got


def test_an_unknown_sort_is_rejected(db):
    """The sort name arrives from a keypress, so it is checked; the column names it maps to
    are literals in `body.py` and never reach SQL from outside."""
    with pytest.raises(BodyError, match="sort must be one of"):
        list_food(db, since="2026-09-01", sort="kcal; DROP TABLE food")
