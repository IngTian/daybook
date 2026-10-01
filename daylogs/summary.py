"""The once-daily summary: build a payload, make one claude call, persist it.

Four parallel section calls plus a synth call behind a workflow state machine
is the shape this wants to grow into. With two subjects to report on, that
machinery costs more than it buys — this is one prompt and one call, one retry.

A report is dated by the day it describes, not the day it runs. The target is
yesterday: a summary of today reads a half-finished day, and next_morning_kg
only exists once tomorrow's weigh-in is in.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
import time

from daylogs import body, money
from daylogs.fmt import hhmm

log = logging.getLogger(__name__)

_VOICE = """\
You are writing the user's daily summary. Use an honest peer voice — direct
about what mattered, direct about what didn't. No manager-speak, no
LinkedIn-positivity. Don't grade the user. Don't compare against an imagined
ideal day. Synthesis over recitation; the user can read raw numbers, your
value is patterns.

If `memory` is provided, let it shape interpretation. Don't quote it back.

Output:
- **Respond in English.** Markdown only. No opening or closing pleasantries.
- Two sections, in this order: `## Body` then `## Money`.
- Open with one or two sentences before the first heading — the read of the
  day, not a greeting.
- If a data field is missing or empty, say so plainly in one short line
  rather than padding.

Formatting — **plain GitHub-flavoured markdown, nothing else**:
- `**bold**` for quantities the reader should notice, and for a real positive
  move backed by data. Use it sparingly; if everything is bold, nothing is.
- Prefix a genuine overrun or regression with `⚠` — e.g.
  `⚠ over budget by $47.71`. A warning must be legible without colour.
- `-` for bullet lists. `##` headings exactly as specified above.
- **No LaTeX and no math delimiters.** This is rendered in a terminal; `$x$`
  and `\\frac{}{}` show up as literal characters. Write `1,840 kcal`, not
  `$1{,}840$`. A bare `$` in front of an amount is fine and expected.
- No HTML and no custom tags.
"""

_TASK = """\
## Body

**Temporal framing — read carefully.** `payload.body.weight_kg` is the
*first* weigh-in of target_date — the morning one, **before** any of the food
listed — or, if that day has none, the most recent earlier morning reading.
It does not reflect that food. `payload.body.next_morning_kg` is the morning
weigh-in of the following day; THAT reflects target_date's intake. Use it as
the ground-truth read on whether the food moved the scale.
`next_morning_delta` = next_morning − same-day (positive = up). When
`next_morning_kg` is null, say "morning weigh-in not in yet" rather than
speculating. Never compare same-day weight against same-day food to claim
food "didn't move the scale" — different days.

- Weight line: the reading plus the 7-day and 30-day deltas. If next_morning
  is present, one line on the overnight change and its likely explanation
  given the food (carb-heavy → glycogen/water; salty → sodium; high protein
  and low carb → typically flat-to-down).
- When `tdee` is present, one line: `Net kcal: <net_kcal> (in <kcal_in>,
  burn −<tdee>)`. `tdee` is resting `bmr` scaled by `activity_factor` — what
  the day actually cost — and it is the baseline `net_kcal` is measured
  against, so do not also compare the intake to `bmr`. When `tdee` is null but
  `bmr` is present, the same line against resting expenditure instead: `(in
  <kcal_in>, BMR −<bmr>)`. Skip it when both are null.
- `activity_source` says where `activity_factor` came from. `profile` is the
  user's ordinary day and is not news — do not remark on it. `logged` means
  they recorded something that departed from it, and
  `payload.body.activity` lists what; that is worth one line.
- Food: list each entry briefly. If `source` is mostly `estimated`, hedge the
  calorie total; if mostly `labeled`, treat it as reliable.

## Money

This app tracks spending only — there is no income, balance, or net worth, so
do not reason about any of them or ask about them.

- `payload.money.yesterday`: each expense's amount, category, description. A
  negative amount is a refund.
- `payload.money.month`: `spent` against `budget`, with `remaining`. Read the
  burn against calendar progress: `day_of_month` of `days_in_month`. 84% of
  budget on day 27 of 31 is fine; on day 12 it is not. Make that comparison
  explicitly — it is the most useful thing you can say.
- `payload.money.month.over_budget`: categories already past their cap. Note
  severity. Cross-check against the category's `history` — sometimes an
  overrun is one recurring line that could be cut.
- `payload.money.month.under_budget_remaining`: spare room. Call out notable
  ones; don't enumerate all of them.
- `payload.money.by_category[].history`: six monthly totals, the last being
  this month. Flag a category that has clearly stepped up or down against its
  own recent shape.
- If the data is thin, keep it short.
"""

SYSTEM_PROMPT = _VOICE + "\n" + _TASK


def target_date(today: str) -> str:
    return (dt.date.fromisoformat(today) - dt.timedelta(days=1)).isoformat()


def build_payload(conn, cfg, *, date: str) -> dict:
    money.check_date(date)
    month = date[:7]
    next_day = (dt.date.fromisoformat(date) + dt.timedelta(days=1)).isoformat()

    # The day's *first* reading, not its last. The prompt below states this field is the
    # morning weigh-in before any of the listed food; `latest_weight` handed it the last
    # reading of the day, so on any day weighed twice the temporal-framing block — and
    # `next_morning_delta` with it — was reasoning from a premise that was simply false.
    same = body.morning_weight(conn, on_or_before=date)
    same_kg = same["kg"] if same else None
    nxt = conn.execute(
        "SELECT kg FROM weight WHERE date = ? ORDER BY measured_at ASC LIMIT 1",
        (next_day,),
    ).fetchone()
    next_kg = nxt["kg"] if nxt else None

    kcal_in = body.day_kcal(conn, date=date)
    # From the *same* reading `day_tdee` uses, which is `latest_weight` — not from `same_kg`.
    # The prompt tells the model `tdee` is resting `bmr` scaled by `activity_factor`, and on a
    # day weighed twice that was arithmetically false: `bmr` came from the fasted reading and
    # `tdee` from the fed one. Reproduced 1.2 kg apart as 1800 x 1.2 = 2160 against a `tdee` of
    # 2146 — a 14 kcal gap the payload disclosed nowhere, so the model was asked to reason from
    # a relation its own numbers did not satisfy.
    #
    # `weight_kg` above stays the fasted reading on purpose: the temporal-framing paragraph
    # rests on it being the weigh-in taken before any of the food listed. Only the BMR moves,
    # to agree with the TDEE that is actually derived from it.
    latest = body.latest_weight(conn, on_or_before=date)
    bmr = body.compute_bmr(cfg, latest["kg"] if latest else None, today=date)
    # Through the data layer, not multiplied here, so the digest and the two panels
    # cannot report different maintenance for the same day.
    factor, factor_source = body.resolved_factor(conn, cfg, date=date)
    tdee = body.day_tdee(conn, cfg, date=date)
    summary = money.summarize_month(conn, month=month, today=date)

    payload: dict = {
        "target_date": date,
        "body": {
            "weight_kg": same_kg,
            "delta_7d": body.weight_delta(conn, end_date=date, days=7),
            "delta_30d": body.weight_delta(conn, end_date=date, days=30),
            "next_morning_kg": next_kg,
            "next_morning_delta": (
                round(next_kg - same_kg, 2)
                if next_kg is not None and same_kg is not None
                else None
            ),
            "bmr": bmr,
            "activity_factor": factor,
            "activity_source": factor_source,
            "tdee": tdee,
            "kcal_in": kcal_in,
            # Against the day's real cost when there is one, and against resting
            # expenditure when there is not. Not defaulting the factor is what keeps
            # the second case a real state rather than a silent 20% restatement of
            # every figure in every past digest.
            "net_kcal": (
                (kcal_in - tdee)
                if tdee is not None
                else ((kcal_in - bmr) if bmr is not None else None)
            ),
            "food": [
                {
                    "time": hhmm(r["ate_at"], cfg.timezone),
                    "description": r["description"],
                    "kcal": r["kcal"],
                    "source": r["source"],
                }
                for r in body.list_food(conn, date=date)
            ],
            # Only days that departed from the baseline have rows here, so an empty
            # list means an ordinary day, not a missing one.
            "activity": [
                {
                    "time": hhmm(r["logged_at"], cfg.timezone),
                    "description": r["description"],
                    "factor": r["factor"],
                    "source": r["source"],
                }
                for r in body.list_activity(conn, date=date)
            ],
        },
        "money": {
            "yesterday": [
                {
                    "amount": r["amount"],
                    "category": r["category"],
                    "description": r["description"],
                    # So the model does not read a prepayment as one day's spending. The
                    # month totals beside this list are prorated, and without the marker a
                    # 240.00 line next to a 20.00 monthly figure looks like a mistake.
                    "prepaid_months": r["prepaid_months"],
                }
                for r in conn.execute(
                    "SELECT amount, category, description, prepaid_months FROM expense"
                    " WHERE date = ? ORDER BY id ASC",
                    (date,),
                )
            ],
            "month": {
                "spent": summary.total_spent,
                "budget": summary.total_budget,
                "remaining": summary.remaining,
                "day_of_month": summary.day_of_month,
                "days_in_month": summary.days_in_month,
                "over_budget": [
                    {"category": c.category, "budget": c.budget, "spent": c.spent}
                    for c in summary.over_budget
                ],
                "under_budget_remaining": [
                    {"category": c.category, "remaining": c.delta}
                    for c in summary.under_budget_remaining
                ],
            },
            "by_category": [
                {
                    "category": c.category,
                    "budget": c.budget,
                    "spent": c.spent,
                    "delta": c.delta,
                    "history": c.history,
                }
                for c in summary.by_category
            ],
        },
    }

    memory = _read_memory(cfg)
    if memory:
        payload["memory"] = memory
    return payload


def _read_memory(cfg) -> str | None:
    try:
        text = cfg.memory_path.read_text(encoding="utf-8").strip()
    except (OSError, AttributeError):
        return None
    return text or None


def get_report(conn, date: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM report WHERE date = ?", (date,)).fetchone()


def latest_report(conn) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM report ORDER BY date DESC LIMIT 1").fetchone()


def prev_report_date(conn, date: str) -> str | None:
    row = conn.execute(
        "SELECT date FROM report WHERE date < ? ORDER BY date DESC LIMIT 1", (date,)
    ).fetchone()
    return row["date"] if row else None


def next_report_date(conn, date: str) -> str | None:
    row = conn.execute(
        "SELECT date FROM report WHERE date > ? ORDER BY date ASC LIMIT 1", (date,)
    ).fetchone()
    return row["date"] if row else None


def upsert_report(conn, *, date: str, content: str) -> None:
    conn.execute(
        "INSERT INTO report (date, content, generated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(date) DO UPDATE SET"
        " content = excluded.content, generated_at = excluded.generated_at",
        (money.check_date(date), content, int(time.time())),
    )


async def generate(conn, cfg, *, date: str, runner, retries: int = 1, on_attempt=None) -> str:
    """One call, plus `retries` extra attempts.

    Nothing is persisted unless a non-empty result comes back, so a failed run
    leaves the existing report (or its absence) untouched rather than writing
    a stub that looks like a real summary.

    `on_attempt` is called before each attempt. Every attempt gets the *whole*
    `summary_timeout_sec`, so a caller showing elapsed against that budget has to be told
    when the clock restarts — otherwise a first attempt that used its full 120 s leaves the
    second reading "121s / 120s" and climbing, which is neither the budget nor the running
    call's elapsed. The retry itself stays invisible: it is not a state the user acts on.
    """
    payload = build_payload(conn, cfg, date=date)
    user_prompt = json.dumps(payload, ensure_ascii=False, indent=2)

    last: Exception | None = None
    for attempt in range(retries + 1):
        if on_attempt is not None:
            on_attempt()
        try:
            out = await runner(
                SYSTEM_PROMPT,
                user_prompt,
                timeout_sec=cfg.summary_timeout_sec,
                model=cfg.claude_model,
            )
            if not out or not out.strip():
                raise ValueError("summary came back empty")
            content = out.strip()
            upsert_report(conn, date=date, content=content)
            return content
        except Exception as e:  # retried, then re-raised unchanged
            last = e
            log.warning("summary attempt %d for %s failed: %s", attempt + 1, date, e)
    raise last
