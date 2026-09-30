import pytest

from daylogs.horizon import Span
from daylogs.money import (
    add_expense,
    group_expenses,
    month_span,
    query_expenses,
    summarize_month,
    summarize_span,
    upsert_budget,
)
from daylogs.moneyview import MoneyView


def _span(months):
    """The span a list of whole calendar months covers.

    `summarize_range(months=[...])` used to do this and had no app caller —
    `summarize_span` is the general form every surface actually uses. The
    arithmetic these tests guard is unchanged; only the way in is.
    """
    if not months:
        return None
    return Span(horizon="MTD", start=f"{months[0]}-01", end=month_span(months[-1]).end)


def _e(db, amount, category, date, description="x"):
    return add_expense(db, amount=amount, description=description, category=category, date=date)


@pytest.fixture()
def seeded(db):
    _e(db, 100.0, "grocery", "2026-06-10", "june shop")
    _e(db, 50.0, "restaurant", "2026-07-11", "july dinner")
    _e(db, 25.0, "grocery", "2026-08-12", "august shop")
    _e(db, 10.0, "transport", "2026-08-13", "bus pass")
    upsert_budget(db, month="2026-06", name="Grocery", category="grocery", amount=200)
    upsert_budget(db, month="2026-08", name="Grocery", category="grocery", amount=300)
    return db


# ── whole-month spans ────────────────────────────────────────────────────
def test_single_month_matches_summarize_month(seeded):
    a = summarize_span(seeded, span=_span(["2026-08"]), today="2026-08-27")
    b = summarize_month(seeded, month="2026-08", today="2026-08-27")
    assert (a.total_spent, a.total_budget, a.remaining) == (
        b.total_spent, b.total_budget, b.remaining,
    )


def test_quarter_sums_spend_across_months(seeded):
    s = summarize_span(seeded, span=_span(["2026-06", "2026-07", "2026-08"]), today="2026-08-27")
    assert s.total_spent == pytest.approx(185.0)


def test_quarter_budget_is_the_sum_of_those_months(seeded):
    s = summarize_span(seeded, span=_span(["2026-06", "2026-07", "2026-08"]), today="2026-08-27")
    assert s.total_budget == pytest.approx(500.0)


def test_all_time_is_unbounded(seeded):
    s = summarize_span(seeded, span=_span([]), today="2026-08-27")
    assert s.total_spent == pytest.approx(185.0)
    assert s.total_budget == pytest.approx(500.0)
    assert s.month == "all"


def test_range_categories_merge_across_months(seeded):
    s = summarize_span(seeded, span=_span(["2026-06", "2026-07", "2026-08"]), today="2026-08-27")
    cats = {c.category: c for c in s.by_category}
    assert cats["grocery"].spent == pytest.approx(125.0)
    assert cats["grocery"].budget == pytest.approx(500.0)


def test_calendar_progress_only_for_a_single_month(seeded):
    one = summarize_span(seeded, span=_span(["2026-08"]), today="2026-08-27")
    assert (one.day_of_month, one.days_in_month) == (27, 31)
    many = summarize_span(seeded, span=_span(["2026-07", "2026-08"]), today="2026-08-27")
    assert (many.day_of_month, many.days_in_month) == (0, 0)
    allt = summarize_span(seeded, span=_span([]), today="2026-08-27")
    assert (allt.day_of_month, allt.days_in_month) == (0, 0)


def test_history_is_still_six_months(seeded):
    s = summarize_span(seeded, span=_span([]), today="2026-08-27")
    for c in s.by_category:
        assert len(c.history) == 6


def test_empty_range_is_zeros(db):
    s = summarize_span(db, span=_span(["2026-01"]), today="2026-08-27")
    assert (s.total_spent, s.total_budget) == (0.0, 0.0)


def test_all_time_on_an_empty_database_does_not_crash(db):
    s = summarize_span(db, span=_span([]), today="2026-08-27")
    assert s.total_spent == 0.0
    assert s.by_category == []


def test_bad_month_still_rejected(seeded):
    from daylogs.money import MoneyError

    with pytest.raises(MoneyError):
        summarize_span(seeded, span=_span(["nonsense"]))


# ── query_expenses ───────────────────────────────────────────────────────
def test_query_respects_the_span(seeded):
    v = MoneyView(anchor="2026-08-31")  # MTD: August only
    assert {r["description"] for r in query_expenses(seeded, v)} == {
        "august shop", "bus pass",
    }
    v.horizon = "3m"   # Jun 3 – Aug 31, so it reaches the June row
    assert len(query_expenses(seeded, v)) == 4


def test_query_all_time(seeded):
    assert len(query_expenses(seeded, MoneyView(anchor="2026-08-31", horizon="all"))) == 4


def test_sort_by_date_both_directions(seeded):
    v = MoneyView(anchor="2026-08-31", horizon="all", sort_field="date", sort_desc=True)
    dates = [r["date"] for r in query_expenses(seeded, v)]
    assert dates == sorted(dates, reverse=True)
    v.sort_desc = False
    dates = [r["date"] for r in query_expenses(seeded, v)]
    assert dates == sorted(dates)


def test_sort_by_amount(seeded):
    v = MoneyView(anchor="2026-08-31", horizon="all", sort_field="amount", sort_desc=True)
    assert [r["amount"] for r in query_expenses(seeded, v)] == [100.0, 50.0, 25.0, 10.0]


def test_sort_by_category(seeded):
    v = MoneyView(anchor="2026-08-31", horizon="all", sort_field="category", sort_desc=False)
    cats = [r["category"] for r in query_expenses(seeded, v)]
    assert cats == sorted(cats)


def test_filter_by_category(seeded):
    v = MoneyView(anchor="2026-08-31", horizon="all", filter_category="grocery")
    assert {r["category"] for r in query_expenses(seeded, v)} == {"grocery"}


def test_filter_by_text_matches_description_or_category(seeded):
    v = MoneyView(anchor="2026-08-31", horizon="all", filter_text="shop")
    assert len(query_expenses(seeded, v)) == 2
    v.filter_text = "transport"
    assert len(query_expenses(seeded, v)) == 1


def test_text_filter_is_case_insensitive(seeded):
    v = MoneyView(anchor="2026-08-31", horizon="all", filter_text="JULY")
    assert len(query_expenses(seeded, v)) == 1


def test_text_filter_does_not_treat_input_as_a_pattern(seeded):
    _e(seeded, 5.0, "other", "2026-08-20", "100% cotton")
    v = MoneyView(anchor="2026-08-31", horizon="all", filter_text="100%")
    assert [r["description"] for r in query_expenses(seeded, v)] == ["100% cotton"]


def test_underscore_in_a_filter_is_literal_too(seeded):
    _e(seeded, 5.0, "other", "2026-08-21", "a_b")
    _e(seeded, 5.0, "other", "2026-08-22", "axb")
    v = MoneyView(anchor="2026-08-31", horizon="all", filter_text="a_b")
    assert [r["description"] for r in query_expenses(seeded, v)] == ["a_b"]


def test_both_filters_compose(seeded):
    v = MoneyView(
        anchor="2026-08-31", horizon="all", filter_category="grocery", filter_text="june"
    )
    assert [r["description"] for r in query_expenses(seeded, v)] == ["june shop"]


def test_filter_that_matches_nothing_returns_empty(seeded):
    v = MoneyView(anchor="2026-08-31", horizon="all", filter_text="zzzz")
    assert query_expenses(seeded, v) == []


def test_unknown_sort_field_is_rejected_not_injected(seeded):
    from daylogs.money import MoneyError

    v = MoneyView(anchor="2026-08-31", horizon="all")
    v.sort_field = "amount; DROP TABLE expense"
    with pytest.raises(MoneyError):
        query_expenses(seeded, v)


# ── group_expenses ───────────────────────────────────────────────────────
def test_groups_are_ordered_by_total_descending(seeded):
    rows = query_expenses(seeded, MoneyView(anchor="2026-08-31", horizon="all"))
    groups = group_expenses(rows, collapsed=frozenset())
    assert [g[0] for g in groups] == ["grocery", "restaurant", "transport"]
    assert groups[0][1] == pytest.approx(125.0)
    assert groups[0][2] == 2


def test_collapsed_group_keeps_totals_but_drops_rows(seeded):
    rows = query_expenses(seeded, MoneyView(anchor="2026-08-31", horizon="all"))
    groups = {g[0]: g for g in group_expenses(rows, collapsed=frozenset({"grocery"}))}
    assert groups["grocery"][1] == pytest.approx(125.0)
    assert groups["grocery"][2] == 2
    assert groups["grocery"][3] == []
    assert len(groups["restaurant"][3]) == 1


def test_grouping_an_empty_list(db):
    assert group_expenses([], collapsed=frozenset()) == []


def test_grouping_preserves_the_incoming_row_order_within_a_group(seeded):
    v = MoneyView(anchor="2026-08-31", horizon="all", sort_field="amount", sort_desc=True)
    rows = query_expenses(seeded, v)
    groups = {g[0]: g for g in group_expenses(rows, collapsed=frozenset())}
    amounts = [r["amount"] for r in groups["grocery"][3]]
    assert amounts == sorted(amounts, reverse=True)
