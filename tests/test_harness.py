"""Tests about the test harness itself.

`CLAUDE.md` says no test result may depend on when the suite runs, and that rule failed six
times while it lived only in prose. These are the assertions that make it a tripwire instead
of a good intention.
"""

import datetime as dt
import pathlib

from conftest import FROZEN_NOW
from helpers import go_body

from daylogs.body import add_weight, list_weight


def test_a_bare_make_app_gets_the_frozen_clock(make_app):
    """The default has to be frozen, not the wall clock.

    Reverting this one `or` puts 190 tests back on a clock that moves, and the failure it
    reintroduces is silent for weeks: a dated fixture slides out of its window, `enter`
    selects nothing, and the test goes on passing while asserting nothing until the day the
    calendar pushes it over the edge.
    """
    app = make_app()
    assert app.now() == FROZEN_NOW
    assert app.today() == "2026-09-15"


def test_a_test_can_still_choose_its_own_today(make_app):
    """Freezing the default must not take the override away — plenty of tests need a
    specific day, and one that reads a *stale* frozen date would be worse than one reading
    the wall clock, because it would look deliberate."""
    other = dt.datetime(2026, 12, 25, 8, 30)
    app = make_app(now=lambda: other)
    assert app.now() == other
    assert app.today() == "2026-12-25"


def test_the_frozen_date_is_not_on_a_month_edge():
    """Encoded because the reasoning is invisible once the date is just a literal: on the
    1st or the last day of a month, a test doing "a month later" lands on `horizon.shift`'s
    day-clamping branch by accident, and a fixture dated "a few days ago" straddles a month
    boundary — which is exactly the class of accident this constant exists to remove."""
    last = (FROZEN_NOW.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(
        days=1
    )
    assert 1 < FROZEN_NOW.day < last.day, f"{FROZEN_NOW.date()} sits on a month edge"


def test_every_test_builds_its_app_through_the_fixture():
    """The frozen clock is applied in `make_app`, so a test constructing `DaylogsApp`
    directly would quietly opt itself back onto the wall clock — one default covering every
    test is the entire reason this change is small, and it only holds while this is true.

    Greps the call sites for the same reason `test_hints.py` does: the alternative is trusting
    everyone to remember, which is the thing that has already failed six times.
    """
    me = pathlib.Path(__file__)
    offenders = _test_files_naming("DaylogsApp(", exclude=me)
    assert offenders == [], (
        f"these build the app directly and bypass the frozen clock: {offenders} — "
        "use the make_app fixture, or pass now= explicitly and say why"
    )
    # Positive control, through the *same* scan. An assertion that something is absent cannot
    # tell "nothing to find" from "stopped looking": neutering the predicate leaves it
    # reporting success forever, which a mutation run demonstrated. Most test files call the
    # fixture, so finding none of them means the scan is broken, not that the repo is clean.
    # 16 files call the fixture as this is written; the floor is deliberately well below that
    # so deleting a test file does not fail this, while a scan that has stopped reading — the
    # glob broken, the predicate short-circuited — collapses to 0 and trips it.
    callers = _test_files_naming("make_app(", exclude=me)
    assert len(callers) >= 8, (
        f"the scan found almost no callers of make_app ({len(callers)}) — "
        "it has stopped reading files, so the assertion above proves nothing"
    )


def _test_files_naming(needle: str, *, exclude: pathlib.Path) -> list[str]:
    """Test files whose source contains `needle`.

    `exclude` is for this module, which names the things it greps for in its own prose and so
    matched itself on the first run.
    """
    return [
        p.name
        for p in sorted(exclude.parent.glob("test_*.py"))
        if p != exclude and needle in p.read_text()
    ]


async def test_a_dated_fixture_stays_in_view_with_no_pin_of_its_own(make_app, db, type_into):
    """The bomb's exact shape, immune by construction.

    Eight tests looked like this and passed for weeks while asserting nothing: seed a weight
    row, open Body, press `enter` to edit the selected row. Body's window is `1m` anchored on
    today, so once the calendar pushed the fixture past the edge `enter` selected nothing, no
    edit was armed, and the assertion failed on a day nobody had touched the code.

    No `now=` here on purpose — that is the point. The date comes from `FROZEN_NOW`, so
    "three days ago" is a fixed instant and this can never age out. Written relative rather
    than as a literal because the relation is what matters; written relative to the *frozen*
    clock rather than the real one because the real one would move which month it lands in.
    """
    three_days_ago = (FROZEN_NOW - dt.timedelta(days=3)).date().isoformat()
    add_weight(db, kg=80.0, date=three_days_ago, at=1)

    app = make_app()
    async with app.run_test() as pilot:
        tab = await go_body(pilot, app)
        await pilot.pause()
        # shift+tab reaches the weight sub-view, as the weight tests in test_tui_body.py do.
        await pilot.press("shift+tab")
        await pilot.pause()
        assert tab.span().start <= three_days_ago <= tab.span().end, (
            "the fixture has to be inside the window the tab is showing"
        )
        await pilot.press("enter")
        await pilot.pause()
        armed = tab._editing
        await pilot.press("escape")
        await pilot.pause()
    assert armed is not None, (
        "`enter` selected nothing, so this test would have asserted nothing — "
        "which is how eight of these passed for weeks before failing"
    )
    assert list_weight(db)[0]["date"] == three_days_ago
