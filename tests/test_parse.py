import datetime as dt
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from daylogs.categories import slugs
from daylogs.fmt import hhmm
from daylogs.parse import (
    ParseError,
    parse_activity,
    parse_budget,
    parse_expense,
    parse_food,
    parse_profile,
    parse_recurring,
    parse_weigh,
    render_activity,
    render_budget,
    render_expense,
    render_food,
    render_recurring,
    render_weigh,
    resolve_when,
)

TZ_NAME = "America/Toronto"
TZ = ZoneInfo(TZ_NAME)
NOW = datetime(2026, 8, 27, 19, 40, tzinfo=TZ)
SLUGS = slugs()


def W(raw):
    return parse_weigh(raw, now=NOW)


def F(raw):
    return parse_food(raw, now=NOW)


def E(raw):
    return parse_expense(raw, now=NOW, known_slugs=SLUGS)


def B(raw):
    return parse_budget(raw, known_slugs=SLUGS)


def R(raw):
    return parse_recurring(raw, known_slugs=SLUGS)


# ── weigh ────────────────────────────────────────────────────────────────
def test_weigh_reads_a_bare_number():
    assert W("78.2").kg == 78.2


def test_weigh_plain_text_is_the_note():
    r = W("78.2 post-run")
    assert (r.kg, r.note) == (78.2, "post-run")


def test_a_weigh_note_containing_a_time_survives():
    """This used to lose the 6:50 AND re-stamp the timestamp to 06:50 — two fields
    corrupted by one entry."""
    r = W("79.4 weighed at 6:50 before food")
    assert r.note == "weighed at 6:50 before food"
    assert datetime.fromtimestamp(r.at, TZ).strftime("%H:%M") == NOW.strftime("%H:%M")


def test_a_weigh_note_starting_with_a_number_survives():
    assert W("80 80 was the goal").note == "80 was the goal"


def test_weigh_takes_a_time():
    r = W("78.2 post-run @07:30")
    assert datetime.fromtimestamp(r.at, TZ).strftime("%H:%M") == "07:30"


def test_weigh_rejects_a_tilde_and_says_what_it_would_have_set():
    with pytest.raises(ParseError, match="note"):
        W("78.2 ~post-run")


def test_weigh_rejects_unsupported_sigils():
    """Any sigil a grammar does not consume is an error."""
    with pytest.raises(ParseError, match="does not have.*kcal"):
        W("78.2 =610")
    with pytest.raises(ParseError, match="does not have.*category"):
        W("78.2 !grocery")
    with pytest.raises(ParseError, match="does not have.*cycle"):
        W("78.2 #monthly")


def test_weigh_rejects_an_implausible_value():
    with pytest.raises(ParseError, match="plausible weight"):
        W("900")


def test_weigh_needs_a_leading_number():
    with pytest.raises(ParseError, match="78.2"):
        W("heavy")


@pytest.mark.parametrize("bad", ["", "   ", "0", "-5"])
def test_weigh_rejects_empty_and_nonpositive(bad):
    """The guard is `0 < kg <= MAX_KG`; testing only 900 leaves its lower bound and the
    empty line unasserted."""
    with pytest.raises(ParseError):
        W(bad)


def _stamp(date: str, hh: int, mm: int, ss: int = 43) -> int:
    """A stamp carrying seconds, so the round trip has something to lose."""
    y, m, d = (int(x) for x in date.split("-"))
    return int(datetime(y, m, d, hh, mm, ss, tzinfo=TZ).timestamp())


WEIGH_ROWS = [
    dict(kg=78.2, note="post-run", date="2026-08-20",
         measured_at=_stamp("2026-08-20", 7, 5)),
    dict(kg=79.4, note="weighed at 6:50 before food", date="2026-08-22",
         measured_at=_stamp("2026-08-22", 6, 50)),
    dict(kg=80.0, note="80 was the goal", date="2026-08-22",
         measured_at=_stamp("2026-08-22", 10, 40)),
    dict(kg=81.5, note=None, date="2026-08-27",
         measured_at=_stamp("2026-08-27", 0, 0)),
    dict(kg=77.0, note="felt !light", date="2026-08-27",
         measured_at=_stamp("2026-08-27", 23, 59)),
]


@pytest.mark.parametrize("row", WEIGH_ROWS, ids=[str(r["kg"]) for r in WEIGH_ROWS])
def test_weigh_round_trips(row):
    got = W(render_weigh(row, TZ_NAME))
    assert got.kg == row["kg"]
    assert (got.note or None) == row["note"]
    assert got.date == row["date"]
    # The clock time survives too, now that the line carries it. Midnight and 23:59 are
    # in the corpus because an off-by-one in the date/time split lands on exactly those.
    assert hhmm(got.at, TZ_NAME) == hhmm(row["measured_at"], TZ_NAME)


# ── food ─────────────────────────────────────────────────────────────────
def test_food_with_explicit_kcal():
    r = F("chicken salad =610")
    assert (r.description, r.kcal) == ("chicken salad", 610)


def test_food_without_kcal_asks_for_an_estimate():
    assert F("chicken salad").kcal is None


def test_a_description_ending_in_a_number_is_not_a_kcal():
    """`coffee 2` was undecidable under the trailing-integer rule."""
    r = F("coffee 2")
    assert (r.description, r.kcal) == ("coffee 2", None)


def test_a_description_that_is_only_a_number_is_allowed():
    """This used to be rejected outright, which made the row uneditable."""
    r = F("750 =350")
    assert (r.description, r.kcal) == ("750", 350)


def test_food_takes_a_date_and_a_time():
    r = F("oatmeal =300 @06-15/07:30")
    assert r.date == "2026-06-15"
    assert dt.datetime.fromtimestamp(r.at, TZ).strftime("%H:%M") == "07:30"


def test_an_implausible_kcal_is_rejected():
    with pytest.raises(ParseError, match="plausible meal"):
        F("cake =99999")


def test_a_non_numeric_kcal_is_rejected():
    with pytest.raises(ParseError, match="kcal"):
        F("cake =lots")


def test_a_repeated_kcal_is_an_error():
    with pytest.raises(ParseError, match="twice"):
        F("cake =100 =200")


def test_food_needs_a_description():
    with pytest.raises(ParseError, match="describe the food"):
        F("=610")


def test_food_rejects_unsupported_sigils():
    """Food accepts @ and =; anything else is an error."""
    with pytest.raises(ParseError, match="does not have.*category"):
        F("chicken salad !restaurant =610")
    with pytest.raises(ParseError, match="does not have.*note"):
        F("chicken salad ~a note =610")
    with pytest.raises(ParseError, match="does not have.*cycle"):
        F("chicken salad #monthly =610")


FOOD_ROWS = [
    dict(description="oatmeal with berries", kcal=350, date="2026-08-20", ate_at=1787223943),
    dict(description="coffee 2", kcal=5, date="2026-08-20", ate_at=1787223943),
    dict(description="750", kcal=350, date="2026-08-20", ate_at=1787223943),
    dict(description="soup !and salad", kcal=400, date="2026-08-20", ate_at=1787223943),
]


@pytest.mark.parametrize("row", FOOD_ROWS, ids=[r["description"][:14] for r in FOOD_ROWS])
def test_food_round_trips(row):
    got = F(render_food(row, TZ_NAME))
    assert got.description == row["description"]
    assert got.kcal == row["kcal"]
    assert got.date == row["date"]
    # To the minute. This assertion was impossible until the zone became an argument:
    # `render_food` used the machine's zone while `F` parses in Toronto, so it held only
    # on a Toronto machine. The seconds still cannot survive — the grammar's only time
    # token is HH:MM — which is what `body.restamp` exists to protect.
    assert dt.datetime.fromtimestamp(got.at, TZ).strftime("%Y-%m-%d %H:%M") == (
        dt.datetime.fromtimestamp(row["ate_at"], TZ).strftime("%Y-%m-%d %H:%M")
    )


# ── expense ──────────────────────────────────────────────────────────────
def test_expense_reads_amount_description_and_category():
    r = E("12.40 lunch !restaurant")
    assert (r.amount, r.description, r.category) == (12.40, "lunch", "restaurant")


def test_the_amount_is_strictly_the_first_token():
    """Not "the first number found anywhere" — strictly first. That is what makes a
    numeric word inside a description safe."""
    r = E("127 750 shelf !grocery")
    assert r.amount == 127.0
    assert r.description == "750 shelf"


def test_a_missing_amount_names_a_valid_line():
    with pytest.raises(ParseError, match="12.40"):
        E("lunch !restaurant")


def test_plain_tokens_join_in_order_wherever_the_sigil_sits():
    assert E("127 Grocery !grocery Item X").description == "Grocery Item X"
    assert E("127 Grocery Item X !grocery").description == "Grocery Item X"


def test_a_description_containing_a_category_word_survives():
    """The headline bug: this used to store category=grocery and description
    "lunch at store restaurant"."""
    r = E("84.10 lunch at grocery store !restaurant")
    assert r.category == "restaurant"
    assert r.description == "lunch at grocery store"


def test_a_description_containing_a_time_survives():
    """`19:30` used to be consumed as the time of day."""
    r = E("60 dinner 19:30 reservation !restaurant")
    assert r.description == "dinner 19:30 reservation"


def test_a_description_that_is_a_category_word_survives():
    r = E("40 Restaurant Depot supplies !grocery")
    assert r.category == "grocery"
    assert r.description == "Restaurant Depot supplies"


def test_no_category_falls_back_to_other():
    """The escape hatch: record now, classify later. money_tab re-opens the prompt
    labelled `fix category`."""
    assert E("12.40 lunch").category == "other"


def test_an_unknown_category_names_the_valid_ones():
    with pytest.raises(ParseError, match="grocery"):
        E("12.40 lunch !pizza")


def test_a_repeated_category_is_an_error():
    with pytest.raises(ParseError, match="twice"):
        E("12.40 lunch !grocery !restaurant")


def test_a_note_may_contain_spaces():
    r = E("127 shelf !grocery ~receipt in wallet")
    assert r.note == "receipt in wallet"
    assert r.description == "shelf"


def test_a_note_stops_at_the_next_sigil():
    r = E("127 shelf ~receipt in wallet !grocery")
    assert (r.note, r.category, r.description) == ("receipt in wallet", "grocery", "shelf")


def test_no_note_is_none():
    assert E("12.40 lunch !restaurant").note is None


def test_a_date_may_be_given():
    assert E("12.40 lunch !restaurant @2026-06-15").date == "2026-06-15"


def test_a_negative_amount_is_a_refund():
    assert E("-24.99 returned shoes !grocery").amount == -24.99


def test_a_zero_amount_is_rejected():
    with pytest.raises(ParseError, match="non-zero"):
        E("0 lunch !restaurant")


def test_a_missing_description_is_rejected():
    with pytest.raises(ParseError, match="say what it was"):
        E("12.40 !restaurant")


def test_a_formatted_amount_is_accepted():
    assert E("$1,240.50 rent !housing").amount == 1240.50


def test_a_decimal_comma_is_still_rejected():
    with pytest.raises(ParseError, match="use a dot for decimals"):
        E("12,40 lunch !restaurant")


def test_an_escaped_sigil_stays_in_the_description():
    assert E(r"12.40 \!important thing !grocery").description == "!important thing"


def test_expense_rejects_unsupported_sigils():
    """Expense accepts `!`, `@`, `~` and `#`; anything else is an error."""
    with pytest.raises(ParseError, match="does not have.*kcal"):
        E("12.40 lunch !restaurant =610")


def test_expense_takes_a_month_count_after_hash_not_a_cycle_keyword():
    """`#` is a supported sigil on an expense now, so `#monthly` is no longer rejected as an
    unknown sigil — it is rejected for its *value*, which is a different error and a more
    useful one.

    The keyword belongs to the recurring grammar, where `#` names the cycle an item renews
    on. On an expense the same sigil answers "over what period does this cost apply", and a
    prepayment is any run of months — a two-year domain, a six-month pass — so it takes a
    count rather than a closed vocabulary. `vocab_for` therefore does not offer `#`
    completion on the expense prompt, or it would suggest exactly this mistake.
    """
    with pytest.raises(ParseError, match="number of months"):
        E("12.40 lunch !restaurant #monthly")


# ── expense round trip ───────────────────────────────────────────────────
EXPENSE_ROWS = [
    dict(amount=12.40, description="lunch", category="restaurant", date="2026-08-20", note=None),
    dict(amount=84.10, description="lunch at grocery store", category="restaurant",
         date="2026-08-20", note=None),
    dict(amount=40.0, description="Restaurant Depot supplies", category="grocery",
         date="2026-08-20", note=None),
    dict(amount=60.0, description="dinner 19:30 reservation", category="restaurant",
         date="2026-08-20", note=None),
    dict(amount=-24.99, description="returned shoes", category="grocery",
         date="2026-01-02", note=None),
    dict(amount=127.0, description="750", category="grocery", date="2026-08-20",
         note="receipt in wallet"),
    dict(amount=9.0, description="buy !milk", category="grocery", date="2026-08-20",
         note="on the !corner"),
    dict(amount=5.0, description="50% off bin", category="other", date="2026-08-20", note=None),
]


@pytest.mark.parametrize("row", EXPENSE_ROWS, ids=[r["description"][:18] for r in EXPENSE_ROWS])
def test_expense_round_trips(row):
    """parse(render(row)) == row. This is the property that lets one grammar serve
    both entry and editing."""
    got = E(render_expense(row))
    for field in ("amount", "description", "category", "date", "note"):
        assert getattr(got, field) == row[field], field


# ── budget ───────────────────────────────────────────────────────────────
def test_budget_name_defaults_to_the_category_display():
    r = B("500 !grocery")
    assert (r.amount, r.name, r.category) == (500.0, "Grocery", "grocery")


def test_budget_takes_an_explicit_name():
    r = B("500 household staples !grocery")
    assert (r.name, r.category) == ("household staples", "grocery")


def test_a_budget_name_containing_a_category_word_survives():
    r = B("300 restaurant fund !grocery")
    assert (r.name, r.category) == ("restaurant fund", "grocery")


def test_budget_requires_a_category():
    with pytest.raises(ParseError, match="!grocery"):
        B("500 household staples")


def test_budget_requires_a_positive_amount():
    with pytest.raises(ParseError, match="positive"):
        B("0 !grocery")
    with pytest.raises(ParseError, match="positive"):
        B("-5 !grocery")


def test_budget_rejects_unsupported_sigils():
    """Budget accepts only !; anything else is an error."""
    with pytest.raises(ParseError, match="does not have.*cycle"):
        B("500 rent !housing #monthly")
    with pytest.raises(ParseError, match="does not have.*note"):
        B("500 rent !housing ~paid by card")
    with pytest.raises(ParseError, match="does not have.*date"):
        B("500 rent !housing @2026-01-01")


BUDGET_ROWS = [
    dict(amount=500.0, name="Grocery", category="grocery"),
    dict(amount=300.0, name="restaurant fund", category="grocery"),
    dict(amount=200.0, name="household staples", category="other"),
    dict(amount=75.0, name="July only line", category="other"),
    dict(amount=10000.55, name="large budget", category="other"),  # :g would render 10000.5
    dict(amount=999999.99, name="huge budget", category="other"),  # :g would render 1e+06
]


@pytest.mark.parametrize("row", BUDGET_ROWS, ids=[r["name"][:14] for r in BUDGET_ROWS])
def test_budget_round_trips(row):
    got = B(render_budget(row))
    assert (got.amount, got.name, got.category) == (row["amount"], row["name"], row["category"])


# ── recurring ────────────────────────────────────────────────────────────
def test_recurring_reads_cost_name_category_and_cycle():
    r = R("20.99 Streaming !subscriptions #monthly")
    assert (r.cost, r.name, r.category, r.cycle) == (20.99, "Streaming", "subscriptions", "monthly")


def test_the_cycle_defaults_to_monthly():
    assert R("20.99 Streaming !subscriptions").cycle == "monthly"


def test_a_name_containing_a_cycle_keyword_survives():
    """The worst case of the old grammar: the keyword was eaten out of the name AND
    it overwrote the cycle, and because the write path upserted by name the mangled
    result INSERTED a second row."""
    r = R("88 Insurance billed annually !other #monthly")
    assert r.name == "Insurance billed annually"
    assert r.cycle == "monthly"


def test_a_name_that_is_only_a_cycle_keyword_survives():
    assert R("10 Annually !other").name == "Annually"


def test_a_name_containing_a_category_word_survives():
    r = R("60 grocery box !grocery")
    assert (r.name, r.category) == ("grocery box", "grocery")


def test_an_unknown_cycle_names_the_valid_ones():
    with pytest.raises(ParseError, match="monthly"):
        R("9.99 Streaming !subscriptions #weekly")


def test_the_cycle_is_case_insensitive():
    assert R("9.99 S !subscriptions #Monthly").cycle == "monthly"


def test_recurring_requires_a_name():
    with pytest.raises(ParseError, match="give it a name"):
        R("20.99 !subscriptions")


def test_recurring_requires_a_positive_cost():
    with pytest.raises(ParseError, match="positive"):
        R("0 Streaming !subscriptions")


def test_recurring_rejects_unsupported_sigils():
    """Recurring accepts ! and #; anything else is an error."""
    with pytest.raises(ParseError, match="does not have.*date"):
        R("88 Insurance !other #monthly @2026-01-01")
    with pytest.raises(ParseError, match="does not have.*note"):
        R("88 Insurance !other #monthly ~paid by card")
    with pytest.raises(ParseError, match="does not have.*kcal"):
        R("88 Insurance !other #monthly =610")


RECURRING_ROWS = [
    dict(cost=20.99, name="Streaming", category="subscriptions", cycle="monthly"),
    dict(cost=120.0, name="Transit Pass", category="transport", cycle="annually"),
    dict(cost=88.0, name="Insurance billed annually", category="other", cycle="monthly"),
    dict(cost=10.0, name="Annually", category="other", cycle="monthly"),
    dict(cost=60.0, name="grocery box", category="grocery", cycle="monthly"),
    # :g would render 10000.5
    dict(cost=10000.55, name="large recurring", category="other", cycle="monthly"),
    # :g would render 1e+06
    dict(cost=999999.99, name="huge recurring", category="other", cycle="monthly"),
]


@pytest.mark.parametrize("row", RECURRING_ROWS, ids=[r["name"][:14] for r in RECURRING_ROWS])
def test_recurring_round_trips(row):
    got = R(render_recurring(row))
    for field in ("cost", "name", "category", "cycle"):
        assert getattr(got, field) == row[field], field


# ── profile ──────────────────────────────────────────────────────────────


def test_profile_reads_all_three_in_one_go():
    p = parse_profile("180 male 1990-01-01")
    assert (p.height_cm, p.sex, p.birthday) == (180.0, "male", "1990-01-01")


def test_profile_is_order_free():
    """Recognised by shape, so there is no argument order to remember."""
    a = parse_profile("1990-01-01 female 165")
    b = parse_profile("165 1990-01-01 female")
    assert a == b


def test_profile_accepts_a_partial_update():
    p = parse_profile("181")
    assert p.height_cm == 181.0
    assert p.sex is None and p.birthday is None
    assert p.fields() == {"height_cm": 181.0}


def test_profile_accepts_a_cm_suffix():
    assert parse_profile("175cm").height_cm == 175.0


def test_profile_accepts_short_sex_forms():
    assert parse_profile("m").sex == "male"
    assert parse_profile("f").sex == "female"


def test_profile_accepts_a_fractional_height():
    assert parse_profile("180.5").height_cm == 180.5


def test_profile_rejects_an_empty_line():
    with pytest.raises(ParseError):
        parse_profile("   ")


def test_profile_rejects_an_implausible_height():
    with pytest.raises(ParseError, match="plausible height"):
        parse_profile("12")
    with pytest.raises(ParseError, match="plausible height"):
        parse_profile("400")


def test_profile_rejects_an_impossible_date():
    with pytest.raises(ParseError, match="not a real date"):
        parse_profile("1990-13-01")


def test_profile_names_the_token_it_could_not_place():
    with pytest.raises(ParseError, match="purple"):
        parse_profile("purple")


def test_profile_fields_omits_what_was_not_given():
    assert parse_profile("female").fields() == {"sex": "female"}


def test_profile_is_pure_and_needs_no_clock():
    """Every other grammar takes `now`; this one has nothing time-relative in it,
    so it must not acquire a hidden dependency on the clock."""
    import inspect

    assert "now" not in inspect.signature(parse_profile).parameters


# ── amounts: a comma is a thousands separator, never a decimal point ──────


def test_a_decimal_comma_is_rejected_rather_than_silently_multiplied():
    """`12,40` used to parse as 1240 — a $12.40 lunch recorded as $1,240.00 with no
    complaint. Weights escaped the consequence only because 782 kg fails a
    plausibility check that expenses have no equivalent of."""
    with pytest.raises(ParseError, match="use a dot for decimals"):
        E("12,40 lunch restaurant")


def test_the_decimal_comma_error_suggests_the_right_line():
    with pytest.raises(ParseError, match=r"12\.40"):
        E("12,40 lunch restaurant")


def test_thousands_separators_still_work():
    assert E("$1,240.50 rent housing").amount == 1240.5
    assert E("1,240 rent housing").amount == 1240.0


def test_a_grouped_thousands_amount_with_several_groups_works():
    assert E("1,234,567.89 house housing").amount == 1234567.89


def test_a_decimal_comma_in_a_weight_is_rejected_too():
    with pytest.raises(ParseError, match="use a dot for decimals"):
        W("78,2")


# ── @ shape resolution ──────────────────────────────────────────────────────
def test_when_defaults_to_now():
    w = resolve_when([], now=NOW)
    assert w.date == NOW.date().isoformat()


def test_when_accepts_a_full_date():
    assert resolve_when(["2026-06-15"], now=NOW).date == "2026-06-15"


def test_when_accepts_a_month_day_in_the_current_year():
    assert resolve_when(["06-15"], now=NOW).date == "2026-06-15"


def test_when_accepts_a_time_and_keeps_todays_date():
    w = resolve_when(["07:30"], now=NOW)
    assert w.date == NOW.date().isoformat()
    assert datetime.fromtimestamp(w.at, TZ).strftime("%H:%M") == "07:30"


def test_when_accepts_a_combined_date_and_time():
    w = resolve_when(["06-15/07:30"], now=NOW)
    assert w.date == "2026-06-15"
    assert datetime.fromtimestamp(w.at, TZ).strftime("%H:%M") == "07:30"


def test_a_date_and_a_time_may_arrive_as_two_tokens():
    w = resolve_when(["06-15", "07:30"], now=NOW)
    assert w.date == "2026-06-15"
    assert datetime.fromtimestamp(w.at, TZ).strftime("%H:%M") == "07:30"


def test_back_dating_without_a_time_keeps_the_wall_clock():
    """Collapsing to midnight would misreport a weigh-in."""
    w = resolve_when(["06-15"], now=NOW)
    assert datetime.fromtimestamp(w.at, TZ).strftime("%H:%M") == NOW.strftime("%H:%M")


def test_two_dates_is_an_error():
    with pytest.raises(ParseError, match="date twice"):
        resolve_when(["06-15", "07-20"], now=NOW)


def test_two_times_is_an_error():
    with pytest.raises(ParseError, match="time twice"):
        resolve_when(["07:30", "08:30"], now=NOW)


def test_an_unparseable_when_names_the_shapes():
    with pytest.raises(ParseError, match="@2026-08-24"):
        resolve_when(["june"], now=NOW)


def test_an_impossible_date_is_rejected():
    with pytest.raises(ParseError, match="not a real date"):
        resolve_when(["02-31"], now=NOW)


def test_an_impossible_time_is_rejected():
    with pytest.raises(ParseError, match="not a valid time"):
        resolve_when(["25:00"], now=NOW)


# ── activity ─────────────────────────────────────────────────────────────
# One field, `=`, carrying the whole day's PAL multiplier. It accepts a level keyword
# as well as a number because a raw PAL is unreadable to a human, and the four
# keywords are already this app's vocabulary for the same quantity.


def A(raw):
    return parse_activity(raw, now=NOW)


def test_activity_reads_what_you_did():
    r = A("gym 1h")
    assert (r.description, r.factor) == ("gym 1h", None)


def test_omitting_the_factor_asks_for_an_estimate():
    """The same shape food uses: no `=` means Claude is asked, and the answer comes
    back in a confirm prompt rather than being written blind."""
    assert A("gym 1h and a long walk").factor is None


def test_a_level_keyword_resolves_to_its_multiplier():
    assert A("gym 1h =active").factor == 1.55
    assert A("desk all day =desk").factor == 1.2


def test_a_level_keyword_is_case_insensitive():
    assert A("gym =ACTIVE").factor == 1.55


def test_a_number_is_taken_as_the_multiplier():
    assert A("gym 1h =1.45").factor == 1.45


def test_a_three_decimal_factor_survives():
    """`light` is 1.375. `to_amount` would reject a third decimal place, which is
    correct for money and wrong for a PAL, so the factor has its own rule."""
    assert A("errands =1.375").factor == 1.375


def test_the_range_ends_are_accepted():
    assert A("in bed =1.2").factor == 1.2
    assert A("race day =1.9").factor == 1.9


@pytest.mark.parametrize("bad", ["1.19", "1.91", "4.0", "0", "-1.4", "inf", "nan"])
def test_a_factor_outside_the_physiological_range_is_rejected(bad):
    """A wrong factor does not misreport one row — it rescales every calorie
    judgement for the day, so it is rejected at the grammar as well as at the write."""
    with pytest.raises(ParseError, match="1.2"):
        A(f"gym ={bad}")


def test_a_decimal_comma_is_rejected_with_the_dot_spelled_out():
    """The same rule amounts follow. `=1,45` silently becoming 145 would be a factor
    a hundred times too large."""
    with pytest.raises(ParseError, match=r"1\.45"):
        A("gym =1,45")


def test_a_word_that_is_not_a_level_is_rejected():
    with pytest.raises(ParseError, match="desk"):
        A("gym =hard")


def test_a_repeated_factor_is_an_error():
    with pytest.raises(ParseError, match="twice"):
        A("gym =1.4 =1.6")


def test_activity_needs_a_description():
    with pytest.raises(ParseError, match="what you did"):
        A("=1.4")


def test_activity_takes_a_date_and_a_time():
    r = A("gym 1h @2026-08-20/07:30")
    assert r.date == "2026-08-20"
    assert datetime.fromtimestamp(r.at, TZ).strftime("%H:%M") == "07:30"


def test_activity_rejects_unsupported_sigils():
    with pytest.raises(ParseError, match="does not have.*category"):
        A("gym !restaurant")
    with pytest.raises(ParseError, match="does not have.*note"):
        A("gym ~felt good")
    with pytest.raises(ParseError, match="does not have.*cycle"):
        A("gym #monthly")


ACTIVITY_ROWS = [
    dict(description="gym 1h", factor=1.45, date="2026-08-20", logged_at=1787223943),
    dict(description="errands", factor=1.375, date="2026-08-20", logged_at=1787223943),
    dict(description="in bed, unwell", factor=1.2, date="2026-08-20", logged_at=1787223943),
    # A row whose inference never landed. It has to round-trip too, or opening the
    # edit prompt on it would invent a factor.
    dict(description="long walk", factor=None, date="2026-08-20", logged_at=1787223943),
    # The description is the escaping hazard: a leading sigil, and a bare number.
    dict(description="=squats", factor=1.5, date="2026-08-20", logged_at=1787223943),
    dict(description="10000", factor=1.5, date="2026-08-20", logged_at=1787223943),
]


@pytest.mark.parametrize(
    "row", ACTIVITY_ROWS, ids=[str(r["description"])[:14] for r in ACTIVITY_ROWS]
)
def test_activity_round_trips(row):
    got = A(render_activity(row, TZ_NAME))
    assert got.description == row["description"]
    assert got.factor == row["factor"]
    assert got.date == row["date"]
    # To the minute, now that `render_activity` takes the zone it will be parsed in.
    # This used to be unassertable: the render read the *machine's* zone while the parse
    # resolved in the configured one, so it held only where those agreed.
    #
    # The seconds still cannot survive — the grammar's only time token is `HH:MM` — and
    # they matter, because `logged_at` is the tie-breaker `resolved_factor` uses to pick
    # a day's latest inference. That is what `body.restamp` protects: it returns None
    # when the minute did not move rather than rewriting the column.
    assert dt.datetime.fromtimestamp(got.at, TZ).strftime("%Y-%m-%d %H:%M") == (
        dt.datetime.fromtimestamp(row["logged_at"], TZ).strftime("%Y-%m-%d %H:%M")
    )


# ── a weigh-in's clock time is now in the line ───────────────────────────
# The weight table shows `time` because a day weighed twice rendered as two rows both
# reading `2026-09-04`, indistinguishable — while `measured_at` was the tie-breaker
# `weight_series_between` used to pick between them. What you can see is what you can edit, so
# the time had to become editable at the same moment it became visible.
#
# `render_weigh`'s old comment said re-deriving the stamp "would shave the seconds off
# every edit". That is what `body.restamp` exists to prevent — its own docstring names
# weight as the motivating case — and it was simply never wired up here.

# 2026-08-27 07:05:43 in Toronto is 11:05:43 UTC — the same stamp test_timezone.py uses,
# so a zone bug shows up as the same four-hour shift in both files.
_TZ = TZ_NAME
STAMP = int(datetime(2026, 8, 27, 7, 5, 43, tzinfo=TZ).timestamp())


def _weigh_row(**kw):
    base = dict(kg=78.2, date="2026-08-27", measured_at=STAMP, note=None)
    return {**base, **kw}


def test_render_weigh_carries_the_reading_time():
    line = render_weigh(_weigh_row(), _TZ)
    assert "@2026-08-27/07:05" in line, line


def test_a_weigh_in_round_trips_to_the_minute():
    row = _weigh_row(note="post-run")
    got = parse_weigh(render_weigh(row, _TZ), now=NOW)
    assert got.kg == 78.2
    assert got.note == "post-run"
    assert got.date == "2026-08-27"
    assert hhmm(got.at, _TZ) == hhmm(STAMP, _TZ)


def test_rendering_a_weigh_in_is_stable_under_a_round_trip():
    """The property the edit prompt needs: open it twice, submit unchanged twice, and
    nothing drifts. Asserted as stability rather than instant-preservation for the same
    reason `render_food`'s is — the grammar's only time token is HH:MM."""
    row = _weigh_row(note="post-run")
    once = render_weigh(row, _TZ)
    parsed = parse_weigh(once, now=NOW)
    twice = render_weigh(
        _weigh_row(kg=parsed.kg, date=parsed.date, measured_at=parsed.at, note=parsed.note),
        _TZ,
    )
    assert once == twice


def test_the_time_is_rendered_in_the_zone_it_is_given():
    """Same trap `render_food` documents: rendering in the machine's zone while the
    parser resolves in the configured one moved every edited row by the offset."""
    assert "07:05" in render_weigh(_weigh_row(), "America/Toronto")
    assert "11:05" in render_weigh(_weigh_row(), "UTC")
