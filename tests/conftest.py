import datetime as dt

import pytest

from daylogs.config import Config
from daylogs.db import connect, ensure_schema

# The harness clock is FROZEN, and a test that wants a different today opts *in* with `now=`.
#
# The alternative — falling back to the wall clock — makes a test's result depend on when the
# suite runs. Many seed a dated row and read it back through a window anchored on today, so
# the calendar alone slides the fixture out of view: no commit, no push, no signal. That has
# happened six times. On 2026-09-26 eight tests began failing on a `main` last touched on
# 2026-09-14, whose last CI run — from the 15th — was green, because push-triggered CI cannot
# see a failure the passage of time caused.
#
# Writing fixture dates *relative to the real clock* would trade that for something worse: the
# row stays "two days old" but which calendar month it lands in changes, so a test straddles
# `_covered_months`, `roll_month_budgets` and `shift`'s day-clamp on some run dates and not
# others — passing ~28 days a month and failing ~2, unreproducibly. Relative to THIS constant
# is the pattern that reads as intent and still resolves identically forever.
#
# Mid-month deliberately: on the 29th of a 30-day month a future test doing "a month later"
# would hit `horizon.shift`'s clamping branch by accident rather than by intent. Naive, like
# the 200 tests that already pin their own clock, and read as wall time in `cfg.timezone`.
# Verified: the whole suite is green frozen here, so nothing was bent to fit the date.
FROZEN_NOW = dt.datetime(2026, 9, 15, 12, 0)


@pytest.fixture(autouse=True)
def _fast_pilot(monkeypatch):
    """Shorten Textual's idle-wait granularity for the whole suite.

    `pilot.pause()` waits for the app to go idle, and Textual polls for that on a
    20 ms tick. The suite awaits it thousands of times, so almost all of a four-minute
    run was sleeping: 236 s -> 83 s, measured, with all 1,163 tests still passing.

    That mattered more than it sounds. A four-minute gate is a gate nobody runs while
    editing, and six defects accumulated behind 1,163 green tests — an audit found them,
    not the suite, because the suite was too slow to be part of the loop.

    `textual._wait.SLEEP_GRANULARITY` is private, so this is patched through
    `monkeypatch.setattr` deliberately: it raises immediately if the attribute is ever
    renamed, rather than silently going back to sleeping.
    """
    import textual._wait

    monkeypatch.setattr(textual._wait, "SLEEP_GRANULARITY", 0.002)


@pytest.fixture()
def db(tmp_path):
    conn = connect(tmp_path / "test.db")
    ensure_schema(conn)
    yield conn
    conn.close()


@pytest.fixture()
def make_cfg(tmp_path):
    """A Config rooted at tmp_path. summary_after_hour defaults to 99 so the
    summary autorun never fires unless a test asks for it."""

    def _make(**kw):
        base = dict(
            root=tmp_path,
            db_path=tmp_path / "test.db",
            inbox_dir=tmp_path / "inbox",
            memory_path=tmp_path / "memory.md",
            summary_after_hour=99,
        )
        return Config(**{**base, **kw})

    return _make


@pytest.fixture()
def make_app(db, make_cfg):
    """Build a DaylogsApp against the test database with injected runners."""
    from daylogs.tui.app import DaylogsApp

    def _make(*, cfg=None, **kw):
        runners = {k: kw.pop(k) for k in list(kw) if k.startswith("runner_")}
        now = kw.pop("now", None) or (lambda: FROZEN_NOW)
        return DaylogsApp(cfg or make_cfg(**kw), db, now=now, **runners)

    return _make


@pytest.fixture()
def type_into():
    """Type a string into whatever has focus, one key at a time."""

    async def _type(pilot, text):
        for ch in text:
            await pilot.press("space" if ch == " " else ch)

    return _type
