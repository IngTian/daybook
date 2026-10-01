import json

import pytest

from daylogs.body import add_food, add_weight
from daylogs.config import Config
from daylogs.money import add_expense, upsert_budget
from daylogs.summary import (
    SYSTEM_PROMPT,
    build_payload,
    generate,
    get_report,
    latest_report,
    next_report_date,
    prev_report_date,
    target_date,
    upsert_report,
)


def _cfg(tmp_path, **kw):
    return Config(
        root=tmp_path,
        db_path=tmp_path / "d.db",
        inbox_dir=tmp_path / "inbox",
        memory_path=tmp_path / "memory.md",
        **kw,
    )


def test_target_date_is_yesterday():
    assert target_date("2026-08-27") == "2026-08-26"
    assert target_date("2026-01-01") == "2025-12-31"


def test_payload_shape_is_empty_but_valid_with_no_data(db, tmp_path):
    p = build_payload(db, _cfg(tmp_path), date="2026-08-26")
    assert p["target_date"] == "2026-08-26"
    assert p["body"]["weight_kg"] is None
    assert p["body"]["bmr"] is None
    assert p["body"]["net_kcal"] is None
    assert p["body"]["food"] == []
    assert p["money"]["yesterday"] == []
    assert p["money"]["month"]["spent"] == 0.0
    assert "memory" not in p


def test_payload_carries_body_numbers(db, tmp_path):
    add_weight(db, kg=78.6, date="2026-08-20", at=1)
    add_weight(db, kg=78.2, date="2026-08-26", at=2)
    add_weight(db, kg=78.4, date="2026-08-27", at=3)
    add_food(db, description="salad", kcal=610, source="labeled", date="2026-08-26", at=10)
    cfg = _cfg(tmp_path, height_cm=180, sex="male", birthday="1996-08-27")
    p = build_payload(db, cfg, date="2026-08-26")
    assert p["body"]["weight_kg"] == 78.2
    assert p["body"]["delta_7d"] == pytest.approx(-0.4)
    assert p["body"]["next_morning_kg"] == 78.4
    assert p["body"]["next_morning_delta"] == pytest.approx(0.2)
    assert p["body"]["kcal_in"] == 610
    assert p["body"]["bmr"] is not None
    assert p["body"]["net_kcal"] == 610 - p["body"]["bmr"]
    assert p["body"]["food"][0]["description"] == "salad"
    assert p["body"]["food"][0]["source"] == "labeled"


def test_payload_next_morning_none_when_no_later_weighin(db, tmp_path):
    add_weight(db, kg=78.2, date="2026-08-26", at=1)
    p = build_payload(db, _cfg(tmp_path), date="2026-08-26")
    assert p["body"]["next_morning_kg"] is None
    assert p["body"]["next_morning_delta"] is None


def test_payload_same_day_weight_is_the_morning_reading_not_a_later_day(db, tmp_path):
    add_weight(db, kg=78.2, date="2026-08-26", at=1)
    add_weight(db, kg=99.9, date="2026-08-28", at=2)
    p = build_payload(db, _cfg(tmp_path), date="2026-08-26")
    assert p["body"]["weight_kg"] == 78.2


def test_payload_carries_money_numbers(db, tmp_path):
    upsert_budget(db, month="2026-08", name="Grocery", category="grocery", amount=500)
    add_expense(
        db, amount=84.10, description="weekly shop", category="grocery", date="2026-08-26"
    )
    p = build_payload(db, _cfg(tmp_path), date="2026-08-26")
    assert p["money"]["yesterday"][0]["description"] == "weekly shop"
    assert p["money"]["month"]["spent"] == pytest.approx(84.10)
    assert p["money"]["month"]["budget"] == 500.0
    assert p["money"]["month"]["day_of_month"] == 26
    assert p["money"]["month"]["days_in_month"] == 31
    assert len(p["money"]["by_category"][0]["history"]) == 6


def test_payload_over_budget_is_surfaced(db, tmp_path):
    upsert_budget(db, month="2026-08", name="Restaurant", category="restaurant", amount=200)
    add_expense(
        db, amount=289.0, description="dinner", category="restaurant", date="2026-08-11"
    )
    p = build_payload(db, _cfg(tmp_path), date="2026-08-26")
    assert p["money"]["month"]["over_budget"][0]["category"] == "restaurant"


def test_payload_includes_memory_when_present(db, tmp_path):
    (tmp_path / "memory.md").write_text("Training for a race in October.")
    p = build_payload(db, _cfg(tmp_path), date="2026-08-26")
    assert "race in October" in p["memory"]


def test_payload_ignores_an_empty_memory_file(db, tmp_path):
    (tmp_path / "memory.md").write_text("   \n")
    assert "memory" not in build_payload(db, _cfg(tmp_path), date="2026-08-26")


def test_payload_is_json_serialisable(db, tmp_path):
    add_expense(db, amount=1.0, description="x", category="other", date="2026-08-26")
    add_weight(db, kg=70.0, date="2026-08-26", at=1)
    json.dumps(build_payload(db, _cfg(tmp_path), date="2026-08-26"))


def test_system_prompt_states_the_temporal_rule_and_no_income():
    assert "next_morning_kg" in SYSTEM_PROMPT
    assert "morning" in SYSTEM_PROMPT.lower()
    assert "spending only" in SYSTEM_PROMPT


def test_system_prompt_asks_for_plain_markdown_and_forbids_latex():
    """It is rendered by a terminal Markdown widget: LaTeX shows up as literal
    characters, and the old custom tags are no longer interpreted."""
    assert "markdown" in SYSTEM_PROMPT.lower()
    assert "No LaTeX" in SYSTEM_PROMPT
    assert "<num>" not in SYSTEM_PROMPT
    assert "<warn>" not in SYSTEM_PROMPT


def test_report_roundtrip_and_latest(db):
    upsert_report(db, date="2026-08-25", content="older")
    upsert_report(db, date="2026-08-26", content="newer")
    assert get_report(db, "2026-08-26")["content"] == "newer"
    assert latest_report(db)["date"] == "2026-08-26"
    assert get_report(db, "2026-01-01") is None


def test_latest_report_none_when_empty(db):
    assert latest_report(db) is None


def test_prev_and_next_report_dates_clamp_at_the_ends(db):
    upsert_report(db, date="2026-08-25", content="a")
    upsert_report(db, date="2026-08-26", content="b")
    assert prev_report_date(db, "2026-08-26") == "2026-08-25"
    assert prev_report_date(db, "2026-08-25") is None
    assert next_report_date(db, "2026-08-25") == "2026-08-26"
    assert next_report_date(db, "2026-08-26") is None


def test_upsert_report_replaces_and_restamps(db):
    upsert_report(db, date="2026-08-26", content="v1")
    first = get_report(db, "2026-08-26")["generated_at"]
    upsert_report(db, date="2026-08-26", content="v2")
    row = get_report(db, "2026-08-26")
    assert row["content"] == "v2"
    assert row["generated_at"] >= first


async def test_generate_persists_and_returns(db, tmp_path):
    calls = []

    async def runner(system_prompt, user_prompt, *, timeout_sec, model=None):
        calls.append((system_prompt, user_prompt))
        return "## Body\n\nHeld steady."

    out = await generate(db, _cfg(tmp_path), date="2026-08-26", runner=runner)
    assert out.startswith("## Body")
    assert get_report(db, "2026-08-26")["content"] == out
    assert len(calls) == 1
    assert "target_date" in calls[0][1]


async def test_generate_passes_configured_timeout_and_model(db, tmp_path):
    seen = {}

    async def runner(system_prompt, user_prompt, *, timeout_sec, model=None):
        seen.update(timeout_sec=timeout_sec, model=model)
        return "ok"

    cfg = _cfg(tmp_path, summary_timeout_sec=45, claude_model="sonnet")
    await generate(db, cfg, date="2026-08-26", runner=runner)
    assert seen == {"timeout_sec": 45, "model": "sonnet"}


async def test_generate_retries_once_then_succeeds(db, tmp_path):
    attempts = {"n": 0}

    async def runner(system_prompt, user_prompt, *, timeout_sec, model=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            from daylogs.claude import ClaudeError

            raise ClaudeError("transient")
        return "ok"

    assert await generate(db, _cfg(tmp_path), date="2026-08-26", runner=runner) == "ok"
    assert attempts["n"] == 2


async def test_generate_raises_after_exhausting_retries_and_writes_nothing(db, tmp_path):
    from daylogs.claude import ClaudeError

    async def runner(system_prompt, user_prompt, *, timeout_sec, model=None):
        raise ClaudeError("down")

    with pytest.raises(ClaudeError):
        await generate(db, _cfg(tmp_path), date="2026-08-26", runner=runner)
    assert get_report(db, "2026-08-26") is None


async def test_generate_rejects_empty_output(db, tmp_path):
    async def runner(system_prompt, user_prompt, *, timeout_sec, model=None):
        return "   "

    with pytest.raises(ValueError, match="empty"):
        await generate(db, _cfg(tmp_path), date="2026-08-26", runner=runner, retries=0)


async def test_generate_does_not_clobber_an_existing_report_on_failure(db, tmp_path):
    from daylogs.claude import ClaudeError

    upsert_report(db, date="2026-08-26", content="the good one")

    async def runner(*a, **k):
        raise ClaudeError("down")

    with pytest.raises(ClaudeError):
        await generate(db, _cfg(tmp_path), date="2026-08-26", runner=runner)
    assert get_report(db, "2026-08-26")["content"] == "the good one"


# ── the day factor in the digest ─────────────────────────────────────────
# `net_kcal` used to be intake against *resting* expenditure, so a digest called a
# sedentary day a deficit it wasn't. These pin the substitution and — just as
# important — pin that a day with no factor still reads exactly as it did, because
# forty-three reports were generated against that wording.


def test_the_payload_carries_the_day_factor_and_the_burn_it_produces(db, tmp_path):
    add_weight(db, kg=80.0, date="2026-08-26", at=1)
    add_food(db, description="salad", kcal=1200, source="labeled", date="2026-08-26", at=2)
    cfg = _cfg(tmp_path, height_cm=180, sex="male", birthday="1996-01-01", activity="desk")
    b = build_payload(db, cfg, date="2026-08-26")["body"]
    assert b["bmr"] == 1780
    assert b["activity_factor"] == 1.2
    assert b["activity_source"] == "profile"
    assert b["tdee"] == 2136
    assert b["net_kcal"] == 1200 - 2136


def test_the_payload_nets_against_resting_bmr_with_no_factor(db, tmp_path):
    add_weight(db, kg=80.0, date="2026-08-26", at=1)
    add_food(db, description="salad", kcal=1200, source="labeled", date="2026-08-26", at=2)
    cfg = _cfg(tmp_path, height_cm=180, sex="male", birthday="1996-01-01")
    b = build_payload(db, cfg, date="2026-08-26")["body"]
    assert (b["activity_factor"], b["activity_source"], b["tdee"]) == (None, None, None)
    assert b["net_kcal"] == 1200 - b["bmr"]


def test_the_payload_lists_the_days_activities(db, tmp_path):
    from daylogs.body import add_activity
    from daylogs.fmt import hhmm

    add_weight(db, kg=80.0, date="2026-08-26", at=1)
    add_activity(db, description="gym 1h", date="2026-08-26", at=1788010000, factor=1.6,
                 source="estimated")
    cfg = _cfg(tmp_path, height_cm=180, sex="male", birthday="1996-01-01", activity="desk")
    b = build_payload(db, cfg, date="2026-08-26")["body"]
    assert b["activity"] == [
        {
            "time": hhmm(1788010000, cfg.timezone),
            "description": "gym 1h",
            "factor": 1.6,
            "source": "estimated",
        }
    ]
    assert b["activity_source"] == "logged"
    assert b["tdee"] == round(1780 * 1.6)


def test_the_payload_has_no_activity_rows_on_an_ordinary_day(db, tmp_path):
    cfg = _cfg(tmp_path, activity="desk")
    assert build_payload(db, cfg, date="2026-08-26")["body"]["activity"] == []


def test_the_payload_does_not_carry_bmi(db, tmp_path):
    """BMI is a restatement of weight, not a second fact, and putting it in front of
    the model invites exactly the banded judgement — "overweight" — that this app
    deliberately never makes."""
    add_weight(db, kg=80.0, date="2026-08-26", at=1)
    cfg = _cfg(tmp_path, height_cm=180, sex="male", birthday="1996-01-01")
    assert "bmi" not in build_payload(db, cfg, date="2026-08-26")["body"]


def test_the_prompt_describes_burn_without_dropping_resting_bmr():
    """Extended, not replaced. A day with no level has no `tdee`, and the prompt still
    has to know what to do with it."""
    assert "tdee" in SYSTEM_PROMPT
    assert "activity_source" in SYSTEM_PROMPT
    assert "BMR −<bmr>" in SYSTEM_PROMPT


def test_bmr_and_tdee_come_from_the_same_reading_on_a_twice_weighed_day(db, tmp_path):
    """The prompt asserts `tdee` is resting `bmr` scaled by `activity_factor`. That has to be
    arithmetically true in the payload, or the model is told to reason from a relation the
    numbers beside it do not satisfy.

    It was not. `bmr` was computed from `morning_weight` (the fasted reading, which
    `weight_kg` correctly reports) while `day_tdee` opens with `latest_weight` — so on a day
    weighed twice the two came from different readings. Reproduced at 1.2 kg apart: `bmr` 1800
    x 1.2 = 2160 against a `tdee` of 2146, a 14 kcal gap with nothing on screen or in the
    payload disclosing it.

    `weight_kg` deliberately stays the *fasted* reading: the prompt's temporal-framing
    paragraph rests on it being the weigh-in taken before any of the food listed. Only the
    BMR moves, to agree with the TDEE derived from it.
    """
    cfg = _cfg(tmp_path, height_cm=180.0, sex="male", birthday="1996-01-01", activity="desk")
    add_weight(db, kg=82.0, date="2026-09-14", at=1_000)       # fasted, first
    add_weight(db, kg=80.8, date="2026-09-14", at=20_000)      # fed, later
    add_food(db, description="lunch", kcal=600, source="labeled", date="2026-09-14", at=5_000)

    b = build_payload(db, cfg, date="2026-09-14")["body"]
    assert b["weight_kg"] == 82.0, "the fasted reading is what the prompt's framing needs"
    assert b["tdee"] == round(b["bmr"] * b["activity_factor"]), (
        f"the prompt says tdee is bmr x factor: {b['bmr']} x {b['activity_factor']} "
        f"= {round(b['bmr'] * b['activity_factor'])}, payload says {b['tdee']}"
    )


def test_a_once_weighed_day_is_unaffected(db, tmp_path):
    """The common case, pinned so the fix above cannot be read as changing it: with one reading
    a day, fasted and latest are the same row and every figure is what it was."""
    cfg = _cfg(tmp_path, height_cm=180.0, sex="male", birthday="1996-01-01", activity="desk")
    add_weight(db, kg=82.0, date="2026-09-14", at=1_000)
    b = build_payload(db, cfg, date="2026-09-14")["body"]
    assert b["weight_kg"] == 82.0
    assert b["tdee"] == round(b["bmr"] * b["activity_factor"])
