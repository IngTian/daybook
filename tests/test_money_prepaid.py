"""A payment that covers several months, counted in the months it covers.

The budget side has always prorated: `roll_month_budgets` writes `monthly_cost`, so a
240.00-a-year subscription gets a 20.00 line every month. The spend side summed the raw
charge, so the renewal month read 240.00 against a 20.00 cap — 12x over — and the other
eleven read a 20.00 saving. It nets out over a year and no single month is ever right,
which is a problem for a tab whose whole question is "am I inside the budget this month".

`#N` on an expense line is how you say a payment covers N months. The charge is stored once,
at its real amount and date — export and the expenses pane both still show the 240.00 that
left the account — and every *total* counts amount/N in each covered month.
"""

import datetime as dt
import sqlite3
import tempfile
from pathlib import Path

import pytest
from helpers import go_money

from daylogs import db as dbmod
from daylogs.horizon import resolve
from daylogs.money import (
    MoneyError,
    _covered_months,
    add_expense,
    list_budget,
    prepaid_inflows,
    roll_month_budgets,
    summarize_month,
    summarize_span,
    update_expense,
    upsert_recurring,
)
from daylogs.parse import ParseError, parse_expense, render_expense

NOW = dt.datetime(2026, 9, 14, 10, 0)
SLUGS = frozenset({"subscriptions", "grocery", "other", "restaurant"})


def _span(anchor="2027-12-31", horizon="all"):
    return resolve(horizon, anchor=anchor)


def _spent(conn, month, category="subscriptions"):
    s = summarize_month(conn, month=month, today="2026-09-30")
    cat = next((c for c in s.by_category if c.category == category), None)
    return cat.spent if cat else None


# ── the migration ────────────────────────────────────────────────────────
def test_an_existing_database_gains_the_column_without_losing_rows():
    """`CREATE TABLE IF NOT EXISTS` cannot add a column, so a database made before this
    column existed would break every reader that selects it. `_ADD_COLUMNS` ALTERs it in,
    guarded by `PRAGMA table_info` so it is safe on every open.

    Built by hand as a v2 `expense` table rather than by an older daylogs, because that is
    the shape the guard has to cope with and there is no other way to get one.
    """
    path = Path(tempfile.mkdtemp()) / "old.db"
    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE expense (id INTEGER PRIMARY KEY, date TEXT NOT NULL,"
        " amount REAL NOT NULL, description TEXT NOT NULL, category TEXT NOT NULL,"
        " note TEXT, created_at INTEGER NOT NULL)"
    )
    raw.execute(
        "INSERT INTO expense (date, amount, description, category, created_at)"
        " VALUES ('2026-01-01', 9.99, 'from before', 'other', 0)"
    )
    raw.commit()
    raw.close()

    conn = dbmod.connect(path)
    dbmod.ensure_schema(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(expense)")]
    assert "prepaid_months" in cols
    row = conn.execute("SELECT description, amount, prepaid_months FROM expense").fetchone()
    assert (row["description"], row["amount"], row["prepaid_months"]) == ("from before", 9.99, None)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == dbmod.SCHEMA_VERSION


def test_ensure_schema_is_still_idempotent():
    """It runs on every open, so a second ALTER would raise "duplicate column name"."""
    path = Path(tempfile.mkdtemp()) / "t.db"
    conn = dbmod.connect(path)
    for _ in range(3):
        dbmod.ensure_schema(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(expense)")]
    assert cols.count("prepaid_months") == 1


# ── the grammar ──────────────────────────────────────────────────────────
def test_hash_n_on_an_expense_says_how_many_months_it_covers():
    r = parse_expense("240 Insurance !subscriptions #12", now=NOW, known_slugs=SLUGS)
    assert (r.amount, r.prepaid_months) == (240.0, 12)


def test_an_ordinary_expense_has_no_marker():
    r = parse_expense("12.40 lunch !restaurant", now=NOW, known_slugs=SLUGS)
    assert r.prepaid_months is None


@pytest.mark.parametrize(
    "line,match",
    [
        ("240 x !other #1", "at least 2"),
        ("240 x !other #0", "at least 2"),
        ("240 x !other #abc", "number of months"),
        ("240 x !other #999", "not a prepayment"),
    ],
)
def test_a_nonsense_month_count_is_rejected(line, match):
    """`#1` is rejected rather than accepted: a payment covering one month *is* an ordinary
    expense, and storing the marker anyway would put a second representation of the plain
    case into every reader that has to prorate."""
    with pytest.raises(ParseError, match=match):
        parse_expense(line, now=NOW, known_slugs=SLUGS)


def test_the_marker_round_trips_through_the_edit_prefill(db):
    """`parse(render(row)) == row` is the property the whole grammar is held to, and the
    marker is displayed in the pane, so it has to be editable — including clearable."""
    add_expense(db, amount=240.0, description="Insurance", category="subscriptions",
                date="2026-09-14", prepaid_months=12)
    row = db.execute("SELECT * FROM expense").fetchone()
    line = render_expense(row)
    assert "#12" in line, line
    back = parse_expense(line, now=NOW, known_slugs=SLUGS)
    assert (back.amount, back.category, back.date, back.prepaid_months) == (
        240.0, "subscriptions", "2026-09-14", 12,
    )


def test_dropping_the_hash_from_an_edit_makes_it_an_ordinary_expense(db):
    """The same "submitted line is authoritative" rule the note follows. 0 is the clearing
    value because `update_expense` drops None to tell "not mentioned" from "set to
    nothing", and the grammar can never produce `#0`."""
    add_expense(db, amount=240.0, description="Insurance", category="subscriptions",
                date="2026-09-14", prepaid_months=12)
    row_id = db.execute("SELECT id FROM expense").fetchone()["id"]
    update_expense(db, row_id, prepaid_months=0)
    assert db.execute("SELECT prepaid_months FROM expense").fetchone()[0] is None


def test_the_data_layer_guards_the_column_too():
    """`add_expense` is called by tests, `__main__` and summary as well as by the prompt, so
    the grammar's check is not the only door."""
    conn = dbmod.connect(Path(tempfile.mkdtemp()) / "t.db")
    dbmod.ensure_schema(conn)
    with pytest.raises(MoneyError, match="between 2 and 120"):
        add_expense(conn, amount=240.0, description="x", category="other",
                    date="2026-09-14", prepaid_months=1)


# ── which months a payment covers ────────────────────────────────────────
def test_coverage_starts_in_the_payment_s_own_month_and_wraps_the_year():
    assert _covered_months("2026-09-14", 3) == ["2026-09", "2026-10", "2026-11"]
    assert _covered_months("2026-11-01", 4) == ["2026-11", "2026-12", "2027-01", "2027-02"]
    assert len(_covered_months("2026-09-14", 12)) == 12
    assert _covered_months("2026-09-14", 12)[-1] == "2027-08"


# ── the arithmetic ───────────────────────────────────────────────────────
def test_the_renewal_month_is_no_longer_over_its_cap(db):
    """The defect, stated as the numbers it produced: 240.00 against a 20.00 cap."""
    upsert_recurring(db, name="Insurance", cost=240.0, cycle="annually", category="subscriptions")
    roll_month_budgets(db, month="2026-09")
    add_expense(db, amount=240.0, description="Insurance renewal", category="subscriptions",
                date="2026-09-14", prepaid_months=12)
    s = summarize_month(db, month="2026-09", today="2026-09-30")
    cat = next(c for c in s.by_category if c.category == "subscriptions")
    assert (cat.budget, cat.spent, cat.delta) == (20.0, 20.0, 0.0)
    assert list_budget(db, month="2026-09")[0]["amount"] == 20.0


def test_every_covered_month_carries_its_share(db):
    add_expense(db, amount=240.0, description="Insurance", category="subscriptions",
                date="2026-09-14", prepaid_months=12)
    for month in ("2026-09", "2026-12", "2027-08"):
        assert _spent(db, month) == 20.0, month


def test_the_month_after_coverage_ends_carries_nothing(db):
    """Twelve months from September is August, so the next September is a new year's
    problem — otherwise the charge would prorate forever."""
    add_expense(db, amount=240.0, description="Insurance", category="subscriptions",
                date="2026-09-14", prepaid_months=12)
    assert _spent(db, "2027-09") is None
    assert _spent(db, "2026-08") is None, "and nothing before it was paid, either"


def test_the_shares_add_back_to_the_amount_actually_paid(db):
    """No cent drift: the shares are summed unrounded and only the total is rounded, so a
    240.01 charge over 12 months does not lose a penny a month. Over all time the tab shows
    exactly what left the account."""
    add_expense(db, amount=240.01, description="Insurance", category="subscriptions",
                date="2026-09-14", prepaid_months=12)
    s = summarize_span(db, span=_span(), today="2027-12-31")
    cat = next(c for c in s.by_category if c.category == "subscriptions")
    assert cat.spent == 240.01


def test_an_ordinary_expense_is_untouched(db):
    """The whole change hangs off `prepaid_months IS NULL`, so the common case has to be
    provably unaffected — including a refund, which is a negative amount."""
    add_expense(db, amount=52.10, description="market", category="grocery", date="2026-09-03")
    add_expense(db, amount=-12.00, description="returned", category="grocery", date="2026-09-04")
    assert _spent(db, "2026-09", "grocery") == 40.10


def test_a_prepayment_and_an_ordinary_charge_in_one_category_both_count(db):
    """They come from two different queries now — one SQL sum, one Python spread — so the
    seam between them is worth an assertion."""
    add_expense(db, amount=240.0, description="Insurance", category="subscriptions",
                date="2026-09-14", prepaid_months=12)
    add_expense(db, amount=9.99, description="Streaming", category="subscriptions",
                date="2026-09-20")
    assert _spent(db, "2026-09") == 29.99


def test_the_six_month_sparkline_prorates_too(db):
    """The bar beside the number has to tell the same story. A single annual charge drew one
    spike in an otherwise flat six months, which reads as a spending event rather than as
    the year's subscription.

    The window here is 2026-04..2026-09 and coverage starts in May, so the first month is a
    real 0 — asserted rather than smoothed over, because it is what proves the spread has a
    *start* and is not just filling every month it can reach.
    """
    add_expense(db, amount=600.0, description="Insurance", category="subscriptions",
                date="2026-05-01", prepaid_months=12)
    s = summarize_month(db, month="2026-09", today="2026-09-30")
    cat = next(c for c in s.by_category if c.category == "subscriptions")
    assert cat.history == [0.0, 50.0, 50.0, 50.0, 50.0, 50.0], (
        f"a level 50 from May, nothing in April, no spike: {cat.history}"
    )


# ── on screen ────────────────────────────────────────────────────────────
async def test_the_expenses_pane_shows_the_real_charge_with_its_marker(make_app, db, type_into):
    """The pane lists payments, so it shows the 240.00 that left the account — a list that
    quietly showed a twelfth would be lying about the row. `#12` is what stops that reading
    as a contradiction of the header, which counts the same payment at 20.00."""
    app = make_app(now=lambda: NOW)
    async with app.run_test(size=(120, 34)) as pilot:
        tab = await go_money(pilot, app)
        await pilot.press("e")
        await type_into(pilot, "240 Insurance !subscriptions #12")
        await pilot.press("enter")
        await pilot.pause()
        while tab.view.pane != "expenses":
            await pilot.press("tab")
            await pilot.pause()
        table = app.query_one("#money-table")
        cells = [str(c) for k in table.rows for c in table.get_row(k)]
    assert any("Insurance #12" in c for c in cells), cells
    assert any("240.00" in c for c in cells), f"the pane must show what was paid: {cells}"
    row = db.execute("SELECT amount, prepaid_months FROM expense").fetchone()
    assert (row["amount"], row["prepaid_months"]) == (240.0, 12)


# ── the shares, listed ───────────────────────────────────────────────────
# A charge dated in June contributes to September's total with no row in September to
# account for it: the header read 20.00 over a list that summed to nothing. In the charge's
# own month it was worse in the other direction — 20.00 counted over a row saying 240.00.
# Both are one missing statement: which payments are being counted here, and for how much.
def test_a_month_inside_the_coverage_lists_the_share_and_its_position(db):
    add_expense(db, amount=240.0, description="Insurance", category="subscriptions",
                date="2026-09-14", prepaid_months=12)
    got = prepaid_inflows(db, resolve("MTD", anchor="2026-12-31"))
    assert len(got) == 1, got
    d = got[0]
    assert (round(d["share"], 2), d["first"], d["last"], d["months"]) == (20.0, 4, 4, 12)
    assert (d["date"], d["amount"]) == ("2026-09-14", 240.0), "the charge, not the share"


def test_the_charges_own_month_lists_it_too(db):
    """The month with the row is the month whose numbers disagree most — the pane says
    240.00 and the header counts 20.00. Leaving it out would explain every month but the
    one where the gap is widest."""
    add_expense(db, amount=240.0, description="Insurance", category="subscriptions",
                date="2026-09-14", prepaid_months=12)
    got = prepaid_inflows(db, resolve("MTD", anchor="2026-09-30"))
    assert [(round(d["share"], 2), d["first"]) for d in got] == [(20.0, 1)]


def test_a_month_outside_the_coverage_lists_nothing(db):
    add_expense(db, amount=240.0, description="Insurance", category="subscriptions",
                date="2026-09-14", prepaid_months=12)
    assert prepaid_inflows(db, resolve("MTD", anchor="2026-08-31")) == []
    assert prepaid_inflows(db, resolve("MTD", anchor="2027-09-30")) == []


def test_a_wide_span_is_one_line_per_payment_not_one_per_month(db):
    """Twelve lines for one subscription would bury the payments the pane is actually for.
    The share is summed over the covered months the span touches, and the position becomes
    a range so the line still says how much of the coverage it is describing."""
    add_expense(db, amount=240.0, description="Insurance", category="subscriptions",
                date="2026-09-14", prepaid_months=12)
    got = prepaid_inflows(db, resolve("3m", anchor="2026-11-30"))
    assert len(got) == 1, got
    assert (round(got[0]["share"], 2), got[0]["first"], got[0]["last"]) == (60.0, 1, 3)


def test_the_listed_shares_account_for_the_header_total(db):
    """The whole point of listing them. A total the pane cannot explain from its own rows is
    the defect; this is the assertion that says it is gone.

    Unrounded on purpose, for the reason `_prepaid_shares` is: rounding each line and then
    adding drifts a cent from a header that rounds once at the end.
    """
    add_expense(db, amount=240.01, description="Insurance", category="subscriptions",
                date="2026-09-14", prepaid_months=12)
    add_expense(db, amount=9.99, description="Streaming", category="subscriptions",
                date="2026-12-02")
    span = resolve("MTD", anchor="2026-12-31")
    plain = db.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM expense WHERE prepaid_months IS NULL"
        " AND date BETWEEN ? AND ?",
        (span.start, span.end),
    ).fetchone()[0]
    shares = sum(d["share"] for d in prepaid_inflows(db, span))
    s = summarize_span(db, span=span, today="2026-12-31")
    assert round(plain + shares, 2) == s.total_spent


def test_an_ordinary_expense_is_never_listed_as_a_share(db):
    add_expense(db, amount=52.10, description="market", category="grocery", date="2026-09-03")
    assert prepaid_inflows(db, resolve("MTD", anchor="2026-09-30")) == []


def test_all_time_lists_every_payment_once_at_its_full_amount(db):
    """An unbounded span touches every covered month, so the share is the whole charge —
    which is what `_months_filter` returning None already means for the totals."""
    add_expense(db, amount=240.0, description="Insurance", category="subscriptions",
                date="2026-09-14", prepaid_months=12)
    got = prepaid_inflows(db, resolve("all", anchor="2027-12-31"))
    assert [(round(d["share"], 2), d["first"], d["last"]) for d in got] == [(240.0, 1, 12)]


def test_a_prepaid_refund_survives_being_listed(db):
    """A negative amount is a refund and is first-class, so the list has to carry one
    rather than filter it out the way the panels once filtered `spent > 0`."""
    add_expense(db, amount=-120.0, description="Insurance refunded", category="subscriptions",
                date="2026-09-14", prepaid_months=12)
    got = prepaid_inflows(db, resolve("MTD", anchor="2026-09-30"))
    assert [round(d["share"], 2) for d in got] == [-10.0]


async def test_a_covered_month_shows_the_share_where_no_payment_row_exists(make_app, db, type_into):
    """October has no expense row at all — the charge is dated in September — and its header
    still counted 20.00. That is the disagreement: a total the pane could not explain from
    anything on it."""
    app = make_app(now=lambda: NOW)
    async with app.run_test(size=(120, 34)) as pilot:
        tab = await go_money(pilot, app)
        await pilot.press("e")
        await type_into(pilot, "240 Insurance !subscriptions #12")
        await pilot.press("enter")
        await pilot.pause()
        while tab.view.pane != "expenses":
            await pilot.press("tab")
            await pilot.pause()
        tab.view.anchor = "2026-10-31"
        tab.reload()
        await pilot.pause()
        table = app.query_one("#money-table")
        cells = [str(c) for k in table.rows for c in table.get_row(k)]
    assert any("⇢" in c for c in cells), f"the share has to be marked, not just dim: {cells}"
    assert any("Insurance #2/12" in c for c in cells), f"month 2 of 12: {cells}"
    assert any("20.00" in c for c in cells), f"the share, not the charge: {cells}"
    assert not any("240.00" in c for c in cells), (
        f"October is not when the money left the account: {cells}"
    )


async def test_the_share_row_cannot_be_edited_or_deleted(make_app, db, type_into):
    """A share is not a row — it is arithmetic over a charge in another month. `enter` and
    `x` treat it exactly as they treat a group header, which is the existing precedent for
    a line with no id. Getting this wrong would arm an edit against the wrong month."""
    app = make_app(now=lambda: NOW)
    async with app.run_test(size=(120, 34)) as pilot:
        tab = await go_money(pilot, app)
        await pilot.press("e")
        await type_into(pilot, "240 Insurance !subscriptions #12")
        await pilot.press("enter")
        await pilot.pause()
        while tab.view.pane != "expenses":
            await pilot.press("tab")
            await pilot.pause()
        tab.view.anchor = "2026-10-31"
        tab.reload()
        await pilot.pause()
        table = app.query_one("#money-table")
        table.move_cursor(row=0)
        await pilot.pause()
        assert tab._selected_id() is None, "the pinned share must carry no row id"
        await pilot.press("enter")
        await pilot.pause()
        assert tab._editing is None, "no edit may be armed from a share"
        await pilot.press("x")
        await pilot.pause()
    assert db.execute("SELECT COUNT(*) FROM expense").fetchone()[0] == 1, "still there"


async def test_the_shares_are_pinned_above_the_payments(make_app, db, type_into):
    """`at the top` is the whole layout decision: they are outside the sort the payments
    below keep, because a share has no date of its own to sort by."""
    app = make_app(now=lambda: NOW)
    async with app.run_test(size=(120, 34)) as pilot:
        tab = await go_money(pilot, app)
        await pilot.press("e")
        await type_into(pilot, "240 Insurance !subscriptions #12")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("e")
        await type_into(pilot, "52.10 market !grocery")
        await pilot.press("enter")
        await pilot.pause()
        while tab.view.pane != "expenses":
            await pilot.press("tab")
            await pilot.pause()
        table = app.query_one("#money-table")
        first = [str(c) for c in table.get_row(list(table.rows)[0])]
        every = [str(c) for k in table.rows for c in table.get_row(k)]
    assert any("⇢" in c for c in first), f"the share belongs on row 0: {first}"
    assert any("52.10" in c for c in every) and any("240.00" in c for c in every), (
        f"and the payments are all still there, at what was paid: {every}"
    )


def test_the_coverage_label_says_which_months_it_is_describing():
    """One month reads `#4/12`; a span catching three of them reads `#4-6/12`. The range
    branch exists because a wide span sums several months into one line, and a line saying
    `#4/12` beside three months' worth of money would misstate what it is."""
    from daylogs.tui.money_tab import _coverage

    assert _coverage({"first": 4, "last": 4, "months": 12}) == "#4/12"
    assert _coverage({"first": 4, "last": 6, "months": 12}) == "#4-6/12"
    assert _coverage({"first": 1, "last": 12, "months": 12}) == "#1-12/12"


def test_several_shares_in_one_month_still_add_back_to_the_header(db):
    """One line rounds harmlessly; the drift only appears once several do. Three 100.00
    charges over three months are 33.3333 each — rounded per line they sum to 99.99, and
    the header, which rounds once at the end, says 100.00. A pane whose rows are a cent
    short of its own total is the defect wearing a smaller hat."""
    for i in range(3):
        add_expense(db, amount=100.0, description=f"Thirds {i}", category="subscriptions",
                    date="2026-09-14", prepaid_months=3)
    span = resolve("MTD", anchor="2026-09-30")
    shares = sum(d["share"] for d in prepaid_inflows(db, span))
    s = summarize_span(db, span=span, today="2026-09-30")
    assert round(shares, 2) == s.total_spent == 100.0
    assert sum(round(d["share"], 2) for d in prepaid_inflows(db, span)) == 99.99, (
        "the cent that rounding per line would have lost"
    )


def test_the_biggest_share_leads_and_a_refund_sits_at_the_bottom(db):
    """A short pinned block answers "what is inflating this month", so it is ordered by
    size. Signed rather than by magnitude, so a refund reads as a subtraction at the end
    instead of leading the list it reduces."""
    add_expense(db, amount=120.0, description="Small", category="subscriptions",
                date="2026-09-14", prepaid_months=12)
    add_expense(db, amount=600.0, description="Large", category="subscriptions",
                date="2026-09-14", prepaid_months=12)
    add_expense(db, amount=-240.0, description="Refunded", category="subscriptions",
                date="2026-09-14", prepaid_months=12)
    got = prepaid_inflows(db, resolve("MTD", anchor="2026-09-30"))
    assert [d["description"] for d in got] == ["Large", "Small", "Refunded"], got


async def test_the_grouped_pane_marks_the_share_too(make_app, db, type_into):
    """`G` puts the marker column to work, so the share takes `⇢` where a group header
    takes `▾`. Two renderers means two chances to leave colour as the only signal."""
    app = make_app(now=lambda: NOW)
    async with app.run_test(size=(120, 34)) as pilot:
        tab = await go_money(pilot, app)
        await pilot.press("e")
        await type_into(pilot, "240 Insurance !subscriptions #12")
        await pilot.press("enter")
        await pilot.pause()
        while tab.view.pane != "expenses":
            await pilot.press("tab")
            await pilot.pause()
        tab.view.grouped = True
        tab.view.anchor = "2026-10-31"
        tab.reload()
        await pilot.pause()
        table = app.query_one("#money-table")
        first = [str(c) for c in table.get_row(list(table.rows)[0])]
    assert "⇢" in first[0], f"the marker column carries it when grouped: {first}"
    assert any("Insurance #2/12" in c for c in first), first


def _content(app, panel_id):
    """A panel's text as a parsed `Content`, whichever shape `.content` is in.

    `Static.content` hands back the raw markup string it was updated with, so the spans
    have to be parsed to assert on a style — and `plain` is the only honest way to look for
    a glyph, since the markup characters are not printed.
    """
    from textual.content import Content
    from textual.widgets import Static

    raw = app.query_one(panel_id, Static).content
    return Content.from_markup(raw) if isinstance(raw, str) else raw


# ── the panels ───────────────────────────────────────────────────────────
def test_the_summary_splits_each_category_into_paid_and_prorated(db):
    """`spent` mixes cash out in this span with a share amortised from a payment made
    elsewhere. The panels need them apart, and they have to still add up."""
    add_expense(db, amount=240.0, description="Insurance", category="subscriptions",
                date="2026-09-14", prepaid_months=12)
    add_expense(db, amount=9.99, description="Streaming", category="subscriptions",
                date="2026-09-20")
    s = summarize_month(db, month="2026-09", today="2026-09-30")
    cat = next(c for c in s.by_category if c.category == "subscriptions")
    assert (cat.spent, cat.prorated) == (29.99, 20.0)
    assert round(cat.spent - cat.prorated, 2) == 9.99, "the rest is cash out this month"


def test_a_category_with_no_prepayment_reports_no_prorated_part(db):
    add_expense(db, amount=52.10, description="market", category="grocery", date="2026-09-03")
    s = summarize_month(db, month="2026-09", today="2026-09-30")
    assert next(c for c in s.by_category if c.category == "grocery").prorated == 0.0


async def test_both_panels_mark_the_prorated_segment(make_app, db, type_into):
    """`▒` in the fill and dim on top of it — the glyph is the signal, the colour only
    emphasises, and the row keeps its own budget-status colour around both."""
    upsert_recurring(db, name="Insurance", cost=240.0, cycle="annually",
                     category="subscriptions")
    roll_month_budgets(db, month="2026-09")
    app = make_app(now=lambda: NOW)
    async with app.run_test(size=(120, 40)) as pilot:
        await go_money(pilot, app)
        await pilot.press("e")
        await type_into(pilot, "240 Insurance !subscriptions #12")
        await pilot.press("enter")
        await pilot.pause()
        budget = _content(app, "#budget-body")
        share = _content(app, "#share-body")
    for name, c in (("BUDGET vs SPENT", budget), ("WHERE IT WENT", share)):
        assert "▒" in c.plain, f"{name} must carry the glyph: {c.plain!r}"
        i = c.plain.index("▒")
        styles = [s.style for s in c.spans if s.start <= i < s.end]
        assert "dim" in styles, f"{name} must dim the run: {styles}"


async def test_a_month_with_no_prepayment_draws_no_segment(make_app, db, type_into):
    """The regression guard on screen: an ordinary month must look exactly as it did."""
    app = make_app(now=lambda: NOW)
    async with app.run_test(size=(120, 40)) as pilot:
        await go_money(pilot, app)
        await pilot.press("e")
        await type_into(pilot, "52.10 market !grocery")
        await pilot.press("enter")
        await pilot.pause()
        budget = _content(app, "#budget-body").plain
        share = _content(app, "#share-body").plain
    assert "▒" not in budget and "▒" not in share, (budget, share)


async def test_the_glyph_is_named_on_screen_only_when_one_is_drawn(make_app, db, type_into):
    """`▒` is new, so it does not get to arrive unexplained — and an ordinary month must not
    carry a legend for something it never draws."""
    app = make_app(now=lambda: NOW)
    async with app.run_test(size=(120, 40)) as pilot:
        await go_money(pilot, app)
        await pilot.press("e")
        await type_into(pilot, "52.10 market !grocery")
        await pilot.press("enter")
        await pilot.pause()
        assert "prorated" not in _content(app, "#budget-title").plain
        assert "prorated" not in _content(app, "#share-title").plain
        await pilot.press("e")
        await type_into(pilot, "240 Insurance !subscriptions #12")
        await pilot.press("enter")
        await pilot.pause()
        budget = _content(app, "#budget-title").plain
        share = _content(app, "#share-title").plain
    assert "▒ prorated" in share, f"named beside the panel that drew it: {share!r}"
    # Each panel keeps its *own* title through the rewrite. `_legend` replaces the whole
    # Static rather than appending to it, so a title looked up wrongly would silently rename
    # a panel the moment a prepayment appeared — which is the drift `_PANEL_TITLES` exists to
    # prevent, and nothing checked it until a mutant renamed WHERE IT WENT and every test
    # still passed.
    assert budget.startswith("BUDGET vs SPENT"), budget
    assert share.startswith("WHERE IT WENT"), share
