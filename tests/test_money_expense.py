import pytest
from helpers import all_expenses

from daylogs.money import (
    MoneyError,
    add_expense,
    delete_expense,
    update_expense,
)


def _add(db, **kw):
    base = dict(amount=12.40, description="lunch", category="restaurant", date="2026-08-27")
    return add_expense(db, **{**base, **kw})


def test_add_and_list(db):
    eid = _add(db)
    rows = all_expenses(db)
    assert len(rows) == 1
    assert rows[0]["id"] == eid
    assert rows[0]["amount"] == 12.40
    assert rows[0]["category"] == "restaurant"
    assert rows[0]["created_at"] > 0


def test_list_sorted_newest_first(db):
    _add(db, date="2026-08-01", description="first")
    _add(db, date="2026-08-27", description="last")
    assert [r["description"] for r in all_expenses(db)] == ["last", "first"]


def test_negative_amount_allowed_as_refund(db):
    eid = _add(db, amount=-24.99, description="returned shoes")
    assert all_expenses(db)[0]["id"] == eid


def test_zero_amount_rejected(db):
    with pytest.raises(MoneyError, match="non-zero"):
        _add(db, amount=0)


def test_empty_description_rejected(db):
    with pytest.raises(MoneyError, match="description"):
        _add(db, description="   ")


def test_unknown_category_rejected(db):
    with pytest.raises(MoneyError, match="category"):
        _add(db, category="nonexistent")


def test_bad_date_rejected(db):
    with pytest.raises(MoneyError):
        _add(db, date="27-08-2026")
    with pytest.raises(MoneyError):
        _add(db, date="2026-02-30")


def test_update_changes_only_given_fields(db):
    eid = _add(db)
    assert update_expense(db, eid, amount=15.0, category="grocery") is True
    row = all_expenses(db)[0]
    assert (row["amount"], row["category"], row["description"]) == (15.0, "grocery", "lunch")


def test_update_validates_category_and_date(db):
    eid = _add(db)
    with pytest.raises(MoneyError):
        update_expense(db, eid, category="nope")
    with pytest.raises(MoneyError):
        update_expense(db, eid, date="nope")


def test_update_rejects_unknown_field(db):
    eid = _add(db)
    with pytest.raises(MoneyError):
        update_expense(db, eid, bogus=1)


def test_update_unknown_id_returns_false(db):
    assert update_expense(db, 999, amount=1.0) is False


def test_delete_returns_row_for_undo(db):
    eid = _add(db)
    row = delete_expense(db, eid)
    assert row["description"] == "lunch"
    assert all_expenses(db) == []
    assert delete_expense(db, eid) is None



# ── the note ─────────────────────────────────────────────────────────────
def test_clearing_a_note_stores_null_not_an_empty_string(db):
    """`""` is the clearing value, and it has to land the way a fresh add lands it.

    `add_expense` normalises through `note or None`; `update_expense` dropped None and passed
    `""` straight into the UPDATE, so a cleared note became `''` while a never-set one was
    NULL. Two states that mean the same thing, and the real database has a row in the wrong
    one. Invisible while nothing displayed notes — but `note IS NOT NULL` already counted it,
    and a renderer would draw a present-but-blank note.
    """
    eid = _add(db, note="CIBC MC")
    assert all_expenses(db)[0]["note"] == "CIBC MC"
    update_expense(db, eid, note="")
    assert all_expenses(db)[0]["note"] is None, "a cleared note must be indistinguishable"


def test_an_added_empty_note_is_null_too(db):
    """The path that was already right, pinned so the two stay agreed."""
    _add(db, note="")
    assert all_expenses(db)[0]["note"] is None


def test_a_note_that_is_only_whitespace_clears_too(db):
    """Submitting `~` with a space after it is the same intent as submitting nothing."""
    eid = _add(db, note="CIBC MC")
    update_expense(db, eid, note="   ")
    assert all_expenses(db)[0]["note"] is None


def test_not_mentioning_the_note_leaves_it_alone(db):
    """The whole reason None is dropped: an edit writes only the fields it parsed, so a line
    that says nothing about the note must not clear one."""
    eid = _add(db, note="CIBC MC")
    update_expense(db, eid, description="dinner")
    row = all_expenses(db)[0]
    assert (row["description"], row["note"]) == ("dinner", "CIBC MC")


@pytest.mark.parametrize("raw", ["", "   ", "\t "])
def test_both_write_paths_agree_on_an_empty_note(db, raw):
    """Add and edit have to land the same state for the same input, or "has a note" stops
    being a single question. They disagreed on whitespace before this: `note or None` keeps
    `"   "` because it is truthy."""
    added = _add(db, note=raw)
    assert all_expenses(db)[0]["note"] is None, f"add stored {raw!r}"
    edited = _add(db, note="CIBC MC", date="2026-08-28")
    update_expense(db, edited, note=raw)
    row = next(r for r in all_expenses(db) if r["id"] == edited)
    assert row["note"] is None, f"edit stored {raw!r}"
    assert added != edited
