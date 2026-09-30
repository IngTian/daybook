import datetime as dt

import pytest
from helpers import all_expenses, assert_armed, go_money

from daylogs.money import (
    add_expense,
    delete_recurring,
    list_budget,
    list_recurring,
    upsert_budget,
    upsert_recurring,
)


async def test_e_logs_an_expense_with_inferred_category(make_app, db, type_into):
    app = make_app()
    async with app.run_test() as pilot:
        await go_money(pilot, app)
        await pilot.press("e")
        await type_into(pilot, "12.40 lunch !restaurant")
        await pilot.press("enter")
        await pilot.pause()
    rows = all_expenses(db)
    assert len(rows) == 1
    assert (rows[0]["amount"], rows[0]["category"], rows[0]["description"]) == (
        12.40,
        "restaurant",
        "lunch",
    )


async def test_e_without_a_category_writes_other_and_reopens_for_correction(
    make_app, db, type_into
):
    app = make_app()
    async with app.run_test() as pilot:
        await go_money(pilot, app)
        await pilot.press("e")
        await type_into(pilot, "12.40 lunch")
        await pilot.press("enter")
        await pilot.pause()
        assert app.prompt.is_open is True
        assert app.prompt.label == "fix category"
        assert "12.40 lunch" in app.prompt.value
    assert all_expenses(db)[0]["category"] == "other"


async def test_fixing_the_category_refiles_the_row_instead_of_booking_a_second_one(
    make_app, db, type_into
):
    """One lunch is one row. This asserted `["other", "restaurant"]` for four versions —
    the duplicate, frozen as expected behaviour by a test named for the re-prompt loop.

    `fix category` routes to `_submit_expense(refiling=True)`, which calls
    `_take_editing("expense")`; nothing armed `_editing`, because the only assignment was
    the `enter`-on-a-row path, so `row_id` was None and the "fix" INSERTed. 12.40 spent
    became 24.80 booked, across a stale `other` row that the default categories pane does
    not even show, and `u` could not help because an add pushes no pre-image. That is the
    one question the Money tab exists to answer, answered wrong.
    """
    app = make_app()
    async with app.run_test() as pilot:
        await go_money(pilot, app)
        await pilot.press("e")
        await type_into(pilot, "12.40 lunch")
        await pilot.press("enter")
        await pilot.pause()
        app.prompt.value = "12.40 lunch !restaurant"
        await pilot.press("enter")
        await pilot.pause()
        assert app.prompt.is_open is False, "the re-prompt looped"
    rows = all_expenses(db)
    assert [r["category"] for r in rows] == ["restaurant"], (
        f"the refile booked a second expense: {[(r['amount'], r['category']) for r in rows]}"
    )
    assert sum(r["amount"] for r in rows) == 12.40, "the month total double-counts the lunch"


async def test_a_refiled_expense_is_undoable(make_app, db, type_into):
    """The refile is an edit now, so it rides the undo stack like every other edit — which
    an INSERT never did, leaving the duplicate unreachable by `u`."""
    app = make_app()
    async with app.run_test() as pilot:
        await go_money(pilot, app)
        await pilot.press("e")
        await type_into(pilot, "12.40 lunch")
        await pilot.press("enter")
        await pilot.pause()
        app.prompt.value = "12.40 lunch !restaurant"
        await pilot.press("enter")
        await pilot.pause()
        assert [r["category"] for r in all_expenses(db)] == ["restaurant"]
        await pilot.press("u")
        await pilot.pause()
    rows = all_expenses(db)
    assert [r["category"] for r in rows] == ["other"], "undo did not put the row back"
    assert len(rows) == 1, "undo left a duplicate behind"


async def test_escaping_the_fix_category_prompt_leaves_the_row_filed_under_other(
    make_app, db, type_into
):
    """Record now, classify later is the point of the fallback: escaping must keep the row,
    not roll the write back. `cancel_editing` clears the armed slot, so the next `e` is a
    fresh entry rather than an edit of this row."""
    app = make_app()
    async with app.run_test() as pilot:
        await go_money(pilot, app)
        await pilot.press("e")
        await type_into(pilot, "12.40 lunch")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert [r["category"] for r in all_expenses(db)] == ["other"]
        await pilot.press("e")
        await type_into(pilot, "5.00 coffee !restaurant")
        await pilot.press("enter")
        await pilot.pause()
    rows = all_expenses(db)
    assert sorted(r["category"] for r in rows) == ["other", "restaurant"], (
        f"the abandoned refile swallowed the next entry: "
        f"{[(r['amount'], r['category']) for r in rows]}"
    )


async def test_e_rejects_a_missing_amount_without_writing(make_app, db, type_into):
    app = make_app()
    async with app.run_test() as pilot:
        await go_money(pilot, app)
        await pilot.press("e")
        await type_into(pilot, "lunch")
        await pilot.press("enter")
        await pilot.pause()
        assert app.is_running is True
    assert all_expenses(db) == []


async def test_refund_is_accepted_as_a_negative_amount(make_app, db):
    app = make_app()
    async with app.run_test() as pilot:
        await go_money(pilot, app)
        await pilot.press("e")
        app.prompt.value = "-24.99 returned shoes !entertainment"
        await pilot.press("enter")
        await pilot.pause()
    assert all_expenses(db)[0]["amount"] == -24.99


async def test_b_sets_a_budget_line(make_app, db, type_into):
    app = make_app()
    async with app.run_test() as pilot:
        await go_money(pilot, app)
        await pilot.press("b")
        await type_into(pilot, "500 !grocery")
        await pilot.press("enter")
        await pilot.pause()
        month = app.today()[:7]
    rows = list_budget(db, month=month)
    assert len(rows) == 1
    assert (rows[0]["amount"], rows[0]["category"], rows[0]["name"]) == (
        500.0,
        "grocery",
        "Grocery",
    )


async def test_b_rejects_an_unknown_category(make_app, db, type_into):
    app = make_app()
    async with app.run_test() as pilot:
        await go_money(pilot, app)
        await pilot.press("b")
        await type_into(pilot, "500 nonsense")
        await pilot.press("enter")
        await pilot.pause()
        month = app.today()[:7]
        assert app.is_running is True
    assert list_budget(db, month=month) == []


async def test_s_adds_a_recurring_item_and_shows_that_pane(make_app, db, type_into):
    app = make_app()
    async with app.run_test() as pilot:
        money_tab = await go_money(pilot, app)
        await pilot.press("s")
        await type_into(pilot, "20.99 streaming !subscriptions")
        await pilot.press("enter")
        await pilot.pause()
        assert money_tab.view.pane == "recurring"
    rows = list_recurring(db)
    assert len(rows) == 1
    assert (rows[0]["name"], rows[0]["monthly_cost"]) == ("streaming", 20.99)


async def test_annual_recurring_stores_monthly_equivalent(make_app, db):
    app = make_app()
    async with app.run_test() as pilot:
        await go_money(pilot, app)
        await pilot.press("s")
        app.prompt.value = "120 cloud storage !subscriptions #annually"
        await pilot.press("enter")
        await pilot.pause()
    row = list_recurring(db)[0]
    assert (row["cycle"], row["monthly_cost"]) == ("annually", 10.0)


async def test_r_rolls_recurring_into_the_viewed_month(make_app, db, type_into):
    app = make_app()
    async with app.run_test() as pilot:
        await go_money(pilot, app)
        await pilot.press("s")
        await type_into(pilot, "20.99 streaming !subscriptions")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        month = app.today()[:7]
    rows = list_budget(db, month=month)
    assert [r["source"] for r in rows] == ["recurring"]
    assert rows[0]["amount"] == 20.99


async def test_bracket_keys_move_the_month(make_app):
    app = make_app()
    async with app.run_test() as pilot:
        money_tab = await go_money(pilot, app)
        start = money_tab.view.anchor
        await pilot.press("left_square_bracket")
        assert money_tab.view.anchor < start
        await pilot.press("right_square_bracket")
        assert money_tab.view.anchor == start


async def test_month_step_crosses_a_year_boundary(make_app):
    app = make_app(now=lambda: dt.datetime(2026, 1, 15, 10, 0))
    async with app.run_test() as pilot:
        money_tab = await go_money(pilot, app)
        assert money_tab.view.anchor == "2026-01-15"
        await pilot.press("left_square_bracket")
        assert money_tab.view.anchor == "2025-12-15"


async def test_tab_cycles_sub_panes(make_app):
    app = make_app()
    async with app.run_test() as pilot:
        money_tab = await go_money(pilot, app)
        assert money_tab.view.pane == "categories"
        await pilot.press("tab")
        assert money_tab.view.pane == "expenses"
        await pilot.press("tab")
        assert money_tab.view.pane == "recurring"
        await pilot.press("tab")
        assert money_tab.view.pane == "categories"


async def test_x_deletes_the_selected_expense_and_u_restores_it(make_app, db):
    app = make_app()
    # `app.today()`, not `dt.date.today()`: the app's clock is pinned to
    # cfg.timezone while `dt.date.today()` follows the process TZ. The two straddle
    # midnight for part of every day, and then the seeded row falls outside the MTD
    # span, the expense pane is empty, and `x` has no row to delete. Reproduce with
    # `TZ=UTC pytest` any Toronto evening.
    today = app.today()
    add_expense(
        db, amount=12.40, description="lunch", category="restaurant", date=today
    )
    async with app.run_test() as pilot:
        await go_money(pilot, app)
        await pilot.press("tab")
        await pilot.pause()
        await pilot.press("x")
        await pilot.press("y")
        await pilot.pause()
        assert all_expenses(db) == []
        await pilot.press("u")
        await pilot.pause()
    rows = all_expenses(db)
    assert len(rows) == 1 and rows[0]["description"] == "lunch"


async def test_x_on_the_categories_pane_explains_itself(make_app, db):
    app = make_app()
    today = app.today()          # the app's clock, not the process TZ — see above
    add_expense(db, amount=1.0, description="x", category="grocery", date=today)
    async with app.run_test() as pilot:
        await go_money(pilot, app)
        await pilot.press("x")
        await pilot.pause()
        assert app.is_running is True
    assert len(all_expenses(db)) == 1


async def test_x_deletes_a_recurring_row(make_app, db, type_into):
    app = make_app()
    async with app.run_test() as pilot:
        await go_money(pilot, app)
        await pilot.press("s")
        await type_into(pilot, "20.99 streaming !subscriptions")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("x")
        await pilot.press("y")
        await pilot.pause()
    assert list_recurring(db) == []


async def test_header_shows_spend_against_budget(make_app, db):
    app = make_app()
    # Two date traps meet here, and the second one bit after the first was fixed.
    #
    # The month: on a month's last day after 20:00 Toronto, `dt.date.today()`
    # under `TZ=UTC` is already the 1st of the next month, so the rows land in a
    # month the app is not showing and the header reads `spent 0.00`. Hence
    # `app.today()` rather than the process clock.
    #
    # The *day*: these tests used to seed `f"{month}-10"`, which is in the future
    # for the first nine days of any month — and the default horizon is
    # month-to-date, so the expense fell outside the window and the header read
    # `spent 0.00` again. It broke on 2026-09-02. Seeding on `app.today()` is
    # inside every month-to-date span by construction, so there is no day left to
    # be wrong about.
    month = app.today()[:7]
    upsert_budget(db, month=month, name="Grocery", category="grocery", amount=500)
    add_expense(
        db, amount=412.0, description="shop", category="grocery", date=app.today()
    )
    async with app.run_test() as pilot:
        money_tab = await go_money(pilot, app)
        money_tab.reload()
        await pilot.pause()
        head = str(app.query_one("#money-head").content)
    assert "412.00" in head and "500.00" in head
    assert "left" in head


async def test_header_says_over_when_past_budget(make_app, db):
    app = make_app()
    month = app.today()[:7]      # the app's clock, not the process TZ — see above
    upsert_budget(db, month=month, name="Restaurant", category="restaurant", amount=200)
    add_expense(
        db, amount=289.0, description="dinner", category="restaurant", date=app.today()
    )
    async with app.run_test() as pilot:
        money_tab = await go_money(pilot, app)
        money_tab.reload()
        await pilot.pause()
        head = str(app.query_one("#money-head").content)
    assert "over" in head


async def test_burn_bar_shows_percentage_and_calendar_progress(make_app, db):
    app = make_app()
    month = app.today()[:7]      # the app's clock, not the process TZ — see above
    upsert_budget(db, month=month, name="Grocery", category="grocery", amount=100)
    add_expense(
        db, amount=50.0, description="shop", category="grocery", date=app.today()
    )
    async with app.run_test() as pilot:
        money_tab = await go_money(pilot, app)
        money_tab.reload()
        await pilot.pause()
        bar = str(app.query_one("#money-bar").content)
    assert "50%" in bar
    assert "day " in bar
    assert "┃" in bar


async def test_over_budget_category_is_flagged_in_the_table(make_app, db):
    app = make_app()
    month = app.today()[:7]      # the app's clock, not the process TZ — see above
    upsert_budget(db, month=month, name="Restaurant", category="restaurant", amount=200)
    add_expense(
        db, amount=289.0, description="dinner", category="restaurant", date=app.today()
    )
    async with app.run_test() as pilot:
        money_tab = await go_money(pilot, app)
        money_tab.reload()
        await pilot.pause()
        table = app.query_one("#money-table")
        cells = [str(c) for c in table.get_row_at(0)]
    assert any("⚠" in c for c in cells)


async def test_empty_month_renders_without_crashing(make_app):
    app = make_app()
    async with app.run_test() as pilot:
        money_tab = await go_money(pilot, app)
        money_tab.reload()
        await pilot.pause()
        assert "0.00" in str(app.query_one("#money-head").content)


# ── panels ──────────────────────────────────────────────────────────────────


def _panel(app, panel_id):
    from textual.widgets import Static

    content = app.query_one(panel_id, Static).content
    return content if isinstance(content, str) else content.plain


async def test_panels_show_a_net_negative_category_with_its_amount(make_app, db):
    """A reimbursed bill lands as a negative expense, so a category can net below
    zero. Filtering those rows out of the panels made WHERE IT WENT add up to gross
    spend while the header showed the net, with nothing on screen explaining it."""
    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    add_expense(db, amount=300.0, description="gas", category="transport", date="2026-08-04")
    add_expense(db, amount=40.0, description="dentist", category="other", date="2026-08-05")
    add_expense(
        db, amount=-240.0, description="dentist refund", category="other", date="2026-08-06"
    )
    app = make_app(now=now)
    async with app.run_test(size=(110, 34)) as pilot:
        await go_money(pilot, app)
        share = _panel(app, "#share-body")
        budget = _panel(app, "#budget-body")
    assert "-200.00" in share
    assert "-200.00" in budget
    # Share is of gross spend, so the one positive category owns all of it.
    assert "100.0%" in share
    # And the negative row gets no share rather than a nonsense negative percent.
    assert "-200.0%" not in share


async def test_panels_shares_total_one_hundred_percent_alongside_a_refund(make_app, db):
    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    add_expense(db, amount=300.0, description="gas", category="transport", date="2026-08-04")
    add_expense(db, amount=100.0, description="food", category="grocery", date="2026-08-04")
    add_expense(db, amount=-200.0, description="refund", category="other", date="2026-08-06")
    app = make_app(now=now)
    async with app.run_test(size=(110, 34)) as pilot:
        await go_money(pilot, app)
        share = _panel(app, "#share-body")
    assert "75.0%" in share
    assert "25.0%" in share


async def test_budget_panel_rows_are_uniform_width_with_a_refund_present(make_app, db):
    """The unclamped bar emitted 58 cells for a 14-cell bar and pushed the amounts
    off the panel."""
    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    upsert_budget(db, month="2026-08", name="other", category="other", amount=200.0)
    add_expense(db, amount=40.0, description="dentist", category="other", date="2026-08-05")
    add_expense(
        db, amount=-240.0, description="dentist refund", category="other", date="2026-08-06"
    )
    add_expense(db, amount=300.0, description="gas", category="transport", date="2026-08-04")
    app = make_app(now=now)
    async with app.run_test(size=(110, 34)) as pilot:
        await go_money(pilot, app)
        lines = _panel(app, "#budget-body").splitlines()
    assert len(lines) == 2
    assert len({len(line) for line in lines}) == 1
    assert "-200.00" in "\n".join(lines)


# ── empty-budget month, and good/bad colour ─────────────────────────────────


async def test_no_budget_month_names_the_fix_instead_of_showing_zero(make_app, db):
    """"0.00 budget / 1,234.00 over" is arithmetically true and reads as stale
    data. The real state is "nobody rolled this month yet"."""
    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    add_expense(db, amount=300.0, description="gas", category="transport", date="2026-08-04")
    upsert_recurring(db, name="Rent", category="housing", cost=750, cycle="monthly")
    upsert_recurring(db, name="Phone", category="utilities", cost=60, cycle="monthly")
    app = make_app(now=now)
    async with app.run_test(size=(110, 34)) as pilot:
        await go_money(pilot, app)
        head = str(app.query_one("#money-head").content)
        bar_shown = app.query_one("#money-bar").display
    assert "r rolls 2 recurring" in head
    assert "810.00" in head
    assert "0.00 budget" not in head
    assert "over" not in head
    assert bar_shown is False


async def test_no_budget_and_no_recurring_points_at_b(make_app, db):
    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    add_expense(db, amount=300.0, description="gas", category="transport", date="2026-08-04")
    app = make_app(now=now)
    async with app.run_test(size=(110, 34)) as pilot:
        await go_money(pilot, app)
        head = str(app.query_one("#money-head").content)
    assert "press b" in head


async def test_burn_bar_returns_once_a_budget_exists(make_app, db):
    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    add_expense(db, amount=300.0, description="gas", category="transport", date="2026-08-04")
    upsert_budget(db, month="2026-08", name="Gas", category="transport", amount=500)
    app = make_app(now=now)
    async with app.run_test(size=(110, 34)) as pilot:
        await go_money(pilot, app)
        head = str(app.query_one("#money-head").content)
        # Read inside the context: after run_test() exits the widget is torn down
        # and `display` reports False regardless of what was rendered.
        bar_shown = app.query_one("#money-bar").display
    assert bar_shown is True
    assert "200.00 left" in head


async def test_over_budget_is_coloured_bad_and_under_budget_good(make_app, db):
    from daylogs.tui.widgets import BAD, GOOD

    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    upsert_budget(db, month="2026-08", name="Gas", category="transport", amount=100)
    add_expense(db, amount=300.0, description="gas", category="transport", date="2026-08-04")
    app = make_app(now=now)
    async with app.run_test(size=(110, 34)) as pilot:
        await go_money(pilot, app)
        head = str(app.query_one("#money-head").content)
        panel = _panel(app, "#budget-body")
    assert BAD in head
    assert GOOD not in head
    assert BAD in panel


async def test_a_refunded_category_is_not_coloured_as_an_overrun(make_app, db):
    """A negative spend is under every cap; painting it red would be nonsense."""
    from daylogs.tui.widgets import BAD

    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    upsert_budget(db, month="2026-08", name="Other", category="other", amount=100)
    add_expense(db, amount=-200.0, description="refund", category="other", date="2026-08-06")
    app = make_app(now=now)
    async with app.run_test(size=(110, 34)) as pilot:
        await go_money(pilot, app)
        panel = _panel(app, "#budget-body")
    assert BAD not in panel


# ── editing a row with enter ─────────────────────────────────────────────────


async def test_enter_on_a_category_still_drills_in(make_app, db):
    """enter acts on what's under the cursor, so the two behaviours it already had
    must survive the addition of a third."""
    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    add_expense(db, amount=12.0, description="lunch", category="restaurant", date="2026-08-04")
    app = make_app(now=now)
    async with app.run_test(size=(120, 34)) as pilot:
        money_tab = await go_money(pilot, app)
        await pilot.press("enter")
        await pilot.pause()
        pane, filtered, prompt_open = (
            money_tab.view.pane,
            money_tab.view.filter_category,
            app.prompt.is_open,
        )
    assert pane == "expenses"
    assert filtered == "restaurant"
    assert prompt_open is False, "a category must not open an edit prompt"


async def test_enter_on_a_group_header_still_folds_it(make_app, db):
    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    add_expense(db, amount=12.0, description="lunch", category="restaurant", date="2026-08-04")
    app = make_app(now=now)
    async with app.run_test(size=(120, 34)) as pilot:
        money_tab = await go_money(pilot, app)
        await pilot.press("tab")            # -> expenses pane
        await pilot.press("G")              # group it
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        collapsed, prompt_open = money_tab.view.collapsed, app.prompt.is_open
    assert "restaurant" in collapsed
    assert prompt_open is False, "a group header must not open an edit prompt"


async def test_enter_on_an_expense_row_edits_it_in_place(make_app, db, type_into):
    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    add_expense(db, amount=12.0, description="lunch", category="restaurant", date="2026-08-04")
    original = all_expenses(db)[0]["id"]
    app = make_app(now=now)
    async with app.run_test(size=(120, 34)) as pilot:
        await go_money(pilot, app)
        await pilot.press("tab")            # -> expenses pane
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.prompt.label == "expense"
        assert "lunch" in app.prompt.value
        app.prompt.value = ""
        await type_into(pilot, "14.50 lunch out !grocery @2026-08-04")
        await pilot.press("enter")
        await pilot.pause()
    rows = all_expenses(db)
    assert len(rows) == 1, "an edit must not insert a second row"
    assert rows[0]["id"] == original
    assert (rows[0]["amount"], rows[0]["description"], rows[0]["category"]) == (
        14.50,
        "lunch out",
        "grocery",
    )
    assert rows[0]["date"] == "2026-08-04"


async def test_editing_an_expense_preserves_created_at(make_app, db, type_into):
    """Implementing edit as delete+re-add would reset created_at and issue a new id."""
    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    add_expense(db, amount=12.0, description="lunch", category="restaurant", date="2026-08-04")
    before = db.execute("SELECT created_at FROM expense").fetchone()["created_at"]
    app = make_app(now=now)
    async with app.run_test(size=(120, 34)) as pilot:
        await go_money(pilot, app)
        await pilot.press("tab")
        await pilot.press("enter")
        await pilot.pause()
        app.prompt.value = ""
        await type_into(pilot, "14.50")
        await pilot.press("enter")
        await pilot.pause()
    assert db.execute("SELECT created_at FROM expense").fetchone()["created_at"] == before


async def test_undoing_an_expense_edit_restores_it(make_app, db, type_into):
    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    add_expense(db, amount=12.0, description="lunch", category="restaurant", date="2026-08-04")
    app = make_app(now=now)
    async with app.run_test(size=(120, 34)) as pilot:
        await go_money(pilot, app)
        await pilot.press("tab")
        await pilot.press("enter")
        await pilot.pause()
        app.prompt.value = ""
        await type_into(pilot, "99.99 oops !housing @2026-08-04")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("u")
        await pilot.pause()
    rows = all_expenses(db)
    assert len(rows) == 1
    assert (rows[0]["amount"], rows[0]["description"], rows[0]["category"]) == (
        12.0,
        "lunch",
        "restaurant",
    )


async def test_enter_on_a_recurring_row_renames_without_duplicating(make_app, db, type_into):
    """The reason update_recurring exists: the `s` prompt upserts by name, so a
    renamed edit through that path INSERTs a second active row."""
    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    upsert_recurring(db, name="streaming", category="subscriptions", cost=20, cycle="monthly")
    app = make_app(now=now)
    async with app.run_test(size=(120, 34)) as pilot:
        await go_money(pilot, app)
        await pilot.press("tab")
        await pilot.press("tab")            # -> recurring pane
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.prompt.label == "recurring"
        app.prompt.value = ""
        await type_into(pilot, "24.99 streaming plus !subscriptions #monthly")
        await pilot.press("enter")
        await pilot.pause()
    rows = list_recurring(db)
    assert len(rows) == 1, "a rename must not insert a second row"
    assert rows[0]["name"] == "streaming plus"
    assert rows[0]["monthly_cost"] == 24.99, "the derived column is recomputed"


async def test_a_colliding_recurring_rename_is_rejected_cleanly(make_app, db, type_into):
    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    upsert_recurring(db, name="Rent", category="housing", cost=750, cycle="monthly")
    upsert_recurring(db, name="Gym", category="other", cost=40, cycle="monthly")
    app = make_app(now=now)
    async with app.run_test(size=(120, 34)) as pilot:
        await go_money(pilot, app)
        await pilot.press("tab")
        await pilot.press("tab")
        await pilot.pause()
        # The pane sorts by cost, so Rent (750) is row 0 — step onto Gym, whose
        # rename genuinely collides.
        await pilot.press("down")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert "Gym" in app.prompt.value, "precondition: editing Gym, not Rent"
        app.prompt.value = ""
        await type_into(pilot, "40 Rent !other #monthly")
        await pilot.press("enter")
        await pilot.pause()
        still_open = app.prompt.is_open
    assert still_open is True, "the prompt keeps the text so the name can be fixed"
    assert len(list_recurring(db)) == 2
async def test_a_failed_undo_does_not_let_the_next_u_reach_past_it(make_app, db, type_into):
    """A failed undo is a no-op, not a consumed one.

    `app_undo` popped before the `try`, so an exception dropped the pre-image on the floor:
    the row it described stayed unrestored *and* the entry was gone, so the next `u`
    silently undid something older — here, a deleted lunch coming back to life under a
    toast saying "restored expense row", with nothing connecting it to the rename the user
    had actually asked to undo.

    The upsert is `ON CONFLICT(id)`, which resolves the only conflict the stack itself can
    cause: the row being present rather than gone. It does not resolve a conflict on
    another unique index, and `recurring.name` is one — rename an item, let a new item take
    the freed name. Resolving that one too (`INSERT OR REPLACE`) would delete the new
    subscription to make room, so the honest answer is that this undo cannot be applied,
    which is only honest if the entry beneath it is still there afterwards.

    Asserted by pressing `u` twice and watching the older row, not by reading the stack's
    depth — the same reason test_undo.py drains instead of measuring `len`.
    """
    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    upsert_recurring(db, name="streaming", category="subscriptions", cost=20, cycle="monthly")
    add_expense(db, amount=12.0, description="lunch", category="restaurant", date="2026-08-04")
    said = []
    app = make_app(now=now)
    async with app.run_test(size=(120, 34)) as pilot:
        await go_money(pilot, app)
        await pilot.press("tab")             # -> expenses pane
        await pilot.pause()
        await pilot.press("x")               # delete the lunch: the older undo entry
        await pilot.press("y")
        await pilot.pause()
        assert all_expenses(db) == [], "precondition: the lunch is deleted and on the stack"
        await pilot.press("tab")             # -> recurring pane
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        app.prompt.value = ""
        await type_into(pilot, "20 streaming plus !subscriptions #monthly")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("s")               # a new item takes the freed name
        await pilot.pause()
        app.prompt.value = ""
        await type_into(pilot, "9.99 streaming !subscriptions #monthly")
        await pilot.press("enter")
        await pilot.pause()
        assert len(list_recurring(db)) == 2, "precondition: the freed name is taken again"

        app.notify = lambda msg, **kw: said.append(str(msg))
        await pilot.press("u")               # cannot be applied
        await pilot.pause()
        await pilot.press("u")               # must hit the same wall, not the lunch
        await pilot.pause()
    assert len(said) == 2, f"expected two toasts, one per u: {said}"
    assert all("undo failed" in m for m in said), f"the second u undid something else: {said}"
    assert all_expenses(db) == [], "the second u reached past the failed entry and restored"


async def test_the_pre_image_a_failed_undo_kept_still_applies(make_app, db, type_into):
    """Keeping the entry has to mean keeping it *usable*: once the name is free again, the
    same `u` restores the row it was always about. Pushing back something that no longer
    round-trips would satisfy the test above and still lose the row."""
    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    upsert_recurring(db, name="streaming", category="subscriptions", cost=20, cycle="monthly")
    app = make_app(now=now)
    async with app.run_test(size=(120, 34)) as pilot:
        await go_money(pilot, app)
        await pilot.press("tab")
        await pilot.press("tab")             # -> recurring pane
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        app.prompt.value = ""
        await type_into(pilot, "20 streaming plus !subscriptions #monthly")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        app.prompt.value = ""
        await type_into(pilot, "9.99 streaming !subscriptions #monthly")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("u")               # fails: the name is taken
        await pilot.pause()
        intruder = next(r["id"] for r in list_recurring(db) if r["cost"] == 9.99)
        delete_recurring(db, intruder)       # the collision is the user's to resolve
        await pilot.press("u")
        await pilot.pause()
    rows = list_recurring(db)
    assert len(rows) == 1
    assert (rows[0]["name"], rows[0]["cost"]) == ("streaming", 20.0), (
        f"the kept pre-image no longer restores the row: {[dict(r) for r in rows]}"
    )


async def test_escaping_an_expense_edit_does_not_corrupt_next_entry(make_app, db, type_into):
    """If user arms an expense edit, presses escape, then submits a fresh entry,
    that fresh entry must INSERT, not UPDATE the abandoned row.

    The clock is pinned and the prompt is asserted open, because the expense table
    filters by month, so an unpinned `now` leaves it empty from Sept 1, `enter`
    arms nothing, and the test passes with the fix removed.
    """
    import datetime as dt

    add_expense(db, amount=12.0, description="original", category="restaurant", date="2026-08-28")
    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    app = make_app(now=now)
    async with app.run_test(size=(120, 34)) as pilot:
        await go_money(pilot, app)
        await pilot.press("tab")
        await pilot.press("enter")
        await pilot.pause()
        assert_armed(app, "money")
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("e")
        await type_into(pilot, "25 fresh !grocery")
        await pilot.press("enter")
        await pilot.pause()
    rows = all_expenses(db)
    assert len(rows) == 2, "must have two expense rows"
    assert any(r["description"] == "original" and r["amount"] == 12.0 for r in rows)
    assert any(r["description"] == "fresh" and r["amount"] == 25.0 for r in rows)


async def test_escaping_a_recurring_edit_does_not_corrupt_next_entry(make_app, db, type_into):
    """If user arms a recurring edit, presses escape, then submits a fresh entry,
    that fresh entry must INSERT, not UPDATE the abandoned row."""
    upsert_recurring(db, name="Original", cost=20, cycle="monthly", category="subscriptions")
    app = make_app()
    async with app.run_test(size=(120, 34)) as pilot:
        await go_money(pilot, app)
        await pilot.press("tab")
        await pilot.press("tab")
        await pilot.press("enter")
        await pilot.pause()
        assert_armed(app, "money")
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("s")
        await type_into(pilot, "10 Fresh !other")
        await pilot.press("enter")
        await pilot.pause()
    rows = list_recurring(db)
    assert len(rows) == 2, "must have two recurring rows"
    assert any(r["name"] == "Original" and r["cost"] == 20 for r in rows)
    assert any(r["name"] == "Fresh" and r["cost"] == 10 for r in rows)


async def test_empty_submit_on_expense_edit_does_not_corrupt_next_entry(make_app, db, type_into):
    """If user arms an expense edit, clears the line, submits empty, then submits a
    fresh entry, that fresh entry must INSERT, not UPDATE the abandoned row.

    The clock is pinned and the prompt asserted open: the Money table defaults to
    MTD, so on an unpinned clock this row falls outside the window from September
    and `enter` would arm nothing, passing every assertion below for free.
    """
    add_expense(db, amount=12.0, description="original", category="restaurant", date="2026-08-28")
    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    app = make_app(now=now)
    async with app.run_test(size=(120, 34)) as pilot:
        await go_money(pilot, app)
        await pilot.press("tab")
        await pilot.press("enter")
        await pilot.pause()
        assert_armed(app, "money")
        app.prompt.value = ""
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("e")
        await type_into(pilot, "25 fresh !grocery")
        await pilot.press("enter")
        await pilot.pause()
    rows = all_expenses(db)
    assert len(rows) == 2, "must have two expense rows"
    assert any(r["description"] == "original" and r["amount"] == 12.0 for r in rows)
    assert any(r["description"] == "fresh" and r["amount"] == 25.0 for r in rows)


async def test_empty_submit_on_recurring_edit_does_not_corrupt_next_entry(make_app, db, type_into):
    """If user arms a recurring edit, clears the line, submits empty, then submits a
    fresh entry, that fresh entry must INSERT, not UPDATE the abandoned row."""
    upsert_recurring(db, name="Original", cost=20, cycle="monthly", category="subscriptions")
    app = make_app()
    async with app.run_test(size=(120, 34)) as pilot:
        await go_money(pilot, app)
        await pilot.press("tab")
        await pilot.press("tab")
        await pilot.press("enter")
        await pilot.pause()
        assert_armed(app, "money")
        app.prompt.value = ""
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("s")
        await type_into(pilot, "10 Fresh !other")
        await pilot.press("enter")
        await pilot.pause()
    rows = list_recurring(db)
    assert len(rows) == 2, "must have two recurring rows"
    assert any(r["name"] == "Original" and r["cost"] == 20 for r in rows)
    assert any(r["name"] == "Fresh" and r["cost"] == 10 for r in rows)


async def test_editing_an_expense_can_clear_its_note(make_app, db, type_into):
    """A line that omits ~note must clear the column.

    The clock is pinned because the expense table filters by month, so an unpinned
    `now` leaves it empty from Sept 1 and the test fails outright.
    """
    import datetime as dt

    add_expense(db, amount=12.0, description="lunch", category="restaurant",
                date="2026-08-28", note="receipt in wallet")
    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    app = make_app(now=now)
    async with app.run_test(size=(120, 34)) as pilot:
        await go_money(pilot, app)
        await pilot.press("tab")
        await pilot.press("enter")
        await pilot.pause()
        assert_armed(app, "money")
        app.prompt.value = ""
        await type_into(pilot, "12.00 lunch !restaurant @2026-08-28")
        await pilot.press("enter")
        await pilot.pause()
    rows = all_expenses(db)
    assert len(rows) == 1
    # `is None`, not `== ""`. This asserted the empty string and so froze a second
    # representation of "no note" as expected behaviour — the same shape CLAUDE.md records
    # for `test_fixing_the_category_does_not_loop_forever`. The name was always right; a
    # cleared note is indistinguishable from one never set, which is what `add_expense` has
    # always produced and what `update_expense` now produces too.
    assert rows[0]["note"] is None, "omitting ~note must clear the note"


async def test_editing_an_expense_with_unchanged_prefill_preserves_note(make_app, db, type_into):
    """Submitting the rendered prefill unchanged must keep the note.

    The clock is pinned and the prompt is asserted open, because the expense table
    filters by month, so an unpinned `now` leaves it empty from Sept 1, the prompt
    never opens, and the test becomes vacuous.
    """
    import datetime as dt

    add_expense(db, amount=12.0, description="lunch", category="restaurant",
                date="2026-08-28", note="receipt in wallet")
    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    app = make_app(now=now)
    async with app.run_test(size=(120, 34)) as pilot:
        await go_money(pilot, app)
        await pilot.press("tab")
        await pilot.press("enter")
        await pilot.pause()
        assert_armed(app, "money")
        # The prefill is "12.00 lunch !restaurant @2026-08-28 ~receipt in wallet". Submit unchanged.
        await pilot.press("enter")
        await pilot.pause()
    rows = all_expenses(db)
    assert len(rows) == 1
    assert rows[0]["note"] == "receipt in wallet"


async def test_month_to_date_header_holds_on_the_first_of_the_month(make_app, db):
    """The regression guard for the day-of-month trap, pinned to the worst case.

    Every other test here runs on the real clock, so none of them can fail on the
    day that matters: on the 1st, a month-to-date span is a single day wide, and
    anything seeded on a hardcoded later day sits in the future and is invisible.
    That is the shape that broke four tests on 2026-09-02, and it is the fifth
    date-relative failure in this repo, so it gets a test that does not depend on
    when it runs.
    """
    import datetime as dt
    from zoneinfo import ZoneInfo

    first = dt.datetime(2026, 9, 1, 9, 0, tzinfo=ZoneInfo("America/Toronto"))
    app = make_app(now=lambda: first)
    upsert_budget(db, month="2026-09", name="Grocery", category="grocery", amount=500)
    add_expense(db, amount=412.0, description="shop", category="grocery", date="2026-09-01")
    async with app.run_test() as pilot:
        money_tab = await go_money(pilot, app)
        money_tab.reload()
        await pilot.pause()
        head = str(app.query_one("#money-head").content)
    assert "412.00" in head, f"spend on the 1st vanished from the header: {head!r}"
    assert "500.00" in head


async def test_a_backdated_expense_lands_inside_the_visible_range(make_app, db, type_into):
    """Logging into a past day brings the window to the row, rather than only forward.

    `max(anchor, r.date)` left the anchor on Sept 5 for an Aug 30 expense, and all three
    surfaces then agreed on the wrong thing: the expenses pane listed only the September
    row, the header still read "spent 10.00" for the month, and the toast said
    "grocery 10.00 this range" about a category that had just been charged 40.00 more. The
    write had succeeded — the row was in the database with nothing on screen saying so,
    which is the one question this tab exists to answer, answered wrong.

    The clock is pinned because the whole test is about which month the anchor sits in. `t`
    is asserted last: the view moving to August is the point, so the one keypress home has
    to keep working, or the fix trades one lost row for a lost month.
    """
    now = lambda: dt.datetime(2026, 9, 5, 10, 0)  # noqa: E731
    add_expense(db, amount=10.0, description="milk", category="grocery", date="2026-09-02")
    said = []
    app = make_app(now=now)
    async with app.run_test(size=(120, 34)) as pilot:
        tab = await go_money(pilot, app)
        app.notify = lambda msg, **kw: said.append(str(msg))
        await pilot.press("tab")            # -> expenses pane
        await pilot.pause()
        await pilot.press("e")
        await type_into(pilot, "40.00 groceries !grocery @2026-08-30")
        await pilot.press("enter")
        await pilot.pause()
        table = app.query_one("#money-table")
        shown = [str(c) for key in table.rows for c in table.get_row(key)]
        assert "groceries" in shown, f"the row just written is not on screen: {shown}"
        head = str(app.query_one("#money-head").content)
        assert "spent 40.00" in head, f"the range total excludes the new row: {head}"
        assert "grocery 40.00" in said[-1], f"the toast quotes a stale total: {said[-1]}"
        await pilot.press("t")
        await pilot.pause()
        assert tab.view.anchor == "2026-09-05", "`t` must still come home to today"


# ── the note on screen ───────────────────────────────────────────────────
async def _expense_cells(pilot, app, *, grouped):
    tab = app.query_one("#money")
    while tab.view.pane != "expenses":
        await pilot.press("tab")
        await pilot.pause()
    tab.view.grouped = grouped
    tab.reload()
    await pilot.pause()
    t = app.query_one("#money-table")
    return [c for k in t.rows for c in t.get_row(k)]


@pytest.mark.parametrize("grouped", [False, True])
async def test_the_note_is_shown_beside_the_description(make_app, db, grouped):
    """48 of 234 real expenses carry a note naming which card paid, and the app displayed it
    nowhere — settable, exported, round-tripped through the edit prefill, invisible."""
    add_expense(db, amount=47.20, description="Costco Gas", category="transport",
                date="2026-09-14", note="CIBC MC")
    app = make_app()
    async with app.run_test(size=(120, 36)) as pilot:
        await go_money(pilot, app)
        cells = await _expense_cells(pilot, app, grouped=grouped)
    joined = " | ".join(str(c) for c in cells)
    assert "Costco Gas" in joined and "CIBC MC" in joined, joined


@pytest.mark.parametrize("grouped", [False, True])
async def test_the_note_is_dim_and_gapped_not_run_together(make_app, db, grouped):
    """Dim carries that it is secondary, and the gap carries it where dim does not render —
    colour is emphasis, never the only signal. Without the gap a monochrome terminal reads
    `Costco Gas CIBC MC` as one description."""
    add_expense(db, amount=47.20, description="Costco Gas", category="transport",
                date="2026-09-14", note="CIBC MC")
    app = make_app()
    async with app.run_test(size=(120, 36)) as pilot:
        await go_money(pilot, app)
        cells = await _expense_cells(pilot, app, grouped=grouped)
    cell = next(c for c in cells if hasattr(c, "spans") and "CIBC MC" in c.plain)
    i = cell.plain.index("CIBC MC")
    assert cell.plain[i - 2 : i] == "  ", f"no gap before the note: {cell.plain!r}"
    styled = [s for s in cell.spans if s.start <= i < s.end]
    assert styled and any("dim" in str(s.style) for s in styled), f"not dimmed: {cell.spans}"
    assert not cell.plain.startswith(" "), "the description itself must not shift"


@pytest.mark.parametrize("grouped", [False, True])
async def test_the_prepaid_marker_shows_in_both_pane_modes(make_app, db, grouped):
    """Grouped mode rendered a bare `description` and so dropped `#N` entirely — the marker
    the prorate invariant relies on to stop a 240.00 row reading as a contradiction of a
    header counting 20.00. One builder now serves both modes so they cannot drift again."""
    add_expense(db, amount=240.0, description="Insurance", category="subscriptions",
                date="2026-09-14", prepaid_months=12)
    app = make_app()
    async with app.run_test(size=(120, 36)) as pilot:
        await go_money(pilot, app)
        cells = await _expense_cells(pilot, app, grouped=grouped)
    joined = " | ".join(str(c) for c in cells)
    assert "#12" in joined, f"the prepaid marker is missing when grouped={grouped}: {joined}"


@pytest.mark.parametrize("grouped", [False, True])
async def test_a_row_with_no_note_gains_nothing(make_app, db, grouped):
    """Including the real database's one row whose note is an empty string rather than NULL:
    it must render as absent, not as a trailing gap."""
    add_expense(db, amount=80.61, description="Gas", category="transport", date="2026-09-14")
    db.execute("UPDATE expense SET note = '' WHERE description = 'Gas'")
    app = make_app()
    async with app.run_test(size=(120, 36)) as pilot:
        await go_money(pilot, app)
        cells = await _expense_cells(pilot, app, grouped=grouped)
    cell = next(c for c in cells if hasattr(c, "plain") and "Gas" in c.plain)
    assert cell.plain == "Gas", f"trailing whitespace or marker crept in: {cell.plain!r}"
