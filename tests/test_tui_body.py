import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from helpers import assert_armed, go_body, go_day

from daylogs.body import add_food, add_weight, list_food, list_weight
from daylogs.estimate import Estimate
from daylogs.fmt import hhmm, wall

WEIGHT_TZ = ZoneInfo("America/Toronto")

# The clock every weight-row test runs on, shared rather than repeated.
#
# Body's weight table is span-filtered by the tab's rolling 1m horizon, so a row seeded at
# a fixed date silently leaves the window once the real clock moves a month past it — and
# then `enter` selects nothing, no edit is armed, and the test passes while asserting
# nothing. Two of these tests carried a comment predicting the row would leave the window
# "on 2026-09-26". It did, and on 2026-09-28 the eight siblings that were never pinned all
# failed at once. This is the sixth date bomb in this repo; it is a constant now so the
# next person pins by using it rather than by rediscovering why.
WEIGHT_DAY = dt.datetime(2026, 8, 27, 9, 0, tzinfo=WEIGHT_TZ)


async def test_w_logs_a_weight(make_app, db, type_into):
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("w")
        await type_into(pilot, "78.2")
        await pilot.press("enter")
        await pilot.pause()
    rows = list_weight(db)
    assert len(rows) == 1 and rows[0]["kg"] == 78.2


async def test_w_with_a_bad_value_writes_nothing(make_app, db, type_into):
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("w")
        await type_into(pilot, "heavy")
        await pilot.press("enter")
        await pilot.pause()
        assert app.is_running is True
    assert list_weight(db) == []


async def test_weight_header_renders_the_reading(make_app, db, type_into):
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("w")
        await type_into(pilot, "78.2")
        await pilot.press("enter")
        await pilot.pause()
        head = str(app.query_one("#weight-head").content)
    assert "78.2 kg" in head


async def test_f_with_explicit_calories_does_not_call_claude(make_app, db, type_into):
    called = []

    async def runner_json(**kw):
        called.append(kw)
        return Estimate(description="x", kcal=1)

    app = make_app(runner_json=runner_json)
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("f")
        await type_into(pilot, "salad =610")
        await pilot.press("enter")
        await pilot.pause()
        today = app.today()
    rows = list_food(db, date=today)
    assert len(rows) == 1
    assert (rows[0]["kcal"], rows[0]["source"]) == (610, "labeled")
    assert called == []


async def test_f_without_calories_estimates_then_logs_as_estimated(
    make_app, db, type_into, monkeypatch
):
    async def fake_from_text(**kw):
        assert kw["description"] == "chicken caesar salad"
        return Estimate(description="chicken caesar salad", kcal=610)

    monkeypatch.setattr("daylogs.tui.body_tab.estimate.from_text", fake_from_text)
    # Pinned, because the confirm line now carries the `@` the row will be written to — it
    # has to, or a typed date is dropped between the two prompts — so the expected line
    # names a clock, and a clock read off the wall is not a fixture.
    app = make_app(now=lambda: dt.datetime(2026, 9, 5, 12, 0))
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("f")
        await type_into(pilot, "chicken caesar salad")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert app.prompt.label == "confirm food"
        assert app.prompt.value == "chicken caesar salad =610 @2026-09-05/12:00"
        await pilot.press("enter")
        await pilot.pause()
        today = app.today()
    rows = list_food(db, date=today)
    assert len(rows) == 1
    assert (rows[0]["kcal"], rows[0]["source"]) == (610, "estimated")


async def test_an_estimate_can_be_corrected_before_accepting(
    make_app, db, type_into, monkeypatch
):
    async def fake_from_text(**kw):
        return Estimate(description="salad", kcal=610)

    monkeypatch.setattr("daylogs.tui.body_tab.estimate.from_text", fake_from_text)
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("f")
        await type_into(pilot, "salad")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        app.prompt.value = "salad with chicken =780"
        await pilot.press("enter")
        await pilot.pause()
        today = app.today()
    row = list_food(db, date=today)[0]
    assert (row["description"], row["kcal"], row["source"]) == (
        "salad with chicken",
        780,
        "estimated",
    )


@pytest.mark.parametrize(
    "key,typed,table,stamp",
    [
        ("f", "pizza and salad @09-04/19:30", "food", "ate_at"),
        ("a", "gym 1h @09-04/19:30", "activity", "logged_at"),
    ],
)
async def test_a_backdated_line_keeps_its_date_through_the_estimate(
    make_app, db, type_into, monkeypatch, key, typed, table, stamp
):
    """A meal or a session you type with an `@` must land on the day you named.

    The confirm prompt is re-parsed through the same grammar that produced it — that is the
    whole point of offering a line rather than a yes/no — and the grammar resolves an absent
    `@` to *now*. So a prefill of `description =kcal` threw the typed date away between the
    two prompts: `f pizza and salad @09-04/19:30` was written to today at the current clock,
    and the toast then quoted a burn figure for the wrong day. Two days' net figures were
    wrong at once, since the meal was added to one and missing from the other, and nothing
    on screen contradicted either.

    The failure paths never had this: `_write_activity` writes the parsed row directly, so
    the same keypress dated the row correctly when Claude was down and silently moved it
    when Claude answered. That is what kept it hidden, and it is why both surfaces are
    parametrized here rather than one being trusted to stay in step with the other.
    """
    from daylogs.estimate import Effort

    async def fake_from_text(**kw):
        return Estimate(description="pizza and salad", kcal=800)

    async def fake_factor(**kw):
        return Effort(factor=1.45)

    monkeypatch.setattr("daylogs.tui.body_tab.estimate.from_text", fake_from_text)
    monkeypatch.setattr("daylogs.tui.body_tab.estimate.factor_from_text", fake_factor)
    app = make_app(now=lambda: dt.datetime(2026, 9, 5, 12, 0))
    async with app.run_test(size=(140, 40)) as pilot:
        await go_body(pilot, app)
        await pilot.press(key)
        await type_into(pilot, typed)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert app.prompt.label.startswith("confirm "), app.prompt.label
        offered = app.prompt.value
        await pilot.press("enter")
        await pilot.pause()
        tz = app.cfg.timezone
    rows = list(db.execute(f"SELECT * FROM {table}"))
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-09-04", (
        f"the row landed on {rows[0]['date']}, not the day that was typed"
    )
    assert hhmm(rows[0][stamp], tz) == "19:30", (
        f"the typed clock time was replaced by now's: {hhmm(rows[0][stamp], tz)}"
    )
    assert "@2026-09-04/19:30" in offered, (
        f"the confirm line does not say which day it will write to: {offered!r}"
    )


async def test_a_photo_estimate_still_lands_on_today(make_app, db, tmp_path, monkeypatch):
    """`_offer`'s `when` is optional for exactly this path.

    A photo carries no typed line, so there is nothing to preserve and the grammar's "an
    absent `@` means now" is the right answer. Pinned because the obvious fix for the typed
    path — have `_offer` always emit a date — would put a fabricated timestamp on a row
    nobody dated, and on a confirm line it reads as a claim about when you ate.
    """
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "meal.jpg").write_bytes(b"\xff\xd8\xff")
    monkeypatch.setattr("daylogs.tui.body_tab.photo.clipboard_image", lambda d: None)

    async def fake_from_image(**kw):
        return Estimate(description="ribeye + eggs", kcal=910)

    monkeypatch.setattr("daylogs.tui.body_tab.estimate.from_image", fake_from_image)
    app = make_app(now=lambda: dt.datetime(2026, 9, 5, 12, 0))
    async with app.run_test(size=(140, 40)) as pilot:
        await go_body(pilot, app)
        await pilot.press("p")
        await pilot.pause()
        await pilot.pause()
        offered = app.prompt.value
        await pilot.press("enter")
        await pilot.pause()
        today = app.today()
    assert "@" not in offered, f"the photo confirm line invented a date: {offered!r}"
    rows = list_food(db, date=today)
    assert len(rows) == 1 and rows[0]["kcal"] == 910


async def test_estimate_failure_surfaces_and_logs_nothing(
    make_app, db, type_into, monkeypatch
):
    from daylogs.claude import ClaudeError

    async def boom(**kw):
        raise ClaudeError("no claude")

    monkeypatch.setattr("daylogs.tui.body_tab.estimate.from_text", boom)
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("f")
        await type_into(pilot, "mystery meal")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert app.prompt.is_open is False
        today = app.today()
    assert list_food(db, date=today) == []


async def test_p_uses_the_inbox_when_the_clipboard_is_empty(
    make_app, db, make_cfg, tmp_path, monkeypatch
):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    img = inbox / "meal.jpg"
    img.write_bytes(b"\xff\xd8\xff")

    monkeypatch.setattr("daylogs.tui.body_tab.photo.clipboard_image", lambda d: None)

    async def fake_from_image(**kw):
        assert "meal.jpg" in str(kw["image_path"])
        return Estimate(description="ribeye + eggs", kcal=910)

    monkeypatch.setattr("daylogs.tui.body_tab.estimate.from_image", fake_from_image)

    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("p")
        await pilot.pause()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        today = app.today()
    rows = list_food(db, date=today)
    assert len(rows) == 1 and rows[0]["kcal"] == 910
    assert not img.exists()
    assert (inbox / "processed" / "meal.jpg").exists()


async def test_a_failed_photo_estimate_leaves_the_inbox_file_pending(
    make_app, db, tmp_path, monkeypatch
):
    from daylogs.claude import ClaudeError

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    img = inbox / "meal.jpg"
    img.write_bytes(b"\xff\xd8\xff")

    monkeypatch.setattr("daylogs.tui.body_tab.photo.clipboard_image", lambda d: None)

    async def boom(**kw):
        raise ClaudeError("down")

    monkeypatch.setattr("daylogs.tui.body_tab.estimate.from_image", boom)

    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("p")
        await pilot.pause()
        await pilot.pause()
    assert img.exists()
    assert not (inbox / "processed").exists() or not list((inbox / "processed").iterdir())


async def test_p_prefers_the_clipboard_over_the_inbox(make_app, db, tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "meal.jpg").write_bytes(b"x")
    clip = tmp_path / "clip.png"
    clip.write_bytes(b"\x89PNG")

    monkeypatch.setattr("daylogs.tui.body_tab.photo.clipboard_image", lambda d: clip)
    seen = {}

    async def fake_from_image(**kw):
        seen["path"] = str(kw["image_path"])
        return Estimate(description="clip meal", kcal=100)

    monkeypatch.setattr("daylogs.tui.body_tab.estimate.from_image", fake_from_image)

    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("p")
        await pilot.pause()
        await pilot.pause()
    assert "clip.png" in seen["path"]
    assert (inbox / "meal.jpg").exists()


async def test_p_with_nothing_available_opens_the_path_prompt(make_app, db, monkeypatch):
    monkeypatch.setattr("daylogs.tui.body_tab.photo.clipboard_image", lambda d: None)
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("p")
        await pilot.pause()
        assert app.prompt.label == "photo path"


async def test_inbox_line_shows_pending_count(make_app, tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.jpg").write_bytes(b"x")
    (inbox / "b.jpg").write_bytes(b"x")
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.pause()
        line = app.query_one("#inbox-line")
        assert line.display is True
        assert "2 photos" in str(line.content)


async def test_inbox_line_hidden_when_empty(make_app):
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.pause()
        assert app.query_one("#inbox-line").display is False


async def test_bracket_keys_move_the_viewing_date(make_app):
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.pause()
        start = app.query_one("#body").viewing_date
        await pilot.press("[")
        assert app.query_one("#body").viewing_date < start
        await pilot.press("]")
        assert app.query_one("#body").viewing_date == start


async def test_tab_toggles_the_table_mode(make_app):
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.pause()
        body = app.query_one("#body")
        assert body.table_mode == "food"
        # Three views now, so `tab` walks rather than toggles, and it must come all the
        # way back round rather than stopping at the end.
        await pilot.press("tab")
        assert body.table_mode == "activity"
        await pilot.press("tab")
        assert body.table_mode == "weight"
        await pilot.press("tab")
        assert body.table_mode == "food"


async def test_x_deletes_the_selected_food_row_and_u_restores_it(make_app, db):
    add_food(db, description="salad", kcal=610, source="labeled", date="2026-08-27", at=1)
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.pause()
        body = app.query_one("#body")
        body.viewing_date = "2026-08-27"
        body.reload()
        await pilot.pause()
        await pilot.press("x")
        await pilot.press("y")
        await pilot.pause()
        assert list_food(db, date="2026-08-27") == []
        await pilot.press("u")
        await pilot.pause()
    rows = list_food(db, date="2026-08-27")
    assert len(rows) == 1 and rows[0]["description"] == "salad"


async def test_x_then_n_cancels_the_delete(make_app, db):
    add_food(db, description="salad", kcal=610, source="labeled", date="2026-08-27", at=1)
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.pause()
        body = app.query_one("#body")
        body.viewing_date = "2026-08-27"
        body.reload()
        await pilot.pause()
        await pilot.press("x")
        await pilot.press("n")
        await pilot.pause()
    assert len(list_food(db, date="2026-08-27")) == 1


async def test_x_with_an_empty_table_does_not_crash(make_app, db):
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()
        assert app.is_running is True


async def test_x_deletes_a_weight_row_in_weight_mode(make_app, db):
    add_weight(db, kg=78.2, date="2026-08-27", at=1)
    app = make_app(now=lambda: WEIGHT_DAY)  # see WEIGHT_DAY
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.pause()
        await pilot.press("shift+tab")
        await pilot.pause()
        await pilot.press("x")
        await pilot.press("y")
        await pilot.pause()
    assert list_weight(db) == []


async def test_the_weight_series_renders_a_braille_chart(make_app, db):
    for day, kg in [("2026-08-25", 79.0), ("2026-08-26", 78.6), ("2026-08-27", 78.2)]:
        add_weight(db, kg=kg, date=day, at=int(day[-2:]))
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.pause()
        body = app.query_one("#body")
        body.viewing_date = "2026-08-27"
        body.reload()
        await pilot.pause()
        chart = str(app.query_one("#weight-chart").content)
    assert any(0x2800 <= ord(ch) <= 0x28FF for ch in chart)


async def test_food_header_omits_bmr_without_a_profile(make_app, db):
    add_food(db, description="salad", kcal=610, source="labeled", date="2026-08-27", at=1)
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.pause()
        body = app.query_one("#body")
        body.viewing_date = "2026-08-27"
        body.reload()
        await pilot.pause()
        head = str(app.query_one("#food-head").content)
    assert "610 kcal in" in head
    assert "BMR" not in head


async def test_the_days_net_is_shown_once_with_a_profile(make_app, make_cfg, db):
    add_weight(db, kg=80.0, date="2026-08-27", at=1)
    add_food(db, description="salad", kcal=610, source="labeled", date="2026-08-27", at=2)
    cfg = make_cfg(height_cm=180, sex="male", birthday="1996-08-27")
    app = make_app(cfg=cfg)
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.pause()
        body = app.query_one("#body")
        body.viewing_date = "2026-08-27"
        body.reload()
        await pilot.pause()
        panel = str(app.query_one("#energy-body").content)
        head = str(app.query_one("#food-head").content)
    # The day's balance lives in the ENERGY panel now. The FOOD header describes the
    # window of rows beneath it, which is a different question, and duplicating `burn`
    # across surfaces is what CLAUDE.md warns produces two baselines for one day.
    assert "BMR" in panel and "net" in panel
    assert "610" in panel, panel
    # "once" is the claim in this test's name, so it is the header that must NOT restate
    # the baseline. `"salad" not in head` was the first version of this and was vacuous:
    # no version of the FOOD header has ever printed a food description.
    assert "BMR" not in head, f"the header restates the day's baseline: {head!r}"
    assert "net" not in head, f"the header restates the day's net: {head!r}"


async def test_photo_path_prompt_rejects_a_non_image(make_app, tmp_path, type_into, monkeypatch):
    monkeypatch.setattr("daylogs.tui.body_tab.photo.clipboard_image", lambda d: None)
    bad = tmp_path / "notes.txt"
    bad.write_text("x")
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("p")
        await pilot.pause()
        app.prompt.value = str(bad)
        await pilot.press("enter")
        await pilot.pause()
        assert app.is_running is True


async def test_stale_weight_is_labelled_with_its_date(make_app, db):
    add_weight(db, kg=80.0, date="2026-07-07", at=1)
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.pause()
        body = app.query_one("#body")
        body.viewing_date = "2026-08-27"
        body.reload()
        await pilot.pause()
        head = str(app.query_one("#weight-head").content)
    assert "80 kg" in head
    assert "last weighed" in head and "Jul 07" in head


async def test_todays_weight_is_not_labelled_stale(make_app, db):
    add_weight(db, kg=78.2, date="2026-08-27", at=1)
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.pause()
        body = app.query_one("#body")
        body.viewing_date = "2026-08-27"
        body.reload()
        await pilot.pause()
        head = str(app.query_one("#weight-head").content)
    assert "last weighed" not in head


# ── profile ──────────────────────────────────────────────────────────────


def _energy(app) -> str:
    from textual.widgets import Static

    content = app.query_one("#energy-body", Static).content
    return content if isinstance(content, str) else content.plain


async def test_energy_panel_names_the_key_not_the_config_file(make_app):
    """Telling someone to edit config.toml is how this panel stayed empty: the
    instruction was correct and nobody was going to leave the app to follow it."""
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        text = _energy(app)
    assert "press h" in text
    assert "config.toml" not in text


async def test_h_sets_the_profile_and_bmr_appears_at_once(make_app, db, type_into, tmp_path):
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        await go_body(pilot, app)
        add_weight(db, kg=80.0, date="2026-08-28", at=1)
        await pilot.press("h")
        await type_into(pilot, "182 male 1995-06-15")
        await pilot.press("enter")
        await pilot.pause()
        text = _energy(app)
        cfg = app.cfg
    assert "BMR" in text
    # The BMR guidance is gone because height/sex/birthday are now set. The *activity*
    # guidance is still there and should be: `h` can set a level too, and this profile
    # line set none, so the day is still measured against resting BMR.
    assert "press h to set height" not in text, text
    # The guidance wraps onto a second line to fit a 46-column panel, so match the phrase
    # that stays on the first — not the whole sentence, which no width renders intact.
    assert "press h to set an" in text, text
    assert (cfg.height_cm, cfg.sex, cfg.birthday) == (182.0, "male", "1995-06-15")
    assert (tmp_path / "config.toml").exists()


async def test_profile_prompt_prefills_with_what_is_already_set(make_app, type_into):
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        await go_body(pilot, app)
        await pilot.press("h")
        await type_into(pilot, "182 male 1995-06-15")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("h")
        await pilot.pause()
        prefill = app.prompt.value
    assert "182" in prefill and "male" in prefill and "1995-06-15" in prefill


async def test_a_partial_profile_keeps_the_other_fields(make_app, type_into):
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        await go_body(pilot, app)
        await pilot.press("h")
        await type_into(pilot, "182 male 1995-06-15")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("h")
        app.prompt.value = ""          # clear the prefill, type only a height
        await type_into(pilot, "184")
        await pilot.press("enter")
        await pilot.pause()
        cfg = app.cfg
    assert cfg.height_cm == 184.0
    assert (cfg.sex, cfg.birthday) == ("male", "1995-06-15")


async def test_a_bad_profile_line_keeps_the_text_and_writes_nothing(make_app, type_into):
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        await go_body(pilot, app)
        await pilot.press("h")
        await type_into(pilot, "purple")
        await pilot.press("enter")
        await pilot.pause()
        still_open = app.prompt.is_open
        kept = app.prompt.value
        cfg = app.cfg
    assert still_open is True
    assert kept == "purple"
    assert cfg.height_cm is None


async def test_weight_deltas_are_coloured_by_direction(make_app, db):
    """Down is good — an assumption, since daylogs stores no goal weight. The arrow
    carries the direction either way, so colour is emphasis, not the only signal."""
    import datetime as dt

    from daylogs.tui.widgets import BAD, GOOD

    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    add_weight(db, kg=82.0, date="2026-08-21", at=1)
    add_weight(db, kg=80.0, date="2026-08-28", at=2)
    app = make_app(now=now)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        head = str(app.query_one("#weight-head").content)
    assert GOOD in head
    assert BAD not in head


# ── editing a row with enter ─────────────────────────────────────────────


async def test_enter_on_a_weight_row_opens_it_prefilled(make_app, db):
    """Body had no key_activate at all, so enter did nothing here."""
    add_weight(db, kg=78.2, date="2026-08-27", at=1, note="post-run")
    app = make_app(now=lambda: WEIGHT_DAY)  # see WEIGHT_DAY
    async with app.run_test(size=(120, 30)) as pilot:
        await go_body(pilot, app)
        # shift+tab, not tab: the strip is weight / food / activity and food is the
        # default, so `weight` is one step *back* along it.
        await pilot.press("shift+tab")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        label, value = app.prompt.label, app.prompt.value
    assert label == "weigh"
    assert "78.2" in value and "post-run" in value and "2026-08-27" in value


async def test_editing_a_weight_updates_in_place(make_app, db, type_into):
    add_weight(db, kg=78.2, date="2026-08-27", at=1, note="post-run")
    original = list_weight(db)[0]["id"]
    app = make_app(now=lambda: WEIGHT_DAY)  # see WEIGHT_DAY
    async with app.run_test(size=(120, 30)) as pilot:
        await go_body(pilot, app)
        await pilot.press("shift+tab")
        await pilot.press("enter")
        await pilot.pause()
        app.prompt.value = ""
        await type_into(pilot, "81.5")
        await pilot.press("enter")
        await pilot.pause()
    rows = list_weight(db)
    assert len(rows) == 1, "an edit must not insert a second row"
    assert rows[0]["id"] == original
    assert rows[0]["kg"] == 81.5
    assert rows[0]["note"] == "", "what's on the line is authoritative; no note = clear"


async def test_a_bare_number_moves_the_row_and_its_stamp_together(make_app, db, type_into):
    """The submitted line is authoritative, and the stamp has to agree with the date.

    A line with no `@` token resolves to *now*, so submitting a bare `81.5` says "81.5,
    taken now" and the row becomes today's. `update_weight` has always written `date=r.date`
    here — so the row already moved — while `measured_at` was left on the old day. That is
    the inconsistency: every query filters on `date`, and `morning_weight`/`latest_weight`
    then order by a stamp belonging to a different month. This asserts they move together.

    Clearing the prefill is how you reach this deliberately; keeping it is the ordinary
    path, and the test below pins that it preserves the stamp to the second.
    """
    add_weight(db, kg=78.2, date="2026-08-27", at=1787223943, note="")
    app = make_app(now=lambda: WEIGHT_DAY)  # see WEIGHT_DAY
    async with app.run_test(size=(120, 30)) as pilot:
        await go_body(pilot, app)
        await pilot.press("shift+tab")
        await pilot.press("enter")
        await pilot.pause()
        app.prompt.value = ""
        await type_into(pilot, "81.5")
        await pilot.press("enter")
        await pilot.pause()
        today = app.today()
    row = list_weight(db)[0]
    assert row["date"] == today, "the line said now, so the row is today's"
    assert wall(row["measured_at"], app.cfg.timezone).date().isoformat() == today, (
        f"the stamp stayed on the old day: {row['measured_at']}"
    )


async def test_submitting_the_prefill_unchanged_preserves_the_stamp_to_the_second(
    make_app, db
):
    """The reason `restamp` returns None at all. The stored stamp carries seconds the
    grammar cannot express, and for weight those seconds are the tie-breaker
    `weight_series_between` uses to pick a day's reading — so the ordinary edit, where you change a
    weight or a note and leave the time alone, must not touch them."""
    at = int(dt.datetime(2026, 9, 4, 7, 5, 43).timestamp())
    add_weight(db, kg=80.0, date="2026-09-04", at=at, note="post-run")
    app = make_app(now=lambda: dt.datetime(2026, 9, 4, 20, 0))
    async with app.run_test(size=(140, 40)) as pilot:
        tab = await _view(pilot, app, "weight")
        tab.horizon = "all"
        tab.reload()
        await pilot.pause()
        app.query_one("#body-table").focus()
        await pilot.press("enter")
        await pilot.pause()
        prefill = app.prompt.value
        app.prompt.value = prefill.replace("80", "80.4", 1)   # the weight only
        await pilot.press("enter")
        await pilot.pause()
    row = list_weight(db)[0]
    assert row["kg"] == 80.4
    assert row["measured_at"] == at, (
        f"the seconds were shaved off an edit that did not touch the time: "
        f"{row['measured_at']} != {at}"
    )


async def test_undoing_an_edit_restores_rather_than_duplicates(make_app, db, type_into):
    """The undo replay used to be a plain INSERT, which is right for a delete and
    wrong for an edit."""
    add_weight(db, kg=78.2, date="2026-08-27", at=1, note="post-run")
    app = make_app(now=lambda: WEIGHT_DAY)  # see WEIGHT_DAY
    async with app.run_test(size=(120, 30)) as pilot:
        await go_body(pilot, app)
        await pilot.press("shift+tab")
        await pilot.press("enter")
        await pilot.pause()
        app.prompt.value = ""
        await type_into(pilot, "99.9")
        await pilot.press("enter")
        await pilot.pause()
        assert list_weight(db)[0]["kg"] == 99.9
        await pilot.press("u")
        await pilot.pause()
    rows = list_weight(db)
    assert len(rows) == 1, "undo of an edit must not insert a second row"
    assert rows[0]["kg"] == 78.2
    assert rows[0]["note"] == "post-run"


async def test_a_bad_edit_keeps_the_text_and_changes_nothing(make_app, db, type_into):
    add_weight(db, kg=78.2, date="2026-08-27", at=1)
    app = make_app(now=lambda: WEIGHT_DAY)  # see WEIGHT_DAY
    async with app.run_test(size=(120, 30)) as pilot:
        await go_body(pilot, app)
        await pilot.press("shift+tab")
        await pilot.press("enter")
        await pilot.pause()
        app.prompt.value = ""
        await type_into(pilot, "heavy")
        await pilot.press("enter")
        await pilot.pause()
        still_open = app.prompt.is_open
        kept = app.prompt.value
    assert still_open is True
    assert kept == "heavy"
    assert list_weight(db)[0]["kg"] == 78.2


async def test_enter_on_a_food_row_edits_it_and_keeps_its_source(make_app, db, type_into):
    """`source` (labelled vs estimated) is provenance the digest reads; editing a
    description must not rewrite it."""
    import datetime as dt

    # `date` and `at` must agree, or this pins nothing about seconds: 1787223943 is
    # 2026-08-20 07:05:43, eight days from the `date` it was filed under — a row the app
    # cannot write, and one an edit now reconciles onto the stated date on purpose.
    at = int(dt.datetime(2026, 8, 28, 7, 5, 43).timestamp())
    add_food(db, description="oatmeal", kcal=350, date="2026-08-28", at=at,
             source="estimated")
    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    app = make_app(now=now)
    async with app.run_test(size=(120, 30)) as pilot:
        await go_body(pilot, app)
        await pilot.press("enter")
        await pilot.pause()
        assert app.prompt.label == "food"
        app.prompt.value = ""
        await type_into(pilot, "oatmeal with berries =400")
        await pilot.press("enter")
        await pilot.pause()
    rows = list_food(db, date="2026-08-28")
    assert len(rows) == 1
    assert rows[0]["description"] == "oatmeal with berries"
    assert rows[0]["kcal"] == 400
    assert rows[0]["source"] == "estimated"
    assert rows[0]["ate_at"] == at, "an unchanged minute keeps the seconds"


async def test_dropping_kcal_on_food_edit_is_rejected(make_app, db, type_into):
    """Dropping =kcal on an edit must be rejected, not silently written as 0.

    Kcal is the row's substance, not optional metadata. render_food always emits
    =kcal, so this only fires when someone deliberately deletes it. Silently
    writing 0 or keeping the old value are both worse than rejecting. Routing an
    edit into the Claude estimator is a deliberate follow-up, not this fix.
    """
    import datetime as dt

    add_food(db, description="oatmeal", kcal=350, date="2026-08-28", at=1, source="labeled")
    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    app = make_app(now=now)
    async with app.run_test(size=(120, 30)) as pilot:
        await go_body(pilot, app)
        await pilot.press("enter")
        await pilot.pause()
        app.prompt.value = ""
        await type_into(pilot, "oatmeal @2026-08-28")
        await pilot.press("enter")
        await pilot.pause()
        still_open = app.prompt.is_open
    assert still_open is True, "dropping =kcal must be rejected"
    rows = list_food(db, date="2026-08-28")
    assert len(rows) == 1
    assert rows[0]["kcal"] == 350, "the original kcal must remain unchanged"


async def test_enter_on_an_empty_table_does_not_crash(make_app):
    app = make_app()
    async with app.run_test(size=(120, 30)) as pilot:
        await go_body(pilot, app)
        await pilot.press("enter")
        await pilot.pause()
        assert app.prompt.is_open is False


async def test_a_rejected_edit_leaves_nothing_on_the_undo_stack(make_app, db, type_into):
    """The pre-image is pushed after the write, not before. Pushing first left an
    entry for `u` to apply to a row that never changed."""
    add_weight(db, kg=78.2, date="2026-08-27", at=1, note="post-run")
    app = make_app()
    async with app.run_test(size=(120, 30)) as pilot:
        await go_body(pilot, app)
        await pilot.press("shift+tab")
        await pilot.press("enter")
        await pilot.pause()
        app.prompt.value = ""
        await type_into(pilot, "heavy")          # rejected by the parser
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        popped = app.undo_stack.pop()
    assert popped is None
    assert list_weight(db)[0]["kg"] == 78.2
async def test_escaping_a_weight_edit_does_not_corrupt_next_entry(make_app, db, type_into):
    """If user arms a weight edit, presses escape, then submits a fresh entry,
    that fresh entry must INSERT, not UPDATE the abandoned row."""
    add_weight(db, kg=78.2, date="2026-08-27", at=1, note="original")
    app = make_app(now=lambda: WEIGHT_DAY)  # see WEIGHT_DAY
    async with app.run_test(size=(120, 30)) as pilot:
        await go_body(pilot, app)
        await pilot.press("shift+tab")
        await pilot.press("enter")
        await pilot.pause()
        assert_armed(app, "body")
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("w")
        await type_into(pilot, "80.1 fresh")
        await pilot.press("enter")
        await pilot.pause()
    rows = list_weight(db)
    assert len(rows) == 2, "must have two weight rows"
    assert any(r["kg"] == 78.2 and r["note"] == "original" for r in rows)
    assert any(r["kg"] == 80.1 and r["note"] == "fresh" for r in rows)


async def test_escaping_a_food_edit_does_not_corrupt_next_entry(make_app, db, type_into):
    """If user arms a food edit, presses escape, then submits a fresh entry,
    that fresh entry must INSERT, not UPDATE the abandoned row.

    The clock is pinned and the prompt is asserted open, because neither is
    optional here: the food table filters by the viewing date, so an unpinned
    `now` leaves it empty on any day but the seeded one, `enter` arms nothing,
    and every assertion below still passes with the fix removed.
    """
    import datetime as dt

    add_food(db, description="oatmeal", kcal=350, date="2026-08-28", at=1, source="labeled")
    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    app = make_app(now=now)
    async with app.run_test(size=(120, 30)) as pilot:
        await go_body(pilot, app)
        await pilot.press("enter")
        await pilot.pause()
        assert_armed(app, "body")
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("f")
        await type_into(pilot, "salad =600 @2026-08-28")
        await pilot.press("enter")
        await pilot.pause()
    rows = list_food(db, date="2026-08-28")
    assert len(rows) == 2, "must have two food rows"
    assert any(r["description"] == "oatmeal" and r["kcal"] == 350 for r in rows)
    assert any(r["description"] == "salad" and r["kcal"] == 600 for r in rows)


async def test_empty_submit_on_weight_edit_does_not_corrupt_next_entry(make_app, db, type_into):
    """If user arms a weight edit, clears the line, submits empty, then submits a
    fresh entry, that fresh entry must INSERT, not UPDATE the abandoned row."""
    add_weight(db, kg=78.2, date="2026-08-27", at=1, note="original")
    app = make_app(now=lambda: WEIGHT_DAY)  # see WEIGHT_DAY
    async with app.run_test(size=(120, 30)) as pilot:
        await go_body(pilot, app)
        await pilot.press("shift+tab")
        await pilot.press("enter")
        await pilot.pause()
        assert_armed(app, "body")
        app.prompt.value = ""
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("w")
        await type_into(pilot, "80.1 fresh")
        await pilot.press("enter")
        await pilot.pause()
    rows = list_weight(db)
    assert len(rows) == 2, "must have two weight rows"
    assert any(r["kg"] == 78.2 and r["note"] == "original" for r in rows)
    assert any(r["kg"] == 80.1 and r["note"] == "fresh" for r in rows)


async def test_empty_submit_on_food_edit_does_not_corrupt_next_entry(make_app, db, type_into):
    """If user arms a food edit, clears the line, submits empty, then submits a
    fresh entry, that fresh entry must INSERT, not UPDATE the abandoned row."""
    import datetime as dt

    add_food(db, description="oatmeal", kcal=350, date="2026-08-28", at=1, source="labeled")
    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    app = make_app(now=now)
    async with app.run_test(size=(120, 30)) as pilot:
        await go_body(pilot, app)
        await pilot.press("enter")
        await pilot.pause()
        assert_armed(app, "body")
        app.prompt.value = ""
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("f")
        await type_into(pilot, "salad =600 @2026-08-28")
        await pilot.press("enter")
        await pilot.pause()
    rows = list_food(db, date="2026-08-28")
    assert len(rows) == 2, "must have two food rows"
    assert any(r["description"] == "oatmeal" and r["kcal"] == 350 for r in rows)
    assert any(r["description"] == "salad" and r["kcal"] == 600 for r in rows)


async def test_a_parse_error_during_edit_keeps_editing_armed(make_app, db, type_into):
    """If an edit submission fails to parse, the retry must still update the same
    row, not insert a new one."""
    add_weight(db, kg=78.2, date="2026-08-27", at=1, note="original")
    app = make_app(now=lambda: WEIGHT_DAY)  # see WEIGHT_DAY
    async with app.run_test(size=(120, 30)) as pilot:
        await go_body(pilot, app)
        await pilot.press("shift+tab")
        await pilot.press("enter")
        await pilot.pause()
        app.prompt.value = ""
        await type_into(pilot, "heavy")
        await pilot.press("enter")
        await pilot.pause()
        # The prompt is still open with an error. Now submit a valid line.
        app.prompt.value = ""
        await type_into(pilot, "80.1 fixed")
        await pilot.press("enter")
        await pilot.pause()
    rows = list_weight(db)
    assert len(rows) == 1, "must have updated, not inserted"
    assert rows[0]["kg"] == 80.1 and rows[0]["note"] == "fixed"
async def test_editing_a_weight_can_clear_its_note(make_app, db, type_into):
    """A line that omits the note words must clear the column."""
    add_weight(db, kg=78.2, date="2026-08-27", at=1, note="post-run")
    app = make_app(now=lambda: WEIGHT_DAY)  # see WEIGHT_DAY
    async with app.run_test(size=(120, 30)) as pilot:
        await go_body(pilot, app)
        await pilot.press("shift+tab")
        await pilot.press("enter")
        await pilot.pause()
        app.prompt.value = ""
        await type_into(pilot, "78.2 @2026-08-27")
        await pilot.press("enter")
        await pilot.pause()
    rows = list_weight(db)
    assert len(rows) == 1
    assert rows[0]["note"] == "", "omitting note words must clear the note"


async def test_editing_a_weight_with_unchanged_prefill_preserves_note(make_app, db, type_into):
    """Submitting the rendered prefill unchanged must keep the note."""
    add_weight(db, kg=78.2, date="2026-08-27", at=1, note="post-run")
    app = make_app()
    async with app.run_test(size=(120, 30)) as pilot:
        await go_body(pilot, app)
        await pilot.press("shift+tab")
        await pilot.press("enter")
        await pilot.pause()
        # The prefill is "78.2 post-run @2026-08-27". Submit it unchanged.
        await pilot.press("enter")
        await pilot.pause()
    rows = list_weight(db)
    assert len(rows) == 1
    assert rows[0]["note"] == "post-run"


async def test_editing_a_food_with_escaped_at_preserves_timestamp(
    make_app, db, type_into
):
    r"""An escaped \@ inside the description is not a time token — ate_at must not change."""
    import datetime as dt

    # `date` and `at` agree, or the stamp moves for a different reason entirely: an edit now
    # settles a row whose two columns disagree onto the stated date, so an inconsistent
    # fixture would fail this test while the escaped-`@` rule it names worked perfectly.
    at = int(dt.datetime(2026, 8, 28, 7, 5, 43).timestamp())
    add_food(
        db, description="meeting", kcal=300, date="2026-08-28", at=at, source="labeled"
    )
    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    app = make_app(now=now)
    async with app.run_test(size=(120, 30)) as pilot:
        await go_body(pilot, app)
        await pilot.press("enter")
        await pilot.pause()
        app.prompt.value = ""
        await type_into(pilot, r"\@work meeting =300")
        await pilot.press("enter")
        await pilot.pause()
    rows = list_food(db, date="2026-08-28")
    assert len(rows) == 1
    assert rows[0]["description"] == "@work meeting", "the backslash is escape syntax, not content"
    assert rows[0]["ate_at"] == at, "escaped @ must not trigger a restamp"


# ── the estimate progress indicator ──────────────────────────────────────
#
# The estimate is one opaque subprocess call of up to `estimate_timeout_sec`
# (60 by default). The original feedback was a 3-second toast, so the remaining
# 57 seconds looked identical to a dropped keypress.
#
# The prompt is CLOSED for the whole wait (on_input_submitted closes on
# success), so the indicator cannot live in its border subtitle. It was a suffix
# on the FOOD header and in the footer's state row, which fixed the lifetime
# problem but not the location one: both live on the tab that started the work,
# so pressing `3` erased every trace of a running estimate and its answer
# arrived later as a prompt with no explanation. It is now the app-level popup,
# and these tests are the same tests pointed at it — every exit they cover
# (success, failure, timeout, a cancelled worker, the photo path) still has to
# leave nothing behind.


def _gate():
    """A runner that blocks until released, so a test can observe mid-flight."""
    import asyncio

    started, release = asyncio.Event(), asyncio.Event()

    async def runner(**kw):
        started.set()
        await release.wait()
        return Estimate(description="gated meal", kcal=500)

    return runner, started, release


def _food_head(app) -> str:
    from textual.widgets import Static

    return str(app.query_one("#food-head", Static).content)


def _popup(app) -> str:
    """The PAINTED popup, or "" when it is hidden.

    Deliberately the rendered widget and not the job dict behind it: the popup owns a
    timer and repaints itself, so asserting on the registry passes while the screen
    stays silent. Asserting on the method rather than the paint is how the first version
    of this shipped half-dead.
    """
    from daylogs.tui.progress import WorkPopup

    w = app.query_one(WorkPopup)
    return str(w.render()) if w.display else ""


async def test_the_popup_shows_estimating_while_the_call_is_in_flight(
    make_app, db, type_into, monkeypatch
):
    runner, started, release = _gate()

    async def gated_from_text(**kw):
        return await runner(**kw)

    monkeypatch.setattr("daylogs.tui.body_tab.estimate.from_text", gated_from_text)
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("f")
        await type_into(pilot, "mystery stew")
        await pilot.press("enter")
        await started.wait()
        await pilot.pause()
        mid, mid_head = _popup(app), _food_head(app)
        assert app.prompt.is_open is False, "the prompt should be closed during the wait"
        release.set()
        await pilot.pause()
        await pilot.pause()
        after = _popup(app)
    assert "estimating" in mid.lower(), f"no indicator mid-flight: {mid!r}"
    assert "60s" in mid, f"the popup must show the budget the call is allowed: {mid!r}"
    assert after == "", f"the popup outlived the call: {after!r}"
    # The other half of the move: the header is the figures again, and stays that way.
    assert "estimating" not in mid_head.lower(), (
        f"the FOOD header still carries the indicator: {mid_head!r}"
    )


async def test_the_footer_state_row_stays_the_tab_state_during_an_estimate(
    make_app, db, type_into, monkeypatch
):
    """Row 1 of the footer answers "what am I looking at" — which day, which horizon, which
    sort. It carried "estimating…" too, and that was one signal in two places, both of them
    on the tab that started the work. The popup is the signal; this stays the state.

    Asserted as the whole string rather than "estimating" being absent, because the absence
    alone would pass on a row that had stopped saying anything at all.
    """
    runner, started, release = _gate()
    monkeypatch.setattr("daylogs.tui.body_tab.estimate.from_text", runner)
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("f")
        await type_into(pilot, "mystery stew")
        await pilot.press("enter")
        await started.wait()
        await pilot.pause()
        mid = app.query_one("#body").status_hint()
        release.set()
        await pilot.pause()
        await pilot.pause()
        after = app.query_one("#body").status_hint()
    assert "estimating" not in mid.lower(), f"the state row duplicates the popup: {mid!r}"
    expected = "1m    [dim]sort[/] [bold]↓date[/] [dim]kcal[/]"
    assert mid == after == expected, (
        f"the row should be the tab's state and nothing else: {mid!r} / {after!r}"
    )


async def test_the_popup_is_hidden_again_and_not_merely_blank(
    make_app, db, type_into, monkeypatch
):
    """Asserted on the painted widget, and on `display` as well as the text.

    The predecessor of this test existed because `_set_estimating` had to call
    `app.refresh_footer()` itself — `reload()` only repaints BodyTab's own widgets — and
    a footer painted by any keypress mid-estimate would otherwise keep claiming an
    estimate was running long after it finished. The popup repaints itself, which is why
    that call is gone; what has to be checked instead is that it goes *away* rather than
    staying as an empty bordered box taking two rows off the screen forever.
    """

    runner, started, release = _gate()
    monkeypatch.setattr("daylogs.tui.body_tab.estimate.from_text", runner)
    app = make_app()
    async with app.run_test(size=(120, 34)) as pilot:
        await go_body(pilot, app)
        await pilot.press("f")
        await type_into(pilot, "mystery stew")
        await pilot.press("enter")
        await started.wait()
        await pilot.pause()
        from daylogs.tui.progress import WorkPopup

        during, shown = _popup(app), app.query_one(WorkPopup).display
        release.set()
        await pilot.pause()
        await pilot.pause()
        after, still_shown = _popup(app), app.query_one(WorkPopup).display
    assert "estimating" in during.lower(), f"painted popup silent mid-estimate: {during!r}"
    assert shown is True
    assert after == "", f"the painted popup still claims work is running: {after!r}"
    assert still_shown is False, "the popup stayed on screen as an empty box"


async def test_a_photo_estimate_does_not_move_the_selected_food_row(
    make_app, db, tmp_path, monkeypatch
):
    """Starting an estimate must not disturb what you were looking at.

    The indicator first repainted via reload(), which calls _fill_table, which does
    table.clear(columns=True) and resets the cursor to row 0.

    The PHOTO path is the one that proves it: `p` opens no prompt, so nothing else
    repaints the tab and the estimate machinery was the only thing touching the table.

    It caught a real one. The in-progress popup appears in the bottom container, which
    changes the tab's *height*, and `PanelTab.on_resize` rebuilt everything on any resize
    — including `_fill_table`, which clears the table and resets the cursor. `on_resize`
    now reloads only when the **width** changes, which is the reason it exists. That also
    fixes the `f` path, which had the same defect through the prompt and was recorded here
    as pre-existing; see the test below.
    """
    import datetime as dt

    from textual.widgets import DataTable

    for i in range(4):
        add_food(db, description=f"row{i}", kcal=100 + i, source="labeled",
                 date="2026-08-28", at=1787000000 + i * 3600)

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "meal.jpg").write_bytes(b"\xff\xd8\xff")
    monkeypatch.setattr("daylogs.tui.body_tab.photo.clipboard_image", lambda d: None)

    runner, started, release = _gate()
    monkeypatch.setattr("daylogs.tui.body_tab.estimate.from_image", runner)
    now = lambda: dt.datetime(2026, 8, 28, 9, 0)  # noqa: E731
    app = make_app(now=now)
    async with app.run_test(size=(120, 34)) as pilot:
        await go_body(pilot, app)
        await pilot.pause()
        table = app.query_one("#body-table", DataTable)
        table.focus()
        table.move_cursor(row=3)
        await pilot.pause()
        assert table.cursor_coordinate.row == 3, (
            "could not select row 3 — this test would prove nothing"
        )
        await pilot.press("p")
        await started.wait()
        await pilot.pause()
        during = table.cursor_coordinate.row
        release.set()
        await pilot.pause()
    assert during == 3, f"starting a photo estimate moved the cursor from row 3 to row {during}"


async def test_opening_a_prompt_no_longer_loses_the_selected_row(
    make_app, db, type_into
):
    """The `f` path's half of the same defect, fixed by the same change.

    Any prompt appearing changes the tab's height, so any prompt used to rebuild the
    table and put the cursor back on row 0. Harmless-looking, and the reason it stayed is
    that the edit path captures the row id before opening — but it means the row you were
    looking at is gone by the time you are typing about it.

    End-to-end, so it depends on Textual actually delivering the Resize before the
    assertion — which in this suite it often does not, and it passes with the guard removed
    for that reason. `test_a_height_change_does_not_rebuild_the_tab` is the tripwire; this
    is the user-visible statement of what the tripwire is for.
    """
    from textual.widgets import DataTable

    from daylogs.body import add_food

    for i in range(4):
        add_food(db, description=f"row{i}", kcal=100 + i, source="labeled",
                 date="2026-08-28", at=1787000000 + i * 3600)
    app = make_app(now=lambda: dt.datetime(2026, 8, 28, 9, 0))
    async with app.run_test(size=(120, 34)) as pilot:
        await go_body(pilot, app)
        await pilot.pause()
        table = app.query_one("#body-table", DataTable)
        table.focus()
        table.move_cursor(row=3)
        await pilot.pause()
        assert table.cursor_coordinate.row == 3, "this test would prove nothing"
        await pilot.press("f")
        await pilot.pause()
        assert app.prompt.is_open is True
        assert table.cursor_coordinate.row == 3, "opening the prompt threw the row away"


def _resize(tab, width: int, height: int) -> None:
    """Deliver a Resize to the tab directly.

    Deliberately not `pilot.resize_terminal` + `pause()`. That is how the defect this
    guards reached CI in the first place: `pause()` waits for the app to go idle, the
    suite shortens Textual's idle-wait granularity to 2 ms, and the Resize is often not
    processed before the assertion runs. So the end-to-end version passed locally with the
    guard *removed* and failed on CI, which is the worst behaviour a tripwire can have.

    The handler is the unit under test; delivering the event is Textual's business.
    """
    from textual.events import Resize
    from textual.geometry import Size

    size = Size(width, height)
    tab.on_resize(Resize(size, size))


def _chart_width(app) -> int:
    from textual.widgets import Static

    c = app.query_one("#weight-chart", Static).content
    c = c if isinstance(c, str) else c.plain
    return max((len(ln) for ln in c.splitlines()), default=0)


async def test_returning_to_a_tab_redraws_at_the_real_panel_width(make_app, db):
    """The trap the width guard walked into, and the reason it needs `_used_fallback`.

    `show_scope` calls `tab.reload()` while the pane it just switched to has not been laid
    out, so `panel_width` measures 0 and takes the 46-column `_FALLBACK`. The Resize that
    would correct it arrives at the *same tab width*, so a width-only guard drops it and
    the fallback build sticks: from the second visit onward every chart and bar drew at 46
    columns inside a 96-column panel, and only healed on the next key that reloaded.

    `refresh_tabs()` (from `u`, `h`, `n`) has the same shape for the two hidden tabs.
    """
    from daylogs.body import add_weight

    for i in range(8):
        add_weight(db, kg=78.0 + i * 0.2, date=f"2026-08-{21 + i:02d}", at=1787000000 + i * 86400)
    app = make_app(now=lambda: dt.datetime(2026, 8, 28, 9, 0))
    async with app.run_test(size=(200, 50)) as pilot:
        await go_body(pilot, app)
        await pilot.pause()
        first = _chart_width(app)
        assert first > 46, f"the panel must be wider than the fallback to prove anything: {first}"
        await pilot.press("1")
        await pilot.pause()
        await pilot.press("2")
        await pilot.pause()
        again = _chart_width(app)
    assert again == first, f"the return visit drew at {again} columns, the first at {first}"


async def test_the_return_visit_does_not_depend_on_a_resize_arriving(make_app, db, monkeypatch):
    """The property that made the first version of this flaky.

    Correcting a pre-layout build from the following `Resize` held locally and failed on CI,
    where the event is often not delivered before the assertion — so the same test passed on
    one PR and failed on the next with identical code. `show_scope` defers its reload with
    `call_after_refresh` instead, which is a Textual callback rather than a terminal event.
    Asserted with every Resize dropped, because "it works when the event arrives" is exactly
    what was not good enough.
    """
    from daylogs.body import add_weight
    from daylogs.tui.common import PanelTab

    for i in range(8):
        add_weight(db, kg=78.0 + i * 0.2, date=f"2026-08-{21 + i:02d}", at=1787000000 + i * 86400)
    monkeypatch.setattr(PanelTab, "on_resize", lambda self, event: None)
    app = make_app(now=lambda: dt.datetime(2026, 8, 28, 9, 0))
    async with app.run_test(size=(200, 50)) as pilot:
        await go_body(pilot, app)
        await pilot.pause()
        first = _chart_width(app)
        await pilot.press("1")
        await pilot.pause()
        await pilot.press("2")
        await pilot.pause()
        again = _chart_width(app)
    assert first > 46 and again == first, (
        f"without a Resize the return visit drew at {again}, the first at {first}"
    )


async def test_a_rebuild_that_had_to_guess_is_not_treated_as_settled(make_app, db):
    """Why `_used_fallback` is cleared *before* the reload, not after.

    A Resize delivered to a tab that is not on screen rebuilds it with panels that measure
    0, so that build guesses. Clearing the flag afterwards would throw away exactly that
    fact, the next Resize at the same width would be dropped, and the guessed build would
    latch — the bug this flag exists to prevent, one layer down. `refresh_tabs()` (`u`,
    `h`, `n`) reloads both off-screen tabs, so it is a reachable state and not a
    hypothetical.
    """
    app = make_app(now=lambda: dt.datetime(2026, 8, 28, 9, 0))
    async with app.run_test(size=(200, 50)) as pilot:
        await go_day(pilot, app)                     # Body is now off screen
        await pilot.pause()
        body_tab = app.query_one("#body")
        _resize(body_tab, 200, 40)
        await pilot.pause()
        assert body_tab._used_fallback is True, (
            "a build whose panels measured 0 was recorded as settled"
        )


async def test_a_height_change_does_not_rebuild_the_tab(make_app, db):
    """Rebuilding clears the table, so anything that changes only the tab's height must
    not: the prompt, the in-progress popup and the theme picker all live in the bottom
    container and all shorten the tab by appearing."""
    from textual.widgets import DataTable

    from daylogs.body import add_food

    for i in range(4):
        add_food(db, description=f"row{i}", kcal=100 + i, source="labeled",
                 date="2026-08-28", at=1787000000 + i * 3600)
    app = make_app(now=lambda: dt.datetime(2026, 8, 28, 9, 0))
    async with app.run_test(size=(120, 34)) as pilot:
        body_tab = await go_body(pilot, app)
        await pilot.pause()
        table = app.query_one("#body-table", DataTable)
        table.focus()
        table.move_cursor(row=3)
        await pilot.pause()
        assert table.cursor_coordinate.row == 3, "this test would prove nothing"
        _resize(body_tab, body_tab.size.width, body_tab.size.height - 3)
        await pilot.pause()
        assert table.cursor_coordinate.row == 3, "a height change threw the selected row away"


async def test_a_width_change_does_rebuild_the_tab(make_app, db):
    """The other direction: the guard must not turn the handler off. A panel's contents
    are built to its measured width, and contents built for a wider one wrap every row."""
    app = make_app(now=lambda: dt.datetime(2026, 8, 28, 9, 0))
    async with app.run_test(size=(120, 34)) as pilot:
        body_tab = await go_body(pilot, app)
        await pilot.pause()
        calls: list[int] = []
        body_tab.reload = lambda: calls.append(1)   # type: ignore[method-assign]
        _resize(body_tab, body_tab.size.width, body_tab.size.height - 3)
        assert calls == [], "a height change rebuilt the tab"
        _resize(body_tab, 80, body_tab.size.height - 3)
        assert calls == [1], "a width change no longer rebuilds the panels"
        _resize(body_tab, 80, body_tab.size.height - 6)
        assert calls == [1], "the same width rebuilt twice"


async def test_the_indicator_clears_when_the_estimate_fails(
    make_app, db, type_into, monkeypatch
):
    from daylogs.claude import ClaudeError

    async def boom(**kw):
        raise ClaudeError("no claude")

    monkeypatch.setattr("daylogs.tui.body_tab.estimate.from_text", boom)
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("f")
        await type_into(pilot, "mystery stew")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        shown = _popup(app)
    assert shown == "", f"the popup survived a failure: {shown!r}"


async def test_the_indicator_clears_when_the_estimate_times_out(
    make_app, db, type_into, monkeypatch
):
    """A timeout reaches the tab as ClaudeError from claude._run's wait_for, so it
    is the same exit as a failure — asserted separately because the issue names
    timeout as its own case."""
    from daylogs.claude import ClaudeError

    async def timed_out(**kw):
        raise ClaudeError("claude -p (json) timed out after 60s")

    monkeypatch.setattr("daylogs.tui.body_tab.estimate.from_text", timed_out)
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("f")
        await type_into(pilot, "mystery stew")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        shown = _popup(app)
    assert shown == "", f"the popup survived a timeout: {shown!r}"


async def test_a_second_estimate_cancels_the_first_without_clearing_the_indicator(
    make_app, db, type_into, monkeypatch
):
    """The race a boolean flag gets wrong.

    `@work(exclusive=True)` cancels the first worker when the second starts. With
    a bool, the cancelled worker's cleanup clears the flag while its replacement
    is still running, so the screen goes quiet mid-call. An in-flight counter
    goes 1 -> 2 -> 1 and stays truthy.
    """
    import asyncio

    started = asyncio.Event()
    release_second = asyncio.Event()
    calls = []

    async def two_stage(**kw):
        calls.append(kw)
        if len(calls) == 1:
            await asyncio.sleep(3600)          # first call: cancelled, never returns
        started.set()
        await release_second.wait()
        return Estimate(description="second", kcal=400)

    monkeypatch.setattr("daylogs.tui.body_tab.estimate.from_text", two_stage)
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("f")
        await type_into(pilot, "first stew")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("f")
        await type_into(pilot, "second stew")
        await pilot.press("enter")
        await started.wait()
        await pilot.pause()
        await pilot.pause()
        mid = _popup(app)
        release_second.set()
        await pilot.pause()
        await pilot.pause()
        after = _popup(app)
    assert len(calls) == 2, f"expected two estimate calls, got {len(calls)}"
    assert "estimating" in mid.lower(), (
        f"the cancelled worker cleared the indicator while the second was in flight: {mid!r}"
    )
    assert mid.count("estimating") == 1, (
        f"the popup is keyed by job, so a superseded estimate must not add a line: {mid!r}"
    )
    assert after == "", f"the popup outlived the second call: {after!r}"


async def test_the_photo_estimate_shows_the_same_indicator(
    make_app, db, tmp_path, monkeypatch
):

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "meal.jpg").write_bytes(b"\xff\xd8\xff")
    monkeypatch.setattr("daylogs.tui.body_tab.photo.clipboard_image", lambda d: None)

    runner, started, release = _gate()
    monkeypatch.setattr("daylogs.tui.body_tab.estimate.from_image", runner)
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("p")
        await started.wait()
        await pilot.pause()
        mid = _popup(app)
        release.set()
        await pilot.pause()
        await pilot.pause()
        after = _popup(app)
    assert "estimating" in mid.lower(), f"no indicator during a photo estimate: {mid!r}"
    assert after == "", f"the popup outlived the photo call: {after!r}"


async def test_no_three_second_toast_is_fired_for_an_estimate(
    make_app, db, type_into, monkeypatch
):
    """The defect itself: a toast whose lifetime is unrelated to the work. The
    indicator replaces it, so firing both would be redundant noise."""

    runner, started, release = _gate()
    toasts = []

    monkeypatch.setattr("daylogs.tui.body_tab.estimate.from_text", runner)
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        app.notify = lambda msg, **kw: toasts.append((msg, kw.get("timeout")))  # type: ignore[method-assign]
        await pilot.press("f")
        await type_into(pilot, "mystery stew")
        await pilot.press("enter")
        await started.wait()
        await pilot.pause()
        release.set()
        await pilot.pause()
    assert not [t for t in toasts if "estimat" in t[0].lower()], (
        f"an estimate toast was still fired: {toasts}"
    )


async def test_no_three_second_toast_is_fired_for_a_photo_estimate(
    make_app, db, tmp_path, monkeypatch
):
    """The photo path had its own copy of the defect ("estimating from photo…",
    also timeout=3). Pinned separately because intercepting only the text path
    leaves this one free to come back."""

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "meal.jpg").write_bytes(b"\xff\xd8\xff")
    monkeypatch.setattr("daylogs.tui.body_tab.photo.clipboard_image", lambda d: None)

    runner, started, release = _gate()
    toasts = []

    monkeypatch.setattr("daylogs.tui.body_tab.estimate.from_image", runner)
    app = make_app()
    async with app.run_test(size=(120, 34)) as pilot:
        await go_body(pilot, app)
        app.notify = lambda msg, **kw: toasts.append((msg, kw.get("timeout")))  # type: ignore[method-assign]
        await pilot.press("p")
        await started.wait()
        await pilot.pause()
        release.set()
        await pilot.pause()
    assert not [t for t in toasts if "estimat" in t[0].lower()], (
        f"a photo estimate toast was still fired: {toasts}"
    )


async def test_only_one_estimate_runs_at_a_time(
    make_app, db, tmp_path, type_into, monkeypatch
):
    """The invariant that makes a plain flag safe.

    Clearing the flag only on the two ending paths is correct *because* a cancelled
    estimate always has a replacement that sets the flag again. That holds only while
    BOTH workers sit in the same exclusive group — `@work`'s group defaults to
    "default", so a photo estimate and a text estimate cancel each other today. Give
    either its own group and the two could run at once, whereupon whichever finished
    first would clear the flag out from under the other and this design would need a
    count instead.

    So this fires a photo estimate and then a text one: same-method presses would
    cancel each other whatever the group is, and would prove nothing.
    """
    import asyncio

    live = 0
    peak = 0
    text_started = asyncio.Event()
    release = asyncio.Event()

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "meal.jpg").write_bytes(b"\xff\xd8\xff")
    monkeypatch.setattr("daylogs.tui.body_tab.photo.clipboard_image", lambda d: None)

    async def image_call(**kw):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        try:
            await asyncio.sleep(3600)          # expects to be cancelled by the text one
            return Estimate(description="photo", kcal=900)
        finally:
            live -= 1

    async def text_call(**kw):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        try:
            text_started.set()
            await release.wait()
            return Estimate(description="typed", kcal=100)
        finally:
            live -= 1

    monkeypatch.setattr("daylogs.tui.body_tab.estimate.from_image", image_call)
    monkeypatch.setattr("daylogs.tui.body_tab.estimate.from_text", text_call)

    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("p")                  # photo estimate starts, blocks
        await pilot.pause()
        await pilot.press("f")                  # text estimate should cancel it
        await type_into(pilot, "typed meal")
        await pilot.press("enter")
        await text_started.wait()
        await pilot.pause()
        release.set()
        await pilot.pause()
    assert peak == 1, (
        f"{peak} estimates were live at once — the photo and text workers no longer "
        "share an exclusive group, so a flag is not enough; use a count"
    )


# ── an abandoned photo must not be eaten by the next meal ─────────────────


def _inbox_state(tmp_path):
    inbox = tmp_path / "inbox"
    processed = inbox / "processed"
    return (
        sorted(p.name for p in inbox.glob("*.jpg")),
        sorted(p.name for p in processed.glob("*.jpg")) if processed.exists() else [],
    )


async def test_escaping_a_photo_confirm_does_not_let_the_next_meal_eat_the_photo(
    make_app, db, tmp_path, type_into, monkeypatch
):
    """`_pending_photo` is what says "this inbox file belongs to the row about to be
    written". Walking away from the confirm prompt has to drop it, or the NEXT
    confirmed estimate — a typed meal, nothing to do with the photo — moves the
    file into `processed/` and the photo is gone without ever being logged.
    """
    import asyncio  # noqa: F401

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "meal.jpg").write_bytes(b"\xff\xd8\xff")
    monkeypatch.setattr("daylogs.tui.body_tab.photo.clipboard_image", lambda d: None)
    monkeypatch.setattr(
        "daylogs.tui.body_tab.estimate.from_image",
        lambda **kw: _immediate(Estimate(description="ribeye", kcal=910)),
    )
    monkeypatch.setattr(
        "daylogs.tui.body_tab.estimate.from_text",
        lambda **kw: _immediate(Estimate(description="side salad", kcal=120)),
    )

    app = make_app()
    async with app.run_test(size=(120, 34)) as pilot:
        await go_body(pilot, app)
        await pilot.press("p")
        await pilot.pause()
        await pilot.pause()
        assert app.prompt.label == "confirm food", "the photo estimate never offered"
        await pilot.press("escape")             # changed my mind
        await pilot.pause()
        # Asserted as state, not as loss: the typed estimate below also releases the
        # photo, so with both clears in place the damage is unreachable and only the
        # invariant itself can be pinned here. An abandoned prompt carries nothing
        # forward — that is what keeps it unreachable when someone adds a new route
        # into `confirm food`.
        tab = app.query_one("#body")
        assert tab._pending_photo is None, "escaping the confirm prompt kept the photo armed"
        assert tab._pending is None, "escaping the confirm prompt kept a stale estimate armed"
        await pilot.press("f")                  # a completely unrelated meal
        await type_into(pilot, "side salad")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        await pilot.press("enter")              # accept the typed estimate
        await pilot.pause()
        today = app.today()
    pending, processed = _inbox_state(tmp_path)
    rows = [r["description"] for r in list_food(db, date=today)]
    assert rows == ["side salad"], f"unexpected food rows: {rows}"
    assert pending == ["meal.jpg"], f"the photo was consumed by an unrelated meal: {pending}"
    assert processed == [], f"the photo was moved to processed without being logged: {processed}"


async def test_a_superseded_photo_estimate_does_not_let_the_next_meal_eat_the_photo(
    make_app, db, tmp_path, type_into, monkeypatch
):
    """Same loss by the other route: the photo estimate is still running when a typed
    one supersedes it. The image worker is cancelled, so its `except` — the only
    place that clears `_pending_photo` on failure — never runs.
    """
    import asyncio

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "meal.jpg").write_bytes(b"\xff\xd8\xff")
    monkeypatch.setattr("daylogs.tui.body_tab.photo.clipboard_image", lambda d: None)

    async def never(**kw):
        await asyncio.sleep(3600)

    monkeypatch.setattr("daylogs.tui.body_tab.estimate.from_image", never)
    monkeypatch.setattr(
        "daylogs.tui.body_tab.estimate.from_text",
        lambda **kw: _immediate(Estimate(description="side salad", kcal=120)),
    )

    app = make_app()
    async with app.run_test(size=(120, 34)) as pilot:
        await go_body(pilot, app)
        await pilot.press("p")                  # photo estimate starts, hangs
        await pilot.pause()
        await pilot.press("f")                  # supersedes it
        await type_into(pilot, "side salad")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert app.prompt.label == "confirm food", "the typed estimate never offered"
        await pilot.press("enter")
        await pilot.pause()
        today = app.today()
    pending, processed = _inbox_state(tmp_path)
    rows = [r["description"] for r in list_food(db, date=today)]
    assert rows == ["side salad"], f"unexpected food rows: {rows}"
    assert pending == ["meal.jpg"], f"the photo was consumed by an unrelated meal: {pending}"
    assert processed == [], f"the photo was moved to processed without being logged: {processed}"


async def _immediate(value):
    return value


# ── which view am I on ───────────────────────────────────────────────────


def _view_row(app) -> str:
    from textual.widgets import Static

    return str(app.query_one("#body-views", Static).content)


async def test_the_table_header_names_the_view_it_is_showing(make_app, make_cfg, db):
    """The header used to say FOOD unconditionally, including while the table
    below it listed weigh-ins with `date | kg | note` columns and the header
    quoted the day's calorie total. A label that is wrong is worse than absent —
    it is the reason the weight view felt unlocated.
    """
    add_weight(db, kg=80.0, date="2026-08-27", at=1)
    add_food(db, description="salad", kcal=610, source="labeled", date="2026-08-27", at=2)
    cfg = make_cfg(height_cm=180, sex="male", birthday="1996-08-27")
    app = make_app(cfg=cfg)
    async with app.run_test() as pilot:
        body = await go_body(pilot, app)
        body.viewing_date = "2026-08-27"
        body.reload()
        await pilot.pause()
        assert body.table_mode == "food"
        assert "FOOD" in _food_head(app)

        # shift+tab: weight is one step back along weight / food / activity.
        await pilot.press("shift+tab")
        await pilot.pause()
        assert body.table_mode == "weight"
        head = _food_head(app)
    assert "WEIGHT" in head, f"the weight table is not labelled: {head!r}"
    assert "FOOD" not in head, f"still labelled FOOD while showing weigh-ins: {head!r}"
    assert "kcal" not in head, f"a calorie total over the weight table: {head!r}"
    assert "BMR" not in head


async def test_body_lists_every_view_with_the_active_one_marked(make_app, db):
    """Money has always drawn its panes as a visible row; Body had the same toggle and
    drew nothing, so `tab` was undiscoverable."""
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.pause()
        row = _view_row(app)
    for view in ("weight", "food", "activity"):
        assert view in row, f"{view} is missing from the strip: {row!r}"
    assert "[b]food[/b]" in row, f"the active view is not marked: {row!r}"
    assert "[b]weight[/b]" not in row


async def test_tab_moves_the_mark_along_the_strip(make_app, db):
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        row = _view_row(app)
    assert "[b]activity[/b]" in row, f"the mark did not follow the tab: {row!r}"
    assert "[b]food[/b]" not in row


# ── TDEE: net against a real maintenance figure ───────────────────────────
# `net` used to compare intake against *resting* expenditure, so a sedentary day
# read as a deficit it wasn't and a hard day understated one. These pin the
# substitution, and — just as important — pin that a profile with no activity level
# still reads exactly as it did before.

# 180 cm, male, born 1996-01-01, weighing 80 kg: Mifflin-St Jeor gives
# 10*80 + 6.25*180 - 5*30 + 5 = 1,780 resting, and 1,780 x 1.2 = 2,136 for a desk
# day. Spelled out so a wrong number is a wrong number, not a recomputed agreement.
_BMR = 1780
_DESK_BURN = 2136
_GYM_BURN = 2848  # x 1.6
DAY = "2026-09-03"


def _profile(make_cfg, **kw):
    return make_cfg(height_cm=180, sex="male", birthday="1996-01-01", **kw)


async def _energy_on(app, date=DAY):
    body = app.query_one("#body")
    body.viewing_date = date
    body.reload()
    return body


def _row(panel: str, label: str) -> str:
    """One labelled line of a panel.

    Asserted per line rather than against the whole blob because the value column is
    right-aligned inside a 7-wide field, so the minus sign is *detached* from its
    digits — `BMR      −  1,780`. A substring check for "−1,780" tests nothing that
    is on the screen.
    """
    for line in panel.splitlines():
        if line.strip().startswith(label):
            return line
    raise AssertionError(f"no {label!r} line in:\n{panel}")


def _has_no_row(panel: str, label: str) -> bool:
    return not any(line.strip().startswith(label) for line in panel.splitlines())


async def test_the_energy_panel_shows_burn_and_how_it_was_derived(
    make_app, make_cfg, db
):
    """The multiplier stays on screen deliberately. A factor rescales every calorie
    judgement for its day, so an inferred number with nothing to make you doubt it
    would quietly become the baseline for everything."""
    add_weight(db, kg=80.0, date=DAY, at=1)
    add_food(db, description="salad", kcal=1200, source="labeled", date=DAY, at=2)
    app = make_app(cfg=_profile(make_cfg, activity="desk"))
    async with app.run_test(size=(110, 34)) as pilot:
        await go_body(pilot, app)
        await _energy_on(app)
        await pilot.pause()
        text = _energy(app)
    assert f"{_BMR:,}" in _row(text, "BMR"), f"resting BMR is gone: {text!r}"
    activity = _row(text, "activity")
    assert "×1.2" in activity, f"the multiplier is not on screen: {activity!r}"
    assert "profile" in activity, f"nothing says where it came from: {activity!r}"
    burn = _row(text, "burn")
    assert "−" in burn and f"{_DESK_BURN:,}" in burn, f"burn line is wrong: {burn!r}"
    assert "-936" in _row(text, "net"), f"net is not 1,200 − 2,136: {text!r}"
    # The arithmetic has to read correctly top to bottom: BMR is a term of `burn`,
    # not a term of `net`, so it must not carry the minus sign any more.
    assert "−" not in _row(text, "BMR"), f"BMR is still subtracted from intake: {text!r}"


async def test_without_a_level_the_energy_panel_is_untouched(make_app, make_cfg, db):
    """Opt-in, all the way down: no level and nothing logged means `net` sits against
    resting BMR exactly as it did before any of this existed."""
    add_weight(db, kg=80.0, date=DAY, at=1)
    add_food(db, description="salad", kcal=1200, source="labeled", date=DAY, at=2)
    app = make_app(cfg=_profile(make_cfg))
    async with app.run_test(size=(110, 34)) as pilot:
        await go_body(pilot, app)
        await _energy_on(app)
        await pilot.pause()
        text = _energy(app)
    bmr = _row(text, "BMR")
    assert "−" in bmr and f"{_BMR:,}" in bmr, f"BMR is not the subtrahend: {bmr!r}"
    assert _has_no_row(text, "burn"), f"a burn line with no factor to build it: {text!r}"
    assert "-580" in _row(text, "net"), f"net is not 1,200 − 1,780: {text!r}"
    # There IS an activity row now, and it must never look like a factor. A dash and the
    # key that sets one — the same shape the BMR row uses when height is missing. This
    # assertion used to be `_has_no_row("activity")`, written when the only alternative
    # was a line implying a multiplier that had never been set.
    activity = _row(text, "activity")
    assert "—" in activity, f"the activity row should be a dash: {activity!r}"
    assert "press h" in activity, f"and should name the fix: {activity!r}"
    assert "×" not in activity, f"no factor was set, so none may be shown: {activity!r}"


async def test_a_logged_factor_says_it_was_logged(make_app, make_cfg, db):
    """"profile" and "logged" are different claims, and the second one is the one
    worth doubting."""
    from daylogs.body import add_activity

    add_weight(db, kg=80.0, date=DAY, at=1)
    add_food(db, description="salad", kcal=1200, source="labeled", date=DAY, at=2)
    add_activity(db, description="gym 1h", date=DAY, at=3, factor=1.6,
                 source="estimated")
    app = make_app(cfg=_profile(make_cfg, activity="desk"))
    async with app.run_test(size=(110, 34)) as pilot:
        await go_body(pilot, app)
        await _energy_on(app)
        await pilot.pause()
        text = _energy(app)
    activity = _row(text, "activity")
    assert "×1.6" in activity and "logged" in activity, f"origin: {activity!r}"
    assert "profile" not in activity, f"claims the baseline it superseded: {activity!r}"
    assert f"{_GYM_BURN:,}" in _row(text, "burn"), f"burn ignored the log: {text!r}"


async def test_percent_of_maintenance_is_against_burn_not_resting_bmr(
    make_app, make_cfg, db
):
    """Otherwise one panel measures the same day against two different baselines.
    2,136 kcal is exactly 100% of a desk day's burn and 120% of resting BMR, so the
    number itself says which one was used."""
    add_weight(db, kg=80.0, date=DAY, at=1)
    add_food(db, description="lots", kcal=_DESK_BURN, source="labeled", date=DAY, at=2)
    app = make_app(cfg=_profile(make_cfg, activity="desk"))
    async with app.run_test(size=(110, 34)) as pilot:
        await go_body(pilot, app)
        await _energy_on(app)
        await pilot.pause()
        text = _energy(app)
    assert "100% of maintenance" in text, f"not measured against burn: {text!r}"


async def test_the_horizon_average_net_is_per_day(make_app, make_cfg, db):
    """One gym session must not restate a whole window's average net. Measuring the
    window's average intake against *today's* burn would say −1,348 here, and
    against resting BMR −280; per day it is −992.
    """
    from daylogs.body import add_activity

    add_weight(db, kg=80.0, date="2026-09-02", at=1)
    add_food(db, description="a", kcal=2000, source="labeled", date="2026-09-02", at=2)
    add_food(db, description="b", kcal=1000, source="labeled", date=DAY, at=3)
    add_activity(db, description="gym", date=DAY, at=4, factor=1.6, source="estimated")
    app = make_app(cfg=_profile(make_cfg, activity="desk"))
    async with app.run_test(size=(110, 34)) as pilot:
        await go_body(pilot, app)
        await _energy_on(app)
        await pilot.pause()
        text = _energy(app)
    flat = text.replace("−", "-")
    assert "-992" in flat, f"average net is not the mean of per-day nets: {text!r}"
    assert "-1,348" not in flat and "-280" not in flat


async def test_the_day_is_measured_against_burn_not_resting_bmr(make_app, make_cfg, db):
    """"burn", not "BMR": with a factor the baseline is what the day actually cost, and
    naming it BMR would measure against resting expenditure while claiming otherwise.

    Asserted on the ENERGY panel. This lived in the FOOD header until that header became a
    description of the *window* of rows beneath it — a different question, and one surface
    fewer reading `burn`, which CLAUDE.md wants because each extra one is another chance
    for two panels to measure the same day against two baselines.
    """
    add_weight(db, kg=80.0, date=DAY, at=1)
    add_food(db, description="salad", kcal=1200, source="labeled", date=DAY, at=2)
    app = make_app(cfg=_profile(make_cfg, activity="desk"))
    async with app.run_test(size=(110, 34)) as pilot:
        await go_body(pilot, app)
        await _energy_on(app)
        await pilot.pause()
        panel = str(app.query_one("#energy-body").content)
        head = str(app.query_one("#food-head").content)
    flat = panel.replace("−", "-")
    assert f"{_DESK_BURN:,}" in flat, f"panel still on resting BMR: {panel!r}"
    assert "burn" in flat, panel
    assert "-936" in flat, f"net is wrong: {panel!r}"
    assert "burn" not in head, f"the header duplicates the day's baseline: {head!r}"


# ── BMI ──────────────────────────────────────────────────────────────────


async def test_the_weight_header_carries_bmi(make_app, make_cfg, db):
    """A bare number: no band and no colour. "overweight" is a judgement this app
    does not otherwise make."""
    add_weight(db, kg=81.0, date=DAY, at=1)
    app = make_app(cfg=make_cfg(height_cm=180))
    async with app.run_test(size=(110, 34)) as pilot:
        await go_body(pilot, app)
        await _energy_on(app)
        await pilot.pause()
        head = str(app.query_one("#weight-head").content)
    assert "BMI 25.0" in head, f"no BMI on the weight header: {head!r}"


async def test_the_weight_header_omits_bmi_without_a_height(make_app, db):
    add_weight(db, kg=81.0, date=DAY, at=1)
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        await go_body(pilot, app)
        await _energy_on(app)
        await pilot.pause()
        head = str(app.query_one("#weight-head").content)
    assert "BMI" not in head, f"BMI with no height to compute it from: {head!r}"


# ── the profile carries the level ─────────────────────────────────────────


async def test_h_sets_the_ordinary_day_level(make_app, db, type_into, tmp_path):
    add_weight(db, kg=80.0, date=DAY, at=1)
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        await go_body(pilot, app)
        await pilot.press("h")
        await type_into(pilot, "180 male 1996-01-01 desk")
        await pilot.press("enter")
        await pilot.pause()
        cfg = app.cfg
    assert cfg.activity == "desk", f"the level did not reach config: {cfg.activity!r}"
    assert "activity" in (tmp_path / "config.toml").read_text()


async def test_the_profile_prefill_carries_the_level(make_app, type_into):
    """What you can see is what you can edit — a prefill that drops a field silently
    clears it on the next submit."""
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        await go_body(pilot, app)
        await pilot.press("h")
        await type_into(pilot, "180 male 1996-01-01 heavy")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("h")
        await pilot.pause()
        prefill = app.prompt.value
    assert "heavy" in prefill, f"the level is missing from the prefill: {prefill!r}"


async def test_a_complete_profile_with_no_level_names_the_missing_level(
    make_app, db, type_into
):
    """An empty state names the fix. Resting BMR is not maintenance, and nothing
    else on screen would tell you a level is what turns one into the other."""
    add_weight(db, kg=80.0, date=DAY, at=1)
    app = make_app()
    said = []
    async with app.run_test(size=(110, 34)) as pilot:
        await go_body(pilot, app)
        app.notify = lambda msg, **kw: said.append(msg)
        await pilot.press("h")
        await type_into(pilot, "180 male 1996-01-01")
        await pilot.press("enter")
        await pilot.pause()
    assert said, "saving a profile said nothing at all"
    assert "desk" in said[-1], f"the level is not named as the next step: {said[-1]!r}"


# ── a: logging a day that departs from the baseline ───────────────────────


def _runner_factor(value, seen=None):
    async def runner(**kwargs):
        if seen is not None:
            seen.update(kwargs)
        return {"factor": value}
    return runner


def _act_rows(db, date=DAY):
    from daylogs.body import list_activity
    return list_activity(db, date=date)


async def test_a_opens_the_activity_prompt(make_app):
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("a")
        await pilot.pause()
        assert app.prompt.is_open and app.prompt.label == "activity"


async def test_a_typed_factor_is_written_without_asking_claude(make_app, db, type_into):
    """`=level` is the escape hatch and the no-LLM path, exactly as `=kcal` is for
    food. It must not spawn a call."""
    called = []

    async def runner(**kwargs):
        called.append(kwargs)
        return {"factor": 1.9}

    app = make_app(runner_json=runner)
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("a")
        await type_into(pilot, "gym 1h =active")
        await pilot.press("enter")
        await pilot.pause()
    rows = _act_rows(db, app.today())
    assert len(rows) == 1
    assert (rows[0]["description"], rows[0]["factor"]) == ("gym 1h", 1.55)
    assert rows[0]["source"] == "labeled", "a typed number is not an estimate"
    assert called == [], "asked Claude for a factor that was given"


async def test_a_without_a_factor_asks_claude_and_offers_the_answer(
    make_app, db, type_into
):
    app = make_app(runner_json=_runner_factor(1.45))
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("a")
        await type_into(pilot, "gym 1h")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        label, value = app.prompt.label, app.prompt.value
    assert label == "confirm activity", f"no review step: {label!r}"
    assert "gym 1h" in value and "=1.45" in value, f"prefill is wrong: {value!r}"
    assert _act_rows(db, app.today()) == [], "wrote the row before it was confirmed"


async def test_confirming_an_inferred_factor_writes_it(make_app, db, type_into):
    app = make_app(runner_json=_runner_factor(1.45))
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("a")
        await type_into(pilot, "gym 1h")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    rows = _act_rows(db, app.today())
    assert len(rows) == 1
    assert (rows[0]["factor"], rows[0]["source"]) == (1.45, "estimated")


async def test_a_confirm_line_with_the_factor_deleted_still_lands_the_estimate(
    make_app, db, type_into
):
    """The prefill always carries the number, so a submitted line without one means it
    was deleted — and falling back to the inferred value beats writing a NULL the user
    did not ask for. The same rule `confirm food` follows for a deleted `=kcal`."""
    app = make_app(runner_json=_runner_factor(1.45))
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("a")
        await type_into(pilot, "gym 1h")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        app.prompt.value = "gym 1h"          # the factor, deleted
        await pilot.press("enter")
        await pilot.pause()
    rows = _act_rows(db, app.today())
    assert len(rows) == 1
    assert rows[0]["factor"] == 1.45, f"the estimate was dropped: {rows[0]['factor']!r}"


async def test_the_confirm_line_can_be_corrected_before_it_lands(
    make_app, db, type_into
):
    app = make_app(runner_json=_runner_factor(1.45))
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("a")
        await type_into(pilot, "gym 1h")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        app.prompt.value = "gym 90m =1.7"
        await pilot.press("enter")
        await pilot.pause()
    rows = _act_rows(db, app.today())
    assert (rows[0]["description"], rows[0]["factor"]) == ("gym 90m", 1.7)


async def test_the_question_is_about_the_whole_day_not_the_newest_entry(
    make_app, db, type_into
):
    """A PAL is not additive, so two sessions and one session are different days. The
    prompt has to carry both, and the profile's ordinary day too."""
    import datetime as dt
    from zoneinfo import ZoneInfo

    from daylogs.body import add_activity

    add_activity(db, description="swim 2km", date=DAY, at=1, factor=1.5,
                 source="estimated")
    seen = {}
    app = make_app(
        runner_json=_runner_factor(1.6, seen),
        height_cm=180, sex="male", birthday="1996-01-01", activity="desk",
        now=lambda: dt.datetime(2026, 9, 3, 9, 0, tzinfo=ZoneInfo("America/Toronto")),
    )
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("a")
        await type_into(pilot, "gym 1h")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
    assert "swim 2km" in seen["user_prompt"], "the day's earlier activity was dropped"
    assert "gym 1h" in seen["user_prompt"]
    assert "desk" in seen["user_prompt"], "the baseline was not sent"


async def test_a_failed_inference_still_records_what_you_did(make_app, db, type_into):
    """The description is your data; the factor is a guess. Losing "gym 1h" because
    the CLI was missing would be the worse outcome, and a NULL factor is a state the
    schema and `resolved_factor` already handle — the day falls back to the baseline."""
    from daylogs.claude import ClaudeError

    async def runner(**kwargs):
        raise ClaudeError("no cli")

    app = make_app(runner_json=runner)
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("a")
        await type_into(pilot, "gym 1h")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
    rows = _act_rows(db, app.today())
    assert len(rows) == 1, "the activity was lost with the estimate"
    assert rows[0]["factor"] is None
    assert rows[0]["description"] == "gym 1h"


async def test_a_bad_activity_line_keeps_the_text_and_writes_nothing(
    make_app, db, type_into
):
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.press("a")
        await type_into(pilot, "gym =9")
        await pilot.press("enter")
        await pilot.pause()
        still_open, kept = app.prompt.is_open, app.prompt.value
    assert still_open is True
    assert kept == "gym =9"
    assert _act_rows(db, app.today()) == []


# ── the activity sub-view ────────────────────────────────────────────────


async def test_activity_is_a_third_sub_view(make_app, db):
    """Without it the table is invisible and its rows can only be superseded, never
    removed."""
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.pause()
        row = _view_row(app)
    assert "activity" in row, f"the view strip does not list it: {row!r}"


async def test_tab_and_shift_tab_walk_three_views_in_opposite_directions(make_app, db):
    """With two views direction was moot and `prev` was an alias for `next`. With
    three that alias would make shift+tab skip a pane."""
    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        await pilot.pause()
        body = app.query_one("#body")
        start = body.table_mode
        await pilot.press("tab")
        await pilot.pause()
        forward = body.table_mode
        await pilot.press("shift+tab")
        await pilot.pause()
        assert body.table_mode == start, "shift+tab did not undo tab"
        await pilot.press("shift+tab")
        await pilot.pause()
        assert body.table_mode != forward, "shift+tab walks the same way as tab"


async def test_the_activity_table_lists_the_days_rows(make_app, db):
    from daylogs.body import add_activity

    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        add_activity(db, description="gym 1h", date=app.today(), at=1, factor=1.45,
                     source="estimated")
        add_activity(db, description="long walk", date=app.today(), at=2, factor=None,
                     source="estimated")
        body = app.query_one("#body")
        body.table_mode = "activity"
        body.reload()
        await pilot.pause()
        table = app.query_one("#body-table")
        cells = [str(c) for r in range(table.row_count)
                 for c in table.get_row_at(r)]
    assert any("gym 1h" in c for c in cells)
    assert any("×1.45" in c for c in cells), f"the factor is not shown: {cells}"
    assert any(c == "—" for c in cells), f"a factorless row needs a dash: {cells}"


async def test_the_days_factor_and_its_origin_are_shown_once(
    make_app, make_cfg, db
):
    from daylogs.body import add_activity

    add_weight(db, kg=80.0, date=DAY, at=1)
    add_activity(db, description="gym", date=DAY, at=2, factor=1.6, source="estimated")
    app = make_app(cfg=_profile(make_cfg, activity="desk"))
    async with app.run_test(size=(110, 34)) as pilot:
        await go_body(pilot, app)
        body = app.query_one("#body")
        body.table_mode = "activity"
        body.viewing_date = DAY
        body.reload()
        await pilot.pause()
        panel = str(app.query_one("#energy-body").content)
        head = str(app.query_one("#food-head").content)
    assert head.startswith("ACTIVITY"), f"the header names the wrong view: {head!r}"
    # The factor, its origin and the burn it produces are in the ENERGY panel. They were
    # in this header too, which was one day's three facts above a table that now lists a
    # window — a label that is wrong is worse than one that is missing.
    assert "×1.6" in panel and "logged" in panel, panel
    assert f"{_GYM_BURN:,}" in panel.replace("−", "-"), f"the burn is missing: {panel!r}"


async def test_no_factor_names_the_fix_where_the_fallback_happens(make_app, make_cfg, db):
    """With no factor the day is measured against *resting* BMR, and the ENERGY panel
    showed that without a word about it — the guidance was in the ACTIVITY header, which
    now describes a window. It belongs beside the number it explains."""
    add_weight(db, kg=80.0, date=DAY, at=1)
    app = make_app(cfg=make_cfg(height_cm=180, sex="male", birthday="1996-08-27"))
    async with app.run_test(size=(110, 34)) as pilot:
        await go_body(pilot, app)
        body = app.query_one("#body")
        body.viewing_date = DAY
        body.reload()
        await pilot.pause()
        panel = str(app.query_one("#energy-body").content)
    assert "BMR" in panel, panel
    assert "press h" in panel, f"the resting-BMR fallback names no fix: {panel!r}"


async def test_enter_edits_an_activity_row(make_app, db, type_into):
    from daylogs.body import add_activity

    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        add_activity(db, description="gym", date=app.today(), at=1, factor=1.4,
                     source="estimated")
        body = app.query_one("#body")
        body.table_mode = "activity"
        body.reload()
        await pilot.pause()
        app.query_one("#body-table").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.prompt.label == "activity", f"wrong prompt: {app.prompt.label!r}"
        assert "gym" in app.prompt.value and "=1.4" in app.prompt.value
        app.prompt.value = "gym, upper body =1.6"
        await pilot.press("enter")
        await pilot.pause()
    rows = _act_rows(db, app.today())
    assert len(rows) == 1, "an edit inserted a second row"
    assert (rows[0]["description"], rows[0]["factor"]) == ("gym, upper body", 1.6)


async def test_editing_an_activity_does_not_ask_claude(make_app, db, type_into):
    """An edit with no `=` leaves the stored factor alone. Only the entry path infers,
    or fixing a typo would silently re-roll the number."""
    from daylogs.body import add_activity

    called = []

    async def runner(**kwargs):
        called.append(kwargs)
        return {"factor": 1.9}

    app = make_app(runner_json=runner)
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        add_activity(db, description="gym", date=app.today(), at=1, factor=1.4,
                     source="estimated")
        body = app.query_one("#body")
        body.table_mode = "activity"
        body.reload()
        await pilot.pause()
        app.query_one("#body-table").focus()
        await pilot.press("enter")
        await pilot.pause()
        app.prompt.value = "gym, upper body"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
    rows = _act_rows(db, app.today())
    assert called == [], "an edit triggered an inference"
    assert (rows[0]["description"], rows[0]["factor"]) == ("gym, upper body", 1.4)


async def test_x_deletes_an_activity_and_u_restores_it(make_app, db):
    from daylogs.body import add_activity

    app = make_app()
    async with app.run_test() as pilot:
        await go_body(pilot, app)
        add_activity(db, description="gym", date=app.today(), at=1, factor=1.4,
                     source="estimated")
        body = app.query_one("#body")
        body.table_mode = "activity"
        body.reload()
        await pilot.pause()
        app.query_one("#body-table").focus()
        await pilot.press("x")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert _act_rows(db, app.today()) == [], "delete did nothing"
        await pilot.press("u")
        await pilot.pause()
    rows = _act_rows(db, app.today())
    assert len(rows) == 1 and rows[0]["factor"] == 1.4


# ── the weight table follows the window ──────────────────────────────────
# It used to be `list_weight(limit=60)`: no span and no viewed date, so `[`/`]` and
# `+`/`-` moved the chart and the header while the table underneath sat still. The
# header said "60 most recent weigh-ins" precisely because claiming the span would
# have been a lie — which made the honest label a standing admission of the bug.


def _weight_rows(app):
    table = app.query_one("#body-table")
    return [str(table.get_row_at(r)[0]) for r in range(table.row_count)]


async def _weight_view(app, *, date, horizon="1m"):
    body = app.query_one("#body")
    body.table_mode = "weight"
    body.viewing_date = date
    body.horizon = horizon
    body.reload()
    return body


async def test_the_weight_table_lists_only_the_windows_weigh_ins(make_app, db):
    for day in ("2026-06-15", "2026-09-01", "2026-09-02"):
        add_weight(db, kg=80.0, date=day, at=int(day[-2:]))
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        await go_body(pilot, app)
        await _weight_view(app, date=DAY)
        await pilot.pause()
        rows = _weight_rows(app)
    assert rows == ["2026-09-02", "2026-09-01"], f"not the window's rows: {rows}"


async def test_a_weigh_in_after_the_window_is_excluded(make_app, db):
    """Viewing an older day must not list readings that had not happened yet — the
    same rule the chart and every figure on the tab follow."""
    for day in ("2026-07-05", "2026-09-02"):
        add_weight(db, kg=80.0, date=day, at=int(day[-2:]))
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        await go_body(pilot, app)
        await _weight_view(app, date="2026-07-10")
        await pilot.pause()
        rows = _weight_rows(app)
    assert rows == ["2026-07-05"], f"leaked a later reading: {rows}"


async def test_zooming_out_brings_more_weigh_ins_into_the_table(make_app, db):
    for day in ("2026-06-15", "2026-09-02"):
        add_weight(db, kg=80.0, date=day, at=int(day[-2:]))
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        await go_body(pilot, app)
        await _weight_view(app, date=DAY)
        await pilot.pause()
        assert _weight_rows(app) == ["2026-09-02"]
        await pilot.press("minus")          # 1m -> MTD ... keep going to 3m
        await pilot.press("minus")
        await pilot.press("minus")
        await pilot.pause()
        wider = _weight_rows(app)
    assert "2026-06-15" in wider, f"zooming out did not reach the table: {wider}"


async def test_stepping_the_period_moves_the_table(make_app, db):
    for day in ("2026-08-02", "2026-09-02"):
        add_weight(db, kg=80.0, date=day, at=int(day[-2:]))
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        await go_body(pilot, app)
        await _weight_view(app, date="2026-09-02")
        await pilot.pause()
        assert _weight_rows(app) == ["2026-09-02"]
        # One press, not thirty-one: `[` steps one whole horizon now, and the default is
        # `1m`. Pressing it a month at a time was the friction that made the old day-step
        # wrong for a tab where every table follows the window.
        await pilot.press("left_square_bracket")
        await pilot.pause()
        stepped = _weight_rows(app)
    assert stepped == ["2026-08-02"], f"the table did not follow `[`: {stepped}"


async def test_the_weight_header_names_the_window_and_the_count(make_app, db):
    for day in ("2026-09-01", "2026-09-02"):
        add_weight(db, kg=80.0, date=day, at=int(day[-2:]))
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        await go_body(pilot, app)
        body = await _weight_view(app, date=DAY)
        await pilot.pause()
        head = str(app.query_one("#food-head").content)
        label = body.span().label
    assert head.startswith("WEIGHT")
    assert label in head, f"the header does not name the window: {head!r}"
    assert "2 weigh-ins" in head, f"the header does not count the rows: {head!r}"
    assert "most recent" not in head, "still admitting it ignores the span"


async def test_one_weigh_in_is_not_pluralised(make_app, db):
    add_weight(db, kg=80.0, date="2026-09-02", at=1)
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        await go_body(pilot, app)
        await _weight_view(app, date=DAY)
        await pilot.pause()
        head = str(app.query_one("#food-head").content)
    assert "1 weigh-in" in head and "1 weigh-ins" not in head, f"{head!r}"


async def test_an_empty_window_names_the_fix(make_app, db):
    """A window with nothing in it is true and useless on its own; it has to say what
    would change it."""
    add_weight(db, kg=80.0, date="2026-01-05", at=1)
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        await go_body(pilot, app)
        await _weight_view(app, date=DAY)
        await pilot.pause()
        head = str(app.query_one("#food-head").content)
    assert "nothing logged" in head, f"{head!r}"
    assert "press w" in head, f"the empty state names no fix: {head!r}"


async def test_the_weight_table_is_newest_first(make_app, db):
    for day in ("2026-08-30", "2026-09-01", "2026-09-02"):
        add_weight(db, kg=80.0, date=day, at=int(day[-2:]))
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        await go_body(pilot, app)
        await _weight_view(app, date=DAY)
        await pilot.pause()
        rows = _weight_rows(app)
    assert rows == sorted(rows, reverse=True), f"not newest first: {rows}"


async def test_all_time_has_no_lower_bound(make_app, db):
    for day in ("2020-01-01", "2026-09-02"):
        add_weight(db, kg=80.0, date=day, at=1)
    app = make_app()
    async with app.run_test(size=(110, 34)) as pilot:
        await go_body(pilot, app)
        await _weight_view(app, date=DAY, horizon="all")
        await pilot.pause()
        rows = _weight_rows(app)
    assert rows == ["2026-09-02", "2020-01-01"], f"all time dropped a row: {rows}"


async def test_a_meal_estimate_and_a_factor_inference_do_not_cancel_each_other(
    make_app, db, type_into
):
    """The activity worker has its own `@work` group, and nothing tested it — removing
    the token left all tests green.

    The severe direction is this one: with a shared group, pressing `f` during a factor
    inference cancels `_run_factor_estimate` before it writes, so "gym 1h" is never
    recorded at all — a direct violation of "a failed activity inference still records the
    activity". A cancelled worker also never reaches `_set_inferring(False)`, so the
    indicator would stick on forever rather than clearing.
    """
    import asyncio

    release = asyncio.Event()

    async def slow_factor(**kw):
        await release.wait()
        return {"factor": 1.6}

    async def quick_food(**kw):
        return {"description": "salad", "kcal": 120}

    calls = {"n": 0}

    async def runner_json(**kw):
        calls["n"] += 1
        # The factor call is the one carrying a `system_prompt` about PAL; the food
        # estimate is the other. Dispatch on that rather than on call order.
        if "activity level" in str(kw.get("system_prompt", "")).lower():
            return await slow_factor(**kw)
        return await quick_food(**kw)

    app = make_app(runner_json=runner_json)
    async with app.run_test(size=(120, 34)) as pilot:
        await go_body(pilot, app)
        await pilot.press("a")
        await type_into(pilot, "gym 1h")
        await pilot.press("enter")
        await pilot.pause()

        # A meal estimate starts while the inference is still in flight.
        await pilot.press("f")
        await type_into(pilot, "salad")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        release.set()
        for _ in range(6):
            await pilot.pause()
        label = app.prompt.label
        body = app.query_one("#body")
        inferring, pending = body._inferring, body._pending_activity
        # The painted popup, not just the flag. `_inferring` is write-only since the
        # indicator moved off the header, so asserting on it alone let `end_work` be
        # deleted with the suite green and the popup line stuck on screen forever.
        popup = _popup(app)
    assert calls["n"] == 2, f"one of the two calls never happened: {calls}"
    # A surviving inference offers its answer, which replaces the food confirm. Sharing
    # the group cancels the factor worker instead, so the label stays "confirm food" and
    # the number is silently never offered — the activity would then only ever be written
    # by the failure path, with no factor.
    assert label == "confirm activity", f"the inference was cancelled: {label!r}"
    assert pending is not None and pending.factor == 1.6
    # A cancelled worker never reaches `_set_inferring(False)`, so the indicator would
    # stick on forever rather than clearing.
    assert inferring is False, "the estimating flag never cleared"
    assert popup == "", f"the popup outlived both calls: {popup!r}"


# ── one window: every table on the tab answers to the horizon ─────────────
# Measured before this change: `+`/`-` moved the chart and the weight table, and did
# nothing whatsoever to the food or activity tables — one row at `1d` and the same one
# row at `all`. Meanwhile the chart above the food table was still plotting weight. The
# window is one concept (`horizon.py`); the tab now has one window.
#
# `1d` is exactly the old behaviour: `horizon.resolve("1d")` gives start == end == the
# anchor, and its label is the day itself.


def _seed_window(db, days=6, start="2026-08-30"):
    from daylogs.body import add_activity, add_food, add_weight

    d0 = dt.date.fromisoformat(start)
    for i in range(days):
        d = (d0 + dt.timedelta(days=i)).isoformat()
        at = int(dt.datetime.fromisoformat(f"{d}T07:05").timestamp())
        add_weight(db, kg=80.0 - i * 0.2, date=d, at=at)
        add_food(db, description=f"meal {i}", kcal=500 + i, source="labeled", date=d, at=at + 18000)
        add_activity(db, description=f"walk {i}", factor=1.4, date=d, at=at + 36000,
                     source="labeled")


def _rows(app) -> int:
    from textual.widgets import DataTable

    return app.query_one("#body-table", DataTable).row_count


def _cols(app) -> list[str]:
    from textual.widgets import DataTable

    return [str(c.label) for c in app.query_one("#body-table", DataTable).columns.values()]


def _head(app) -> str:
    from textual.widgets import Static

    c = app.query_one("#food-head", Static).content
    return c if isinstance(c, str) else c.plain


async def _view(pilot, app, name: str):
    """Land on a named Body sub-view without depending on where the tab opens."""
    tab = await go_body(pilot, app)
    tab.table_mode = name
    tab.reload()
    await pilot.pause()
    return tab


async def test_the_food_table_follows_the_horizon(make_app, db):
    _seed_window(db)
    app = make_app(now=lambda: dt.datetime(2026, 9, 4, 20, 0))
    async with app.run_test(size=(140, 40)) as pilot:
        tab = await _view(pilot, app, "food")
        tab.horizon = "1d"
        tab.reload()
        await pilot.pause()
        one_day = _rows(app)
        tab.horizon = "1w"
        tab.reload()
        await pilot.pause()
        one_week = _rows(app)
        tab.horizon = "all"
        tab.reload()
        await pilot.pause()
        everything = _rows(app)
    # The seed covers 2026-08-30 .. 09-04 and the clock is on 09-04, so `1d` is exactly
    # that day's one meal — which is precisely the old per-day behaviour, reachable again
    # by zooming all the way in.
    assert one_day == 1, f"1d should be the anchor day alone: {one_day}"
    assert one_week == 6, f"a week should reach back to Aug 30: {one_week}"
    assert everything == 6, f"all time should list every meal: {everything}"
    assert one_week > one_day, f"zooming out added no rows: {one_day} -> {one_week}"


async def test_the_activity_table_follows_the_horizon(make_app, db):
    _seed_window(db)
    app = make_app(now=lambda: dt.datetime(2026, 9, 4, 20, 0))
    async with app.run_test(size=(140, 40)) as pilot:
        tab = await _view(pilot, app, "activity")
        tab.horizon = "1d"
        tab.reload()
        await pilot.pause()
        one_day = _rows(app)
        tab.horizon = "all"
        tab.reload()
        await pilot.pause()
        everything = _rows(app)
    assert one_day == 1, f"1d should be the anchor day alone: {one_day}"
    assert everything == 6, f"all time should list every activity: {everything}"


async def test_one_day_is_exactly_the_day(make_app, db):
    """`1d` has to reproduce the old per-day behaviour precisely, or zooming all the way
    in is not a way back to "what did I eat today"."""
    _seed_window(db, days=6, start="2026-08-30")
    app = make_app(now=lambda: dt.datetime(2026, 9, 2, 20, 0))
    async with app.run_test(size=(140, 40)) as pilot:
        tab = await _view(pilot, app, "food")
        tab.horizon = "1d"
        tab.reload()
        await pilot.pause()
        rows = _rows(app)
        head = _head(app)
    assert rows == 1, f"exactly one meal was logged on 2026-09-02: {rows}"
    assert "Sep 2 2026" in head, f"the header should name the single day: {head!r}"


async def test_an_older_anchor_does_not_list_newer_rows(make_app, db):
    """The right edge matters, for the reason `list_weight`'s docstring gives: with a
    lower bound alone, viewing an older day listed rows that had not happened yet."""
    _seed_window(db, days=6, start="2026-08-30")
    app = make_app(now=lambda: dt.datetime(2026, 9, 4, 20, 0))
    async with app.run_test(size=(140, 40)) as pilot:
        tab = await _view(pilot, app, "food")
        tab.viewing_date = "2026-09-01"
        tab.horizon = "all"
        tab.reload()
        await pilot.pause()
        table = app.query_one("#body-table")
        dates = [str(table.get_row_at(i)[0]) for i in range(table.row_count)]
    assert dates, "nothing listed at all"
    assert max(dates) <= "2026-09-01", f"listed rows from the future: {dates}"


async def test_the_food_and_activity_tables_say_which_day_each_row_is(make_app, db):
    """A window of rows with only a clock time is unreadable — two `08:00` rows five days
    apart look like the same morning."""
    _seed_window(db)
    app = make_app(now=lambda: dt.datetime(2026, 9, 4, 20, 0))
    async with app.run_test(size=(140, 40)) as pilot:
        tab = await _view(pilot, app, "food")
        tab.horizon = "all"
        tab.reload()
        await pilot.pause()
        food_cols = _cols(app)
        tab.table_mode = "activity"
        tab.reload()
        await pilot.pause()
        activity_cols = _cols(app)
    assert food_cols[:2] == ["date", "time"], food_cols
    assert activity_cols[:2] == ["date", "time"], activity_cols


async def test_the_weight_table_shows_the_reading_time(make_app, db):
    """Two readings on one day rendered as two rows both saying `2026-09-04`, and
    `measured_at` — the tie-breaker `weight_series_between` uses to choose between them — was
    invisible. The morning reading is the trend's; the later one is the headline's."""
    from daylogs.body import add_weight

    add_weight(db, kg=80.4, date="2026-09-04",
               at=int(dt.datetime(2026, 9, 4, 7, 5).timestamp()))
    add_weight(db, kg=80.9, date="2026-09-04",
               at=int(dt.datetime(2026, 9, 4, 10, 40).timestamp()))
    app = make_app(now=lambda: dt.datetime(2026, 9, 4, 20, 0))
    async with app.run_test(size=(140, 40)) as pilot:
        tab = await _view(pilot, app, "weight")
        tab.horizon = "1d"
        tab.reload()
        await pilot.pause()
        cols = _cols(app)
        table = app.query_one("#body-table")
        shown = [[str(c) for c in table.get_row_at(i)] for i in range(table.row_count)]
    assert cols[:2] == ["date", "time"], cols
    times = sorted(r[1] for r in shown)
    assert times == ["07:05", "10:40"], f"the two readings are not distinguishable: {shown}"


async def test_the_headers_all_name_the_window(make_app, db):
    """Three tables over one window, so three headers of one shape. The day's own energy
    balance stays in the ENERGY panel, which already carried it."""
    _seed_window(db)
    app = make_app(now=lambda: dt.datetime(2026, 9, 4, 20, 0))
    async with app.run_test(size=(140, 40)) as pilot:
        heads = {}
        for view in ("weight", "food", "activity"):
            tab = await _view(pilot, app, view)
            tab.horizon = "1w"
            tab.reload()
            await pilot.pause()
            heads[view] = _head(app)
    for view, head in heads.items():
        assert "Aug 29 – Sep 4 2026" in head, f"{view} header does not name the window: {head!r}"
    assert head.startswith("ACTIVITY"), heads["activity"]
    assert heads["food"].startswith("FOOD"), heads["food"]
    assert heads["weight"].startswith("WEIGHT"), heads["weight"]


async def test_an_empty_window_names_the_key_that_fills_it(make_app, db):
    """The same empty state the weight table already had: "true, useless, and reads as
    stale data" is what a bare 0 would be."""
    app = make_app(now=lambda: dt.datetime(2026, 9, 4, 20, 0))
    async with app.run_test(size=(140, 40)) as pilot:
        heads = {}
        for view, key in (("food", "f"), ("activity", "a")):
            tab = await _view(pilot, app, view)
            tab.horizon = "1w"
            tab.reload()
            await pilot.pause()
            heads[view] = (_head(app), key)
    for view, (head, key) in heads.items():
        assert f"press {key}" in head, f"{view}'s empty state names no fix: {head!r}"
        assert "widen" in head, f"{view}'s empty state does not mention zooming out: {head!r}"


async def test_editing_a_weights_time_moves_the_stamp(make_app, db):
    """The converse of "an unchanged minute preserves the seconds". The time is in the
    line because the table shows it, so changing it has to actually move `measured_at` —
    which is the column `weight_series_between` uses to pick a day's reading, so a time edit that
    silently did nothing would leave the trend on the wrong one of two weigh-ins.
    """
    at = int(dt.datetime(2026, 9, 4, 7, 5, 43).timestamp())
    add_weight(db, kg=80.0, date="2026-09-04", at=at)
    app = make_app(now=lambda: dt.datetime(2026, 9, 4, 20, 0))
    async with app.run_test(size=(140, 40)) as pilot:
        tab = await _view(pilot, app, "weight")
        tab.horizon = "1d"
        tab.reload()
        await pilot.pause()
        app.query_one("#body-table").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.prompt.is_open and "07:05" in app.prompt.value, app.prompt.value
        app.prompt.value = "80 @2026-09-04/09:30"
        await pilot.press("enter")
        await pilot.pause()
    row = list_weight(db)[0]
    assert hhmm(row["measured_at"], app.cfg.timezone) == "09:30", (
        f"the edited time did not land: {row['measured_at']}"
    )
    assert row["measured_at"] != at, "the stamp did not move at all"


async def test_a_weight_edit_that_moves_only_the_date_takes_the_stamp_with_it(make_app, db):
    """The other half of the date-only defect, at the UI layer.

    A line naming a date and no time is legal, and the grammar resolves the missing time to
    *now* — so restamping from the parsed instant would move a 07:05 reading to whenever the
    edit happened. Keeping the stored stamp instead left it on the old day, which inverts
    `morning_weight` and `latest_weight`. The rule is: reuse the row's own clock time, move
    it to the stated date.
    """
    from daylogs.body import list_weight

    at = int(dt.datetime(2026, 9, 4, 7, 5, 43).timestamp())
    add_weight(db, kg=80.0, date="2026-09-04", at=at)
    app = make_app(now=lambda: dt.datetime(2026, 9, 4, 20, 0))
    async with app.run_test(size=(140, 40)) as pilot:
        tab = await _view(pilot, app, "weight")
        tab.horizon = "all"
        tab.reload()
        await pilot.pause()
        app.query_one("#body-table").focus()
        await pilot.press("enter")
        await pilot.pause()
        app.prompt.value = "80 @2026-09-05"          # a date, deliberately no time
        await pilot.press("enter")
        await pilot.pause()
    row = list_weight(db)[0]
    assert row["date"] == "2026-09-05"
    stamp = dt.datetime.fromtimestamp(row["measured_at"])
    assert stamp.strftime("%Y-%m-%d") == "2026-09-05", (
        f"the stamp stayed on the old day: {stamp}"
    )
    assert stamp.strftime("%H:%M") == "07:05", (
        f"the reading's own clock time was replaced by the edit's: {stamp}"
    )


async def test_a_time_token_is_recognised_in_either_order(make_app, db):
    """`parse.resolve_when` is deliberately order-free, so `@09:30/2026-09-04` means the
    same as `@2026-09-04/09:30`. The guard only shape-matched the LAST `/`-separated part,
    so the reversed form parsed a time and was then treated as "no time given" — the edit
    silently kept the old stamp while the user had plainly typed one."""
    from daylogs.body import list_weight

    at = int(dt.datetime(2026, 9, 4, 7, 5, 43).timestamp())
    add_weight(db, kg=80.0, date="2026-09-04", at=at)
    app = make_app(now=lambda: dt.datetime(2026, 9, 4, 20, 0))
    async with app.run_test(size=(140, 40)) as pilot:
        tab = await _view(pilot, app, "weight")
        tab.horizon = "all"
        tab.reload()
        await pilot.pause()
        app.query_one("#body-table").focus()
        await pilot.press("enter")
        await pilot.pause()
        app.prompt.value = "80 @09:30/2026-09-04"
        await pilot.press("enter")
        await pilot.pause()
    row = list_weight(db)[0]
    assert hhmm(row["measured_at"], app.cfg.timezone) == "09:30", (
        f"the reversed token's time did not land: {row['measured_at']}"
    )


async def test_no_energy_panel_line_is_wider_than_the_panel(make_app, make_cfg, db):
    """`.panel-body` truncates with an ellipsis, so an over-wide line disappears mid-word
    and a substring assertion on its prefix goes on passing. The no-factor guidance shipped
    at 52 cells against a 46-column panel and read "press h to set an ordin…".

    Checked at the narrowest supported layout and at the size much of this suite uses.
    """
    add_weight(db, kg=80.0, date=DAY, at=1)
    add_food(db, description="salad", kcal=1200, source="labeled", date=DAY, at=2)
    for width in (100, 110):
        app = make_app(cfg=make_cfg(height_cm=180, sex="male", birthday="1996-08-27"))
        async with app.run_test(size=(width, 34)) as pilot:
            await go_body(pilot, app)
            tab = app.query_one("#body")
            tab.viewing_date = DAY
            tab.reload()
            await pilot.pause()
            avail = app.query_one("#panel-energy").content_size.width
            body_text = str(app.query_one("#energy-body").content)
        assert avail > 0, "the panel was never laid out, so this proves nothing"
        too_wide = [ln for ln in body_text.splitlines() if len(ln) > avail]
        assert not too_wide, f"at {width} columns ({avail} available): {too_wide}"


async def test_bracket_steps_a_whole_window_not_a_single_day(make_app, db):
    """`[` stepped one calendar day, which was the right size while the food and activity
    tables were per-day. Against a windowed tab it means 30 presses to page back a month —
    and at `all` it moved the right edge of a window whose own header says ALL TIME, so the
    header excluded rows while claiming to show everything. Measured before the fix:
    `FOOD   ALL TIME   9 meals` became `6 meals` after one press.

    `horizon.shift` is what Money has always used; its `_STEP["all"]` is `(0, 0)`, so `[`
    is deliberately a no-op there.
    """
    for d in ("2026-08-04", "2026-09-04"):
        add_food(db, description=f"meal {d}", kcal=600, source="labeled", date=d,
                 at=int(dt.datetime.fromisoformat(f"{d}T12:00").timestamp()))
    app = make_app(now=lambda: dt.datetime(2026, 9, 4, 22, 0))
    async with app.run_test(size=(140, 40)) as pilot:
        tab = await _view(pilot, app, "food")
        tab.horizon = "1m"
        tab.viewing_date = "2026-09-04"
        tab.reload()
        await pilot.pause()
        await pilot.press("left_square_bracket")
        await pilot.pause()
        stepped = tab.span()
        assert stepped.end == "2026-08-04", f"one press should step a whole month: {stepped}"

        tab.horizon = "all"
        tab.viewing_date = "2026-09-04"
        tab.reload()
        await pilot.pause()
        before = _head(app)
        await pilot.press("left_square_bracket")
        await pilot.pause()
        assert _head(app) == before, (
            f"`[` moved a window labelled ALL TIME: {before!r} -> {_head(app)!r}"
        )


async def test_the_food_header_totals_the_whole_window(make_app, db):
    """The header's intake is the sum of every row on screen, over multiple days. The only
    assertion that read it had a one-meal, one-day fixture, so it could not tell "the
    window's intake" from "the day's" — a mutation returning just the newest row's calories
    left all 117 tests in this file passing."""
    for d, k in (("2026-09-02", 500), ("2026-09-03", 600), ("2026-09-04", 700)):
        add_food(db, description=f"meal {d}", kcal=k, source="labeled", date=d,
                 at=int(dt.datetime.fromisoformat(f"{d}T12:00").timestamp()))
    app = make_app(now=lambda: dt.datetime(2026, 9, 4, 22, 0))
    async with app.run_test(size=(140, 40)) as pilot:
        tab = await _view(pilot, app, "food")
        tab.horizon = "1w"
        tab.reload()
        await pilot.pause()
        head = _head(app)
    assert "3 meals" in head, head
    assert "1,800 kcal in" in head, f"the header does not total the window: {head!r}"


async def test_the_header_says_when_the_row_cap_bit(make_app, db, monkeypatch):
    """The cap is a safety net on a pathological "all time", and the count beside it would
    otherwise be a silent prefix of the truth."""
    monkeypatch.setattr("daylogs.tui.body_tab._ROW_CAP", 2)
    for d in ("2026-09-02", "2026-09-03", "2026-09-04"):
        add_food(db, description=f"meal {d}", kcal=500, source="labeled", date=d,
                 at=int(dt.datetime.fromisoformat(f"{d}T12:00").timestamp()))
    app = make_app(now=lambda: dt.datetime(2026, 9, 4, 22, 0))
    async with app.run_test(size=(140, 40)) as pilot:
        tab = await _view(pilot, app, "food")
        tab.horizon = "all"
        tab.reload()
        await pilot.pause()
        head = _head(app)
        rows = _rows(app)
    assert rows == 2, f"the cap did not bound the query: {rows}"
    assert "(capped)" in head, f"a truncated list did not say so: {head!r}"


async def test_the_readme_food_box_matches_what_the_app_renders(make_app, db):
    """The README shows a FOOD box. Hand-drawn illustrations of live widgets are banned
    here — `tools/screenshots.py` exists because the Day tab's ASCII "drifted twice" — and
    one slipped in two slices ago with a border a column short and a truncation the code
    cannot emit. This box was copied from a real render; this keeps it that way by pinning
    the parts that drift: the header's shape and the column names."""
    import re
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text().splitlines()
    start = next(i for i, ln in enumerate(readme) if ln.strip().startswith("FOOD   Aug"))
    box = [ln for ln in readme[start:start + 3]]
    header_line, _views, columns = box

    # More than one, so the plural in the header matches the illustration's.
    for d in ("2026-09-02", "2026-09-03", "2026-09-04"):
        add_food(db, description="lunch", kcal=663, source="labeled", date=d,
                 at=int(dt.datetime.fromisoformat(f"{d}T12:30").timestamp()))
    app = make_app(now=lambda: dt.datetime(2026, 9, 4, 22, 0))
    async with app.run_test(size=(140, 40)) as pilot:
        tab = await _view(pilot, app, "food")
        tab.horizon = "1w"
        tab.reload()
        await pilot.pause()
        real_head = _head(app)
        real_cols = _cols(app)

    assert columns.split() == real_cols, (
        f"the README's columns are not the app's: {columns.split()} vs {real_cols}"
    )
    # Same shape, not the same numbers: the README's window and counts are illustrative.
    shape = re.sub(r"[\d,]+", "N", header_line.strip())
    assert re.sub(r"[\d,]+", "N", real_head) == shape, (
        f"the README header has a different shape:\n  README: {shape}\n"
        f"  app:    {re.sub(r'[\\d,]+', 'N', real_head)}"
    )


@pytest.mark.parametrize(
    "kind,line,table,col",
    [
        ("weight", "80 @2026-09-02", "weight", "measured_at"),
        ("food", "salad =610 @2026-09-02", "food", "ate_at"),
        ("activity", "gym 1h =1.55 @2026-09-02", "activity", "logged_at"),
    ],
)
async def test_a_date_only_edit_moves_every_tables_stamp_the_same_way(
    make_app, db, kind, line, table, col
):
    """One rule for all three edit paths: a line naming a date and no time keeps the row's
    own clock time and moves it to that date.

    Only the weight path went through `_restamp_for`, and the other two failed in the two
    opposite directions the helper exists to prevent — reproduced with the row stored at
    2026-09-01 07:05:30 and the clock at 21:42:

      food     `date` moved to the 2nd while `ate_at` stayed on the 1st, so the row sorted
               within its new day by a timestamp belonging to the old one;
      activity `logged_at` jumped to 21:42, *now's* clock — and that column is the
               tie-breaker `resolved_factor` uses to pick a day's latest inference.
    """
    from daylogs.body import add_activity

    at = int(dt.datetime(2026, 9, 1, 7, 5, 30).timestamp())
    if kind == "weight":
        add_weight(db, kg=80.0, date="2026-09-01", at=at)
    elif kind == "food":
        add_food(db, description="salad", kcal=610, source="labeled", date="2026-09-01", at=at)
    else:
        add_activity(db, description="gym 1h", factor=1.55, date="2026-09-01", at=at,
                     source="labeled")

    app = make_app(now=lambda: dt.datetime(2026, 9, 5, 21, 42))
    async with app.run_test(size=(140, 40)) as pilot:
        tab = await _view(pilot, app, kind)
        tab.horizon = "all"
        tab.viewing_date = "2026-09-01"
        tab.reload()
        await pilot.pause()
        app.query_one("#body-table").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.prompt.is_open, "the row did not arm for editing"
        app.prompt.value = line
        await pilot.press("enter")
        await pilot.pause()
        tz = app.cfg.timezone

    row = list(db.execute(f"SELECT * FROM {table}"))[0]
    assert row["date"] == "2026-09-02"
    moved = wall(row[col], tz)
    assert moved.date().isoformat() == "2026-09-02", (
        f"{kind}: the stamp stayed on the old day ({moved})"
    )
    assert moved.strftime("%H:%M") == "07:05", (
        f"{kind}: the row's own clock time was replaced ({moved}) — 21:42 means it took now's"
    )


async def test_an_edit_settles_a_row_whose_date_and_stamp_disagree(make_app, db):
    """CLAUDE.md's stance, now that one rule drives all three edit paths: "`date` stays
    authoritative over `ate_at` when the two disagree … the render is asserted to be
    *stable* rather than instant-preserving: it settles once instead of creeping on every
    edit."

    The app never writes such a row, but a zone change can produce one — Aug 27 in Toronto
    is Aug 28 in Kiritimati — and the two columns are stored separately. An edit moves the
    stamp onto the stated date at its own clock time, and a second identical edit changes
    nothing, which is what "settles once" means.
    """
    stamp = int(dt.datetime(2026, 8, 20, 7, 5, 43).timestamp())
    add_food(db, description="oatmeal", kcal=350, source="labeled", date="2026-08-28",
             at=stamp)
    app = make_app(now=lambda: dt.datetime(2026, 8, 28, 9, 0))
    async with app.run_test(size=(140, 40)) as pilot:
        tab = await _view(pilot, app, "food")
        tab.horizon = "all"
        tab.viewing_date = "2026-08-28"
        tab.reload()
        await pilot.pause()
        app.query_one("#body-table").focus()
        await pilot.press("enter")
        await pilot.pause()
        first = app.prompt.value
        await pilot.press("enter")               # submit the prefill unchanged
        await pilot.pause()
        settled = list(db.execute("SELECT * FROM food"))[0]["ate_at"]
        tz = app.cfg.timezone
        # Again, unchanged: it must not creep.
        await pilot.press("enter")
        await pilot.pause()
        second = app.prompt.value
        await pilot.press("enter")
        await pilot.pause()
    again = list(db.execute("SELECT * FROM food"))[0]["ate_at"]
    assert wall(settled, tz).date().isoformat() == "2026-08-28", (
        f"the stamp did not settle onto the stated date: {wall(settled, tz)}"
    )
    assert wall(settled, tz).strftime("%H:%M") == "07:05", "the clock time should survive"
    assert again == settled, "a second identical edit moved it again — it is creeping"
    assert second == first, f"the rendered line is not stable: {first!r} -> {second!r}"


# ── sorting from the keys ────────────────────────────────────────────────
async def test_k_sorts_the_food_table_by_kcal_and_again_flips_it(make_app, db):
    """The idiom Money's `d`/`c`/`k` already use: a different field switches and resets to
    descending, the same field again flips direction. Asserted through the rendered table,
    not the attribute, because what the user sees is the row order."""
    now = lambda: dt.datetime(2026, 9, 4, 20, 0)  # noqa: E731
    for kc, hh in ((500, 8), (900, 12), (200, 19)):
        add_food(db, description=f"meal {kc}", kcal=kc, date="2026-09-04",
                 at=int(dt.datetime(2026, 9, 4, hh, 0).timestamp()), source="labeled")
    app = make_app(now=now)
    async with app.run_test(size=(120, 34)) as pilot:
        body_tab = await go_body(pilot, app)
        assert body_tab.table_mode == "food"

        def kcals():
            t = app.query_one("#body-table")
            return [str(t.get_row(k)[3]) for k in t.rows]

        assert kcals() == ["200", "900", "500"], "the default is newest first"
        await pilot.press("k")
        await pilot.pause()
        assert kcals() == ["900", "500", "200"], "k should sort by kcal, biggest first"
        await pilot.press("k")
        await pilot.pause()
        assert kcals() == ["200", "500", "900"], "the same key again should flip direction"
        await pilot.press("d")
        await pilot.pause()
        assert kcals() == ["200", "900", "500"], "d should go back to date, newest first"


async def test_the_state_row_names_the_number_the_table_actually_shows(make_app, db):
    """`k`'s keymap label has to stay generic — KEYMAP is static and the sub-view is not —
    so the state row is where the specific word lives: kcal on food, kg on weight, factor
    on activity. A single label reading "kcal" would be wrong on two tables out of three."""
    app = make_app()
    async with app.run_test(size=(120, 34)) as pilot:
        body_tab = await go_body(pilot, app)
        seen = {}
        for _ in range(3):
            seen[body_tab.table_mode] = body_tab.status_hint()
            await pilot.press("tab")
            await pilot.pause()
    assert "kcal" in seen["food"], seen["food"]
    assert "kg" in seen["weight"], seen["weight"]
    assert "factor" in seen["activity"], seen["activity"]
    assert "↓date" in seen["food"], "the active field should be marked with its direction"


async def test_the_sort_survives_switching_sub_view(make_app, db):
    """`tab` keeps the sort, the way Money keeps it across panes — otherwise looking at
    another table and coming back silently resets what you asked for."""
    app = make_app()
    async with app.run_test(size=(120, 34)) as pilot:
        body_tab = await go_body(pilot, app)
        await pilot.press("k")
        await pilot.press("k")
        await pilot.pause()
        assert (body_tab.sort_field, body_tab.sort_desc) == ("value", False)
        await pilot.press("tab")
        await pilot.pause()
        assert (body_tab.sort_field, body_tab.sort_desc) == ("value", False)
