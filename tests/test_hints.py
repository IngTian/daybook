"""The prompt's discoverability contract.

`profile` shipped with a working grammar and no way to learn it, which is the
regression these tests exist to prevent: a new prompt with no hint now fails the
suite, and every example shown to the user is parsed to prove it is valid.
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
    parse_profile,
    parse_recurring,
    parse_weigh,
)
from daylogs.tui import hints

SRC = Path(__file__).resolve().parents[1] / "daylogs"

# Fixed, because a parser result must never depend on when the suite runs.
NOW = dt.datetime(2026, 8, 28, 9, 0, tzinfo=ZoneInfo("America/Toronto"))

# Which parser is behind each prompt. Prompts whose input is free text (filter), a
# filesystem path (photo path) or a date (go to date) have no grammar parser and are
# checked separately.
PARSERS = {
    "weigh": parse_weigh,
    "food": parse_food,
    "confirm food": parse_food,
    "activity": parse_activity,
    "confirm activity": parse_activity,
    "expense": parse_expense,
    # `fix category` re-enters `_submit_expense`, so its grammar is the expense grammar —
    # the whole line, amount first. It sat in NO_PARSER described as "a bare category",
    # which is the same lie the note below records about `profile`, and it hid a
    # placeholder the parser rejects outright: the example was `restaurant`, and
    # `parse_expense` answers that with "start with an amount". Following the completion
    # instead gave `12.40 lunch restaurant`, which parses and falls back to `other` again.
    "fix category": parse_expense,
    "budget": parse_budget,
    "recurring": parse_recurring,
}

# Grammars whose whole input is the line: no `now` to resolve a date against and no
# vocabulary to check a slug against. Same guarantee as PARSERS, one fewer argument —
# `profile` sat in NO_PARSER for a while, which was a lie about a prompt that does
# have a grammar, and left its example unchecked by the parametrized case.
PLAIN_PARSERS = {"profile": parse_profile, "new category": parse_category}

# Prompts with no grammar behind them: free text, a filesystem path, a date. `theme` used
# to be here too, described as "validated by `themes.check`" — both the prompt and that
# function are gone, replaced by a picker, and the entry outlived them.
NO_PARSER = {"filter", "photo path", "go to date"}


def test_every_prompt_opened_in_the_app_has_a_hint():
    """The invariant. There is no table of labels to check against — they are string
    literals at the call sites — so the source is the register."""
    opened = set()
    for path in SRC.rglob("*.py"):
        opened |= hints.labels_in_source(path.read_text())
    assert opened, "found no prompt.open() call sites — the regex has rotted"
    missing = sorted(opened - {h.label for h in hints.HINTS})
    assert not missing, f"prompts with no hint: {missing}"


def test_no_hint_describes_a_prompt_that_does_not_exist():
    """The other direction: a stale hint is a lie about what the app does."""
    opened = set()
    for path in SRC.rglob("*.py"):
        opened |= hints.labels_in_source(path.read_text())
    stale = sorted(h.label for h in hints.HINTS if h.label not in opened)
    assert not stale, f"hints for prompts that are never opened: {stale}"


def test_no_edit_hints_remain():
    assert not [h for h in hints.HINTS if h.label.startswith("edit ")]


def test_hint_labels_are_unique():
    labels = [h.label for h in hints.HINTS]
    assert len(labels) == len(set(labels))


def test_every_hint_has_both_an_example_and_a_grammar():
    for h in hints.HINTS:
        assert h.example.strip(), f"{h.label} has no example"
        assert h.grammar.strip(), f"{h.label} has no grammar"


def _call(parser, example):
    """Call `parser` with only the keyword arguments it actually declares.

    These loops used to pass `now=` and `known_slugs=` to every parser, which is what forced
    all eight to carry both whether they read them or not — five did not, and one of those
    (`parse_budget`'s `now`) invited writing a budget to the wrong month. Filtering on the
    signature keeps the loop generic without dictating the signatures, and cannot go stale
    the way a hand-maintained per-parser table would.
    """
    accepts = inspect.signature(parser).parameters
    kw = {k: v for k, v in (("now", NOW), ("known_slugs", slugs())) if k in accepts}
    return parser(example, **kw)


@pytest.mark.parametrize("label", sorted(PARSERS))
def test_every_example_is_a_line_the_parser_accepts(label):
    """An example a reader copies verbatim must work. Otherwise the hint is worse
    than no hint."""
    hint = hints.for_label(label)
    _call(PARSERS[label], hint.example)


@pytest.mark.parametrize("label", sorted(PLAIN_PARSERS))
def test_every_plain_example_is_a_line_the_parser_accepts(label):
    PLAIN_PARSERS[label](hints.for_label(label).example)


def test_the_profile_example_carries_every_field():
    """Beyond parsing: the example has to *show* all four fields. One that omits the
    field a slice added is how `profile` was undiscoverable in the first place."""
    p = parse_profile(hints.for_label("profile").example)
    assert p.height_cm and p.sex and p.birthday and p.activity


def test_the_profile_grammar_names_every_ordinary_day_level():
    """The closed vocabulary is written down rather than tab-completed, so the
    grammar line is the only place it can be discovered."""
    from daylogs.body import ACTIVITY_LEVELS

    grammar = hints.for_label("profile").grammar
    for level in ACTIVITY_LEVELS:
        assert level in grammar, f"{level!r} is undiscoverable: {grammar!r}"


def test_profile_declares_no_bare_word_vocabulary():
    """Guards a deliberate omission. Declaring the empty sigil would make `complete`
    treat "180", "male" and a birthday as level candidates, and `refresh_candidates`
    replaces the grammar with "no match" when nothing matches — so three of the four
    fields would read as rejected while being typed correctly."""
    assert hints.vocab_for(hints.for_label("profile")) == {}


def test_labels_without_a_parser_are_deliberate_not_forgotten():
    """Keeps the parser map honest: a new grammar-backed prompt must be added to
    PARSERS rather than quietly landing in the free-text bucket."""
    covered = set(PARSERS) | set(PLAIN_PARSERS) | NO_PARSER
    uncovered = sorted({h.label for h in hints.HINTS} - covered)
    assert not uncovered, f"classify these in PARSERS, PLAIN_PARSERS or NO_PARSER: {uncovered}"


def test_the_parser_tables_name_no_prompt_that_stopped_existing():
    """The other direction, which nothing checked. `theme` sat in NO_PARSER for a release
    after both its prompt and `themes.check` were deleted, describing a function that no
    longer existed — and since the assertion above only subtracts, a stale extra entry was
    invisible. A label that lingers here is a slot a future prompt of the same name would
    silently inherit, with its example unchecked by anything."""
    labels = {h.label for h in hints.HINTS}
    stale = sorted((set(PARSERS) | set(PLAIN_PARSERS) | NO_PARSER) - labels)
    assert not stale, f"these are classified but no prompt has them any more: {stale}"


def test_labels_in_source_finds_both_call_shapes():
    text = 'self.app.prompt.open("weigh")\nself.prompt.open("filter", self.view.filter_text)\n'
    assert hints.labels_in_source(text) == {"weigh", "filter"}


def test_labels_in_source_handles_a_multiline_call():
    text = 'self.app.prompt.open(\n    "confirm food", f"{x} {y}"\n)\n'
    assert hints.labels_in_source(text) == {"confirm food"}


# ── sigil vocabularies ──────────────────────────────────────────────────────
def test_expense_offers_categories_for_bang():
    v = hints.vocab_for(hints.for_label("expense"))
    assert "grocery" in v["!"]


def test_recurring_offers_both_categories_and_cycles():
    v = hints.vocab_for(hints.for_label("recurring"))
    assert "subscriptions" in v["!"]
    assert v["#"] == ("annually", "monthly")


def test_weigh_offers_no_vocabulary():
    assert hints.vocab_for(hints.for_label("weigh")) == {}


def test_fix_category_completes_the_sigil_the_parser_actually_needs():
    """It used to complete on the empty sigil, on the grounds that "its whole input is a
    slug". Its whole input is an *expense line* — the prompt re-enters `_submit_expense` —
    so the bare word the completion offered landed in the description and fell back to
    `other` a second time, silently. `!` is the token that changes the category."""
    v = hints.vocab_for(hints.for_label("fix category"))
    assert "grocery" in v["!"]
    assert "" not in v, "a bare completion here writes the slug into the description"


def test_categories_come_from_config_at_runtime(make_cfg):
    cfg = make_cfg(extra_categories=(("gym", "Gym", ""),))
    v = hints.vocab_for(hints.for_label("expense"), cfg)
    assert "gym" in v["!"]


def test_every_sigil_named_by_a_hint_is_a_real_sigil():
    from daylogs.sigil import SIGILS

    for h in hints.HINTS:
        for s in h.sigils:
            assert s == "" or s in SIGILS, f"{h.label} names {s!r}"


def test_a_hint_that_declares_a_sigil_names_it_and_shows_it():
    """The three-way check. An example that parses is not an example that is right:
    `12.40 lunch restaurant` parsed fine and filed under `other`."""
    for h in hints.HINTS:
        for s in h.sigils:
            if s == "":  # fix category's whole input is the value; no sigil to show
                continue
            assert s in h.grammar, f"{h.label}: accepts {s} but the grammar never names it"
            assert s in h.example, f"{h.label}: accepts {s} but the example omits it"


def test_an_example_that_uses_a_sigil_has_it_named_in_the_grammar():
    """The other direction, for the grammar-parsed prompts only. `photo path` is
    exempt: its `~/Downloads/...` is a home-directory tilde, and that prompt is not
    parsed by the grammar at all."""
    from daylogs import sigil

    for label in PARSERS:
        h = hints.for_label(label)
        for tok in sigil.tokenize(h.example):
            if tok.sigil:
                assert (
                    tok.sigil in h.grammar
                ), f"{h.label}: example uses {tok.sigil}, grammar does not"


def test_no_hint_declares_a_vocabulary_it_cannot_resolve():
    """`vocab_for` had a `label == "theme"` branch that returned Textual's theme list,
    because that prompt completed a bare word and `fix category` completes a different
    one — the empty sigil alone could not decide between them. `T` is a picker now, so
    that branch is gone and the empty sigil means categories again, unambiguously.

    Which makes the general property assertable: every sigil a hint declares resolves to
    a non-empty vocabulary. A hint declaring one that resolves to nothing would make the
    prompt answer "no match" to everything typed into it.
    """
    for h in hints.HINTS:
        vocab = hints.vocab_for(h)
        for sig in h.sigils:
            assert vocab.get(sig), f"{h.label} declares {sig!r} and gets nothing back"


# ── sentences that have to be true where they are read ──────────────────────
def test_the_filter_hint_does_not_promise_a_one_press_clear():
    """It said "esc clears it", and one press does not.

    `escape` inside a prompt is claimed by `InlinePrompt.on_key`, which stops the event,
    closes the prompt and touches nothing else — so the filter is still on after the press
    the sentence is displayed above. Clearing it is the *tab's* `esc` (`key_back` ->
    `MoneyView.back`), one press later. Emptying the line and submitting does not clear it
    either: `on_input_submitted` reads an empty value as a cancel before any tab sees it.
    A hint read inside the prompt has to describe the key as it behaves there.
    """
    grammar = hints.for_label("filter").grammar
    assert "esc again" in grammar, f"the hint still promises one press: {grammar!r}"


def test_the_goto_hint_states_the_rule_resolve_goto_actually_applies():
    """It said "2026-06 for the whole month", which is true on neither tab as written.

    There is one `Hint` per label and `g` is app-scope, so this one sentence is read on all
    three tabs. A bare month resolves to its **last day** and nothing more; "the whole
    month" is what a horizon at least a month wide then makes of that anchor. The Day tab
    has no window at all, so `g 2026-06` answers "no summary for 2026-06-30" — a date the
    user never typed. The sentence therefore states the rule, and the rule is asserted
    against the function that implements it.
    """
    from daylogs.horizon import resolve_goto

    grammar = hints.for_label("go to date").grammar
    assert resolve_goto("2026-06") == "2026-06-30"
    assert "last day" in grammar, grammar
    assert "whole month" not in grammar, f"still promising a window it does not set: {grammar!r}"
