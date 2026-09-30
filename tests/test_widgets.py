import pytest

from daylogs.tui.widgets import (
    BAD,
    arrow,
    budget_bars,
    burn_bar,
    mark,
    money,
    ranked_bars,
    shade,
    signed,
    sparkline,
    trend_style,
    wide_sparkline,
)


def test_sparkline_empty_and_single():
    assert sparkline([]) == ""
    assert len(sparkline([5.0])) == 1


def test_sparkline_length_matches_input_up_to_width():
    assert len(sparkline([1, 2, 3, 4, 5])) == 5
    assert len(sparkline(list(range(100)), width=10)) == 10


def test_sparkline_monotonic_input_is_monotonic_output():
    blocks = "▁▂▃▄▅▆▇█"
    out = sparkline([1, 2, 3, 4, 5, 6, 7, 8])
    idx = [blocks.index(c) for c in out]
    assert idx == sorted(idx)
    assert idx[0] == 0 and idx[-1] == len(blocks) - 1


def test_sparkline_flat_input_does_not_divide_by_zero():
    out = sparkline([4.0, 4.0, 4.0])
    assert len(out) == 3 and len(set(out)) == 1


def test_sparkline_handles_negatives():
    assert len(sparkline([-2.0, 0.0, 2.0])) == 3


def test_sparkline_keeps_the_most_recent_values_when_truncating():
    out = sparkline([0, 0, 0, 1, 9], width=2)
    assert len(out) == 2
    assert out[-1] == "█"


def test_burn_bar_proportion_and_bounds():
    assert burn_bar(0, 100, width=10).count("█") == 0
    assert burn_bar(50, 100, width=10).count("█") == 5
    assert burn_bar(100, 100, width=10).count("█") == 10
    assert burn_bar(250, 100, width=10).count("█") == 10


def test_burn_bar_zero_budget_is_empty_not_a_crash():
    assert "█" not in burn_bar(50, 0, width=10)


def test_burn_bar_negative_spend_does_not_underflow():
    assert burn_bar(-50, 100, width=10).count("█") == 0


def test_burn_bar_has_fixed_width():
    assert len(burn_bar(30, 100, width=17)) == 17
    assert len(burn_bar(30, 100, width=17, marker_frac=0.5)) == 17


def test_burn_bar_marker_sits_at_calendar_progress():
    bar = burn_bar(0, 100, width=10, marker_frac=0.5)
    assert "┃" in bar
    assert bar.index("┃") == 5


def test_burn_bar_marker_clamped_inside_the_bar():
    assert burn_bar(0, 100, width=10, marker_frac=1.0).index("┃") == 9
    assert burn_bar(0, 100, width=10, marker_frac=0.0).index("┃") == 0


def test_money_formats_thousands_and_two_places():
    assert money(1240.5) == "1,240.50"
    assert money(0) == "0.00"
    assert money(-24.99) == "-24.99"


def test_signed_always_shows_a_sign():
    assert signed(88.0) == "+88.00"
    assert signed(-89.0) == "-89.00"
    assert signed(0) == "+0.00"


# ── ranked bars (the pie replacement) ───────────────────────────────────
def test_ranked_bars_one_line_per_item_in_the_given_order():
    lines = ranked_bars([("transport", 1500.0), ("housing", 750.0)], width=80)
    assert len(lines) == 2
    assert lines[0].startswith("transport")
    assert lines[1].startswith("housing")


def test_ranked_bars_shares_sum_to_about_a_hundred():
    items = [("a", 50.0), ("b", 30.0), ("c", 20.0)]
    lines = ranked_bars(items, width=80)
    shares = [float(ln.split("%")[0].split()[-1]) for ln in lines]
    assert sum(shares) == pytest.approx(100.0, abs=0.2)


def test_ranked_bars_biggest_gets_the_longest_bar():
    lines = ranked_bars([("big", 90.0), ("small", 10.0)], width=80)
    assert lines[0].count("█") > lines[1].count("█")


def test_ranked_bars_shows_the_amount():
    assert "1,517.91" in ranked_bars([("transport", 1517.91)], width=80)[0]


def test_ranked_bars_tiny_share_still_gets_a_visible_bar():
    lines = ranked_bars([("big", 9999.0), ("tiny", 1.0)], width=80)
    assert "█" in lines[1]


def test_ranked_bars_empty_input():
    assert ranked_bars([], width=80) == []


def test_ranked_bars_all_zero_does_not_divide_by_zero():
    lines = ranked_bars([("a", 0.0), ("b", 0.0)], width=80)
    assert len(lines) == 2


def test_ranked_bars_respects_width():
    for w in (30, 50, 80):
        assert all(len(ln) <= w for ln in ranked_bars([("transport", 10.0)], width=w))


def test_ranked_bars_truncates_a_long_label():
    line = ranked_bars([("averyveryverylongcategory", 10.0)], width=80, label_width=8)[0]
    assert line.startswith("averyve")


# ── budget bars ─────────────────────────────────────────────────────────
def test_budget_bars_scale_to_each_categorys_own_cap():
    """Half of 500 and half of 100 should look equally full — the question is
    'how close to this limit', not 'how big versus other categories'."""
    lines = budget_bars([("a", 250.0, 500.0), ("b", 50.0, 100.0)], width=80)
    assert lines[0].count("█") == lines[1].count("█")


def test_budget_bars_flag_an_overrun_with_a_glyph():
    lines = budget_bars([("restaurant", 289.0, 200.0)], width=80)
    assert "⚠" in lines[0]


def test_budget_bars_do_not_flag_a_category_inside_budget():
    assert "⚠" not in budget_bars([("grocery", 412.0, 500.0)], width=80)[0]


def test_budget_bars_clamp_an_overrun_to_full():
    line = budget_bars([("restaurant", 900.0, 200.0)], width=80, bar_width=10)[0]
    assert line.count("█") == 10


def test_budget_bars_show_a_dash_when_there_is_no_budget():
    line = budget_bars([("transport", 96.0, 0.0)], width=80)[0]
    assert "—" in line
    assert "⚠" not in line


def test_budget_bars_show_both_numbers():
    line = budget_bars([("grocery", 412.0, 500.0)], width=80)[0]
    assert "412.00" in line and "500.00" in line


def test_budget_bars_empty_input():
    assert budget_bars([], width=80) == []


# ── refunds: a category can go net negative ─────────────────────────────────
# A reimbursed bill or a returned order lands as a negative expense, so these
# are ordinary inputs, not edge cases. Both renderers broke on real August data.


def test_ranked_bars_shares_are_of_gross_spend_not_the_signed_net():
    """With a refund in the list, dividing by the signed sum inflates every other
    share — the denominator shrank by the refund. Shares must still total 100%."""
    items = [("transport", 300.0), ("grocery", 100.0), ("other", -200.0)]
    lines = ranked_bars(items, width=80)
    assert "75.0%" in lines[0]
    assert "25.0%" in lines[1]


def test_ranked_bars_show_no_percentage_for_a_net_negative_category():
    line = ranked_bars([("grocery", 100.0), ("other", -200.0)], width=80)[1]
    assert "%" not in line
    assert "—" in line
    assert "-200.00" in line


def test_ranked_bars_never_exceed_the_bar_width():
    """A single positive row against a refund gives a share above 1.0 if the
    denominator is the net; the bar must still fit its cells."""
    line = ranked_bars([("grocery", 100.0), ("other", -90.0)], width=80, bar_width=10)[0]
    assert line.count("█") == 10


def test_budget_bars_keep_the_numbers_when_spend_is_net_negative():
    """filled went to -44 of 14 cells, and `_EMPTY * (14 - -44)` emitted a 58-char
    bar that pushed the amounts past the width — the row rendered as bare dots."""
    line = budget_bars([("other", -240.00, 200.0)], width=64, bar_width=14)[0]
    assert "-240.00" in line
    assert "200.00" in line
    assert "⚠" not in line


def test_budget_bars_rows_are_uniform_width_across_a_sign_change():
    lines = budget_bars(
        [("other", -240.00, 200.0), ("grocery", 500.00, 700.0)], width=64
    )
    assert len({len(line) for line in lines}) == 1


def test_budget_bars_align_a_budgeted_row_with_an_unbudgeted_one():
    """The `—` placeholder must occupy the same columns as an amount, or the whole
    budget column skews by a character."""
    lines = budget_bars([("grocery", 412.0, 500.0), ("transport", 96.0, 0.0)], width=80)
    assert len({len(line) for line in lines}) == 1


# ── sparkline scaling and width ─────────────────────────────────────────────


def test_sparkline_flat_nonzero_series_sits_mid_scale_not_on_the_floor():
    """Six months of level rent all landed on the lowest glyph, so the
    second-largest spend category rendered as an empty floor."""
    out = sparkline([750.0, 750.0, 750.0])
    assert set(out) == {"▅"}


def test_sparkline_flat_zero_series_stays_on_the_floor():
    assert set(sparkline([0.0, 0.0, 0.0])) == {"▁"}


def test_sparkline_from_zero_keeps_a_steady_series_visible():
    """min..max scaling claims a zero that isn't in the data."""
    out = sparkline([900.0, 950.0, 1000.0], from_zero=True)
    assert out[0] != "▁"


def test_sparkline_from_zero_still_ranks_within_the_series():
    out = sparkline([100.0, 500.0, 1000.0], from_zero=True)
    assert out[0] < out[1] < out[2]


def test_sparkline_from_zero_handles_an_all_zero_series():
    assert set(sparkline([0.0, 0.0], from_zero=True)) == {"▁"}


def test_sparkline_from_zero_tolerates_a_refund():
    """A net-negative month must not blow past the glyph table."""
    out = sparkline([-200.0, 300.0], from_zero=True)
    assert len(out) == 2


def test_wide_sparkline_fills_the_requested_width():
    assert len(wide_sparkline([1, 2, 3, 4, 5, 6], width=24)) == 24


def test_wide_sparkline_gives_each_sample_an_equal_block():
    """Six values across 24 cells is four cells each — that is what makes the
    shape legible where six single cells read as noise."""
    out = wide_sparkline([1, 2, 3, 4, 5, 6], width=24)
    blocks = [out[i * 4:(i + 1) * 4] for i in range(6)]
    assert all(len(set(b)) == 1 for b in blocks)
    assert len(set(b[0] for b in blocks)) == 6


def test_wide_sparkline_preserves_order():
    out = wide_sparkline([1, 5, 9], width=12)
    assert out[0] < out[5] < out[-1]


def test_wide_sparkline_empty_and_zero_width():
    assert wide_sparkline([], width=10) == ""
    assert wide_sparkline([1, 2], width=0) == ""


def test_wide_sparkline_narrower_than_the_sample_count_still_fits():
    assert len(wide_sparkline([1, 2, 3, 4, 5, 6], width=3)) == 3


def test_mark_is_a_noop_without_a_style():
    assert mark("hello") == "hello"
    assert mark("hello", "red") == "[red]hello[/]"


# ── narrow panels: the numbers outrank the bar ──────────────────────────────


def test_budget_bars_keep_the_cap_when_the_panel_is_narrow():
    """At a 42-column panel the line overflowed and `line[:width]` cut the budget
    off, leaving "900.00 /" — the comparison the panel exists to show."""
    line = budget_bars([("housing", 900.0, 900.0)], width=42)[0]
    assert "900.00 /" in line
    assert line.rstrip().endswith("900.00") or "900.00  " in line
    assert len(line) <= 42


def test_ranked_bars_keep_the_amount_when_the_panel_is_narrow():
    line = ranked_bars([("housing", 900.0)], width=42)[0]
    assert "900.00" in line
    assert len(line) <= 42


def test_a_narrow_panel_shrinks_the_bar_rather_than_the_figures():
    wide = budget_bars([("housing", 900.0, 900.0)], width=64)[0]
    narrow = budget_bars([("housing", 900.0, 900.0)], width=42)[0]
    assert narrow.count("█") < wide.count("█")
    for line in (wide, narrow):
        assert "900.00" in line


def test_a_bar_that_cannot_be_meaningful_is_dropped_entirely():
    """A two-cell bar carries no information and only steals columns."""
    line = budget_bars([("housing", 900.0, 900.0)], width=38)[0]
    assert "█" not in line
    assert "900.00" in line


def test_very_narrow_widths_still_produce_a_bounded_line():
    for width in (30, 24, 16, 10):
        for line in budget_bars([("housing", 900.0, 900.0)], width=width):
            assert len(line) <= width
        for line in ranked_bars([("housing", 900.0)], width=width):
            assert len(line) <= width


# ── view_row ─────────────────────────────────────────────────────────────


def test_view_row_marks_only_the_active_name():
    from daylogs.tui.widgets import view_row

    assert view_row(("weight", "food"), "food") == "weight   [b]food[/b]"


def test_view_row_is_what_both_tabs_use():
    """Body and Money must draw this row identically — the point of the shared
    helper is that "they look the same" is true by construction rather than by
    two call sites happening to agree.
    """
    from daylogs.moneyview import PANES
    from daylogs.tui.widgets import view_row

    body = view_row(("weight", "food"), "weight")
    money = view_row(PANES, PANES[0])
    assert body.startswith("[b]") and money.startswith("[b]")
    assert "   " in body and "   " in money


def test_view_row_marks_nothing_when_the_active_name_is_unknown():
    """A typo'd mode should degrade to an unmarked row, not raise in a render."""
    from daylogs.tui.widgets import view_row

    assert view_row(("weight", "food"), "nonsense") == "weight   food"


# ── the arrow is part of the judgement ───────────────────────────────────


def test_an_unchanged_value_gets_a_neutral_arrow():
    """`▲0` for a weight that did not move. Two of the three sites drawing this arrow
    were two-way (`▼` if negative else `▲`), so zero fell into the "up" branch — and
    `trend_style` withholds colour at zero on purpose, which left the wrong glyph as the
    only signal the reader gets."""
    assert arrow(0) == "→"
    assert arrow(0.0) == "→"


def test_a_falling_value_points_down_and_a_rising_one_up():
    assert arrow(-0.4) == "▼"
    assert arrow(0.4) == "▲"


def test_no_value_has_no_arrow():
    assert arrow(None) == ""


def test_the_arrow_and_the_colour_agree_about_zero():
    """They are two halves of one judgement, so they must not disagree: `trend_style`
    already treats zero as neither, and the arrow now does too."""
    assert (arrow(0), trend_style(0)) == ("→", "")


# ── a bar whose segments mean different things ────────────────────────────
# A category's `spent` mixes cash that left this period with a share amortised from a
# payment made elsewhere, and the panel said nothing: a category read 30.00 / 50.00 as one
# green bar with 20.00 of the 30.00 being proration. The glyph is what tells them
# apart; the colour only emphasises it, which is why `shade` is a separate step applied
# after the width arithmetic is already done.
def test_shade_styles_the_run_without_changing_what_is_printed():
    from textual.content import Content

    line = "subscriptions ▒▒▒███···  30.00 /  50.00"
    out = shade(line, "▒", "dim")
    assert out != line, "it has to actually do something"
    assert Content.from_markup(out).plain == line, (
        "markup must not change a single printed cell — the builders already measured"
    )
    spans = [(s.start, s.end, s.style) for s in Content.from_markup(out).spans]
    assert spans == [(14, 17, "dim")], f"exactly the glyph run: {spans}"


def test_shade_composes_inside_a_whole_line_colour():
    """The caller wraps the finished row in its budget-status colour. Textual's markup
    nests, so the segment style has to survive being inside that."""
    from textual.content import Content

    line = "grocery       ▒▒███ 540.69 / 500.00"
    out = mark(shade(line, "▒", "dim"), BAD)
    c = Content.from_markup(out)
    assert c.plain == line
    assert (0, len(line), BAD) in [(s.start, s.end, s.style) for s in c.spans]
    assert (14, 16, "dim") in [(s.start, s.end, s.style) for s in c.spans]


def test_shade_is_a_no_op_without_a_style_or_a_glyph():
    assert shade("abc ███", "▒", "dim") == "abc ███"
    assert shade("abc ▒▒▒", "▒", "") == "abc ▒▒▒"


def test_budget_bars_marks_the_prorated_part_of_the_fill():
    line = budget_bars([("subs", 100.0, 200.0)], width=60,
                       prorated={"subs": 50.0})[0]
    bar = line[14:28]
    assert bar.count("▒") == 4 and bar.count("█") == 3, (
        f"half of a half-full 14-cell bar: {bar!r}"
    )
    assert len(bar) == 14, "the bar keeps its width"


def test_budget_bars_without_prorated_is_exactly_what_it_was():
    """The regression guard: every existing caller passes nothing, and must be untouched."""
    items = [("subs", 100.0, 200.0), ("rent", 750.0, 750.0), ("food", 540.69, 500.0)]
    assert budget_bars(items, width=60) == budget_bars(items, width=60, prorated={})
    assert "▒" not in "".join(budget_bars(items, width=60))


def test_a_tiny_prorated_share_still_gets_a_cell():
    """Same lesson as the from_zero sparkline: a real component that rounds to zero cells
    renders as absent, and absent is a different claim from small."""
    line = budget_bars([("subs", 100.0, 200.0)], width=60, prorated={"subs": 0.40})[0]
    assert "▒" in line, f"0.40 of 200 is under half a cell but is not nothing: {line!r}"
    assert line[14:28].count("▒") == 1


def test_the_prorated_run_never_exceeds_the_filled_run():
    """A share can exceed the drawn fill when the category is over budget and the fill is
    clamped: 300 of a 100 cap is 42 cells of a 14-cell bar.

    Asserted on the **whole line**, not on a slice of it. A slice of the bar region shows 14
    glyphs either way, which is exactly how the first version of this test passed while the
    clamp was removed. The harm is the one `budget_bars` already records for an unclamped
    width — the bar runs long and pushes the amounts off the end of the line.
    """
    line = budget_bars([("subs", 300.0, 100.0)], width=60, prorated={"subs": 300.0})[0]
    assert line.count("▒") == 14, f"14 cells, not 42: {line!r}"
    assert "·" not in line, "a full bar has no empty tail"
    assert "300.00" in line and "100.00" in line, (
        f"the amounts must survive — an over-long bar shoves them past the width: {line!r}"
    )


def test_a_prorated_refund_draws_no_segment(  ):
    """A negative share is a refund. A part-to-whole has no negative slice — the same
    stance `ranked_bars` already takes on a net-negative category."""
    line = budget_bars([("subs", 100.0, 200.0)], width=60, prorated={"subs": -30.0})[0]
    assert "▒" not in line, line


def test_ranked_bars_marks_the_prorated_part_too():
    lines = ranked_bars([("subs", 100.0), ("food", 100.0)], width=60,
                        prorated={"subs": 100.0})
    assert "▒" in lines[0] and "█" not in lines[0][14:30], (
        f"all of subs is prorated: {lines[0]!r}"
    )
    assert "▒" not in lines[1], f"and none of food is: {lines[1]!r}"


def test_ranked_bars_without_prorated_is_exactly_what_it_was():
    items = [("subs", 100.0), ("food", 250.0), ("refund", -10.0)]
    assert ranked_bars(items, width=60) == ranked_bars(items, width=60, prorated={})
