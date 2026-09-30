"""Pure parsers for the footer prompt grammars.

Sigil-based grammar: fields are marked with sigils (`!` category, `@` date/time,
`~` note, `=` kcal, `#` cycle), not scavenged from free text. The amount is
strictly the first token or absent. Whatever is not sigiled remains as plain
text, in order. An unsupported sigil for a given grammar is an error, not silently
discarded.

Nothing here touches the database or the clock — `now` is injected, so every
case is testable and nothing depends on when the suite runs.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from daylogs import money, sigil
from daylogs.body import ACTIVITY_LEVELS, FACTOR_MAX, FACTOR_MIN
from daylogs.categories import FALLBACK_SLUG, get
from daylogs.config import is_zone
from daylogs.fmt import hhmm

_NUM = re.compile(r"^-?\$?[\d,]+(?:\.\d+)?$")
# Plausibility limits. Once shared with a separate edit grammar; now one grammar.
MAX_KG = 500.0
MAX_KCAL = 20000
# The one time-of-day shape the grammar accepts.
TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_WHEN_FULL = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_WHEN_SHORT = re.compile(r"^(\d{2})-(\d{2})$")


@dataclass(frozen=True)
class When:
    date: str  # ISO
    at: int    # epoch seconds


def resolve_when(values: list[str], *, now: dt.datetime) -> When:
    """Resolve `@` values by shape, the way parse_profile resolves its fields.

    A token can only be one of four things, so there is no order to remember and
    nothing to disambiguate. A date and a time may arrive as one combined token or
    as two separate ones; a second date or a second time is an error rather than
    last-wins.
    """
    date: dt.date | None = None
    time: dt.time | None = None

    for value in values:
        for part in value.split("/"):
            if not part:
                continue
            if m := _WHEN_FULL.match(part):
                if date is not None:
                    raise ParseError("@ gave a date twice")
                date = _safe_date(int(m[1]), int(m[2]), int(m[3]))
            elif m := _WHEN_SHORT.match(part):
                if date is not None:
                    raise ParseError("@ gave a date twice")
                date = _safe_date(now.year, int(m[1]), int(m[2]))
            elif m := TIME_RE.match(part):
                if time is not None:
                    raise ParseError("@ gave a time twice")
                hh, mm = int(m[1]), int(m[2])
                if hh > 23 or mm > 59:
                    raise ParseError(f"@{part} is not a valid time")
                time = dt.time(hh, mm)
            else:
                raise ParseError(
                    f"@{part}: try @2026-08-24, @08-24, @14:30 or @08-24/14:30"
                )

    day = date or now.date()
    if time is not None:
        when = dt.datetime.combine(day, time, tzinfo=now.tzinfo)
    elif date is None:
        when = now
    else:
        # Back-dating without a time keeps the current wall-clock time on that
        # date. Silently collapsing to midnight would misreport a weigh-in.
        when = dt.datetime.combine(day, now.timetz())
    return When(date=day.isoformat(), at=int(when.timestamp()))


class ParseError(ValueError):
    """Raised for any input the grammar cannot accept.

    The message is shown verbatim in the prompt, so it reads as guidance
    rather than as a stack trace.
    """


@dataclass(frozen=True)
class WeighInput:
    kg: float
    note: str | None
    date: str
    at: int


@dataclass(frozen=True)
class FoodInput:
    description: str
    kcal: int | None
    date: str
    at: int


@dataclass(frozen=True)
class ActivityInput:
    """`factor` is the whole **day's** PAL, not this activity's own contribution.

    A PAL multiplier describes a day and is not additive, so "gym" plus "walked" is
    not 1.375 + 1.2. `None` means no number was given and one should be inferred —
    the same shape food uses for a missing `=kcal`.
    """

    description: str
    factor: float | None
    date: str
    at: int


@dataclass(frozen=True)
class ExpenseInput:
    amount: float
    description: str
    category: str
    date: str
    note: str | None = None
    # How many months this one payment covers, or None for an ordinary expense. `#12` on an
    # annual renewal. It exists because the budget side already prorates — `roll_month_budgets`
    # writes `monthly_cost` — so without it the renewal month read 12x over its cap and the
    # other eleven read a saving, and no single month was ever right.
    prepaid_months: int | None = None


@dataclass(frozen=True)
class CategoryInput:
    """A category to add. `display` is optional because `all_categories` already falls
    back to the slug, so writing the slug twice would only be noise that goes stale."""

    slug: str
    display: str | None


@dataclass(frozen=True)
class BudgetInput:
    amount: float
    name: str
    category: str


@dataclass(frozen=True)
class RecurringInput:
    cost: float
    name: str
    category: str
    cycle: str


def parse_weigh(raw: str, *, now: dt.datetime) -> WeighInput:
    toks = _tokens(raw)
    kg = _leading_amount(toks, "a weight, e.g. 78.2")
    if not 0 < kg <= MAX_KG:
        raise ParseError(f"{kg:g} kg is not a plausible weight")
    g = sigil.group(toks[1:])
    _reject_unsupported(g, frozenset(["@"]), "weigh-in")
    when = resolve_when(g.by_sigil.get("@", []), now=now)
    return WeighInput(kg=kg, note=g.text or None, date=when.date, at=when.at)


def render_weigh(row, tz: str) -> str:
    """The inverse, for prefilling an edit.

    The time is in the line because the table shows it. A day weighed twice rendered as
    two rows both reading the same date, indistinguishable — while `measured_at` was the
    tie-breaker `weight_series_between` used to pick between them. What you can see is what you
    can edit, so the time became editable in the same change that made it visible.

    This used to emit no time, on the grounds that "re-deriving it from an HH:MM token
    would shave the seconds off every edit". That is exactly what `body.restamp` exists
    to prevent — its own docstring names weight as the motivating case — and it was
    simply never wired up here. The caller restamps only when the minute actually moved.

    `tz` is required and must be the zone the line will be *parsed* in, for the reason
    `render_food` documents: rendering through the machine's zone while the parser
    resolves in the configured one moves the row by the offset.
    """
    parts = [f"{row['kg']:g}"]
    if row["note"]:
        parts.append(sigil.escape(row["note"]))
    parts.append(f"@{row['date']}/{hhmm(row['measured_at'], tz)}")
    return " ".join(parts)


def parse_food(raw: str, *, now: dt.datetime) -> FoodInput:
    """kcal comes only from `=`. A trailing bare integer used to mean calories,
    which made `coffee 2` undecidable between a 2 kcal coffee and a description
    that ends in a number."""
    toks = _tokens(raw)
    g = sigil.group(toks)
    if not g.text:
        raise ParseError("describe the food, e.g. chicken salad =610")
    _reject_unsupported(g, frozenset(["@", "="]), "food entry")
    raw_kcal = _single(g, "=", "kcal")
    kcal: int | None = None
    if raw_kcal is not None:
        if not raw_kcal.isdigit():
            raise ParseError(f"={raw_kcal}: kcal must be a whole number, e.g. =610")
        kcal = int(raw_kcal)
        if kcal > MAX_KCAL:
            raise ParseError(f"{kcal} kcal is not a plausible meal")
    when = resolve_when(g.by_sigil.get("@", []), now=now)
    return FoodInput(description=g.text, kcal=kcal, date=when.date, at=when.at)


def render_food(row, tz: str) -> str:
    """The inverse, for prefilling an edit.

    `tz` is required and must be the zone the line will be *parsed* in, which is
    `cfg.timezone`. It used to render through the machine's zone while `parse_food`
    resolved in the configured one, so on a machine whose zone differed the prefill
    round-trip moved the row by the offset — from a plain `enter` on a food row.
    """
    return " ".join([
        sigil.escape(row["description"]),
        f"={int(row['kcal'])}",
        f"@{row['date']}/{hhmm(row['ate_at'], tz)}",
    ])


def to_factor(value: str) -> float:
    """A whole-day activity multiplier, from a level keyword or a number.

    The keyword form exists because a raw PAL number is unreadable to a human, and
    the four levels are already this app's vocabulary for exactly this quantity. The
    number form is what an inference returns, and what an expert would type.

    Not routed through `to_amount`: that rejects a third decimal place, which is
    right for money and wrong here — `light` is 1.375. The decimal-comma rule is
    kept, though, because `=1,45` becoming 145 would be a factor a hundred times too
    large, which is the same class of error as a $1,240 lunch.
    """
    low = value.lower()
    if low in ACTIVITY_LEVELS:
        return ACTIVITY_LEVELS[low]
    if "," in value:
        raise ParseError(f"={value}: use a dot for decimals, e.g. ={value.replace(',', '.')}")
    try:
        factor = float(value)
    except ValueError as e:
        raise ParseError(
            f"={value}: give a level ({'/'.join(ACTIVITY_LEVELS)}) or a number, e.g. =1.45"
        ) from e
    # `nan` parses and compares False against both bounds, so this rejects it too.
    if not FACTOR_MIN <= factor <= FACTOR_MAX:
        raise ParseError(
            f"={value}: a whole-day activity factor is between {FACTOR_MIN} and {FACTOR_MAX}"
        )
    return factor


def parse_activity(raw: str, *, now: dt.datetime) -> ActivityInput:
    """What you did, and optionally what the whole day came to.

    `=` carries the day's multiplier; omitting it means "estimate this", exactly as
    omitting `=kcal` does for food. There is no bare-number form: a description may
    end in a number ("walk 5000"), which is the ambiguity the sigil grammar exists to
    remove.
    """
    toks = _tokens(raw)
    g = sigil.group(toks)
    if not g.text:
        raise ParseError("say what you did, e.g. gym 1h =active")
    _reject_unsupported(g, frozenset(["@", "="]), "activity")
    raw_factor = _single(g, "=", "factor")
    when = resolve_when(g.by_sigil.get("@", []), now=now)
    return ActivityInput(
        description=g.text,
        factor=to_factor(raw_factor) if raw_factor is not None else None,
        date=when.date,
        at=when.at,
    )


def render_activity(row, tz: str) -> str:
    """The inverse, for prefilling an edit.

    The factor renders as a number rather than as the keyword that may have produced
    it: 1.55 typed as `=active` and 1.55 inferred are the same stored value, and
    guessing which word a number came from would put a claim in the line that the
    database does not hold. A row whose inference never landed renders with no `=` at
    all, so opening its edit prompt does not invent a factor.
    """
    parts = [sigil.escape(row["description"])]
    if row["factor"] is not None:
        parts.append(f"={row['factor']:g}")
    parts.append(f"@{row['date']}/{hhmm(row['logged_at'], tz)}")
    return " ".join(parts)


def _tokens(raw: str) -> list[sigil.Token]:
    toks = sigil.fold_spans(sigil.tokenize(raw))
    if not toks:
        raise ParseError("nothing to log")
    return toks


def _leading_amount(tokens: list[sigil.Token], hint: str) -> float:
    """The amount is the first token or there is no amount.

    Strictly positional, unlike the old grammar's "first number that survives
    field extraction". That is what makes a numeric word inside a description safe.
    """
    first = tokens[0]
    if first.sigil or not _NUM.match(first.value):
        raise ParseError(f"start with {hint}")
    return to_amount(first.value)


def _single(g: sigil.Grouped, sigil_char: str, field: str) -> str | None:
    values = g.by_sigil.get(sigil_char, [])
    if len(values) > 1:
        raise ParseError(f"{sigil_char} gave the {field} twice")
    return values[0] if values else None


def _months(value: str | None) -> int | None:
    """`#N` on an expense: how many months this one payment covers.

    A count, not the keyword `#annually` the recurring grammar takes. The sigil means the
    same thing in both places — over what period does this cost apply — but a recurring
    item has a closed vocabulary of cycles it renews on, while a prepayment is any run of
    months: a two-year domain, a six-month pass. One number reads the same in every case
    and needs no mapping table.

    Rejects 1 rather than accepting it: a payment covering one month *is* an ordinary
    expense, and storing the marker anyway would put a second representation of the plain
    case into every reader.
    """
    if value is None:
        return None
    try:
        n = int(value)
    except ValueError:
        raise ParseError(f"# wants a number of months, e.g. #12 — got {value!r}") from None
    if n < 2:
        raise ParseError("# is how many months the payment covers, so at least 2")
    if n > 120:
        raise ParseError("# over 120 months is not a prepayment, it is a mistake")
    return n


def _vocab(value: str, allowed, field: str) -> str:
    low = value.lower()
    if low not in allowed:
        raise ParseError(f"{value!r} is not a {field} — one of {', '.join(sorted(allowed))}")
    return low


def _reject_unsupported(g: sigil.Grouped, allowed: frozenset[str], entity: str) -> None:
    """Reject any sigil the entity does not consume.

    The premise is "what you typed is what gets recorded" — silently discarding
    sigiled input is this slice's own failure mode. `f`/`e` are adjacent and both
    start with free text, so typing !restaurant on a food line is a live mistake.
    """
    used = frozenset(g.by_sigil.keys())
    unsupported = used - allowed
    if unsupported:
        sigil_char = sorted(unsupported)[0]
        special = _NO_SUCH_FIELD.get((entity, sigil_char))
        if special:
            raise ParseError(special)
        field_name = {"!": "category", "#": "cycle", "~": "note", "=": "kcal", "@": "date/time"}
        field = field_name.get(sigil_char, 'field')
        hint = f"a {entity} does not have a {field} — drop {sigil_char}"
        raise ParseError(hint)


# Where the generic "does not have a <field>" line would be untrue. A weigh-in
# does have a note — it is the bare words — so naming the sigil is the whole fix.
_NO_SUCH_FIELD = {
    ("weigh-in", "~"): "a weigh-in's note is just the words — drop the ~",
}


def parse_expense(raw: str, *, now: dt.datetime, known_slugs: frozenset[str]) -> ExpenseInput:
    toks = _tokens(raw)
    amount = _leading_amount(toks, "an amount, e.g. 12.40 lunch !restaurant")
    g = sigil.group(toks[1:])
    if amount == 0:
        raise ParseError("amount must be non-zero")
    if not g.text:
        raise ParseError("say what it was, e.g. 12.40 lunch !restaurant")
    _reject_unsupported(g, frozenset(["!", "@", "~", "#"]), "expense")
    slug = _single(g, "!", "category")
    note = _single(g, "~", "note")
    return ExpenseInput(
        amount=amount,
        description=g.text,
        category=_vocab(slug, known_slugs, "category") if slug else FALLBACK_SLUG,
        date=resolve_when(g.by_sigil.get("@", []), now=now).date,
        note=note or None,
        prepaid_months=_months(_single(g, "#", "months")),
    )


def render_expense(row) -> str:
    """The inverse, for prefilling an edit. `~` renders last because it absorbs the
    plain tokens after it.

    `#N` rides the line for the reason every displayed column does: the pane marks a
    prepaid row, so the edit has to be able to change or clear the thing being marked.
    Submitting a line with the `#` dropped makes it an ordinary expense again, which is
    the same "the submitted line is authoritative" rule the note already follows.
    """
    parts = [f"{row['amount']:.2f}", sigil.escape(row["description"]),
             f"!{row['category']}", f"@{row['date']}"]
    months = row["prepaid_months"] if "prepaid_months" in row.keys() else None
    if months:
        parts.append(f"#{months}")
    if row["note"]:
        parts.append(f"~{sigil.escape(row['note'])}")
    return " ".join(parts)


def parse_budget(raw: str, *, known_slugs: frozenset[str]) -> BudgetInput:
    toks = _tokens(raw)
    amount = _leading_amount(toks, "a positive amount, e.g. 500 !grocery")
    if amount <= 0:
        raise ParseError("a budget needs a positive amount, e.g. 500 !grocery")
    g = sigil.group(toks[1:])
    _reject_unsupported(g, frozenset(["!"]), "budget")
    slug = _single(g, "!", "category")
    if slug is None:
        raise ParseError("name a category, e.g. 500 !grocery")
    category = _vocab(slug, known_slugs, "category")
    cat = get(category)
    return BudgetInput(
        amount=amount,
        name=g.text or (cat.display if cat else category),
        category=category,
    )


def render_budget(row) -> str:
    return f"{row['amount']:.2f} {sigil.escape(row['name'])} !{row['category']}"


def parse_recurring(raw: str, *, known_slugs: frozenset[str]) -> RecurringInput:
    toks = _tokens(raw)
    cost = _leading_amount(toks, "a positive cost, e.g. 20.99 Streaming !subscriptions")
    if cost <= 0:
        raise ParseError("a recurring cost must be positive")
    g = sigil.group(toks[1:])
    if not g.text:
        raise ParseError("give it a name, e.g. 20.99 Streaming !subscriptions")
    _reject_unsupported(g, frozenset(["!", "#"]), "recurring cost")
    slug = _single(g, "!", "category")
    cycle = _single(g, "#", "cycle")
    return RecurringInput(
        cost=cost,
        name=g.text,
        category=_vocab(slug, known_slugs, "category") if slug else FALLBACK_SLUG,
        cycle=_vocab(cycle, money.CYCLES, "cycle") if cycle else "monthly",
    )


def render_recurring(row) -> str:
    return (
        f"{row['cost']:.2f} {sigil.escape(row['name'])} "
        f"!{row['category']} #{row['cycle']}"
    )


# A slug's whole job is to be typed as `!gym`, so it must be one token the tokeniser
# leaves alone: no whitespace, and nothing that is a sigil or an escape.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def parse_category(raw: str, *, known_slugs=frozenset()) -> CategoryInput:
    """`gym` or `gym Gym & Pool` — the first token is the slug, the rest is the name.

    No colour: `categories.auto_color` hashes the slug to a stable palette entry, which
    is already how a config-added category gets its hue, and picking one by hand is a
    decision with no good answer at a prompt.

    `known_slugs` is passed in rather than read from config, because this module is pure.
    Rejecting a duplicate here is the point: `all_categories` *silently* drops a config
    entry that shadows an existing slug, which is right for a hand-edited file and wrong
    for a prompt, where the user would get no feedback and assume it worked.
    """
    words = raw.split()
    if not words:
        raise ParseError("give a slug, e.g. gym Gym")
    slug = words[0].lower()
    if not _SLUG_RE.match(slug):
        raise ParseError(
            f"{words[0]!r} cannot be a slug — lowercase letters, digits, - and _ only,"
            " because you have to be able to type it as !gym"
        )
    if slug in known_slugs:
        raise ParseError(f"{slug} already exists")
    display = " ".join(words[1:]) or None
    return CategoryInput(slug=slug, display=display)


def _safe_date(y: int, m: int, d: int) -> dt.date:
    try:
        return dt.date(y, m, d)
    except ValueError as e:
        raise ParseError(f"{y:04d}-{m:02d}-{d:02d} is not a real date") from e


_THOUSANDS = re.compile(r"^-?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")


def to_amount(tok: str) -> float:
    """A money or weight amount, tolerating `$` and thousands separators.

    A comma is only accepted where it really separates thousands. Stripping every
    comma made `12,40` — a decimal comma, which is how most of the world writes it —
    parse as 1240, so a $12.40 lunch was recorded as $1,240.00 with no complaint at
    all. Weights escaped the consequence only because 782 kg fails a plausibility
    check that expenses have no equivalent of.
    """
    bare = tok.replace("$", "")
    if "," in bare and not _THOUSANDS.match(bare):
        raise ParseError(f"{tok}: use a dot for decimals, e.g. {bare.replace(',', '.')}")
    cleaned = bare.replace(",", "")
    if "." in cleaned and len(cleaned.split(".")[1]) > 2:
        raise ParseError(f"{tok} has more than two decimal places")
    return float(cleaned)


# ── profile ──────────────────────────────────────────────────────────────
_PROFILE_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_HEIGHT = re.compile(r"^(\d{2,3}(?:\.\d+)?)(?:cm)?$", re.IGNORECASE)
_SEX = {"m": "male", "male": "male", "f": "female", "female": "female"}
_MIN_CM, _MAX_CM = 90.0, 250.0


@dataclass(frozen=True)
class ProfileInput:
    """Every field optional: `profile › 181` should change only the height.

    The first three exist solely to compute BMR, and BMR is the one number that makes
    a calorie count mean anything. Editing config.toml by hand to get it was the
    only way, which is why the ENERGY panel sat empty.

    `activity` says what an *ordinary* day looks like, which is what turns resting
    BMR into a real maintenance figure. It belongs in the profile rather than being
    logged daily because "sat at a desk" is true almost every day, and a field that
    must be retyped daily is one that gets skipped — leaving `net` measured against
    resting expenditure, which is the thing it exists to fix.
    """

    height_cm: float | None = None
    sex: str | None = None
    birthday: str | None = None
    activity: str | None = None
    timezone: str | None = None

    def fields(self) -> dict[str, float | str]:
        return {
            k: v
            for k, v in (
                ("height_cm", self.height_cm),
                ("sex", self.sex),
                ("birthday", self.birthday),
                ("activity", self.activity),
                ("timezone", self.timezone),
            )
            if v is not None
        }


def parse_profile(raw: str) -> ProfileInput:
    """Height, sex and birthday in any order, recognised by shape.

    No `now` argument and no keyword prefixes: a bare number in a plausible human
    range can only be a height, `male`/`female` can only be a sex, and an ISO date
    can only be a birthday. Order-free beats an argument order nobody remembers.
    """
    height: float | None = None
    sex: str | None = None
    birthday: str | None = None
    activity: str | None = None
    timezone: str | None = None
    for word in raw.split():
        low = word.lower()
        if low in _SEX:
            sex = _SEX[low]
            continue
        # A level keyword cannot be mistaken for anything else here: the other three
        # fields are a number, `male`/`female`, and an ISO date.
        if low in ACTIVITY_LEVELS:
            activity = low
            continue
        if (m := _PROFILE_DATE.match(low)) is not None:
            try:
                birthday = dt.date(int(m[1]), int(m[2]), int(m[3])).isoformat()
            except ValueError as e:
                raise ParseError(f"{word} is not a real date") from e
            continue
        if (m := _HEIGHT.match(low)) is not None:
            value = float(m[1])
            if not _MIN_CM <= value <= _MAX_CM:
                raise ParseError(f"{m[1]} cm is not a plausible height")
            height = value
            continue
        # Last, and on the *original* word rather than the lowercased one: ZoneInfo
        # keys are case-sensitive, and `america/toronto` is not a zone. Recognised by
        # asking the zone database instead of by shape, so it needs no pattern to keep
        # in step with tzdata and cannot collide with the four fields above — none of
        # `180`, `male`, an ISO date or a level is a zone name.
        if is_zone(word):
            timezone = word
            continue
        raise ParseError(
            f"don't know what {word!r} is — try 180 male 1990-01-01 desk America/Toronto"
        )
    if all(v is None for v in (height, sex, birthday, activity, timezone)):
        raise ParseError(
            "give a height, a sex, a birthday, an ordinary-day level or a timezone — "
            "e.g. 180 male 1990-01-01 desk America/Toronto"
        )
    return ProfileInput(
        height_cm=height,
        sex=sex,
        birthday=birthday,
        activity=activity,
        timezone=timezone,
    )
