"""The README examples are valid under the grammar they advertise.

Every example in the "What you type" table is parsed by the real parser to prove
it is valid. This entire slice exists because examples that merely looked right
silently corrupted rows.
"""

import datetime as dt
import inspect
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from daylogs.categories import slugs
from daylogs.parse import (
    parse_activity,
    parse_budget,
    parse_category,
    parse_expense,
    parse_food,
    parse_recurring,
    parse_weigh,
)

README = Path(__file__).resolve().parents[1] / "README.md"
NOW = dt.datetime(2026, 8, 28, 9, 0, tzinfo=ZoneInfo("America/Toronto"))

# Map prompt labels to their parsers. Two tables, because not every grammar takes a
# clock or a vocabulary — `new category` is the whole line — and an unmapped prompt is a
# hard failure rather than a skip: it skipped for exactly one release and that release
# shipped a README example nothing had parsed.
PARSERS = {
    "weigh ›": parse_weigh,
    "food ›": parse_food,
    "activity ›": parse_activity,
    "expense ›": parse_expense,
    "budget ›": parse_budget,
    "recurring ›": parse_recurring,
}

# Same guarantee, one fewer argument.
PLAIN_PARSERS = {
    "new category ›": parse_category,
}


def extract_what_you_type_table(text: str) -> list[tuple[str, str]]:
    """Extract (prompt, example) pairs from the README's "What you type" table.

    Returns pairs like ("weigh ›", "78.2") for each row.
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if "### What you type" in line:
            start = i
            break
    if start is None:
        raise ValueError("### What you type not found in README")

    # Skip header and separator lines, collect until we hit a blank or non-table line.
    rows = []
    for line in lines[start:]:
        if line.startswith("| `") and " ›`" in line:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3:
                prompt = parts[1].strip("`")
                example = parts[2].strip("`")
                rows.append((prompt, example))
        elif line.startswith("Sigils mark") or (rows and not line.startswith("|")):
            break
    if len(rows) == 0:
        raise ValueError("No examples found in README table — extraction may have failed")
    return rows


@pytest.mark.parametrize(
    "prompt,example",
    extract_what_you_type_table(README.read_text()),
    ids=lambda x: x if isinstance(x, str) else None,
)
def test_readme_example_parses(prompt: str, example: str) -> None:
    """Each example in the README is valid under its prompt's grammar, and where the
    example uses a sigil, the parsed field carries the expected value.

    This test exists because examples that merely looked right silently corrupted rows.
    """
    parser = PARSERS.get(prompt)
    if parser is None:
        plain = PLAIN_PARSERS.get(prompt)
        assert plain is not None, (
            f"{prompt} is advertised in the README with nothing checking it —"
            " add it to PARSERS or PLAIN_PARSERS"
        )
        plain(example)
        return

    # Only the keyword arguments each parser declares — five parsers do not take one of
    # these, and hard-coding both is what kept those five parameters alive unread.
    accepts = inspect.signature(parser).parameters
    kw = {k: v for k, v in (("now", NOW), ("known_slugs", slugs())) if k in accepts}
    result = parser(example, **kw)

    # Assert parsed fields match the sigils present in the example.
    if "!grocery" in example:
        msg = f"!grocery → category must be 'grocery', got {result.category}"
        assert result.category == "grocery", msg
    if "!restaurant" in example:
        assert result.category == "restaurant", "!restaurant → category must be 'restaurant'"
    if "!subscriptions" in example:
        msg = "!subscriptions → category must be 'subscriptions'"
        assert result.category == "subscriptions", msg
    if "=610" in example:
        assert result.kcal == 610, "=610 → kcal must be 610"
    if "=active" in example:
        assert result.factor == 1.55, "=active → factor must be the active multiplier"
    if "=1.45" in example:
        assert result.factor == 1.45, "=1.45 → factor must be 1.45"
    if "#monthly" in example:
        assert result.cycle == "monthly", "#monthly → cycle must be 'monthly'"
    if example.startswith("-"):
        msg = f"negative amount example → amount must be < 0, got {result.amount}"
        assert result.amount < 0, msg
    if "@07:30" in example:
        # Check that time is parsed (at field is not midnight)
        import datetime as dt
        parsed_time = dt.datetime.fromtimestamp(result.at, ZoneInfo("America/Toronto"))
        assert parsed_time.hour == 7 and parsed_time.minute == 30, "@07:30 → time must be 07:30"


# ── the theme picker illustration ────────────────────────────────────────
def test_the_picker_illustration_is_output_the_picker_can_produce():
    """Hand-drawn art of a live widget is banned here for a reason — `tools/screenshots.py`
    exists because the Day tab's ASCII "drifted twice … an illustration maintained by hand
    cannot be verified against the thing it illustrates".

    The picker block came back hand-written and was wrong in three ways at once: its top
    border was one column short of its sides, it showed a *truncated* name (`solarized…`)
    that `strip` can never emit — it only ever appends `" …"` after a whole name — and it
    named a neighbourhood of the list the cursor position does not produce.

    So it is pinned to the pure function that generates it. A three-row box at 84 columns
    leaves 80 for content: two border columns and two of padding.
    """
    from daylogs.tui import themes

    lines = README.read_text().splitlines()
    top = next(i for i, ln in enumerate(lines) if ln.startswith("╭─ theme"))
    box = lines[top : top + 3]

    widths = {len(ln) for ln in box}
    assert len(widths) == 1, f"the box is not rectangular: {sorted(widths)}"
    width = widths.pop()

    body = box[1]
    assert body.startswith("│ ") and body.endswith("│"), body
    content = body[2:-1].rstrip()
    names = themes.names()
    assert content == themes.strip(names, names.index("nord"), width - 4), (
        f"the illustration is not what `strip` produces:\n  README: {content!r}\n"
        f"  real:   {themes.strip(names, names.index('nord'), width - 4)!r}"
    )
    assert f"of {len(names)}" in box[0], f"the count is stale: {box[0]!r}"
    assert "esc restores nord" in box[2], f"the subtitle names the wrong theme: {box[2]!r}"


# ── the data section ─────────────────────────────────────────────────────
def test_the_readme_names_every_table_the_export_writes():
    """The README enumerated six of seven and omitted `activity`, one clause above a
    sentence promising "the table list comes from the database rather than a hand-kept
    list, so nothing is silently left out". The enumeration *is* the hand-kept list, so it
    is pinned to the schema here — the same treatment the theme-picker box gets.

    This is the durability section on the PyPI front page: a reader who logs activities was
    being told their activity log does not leave the app, and `day export` writes
    `activity.csv`.
    """
    import sqlite3

    from daylogs.db import ensure_schema, table_names

    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    tables = table_names(conn)

    text = README.read_text()
    start = text.index("day export <dir>` writes one CSV per table")
    passage = text[start : start + 400]
    for t in tables:
        assert f"`{t}`" in passage, f"the export list omits {t!r}: {passage!r}"
    # And the count stated in the Data section has to agree with the schema.
    words = {6: "six", 7: "seven", 8: "eight"}
    assert f"SQLite, {words[len(tables)]} tables" in text, (
        f"the Data section's table count disagrees with the {len(tables)}-table schema"
    )


def test_the_profile_prompt_box_is_rectangular_and_matches_its_hint():
    """The third hand-drawn widget box to be wrong: 72 / 73 / 73 columns, a top border one
    short of its sides — the same defect the theme-picker box had, in a README that declares
    the project does not hand-draw widget art.

    Pinned to the data the widget itself renders from, so the three slots cannot drift:
    border title = the label, body = the example, border subtitle = the grammar.
    """
    from daylogs.tui.hints import for_label

    lines = README.read_text().splitlines()
    top = next(i for i, ln in enumerate(lines) if ln.startswith("╭─ profile"))
    box = lines[top : top + 3]

    widths = {len(ln) for ln in box}
    assert len(widths) == 1, f"the box is not rectangular: {sorted(widths)}"

    h = for_label("profile")
    assert box[0].startswith(f"╭─ {h.label} › "), box[0]
    assert box[1].strip(" │") == h.example, (
        f"the body is not the hint's example:\n  README: {box[1].strip(' │')!r}\n"
        f"  hint:   {h.example!r}"
    )
    assert box[2].startswith(f"╰─ {h.grammar} "), (
        f"the subtitle is not the hint's grammar:\n  README: {box[2]!r}\n  hint: {h.grammar!r}"
    )


def test_the_readme_never_describes_weight_as_last_reading_wins():
    """It did, and it prescribed an action in the same breath: "correcting a day means
    logging it again, the same last-reading-wins rule two weigh-ins on one day follow" —
    twenty-eight lines above the passage that correctly says the trend uses each day's
    *first* reading.

    A reader with a bad weigh-in would re-weigh to fix the trend, and the trend keeps the
    first reading. Latest-wins is the *activity factor's* rule; weight is the opposite, and
    `weight_series_between`/`morning_weight` both take `MIN(measured_at)` to prove it.
    """
    text = README.read_text()
    assert "last-reading-wins" not in text, (
        "that phrase only ever appeared in a false claim about weight — if it is back, "
        "check which table it is describing"
    )
    assert "each day's **first** reading" in text, (
        "the README must state which of a day's readings the trend uses"
    )
    # And the contrast has to be stated where the activity rule is, because that is where a
    # reader learns "log again and the newer row wins" and could carry it across.
    assert "Weight is the opposite" in text, (
        "the activity-factor passage must say weight does NOT follow latest-wins — that is "
        "the sentence a reader carries into the wrong action"
    )
