import pytest

from daylogs.money import MoneyError, add_expense, summarize_month, upsert_budget


def _e(db, amount, category, date, description="x"):
    return add_expense(db, amount=amount, description=description, category=category, date=date)


def _b(db, category, amount, month="2026-08", name=None):
    return upsert_budget(
        db, month=month, name=name or category.title(), category=category, amount=amount
    )


def _cats(summary):
    return {c.category: c for c in summary.by_category}


def test_empty_month_is_all_zeros(db):
    s = summarize_month(db, month="2026-08", today="2026-08-27")
    assert (s.total_spent, s.total_budget, s.remaining) == (0.0, 0.0, 0.0)
    assert s.by_category == []
    assert s.over_budget == []
    assert s.under_budget_remaining == []


def test_totals_and_remaining(db):
    _b(db, "grocery", 500)
    _b(db, "restaurant", 200)
    _e(db, 412.0, "grocery", "2026-08-10")
    _e(db, 289.0, "restaurant", "2026-08-11")
    s = summarize_month(db, month="2026-08", today="2026-08-27")
    assert s.total_budget == 700.0
    assert s.total_spent == 701.0
    assert s.remaining == pytest.approx(-1.0)


def test_per_category_delta_is_budget_minus_spent(db):
    _b(db, "grocery", 500)
    _e(db, 412.0, "grocery", "2026-08-10")
    cat = _cats(summarize_month(db, month="2026-08"))
    assert cat["grocery"].budget == 500.0
    assert cat["grocery"].spent == 412.0
    assert cat["grocery"].delta == pytest.approx(88.0)


def test_refund_reduces_category_spend(db):
    _b(db, "grocery", 500)
    _e(db, 100.0, "grocery", "2026-08-10")
    _e(db, -25.0, "grocery", "2026-08-11", "refund")
    assert _cats(summarize_month(db, month="2026-08"))["grocery"].spent == pytest.approx(75.0)


def test_category_with_spend_but_no_budget_appears_with_zero_budget(db):
    _e(db, 40.0, "transport", "2026-08-10")
    cat = _cats(summarize_month(db, month="2026-08"))
    assert cat["transport"].budget == 0.0
    assert cat["transport"].delta == pytest.approx(-40.0)


def test_category_with_budget_but_no_spend_appears_with_zero_spend(db):
    _b(db, "education", 300)
    cat = _cats(summarize_month(db, month="2026-08"))
    assert cat["education"].spent == 0.0
    assert cat["education"].delta == pytest.approx(300.0)


def test_multiple_budget_lines_in_one_category_sum(db):
    _b(db, "subscriptions", 20.99, name="streaming")
    _b(db, "subscriptions", 10.0, name="cloud storage")
    assert _cats(summarize_month(db, month="2026-08"))["subscriptions"].budget == pytest.approx(
        30.99
    )


def test_over_and_under_budget_lists(db):
    _b(db, "restaurant", 200)
    _b(db, "grocery", 500)
    _e(db, 289.0, "restaurant", "2026-08-11")
    _e(db, 412.0, "grocery", "2026-08-10")
    s = summarize_month(db, month="2026-08")
    assert [c.category for c in s.over_budget] == ["restaurant"]
    assert [c.category for c in s.under_budget_remaining] == ["grocery"]


def test_over_budget_ignores_categories_with_no_budget(db):
    _e(db, 40.0, "transport", "2026-08-10")
    assert summarize_month(db, month="2026-08").over_budget == []


def test_exactly_on_budget_is_neither_over_nor_under(db):
    _b(db, "grocery", 100)
    _e(db, 100.0, "grocery", "2026-08-10")
    s = summarize_month(db, month="2026-08")
    assert s.over_budget == [] and s.under_budget_remaining == []


def test_history_is_six_months_current_month_last(db):
    _e(db, 100.0, "grocery", "2026-03-05")
    _e(db, 200.0, "grocery", "2026-06-05")
    _e(db, 412.0, "grocery", "2026-08-05")
    cat = _cats(summarize_month(db, month="2026-08"))
    # window is 2026-03 .. 2026-08
    assert cat["grocery"].history == [100.0, 0.0, 0.0, 200.0, 0.0, 412.0]
    assert cat["grocery"].history[-1] == cat["grocery"].spent


def test_history_ignores_months_outside_the_window(db):
    _e(db, 999.0, "grocery", "2026-01-05")
    _e(db, 10.0, "grocery", "2026-08-05")
    assert 999.0 not in _cats(summarize_month(db, month="2026-08"))["grocery"].history


def test_history_crosses_a_year_boundary(db):
    _e(db, 11.0, "grocery", "2025-11-05")
    _e(db, 22.0, "grocery", "2026-01-05")
    cat = _cats(summarize_month(db, month="2026-01"))
    # window is 2025-08 .. 2026-01
    assert cat["grocery"].history == [0.0, 0.0, 0.0, 11.0, 0.0, 22.0]


def test_history_december_upper_bound_does_not_leak_next_year(db):
    _e(db, 5.0, "grocery", "2026-12-31")
    _e(db, 999.0, "grocery", "2027-01-01")
    cat = _cats(summarize_month(db, month="2026-12"))
    assert cat["grocery"].spent == 5.0


def test_calendar_progress_fields(db):
    s = summarize_month(db, month="2026-08", today="2026-08-27")
    assert (s.day_of_month, s.days_in_month) == (27, 31)


def test_day_of_month_clamps_for_a_past_month(db):
    s = summarize_month(db, month="2026-07", today="2026-08-27")
    assert (s.day_of_month, s.days_in_month) == (31, 31)


def test_day_of_month_zero_for_a_future_month(db):
    s = summarize_month(db, month="2026-09", today="2026-08-27")
    assert s.day_of_month == 0


def test_february_length_is_correct(db):
    assert summarize_month(db, month="2028-02", today="2028-02-10").days_in_month == 29


def test_bad_month_rejected(db):
    with pytest.raises(MoneyError):
        summarize_month(db, month="August")


def test_history_is_one_query_not_one_per_month(db):
    """The obvious implementation issues one query per history month. Guard against that
    regression: the whole six-month window must be a single grouped query."""
    _e(db, 10.0, "grocery", "2026-08-05")
    seen: list[str] = []
    db.set_trace_callback(seen.append)
    try:
        summarize_month(db, month="2026-08")
    finally:
        db.set_trace_callback(None)
    grouped = [q for q in seen if "substr(date, 1, 7)" in q]
    assert len(grouped) == 1
