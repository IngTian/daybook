"""Money: expenses, recurring items, budgets, and month arithmetic.

Expenses only. `amount` is positive for a spend and negative for a refund;
there is no income concept, no account, and no balance. That single decision
is what removes a double-entry ledger, a family of reserve helpers, and
end-of-month cash projection — most of the volume of a finance module, and
rather more than that of its confusion.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
import sqlite3
import time
from dataclasses import dataclass, field

from daylogs.categories import slugs
from daylogs.horizon import Span

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_LIMIT_CAP = 5000
CYCLES = frozenset({"monthly", "annually"})
HISTORY_MONTHS = 6
_BUDGET_SOURCES = frozenset({"manual", "recurring"})


class MoneyError(ValueError):
    pass


# ── validators (shared with summary.py and __main__.py) ──────────────────
def check_date(date: str) -> str:
    if not _DATE_RE.match(date):
        raise MoneyError(f"date {date!r} must be YYYY-MM-DD")
    try:
        dt.date.fromisoformat(date)
    except ValueError as e:
        raise MoneyError(f"date {date!r} is not a real date") from e
    return date


def check_month(month: str) -> str:
    if not _MONTH_RE.match(month):
        raise MoneyError(f"month {month!r} must be YYYY-MM")
    try:
        dt.date.fromisoformat(f"{month}-01")
    except ValueError as e:
        raise MoneyError(f"month {month!r} is not a real year-month") from e
    return month


def check_category(category: str, cfg=None) -> str:
    if category not in slugs(cfg):
        raise MoneyError(
            f"category {category!r} is not known; add it to config.toml as a "
            "[[category]] block if you want it"
        )
    return category


def _now() -> int:
    return int(time.time())


def months_ending(month: str, n: int) -> list[str]:
    """n YYYY-MM strings ending at `month` inclusive, ascending."""
    check_month(month)
    y, m = int(month[:4]), int(month[5:7])
    out: list[str] = []
    for back in range(n - 1, -1, -1):
        total = y * 12 + (m - 1) - back
        yy, mm = divmod(total, 12)
        out.append(f"{yy:04d}-{mm + 1:02d}")
    return out


# ── expenses ─────────────────────────────────────────────────────────────
def add_expense(
    conn,
    *,
    amount: float,
    description: str,
    category: str,
    date: str,
    note: str | None = None,
    prepaid_months: int | None = None,
    cfg=None,
) -> int:
    if float(amount) == 0:
        raise MoneyError("amount must be non-zero")
    if not description.strip():
        raise MoneyError("description must be non-empty")
    cur = conn.execute(
        "INSERT INTO expense"
        " (date, amount, description, category, note, prepaid_months, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            check_date(date),
            float(amount),
            description.strip(),
            check_category(category, cfg),
            note or None,
            _check_months(prepaid_months),
            _now(),
        ),
    )
    return int(cur.lastrowid)


def update_expense(conn, id: int, cfg=None, **fields) -> bool:
    fields = {k: v for k, v in fields.items() if v is not None}
    allowed = {"amount", "description", "category", "date", "note", "prepaid_months"}
    unknown = set(fields) - allowed
    if unknown:
        raise MoneyError(f"cannot update {sorted(unknown)} on expense")
    if not fields:
        return False
    if "date" in fields:
        check_date(fields["date"])
    if "category" in fields:
        check_category(fields["category"], cfg)
    if "amount" in fields and float(fields["amount"]) == 0:
        raise MoneyError("amount must be non-zero")
    if "prepaid_months" in fields:
        # 0 is the clearing value, for the same reason "" is the note's: this function
        # drops None so it can tell "the line did not mention it" from "the line said
        # nothing for it". The grammar cannot produce `#0` — `_months` demands at least 2 —
        # so 0 can only ever come from a caller meaning "make this an ordinary expense".
        fields["prepaid_months"] = _check_months(fields["prepaid_months"] or None)
    sets = ", ".join(f"{k} = ?" for k in fields)
    cur = conn.execute(f"UPDATE expense SET {sets} WHERE id = ?", (*fields.values(), int(id)))
    return cur.rowcount > 0


def delete_expense(conn, id: int) -> dict | None:
    return _delete(conn, "expense", id)


def _delete(conn, table: str, id: int) -> dict | None:
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (int(id),)).fetchone()
    if row is None:
        return None
    conn.execute(f"DELETE FROM {table} WHERE id = ?", (int(id),))
    return dict(row)


# ── recurring ────────────────────────────────────────────────────────────
def monthly_equivalent(cost: float, cycle: str) -> float:
    if cycle not in CYCLES:
        raise MoneyError(f"cycle must be one of {sorted(CYCLES)}")
    return round(float(cost) / 12, 2) if cycle == "annually" else round(float(cost), 2)


def upsert_recurring(
    conn,
    *,
    name: str,
    cost: float,
    cycle: str,
    category: str,
    note: str | None = None,
    active: bool = True,
    cfg=None,
) -> int:
    """Add an item, or update one of the same name.

    `active` applies on **insert only** — it is deliberately absent from the ON CONFLICT
    clause. Pausing is a state you set on purpose with `o`; a re-add is about cost, cycle
    and category, and it arrives through a grammar that cannot express the flag, so
    `active = excluded.active` meant every re-add carried the parameter's `True` default.
    Raising a paused subscription's price un-paused it, and the next roll charged for it
    with nothing on screen connecting the two.

    `note` is still overwritten, which has the same shape and is left alone on purpose:
    the recurring grammar has no `~note`, so that column is unreachable from the UI in
    both directions and fixing one half of an unreachable field would be pretending.
    """
    name = name.strip()
    if not name:
        raise MoneyError("recurring item needs a name")
    if float(cost) <= 0:
        raise MoneyError("recurring cost must be > 0")
    monthly = monthly_equivalent(cost, cycle)
    check_category(category, cfg)
    conn.execute(
        """
        INSERT INTO recurring (name, category, cost, cycle, monthly_cost, active, note)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
          category = excluded.category,
          cost = excluded.cost,
          cycle = excluded.cycle,
          monthly_cost = excluded.monthly_cost,
          note = excluded.note
        """,
        (name, category, float(cost), cycle, monthly, 1 if active else 0, note or None),
    )
    row = conn.execute("SELECT id FROM recurring WHERE name = ?", (name,)).fetchone()
    return int(row["id"])


def list_recurring(conn, *, active_only: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM recurring"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY monthly_cost DESC, name ASC"
    return list(conn.execute(sql))


def update_recurring(conn, id: int, cfg=None, **fields) -> bool:
    """Edit a recurring item in place, by id.

    `upsert_recurring` cannot serve as the edit path: it resolves conflicts on
    `name`, so renaming an item matches nothing and INSERTs a second row. Both then
    look active, and the next `roll_month_budgets` writes two budget lines for one
    subscription. Keyed by id, a rename is a rename.

    `monthly_cost` is a stored derived column that `roll_month_budgets` reads, so it
    is recomputed whenever cost or cycle moves. Leaving it stale would put the wrong
    number into next month's budget, a month later, with nothing on screen
    connecting the two.
    """
    fields = {k: v for k, v in fields.items() if v is not None}
    allowed = {"name", "category", "cost", "cycle", "note", "active"}
    unknown = set(fields) - allowed
    if unknown:
        raise MoneyError(f"cannot update {sorted(unknown)} on recurring")
    if not fields:
        return False
    row = conn.execute("SELECT * FROM recurring WHERE id = ?", (int(id),)).fetchone()
    if row is None:
        return False

    if "category" in fields:
        check_category(fields["category"], cfg)
    if "name" in fields:
        name = str(fields["name"]).strip()
        if not name:
            raise MoneyError("a recurring item needs a name")
        # Pre-check rather than letting the UNIQUE(name) constraint raise: the
        # sqlite error names a column, this names the item the user can go fix.
        clash = conn.execute(
            "SELECT id FROM recurring WHERE name = ? AND id != ?", (name, int(id))
        ).fetchone()
        if clash is not None:
            raise MoneyError(f"{name!r} already exists")
        fields["name"] = name

    cost = float(fields.get("cost", row["cost"]))
    cycle = fields.get("cycle", row["cycle"])
    if cost <= 0:
        raise MoneyError("a recurring cost must be positive")
    if "cost" in fields or "cycle" in fields:
        fields["monthly_cost"] = monthly_equivalent(cost, cycle)
    if "active" in fields:
        fields["active"] = 1 if fields["active"] else 0

    sets = ", ".join(f"{k} = ?" for k in fields)
    cur = conn.execute(
        f"UPDATE recurring SET {sets} WHERE id = ?", (*fields.values(), int(id))
    )
    if "name" in fields:
        _rename_rolled_budgets(conn, old=row["name"], new=fields["name"])
    return cur.rowcount > 0


def budget_line(conn, *, month: str, category: str) -> sqlite3.Row | None:
    """The budget row for one category in one month, or None.

    A category can hold more than one line in a month — `budget` is UNIQUE(month, name),
    not (month, category), so a `Rent` and a `Storage` line can both be `housing`. The
    newest wins here, which is the one an edit prefill should show; the pane's own figures
    keep summing all of them, so this is a lookup for editing, not a total.
    """
    check_month(month)
    return conn.execute(
        "SELECT * FROM budget WHERE month = ? AND category = ? ORDER BY id DESC LIMIT 1",
        (month, category),
    ).fetchone()


def _rename_rolled_budgets(conn, *, old: str, new: str) -> None:
    """Carry a recurring item's rename through to the budget lines it produced.

    Budget rows are keyed by `name`, recurring items by `id`, and nothing joined the
    two. So a rename left the old month's line standing and the next `roll_month_budgets`
    added a second line for the same subscription: one 24.99 item became 49.98 of budget,
    permanently, in the single number the Money tab exists to answer.

    Fixed here rather than by reconciling at roll time, because this is the only place
    that knows a rename happened. From the budget table alone an unclaimed line is
    indistinguishable from one whose item was *deleted* — and those want opposite
    treatment: a month you have already paid for keeps its line, and next month's roll
    simply will not include it.

    Only `source='recurring'` rows move. A number you typed by hand is yours; the roll
    has never overwritten one and neither does this.
    """
    if old == new:
        return
    # Drop the old line in any month that already has one under the new name, rather
    # than colliding with UNIQUE(month, name). Such a month has both because it was
    # rolled after the rename — the old row is precisely the duplicate being cleaned up.
    conn.execute(
        "DELETE FROM budget WHERE name = ? AND source = 'recurring'"
        " AND month IN (SELECT month FROM budget WHERE name = ?)",
        (old, new),
    )
    conn.execute(
        "UPDATE budget SET name = ? WHERE name = ? AND source = 'recurring'",
        (new, old),
    )


def delete_recurring(conn, id: int) -> dict | None:
    return _delete(conn, "recurring", id)


# ── budgets ──────────────────────────────────────────────────────────────
def upsert_budget(
    conn,
    *,
    month: str,
    name: str,
    category: str,
    amount: float,
    source: str = "manual",
    note: str | None = None,
    cfg=None,
) -> int:
    name = name.strip()
    if not name:
        raise MoneyError("budget line needs a name")
    if float(amount) < 0:
        raise MoneyError("budget amount must be >= 0")
    if source not in _BUDGET_SOURCES:
        raise MoneyError(f"source must be one of {sorted(_BUDGET_SOURCES)}")
    check_month(month)
    check_category(category, cfg)
    conn.execute(
        """
        INSERT INTO budget (month, name, category, amount, source, note)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(month, name) DO UPDATE SET
          category = excluded.category,
          amount = excluded.amount,
          source = excluded.source,
          note = excluded.note
        """,
        (month, name, category, float(amount), source, note or None),
    )
    row = conn.execute(
        "SELECT id FROM budget WHERE month = ? AND name = ?", (month, name)
    ).fetchone()
    return int(row["id"])


def list_budget(conn, *, month: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM budget WHERE month = ? ORDER BY category ASC, name ASC",
            (check_month(month),),
        )
    )


def pending_roll(conn, *, month: str) -> tuple[int, float]:
    """(count, monthly total) that `roll_month_budgets` would add to `month`.

    Budgets are per-month rows, so a month nobody rolled has none — and the
    header then reads "0.00 budget / 1,234.00 over", which is arithmetically true
    and completely useless. Knowing what a roll *would* produce lets the empty
    state name the fix instead of just reporting a zero.
    """
    check_month(month)
    existing = {r["name"] for r in list_budget(conn, month=month)}
    pending = [r for r in list_recurring(conn, active_only=True) if r["name"] not in existing]
    return len(pending), round(sum(r["monthly_cost"] for r in pending), 2)


def roll_month_budgets(conn, *, month: str, cfg=None) -> int:
    """Create a budget line for each active recurring item that has no line in
    `month` yet, and return how many were created. Never overwrites an
    existing line — if you hand-set a number for a subscription this month,
    the roll leaves it alone."""
    check_month(month)
    existing = {r["name"] for r in list_budget(conn, month=month)}
    created = 0
    for r in list_recurring(conn, active_only=True):
        if r["name"] in existing:
            continue
        upsert_budget(
            conn,
            month=month,
            name=r["name"],
            category=r["category"],
            amount=r["monthly_cost"],
            source="recurring",
            cfg=cfg,
        )
        created += 1
    return created


# ── month summary ────────────────────────────────────────────────────────
@dataclass
class CategorySpend:
    category: str
    budget: float
    spent: float
    delta: float
    history: list[float] = field(default_factory=list)


@dataclass
class MonthSummary:
    month: str
    total_spent: float
    total_budget: float
    remaining: float
    by_category: list[CategorySpend]
    top_expenses: list[sqlite3.Row]
    day_of_month: int
    days_in_month: int
    over_budget: list[CategorySpend]
    under_budget_remaining: list[CategorySpend]


def month_span(month: str) -> Span:
    """The whole of one calendar month, as a Span."""
    check_month(month)
    y, m = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(y, m)[1]
    return Span(horizon="MTD", start=f"{month}-01", end=f"{month}-{last:02d}")


def summarize_month(conn, *, month: str, today: str | None = None) -> MonthSummary:
    """One whole calendar month. A thin wrapper over summarize_span, so the tests
    written against this signature keep guarding the arithmetic unchanged."""
    return summarize_span(conn, span=month_span(month), today=today)


def summarize_span(
    conn, *, span: Span | None, today: str | None = None
) -> MonthSummary:
    """Per-category budget vs spent over an arbitrary date span, plus totals, the
    top five spends, and a six-month per-category history.

    `span=None` means all time. Spend is filtered by **date**, so a one-week
    horizon really covers seven days rather than the whole month. **Budget over a
    span is the sum of the calendar months it touches** — a span containing an
    unbudgeted month is honestly under-budgeted, not an error.

    Query count is constant regardless of span width: one for the history window,
    one for span spend, one for budgets, one for the top five. The obvious
    implementation issues one query per month in the span.
    """
    months = span.months() if span is not None else []
    anchor = months[-1] if months else _latest_expense_month(conn)
    window = months_ending(anchor, HISTORY_MONTHS) if anchor else []

    history: dict[str, list[float]] = {}
    if window:
        rows = conn.execute(
            """
            SELECT substr(date, 1, 7) AS ym, category, SUM(amount) AS total
            FROM expense
            WHERE prepaid_months IS NULL AND date >= ? AND date < ?
            GROUP BY ym, category
            """,
            (f"{window[0]}-01", _month_after(window[-1])),
        ).fetchall()
        idx_of = {ym: i for i, ym in enumerate(window)}
        raw: dict[str, list[float]] = {}
        for r in rows:
            i = idx_of.get(r["ym"])
            if i is None:
                continue
            raw.setdefault(r["category"], [0.0] * HISTORY_MONTHS)[i] = float(r["total"])
        # The sparkline has to agree with the number beside it, so it prorates too — a
        # single annual charge otherwise drew one spike in a flat six-month row and read as
        # a spending event rather than as the year's subscription.
        for ym, cat, share in _prepaid_shares(conn):
            i = idx_of.get(ym)
            if i is None:
                continue
            raw.setdefault(cat, [0.0] * HISTORY_MONTHS)[i] += share
        history = {c: [round(v, 2) for v in vals] for c, vals in raw.items()}

    spent_by_cat = _spent_by_category(conn, span)
    budget_by_cat = _budget_by_category(conn, months)

    by_category: list[CategorySpend] = []
    for cat in sorted(set(budget_by_cat) | set(spent_by_cat)):
        budget = budget_by_cat.get(cat, 0.0)
        spent = spent_by_cat.get(cat, 0.0)
        by_category.append(
            CategorySpend(
                category=cat,
                budget=budget,
                spent=spent,
                delta=round(budget - spent, 2),
                history=history.get(cat, [0.0] * HISTORY_MONTHS),
            )
        )

    total_budget = round(sum(c.budget for c in by_category), 2)
    total_spent = round(sum(c.spent for c in by_category), 2)

    where, args = _span_where(span)
    top_expenses = list(
        conn.execute(
            f"SELECT * FROM expense WHERE amount > 0{where}"
            " ORDER BY amount DESC, id DESC LIMIT 5",
            args,
        )
    )

    # Burn-against-elapsed only means something for a single month; across a
    # quarter it would invite a false read, so the caller is told to hide it.
    day, total_days = _calendar_progress(months[0], today) if len(months) == 1 else (0, 0)

    return MonthSummary(
        month=months[-1] if months else "all",
        total_spent=total_spent,
        total_budget=total_budget,
        remaining=round(total_budget - total_spent, 2),
        by_category=by_category,
        top_expenses=top_expenses,
        day_of_month=day,
        days_in_month=total_days,
        over_budget=[c for c in by_category if c.budget > 0 and c.delta < 0],
        under_budget_remaining=[c for c in by_category if c.budget > 0 and c.delta > 0],
    )


def _latest_expense_month(conn) -> str | None:
    row = conn.execute("SELECT max(substr(date, 1, 7)) AS m FROM expense").fetchone()
    return row["m"] if row and row["m"] else None


def _span_where(span: Span | None) -> tuple[str, list]:
    """An inclusive date interval, which keeps this off `substr` and lets the
    index on `date` do the work. Filtering by date rather than by month is what
    makes a one-week horizon actually mean seven days."""
    if span is None:
        return "", []
    if span.start is None:
        return " AND date <= ?", [span.end]
    return " AND date >= ? AND date <= ?", [span.start, span.end]


def _check_months(n: int | None) -> int | None:
    """Guard the column at the data layer, not only in the grammar.

    `parse.py` already rejects `#1` and `#abc`, but `add_expense` is called by
    `summary`/tests/`__main__` too, and a 1 here would mean "prepaid over one month",
    which is an ordinary expense wearing a marker — a second representation of the plain
    case that every reader would then have to know about.
    """
    if n is None:
        return None
    n = int(n)
    if not 2 <= n <= 120:
        raise MoneyError(f"prepaid_months must be between 2 and 120, got {n}")
    return n


def _covered_months(date: str, n: int) -> list[str]:
    """The `n` calendar months a prepayment dated `date` covers, starting with its own."""
    y, m = int(date[:4]), int(date[5:7])
    return [f"{y + (m - 1 + i) // 12:04d}-{(m - 1 + i) % 12 + 1:02d}" for i in range(n)]


def _prepaid_shares(conn) -> list[tuple[str, str, float]]:
    """`(month, category, share)` for every month every prepaid expense covers.

    One payment covering twelve months becomes twelve shares of a twelfth. Unrounded on
    purpose: callers round their own totals, so twelve shares of 19.9958 still add back to
    239.95 rather than drifting by a cent a month.

    Spread in Python rather than SQL because it needs a row-per-month expansion, which in
    SQLite means a recursive CTE for the sake of a handful of rows a year. A loop over
    `WHERE prepaid_months IS NOT NULL` is the same answer and can be read.
    """
    out: list[tuple[str, str, float]] = []
    rows = conn.execute(
        "SELECT date, amount, category, prepaid_months FROM expense"
        " WHERE prepaid_months IS NOT NULL"
    )
    for r in rows:
        n = int(r["prepaid_months"])
        share = float(r["amount"]) / n
        out += [(ym, r["category"], share) for ym in _covered_months(r["date"], n)]
    return out


def _months_filter(span: Span | None) -> set[str] | None:
    """The months a prepaid share must fall in to count, or None for no filter.

    A covered month counts if the span touches that month at all, which is deliberately
    the same rule `_budget_by_category` already uses — budgets are stored per calendar
    month and summed over `span.months()` regardless of how much of each month the span
    covers. Making prepaid spend agree with that is the whole point: a monthly figure
    against a monthly cap. It does mean a one-week window shows a full month's share, and
    that is the pre-existing mismatch CLAUDE.md already records for budgets at `1w`.

    `Span.months()` returns `[]` for an unbounded span, which is why "no filter" is None
    and not an empty set — an empty set would count nothing over all time.
    """
    if span is None:
        return None
    months = span.months()
    return set(months) if months else None


def prepaid_inflows(conn, span: Span | None) -> list[dict]:
    """Every prepaid charge contributing to `span`'s totals, and what it contributes.

    The pane lists payments by date, so a charge dated in June contributes to September's
    total with no row in September to account for it: the header read 20.00 over a list
    that summed to nothing. In the charge's own month it disagreed the other way — 20.00
    counted, over a row saying 240.00. Both are one missing statement, which is all this
    is: which payments are being counted here, and for how much.

    One entry per **charge**, not per covered month, with `share` summed over the months of
    its coverage that the span actually touches. Twelve lines for one subscription would
    bury the payments the pane exists for. `first`/`last` are 1-based positions inside the
    charge's own N months, so a caller can say "month 4 of 12" — or "4 to 6" on a span wide
    enough to catch several — without re-deriving the coverage.

    `share` is **unrounded**, for the reason `_prepaid_shares` is: rounding per line and
    then adding drifts a cent away from a header that rounds once at the end, and a list
    that cannot account for the total is the defect this exists to fix. Callers format it.

    Filtered through `_months_filter`, the same rule `_spent_by_category` uses, because
    agreeing with the total is the entire point — a second month rule here would put the
    list and the header back into disagreement by a different route.
    """
    keep = _months_filter(span)
    out: list[dict] = []
    rows = conn.execute(
        "SELECT id, date, amount, category, description, prepaid_months FROM expense"
        " WHERE prepaid_months IS NOT NULL"
    )
    for r in rows:
        n = int(r["prepaid_months"])
        covered = _covered_months(r["date"], n)
        hit = [i for i, ym in enumerate(covered) if keep is None or ym in keep]
        if not hit:
            continue
        out.append(
            {
                "id": int(r["id"]),
                "date": r["date"],
                "category": r["category"],
                "description": r["description"],
                "amount": float(r["amount"]),
                "months": n,
                "share": float(r["amount"]) / n * len(hit),
                "first": hit[0] + 1,
                "last": hit[-1] + 1,
            }
        )
    # Biggest contributor first: this is a short pinned block answering "what is inflating
    # this month", not the payment log below it, which keeps its own sort. Signed rather
    # than by magnitude, so a refund sits at the bottom where it reads as a subtraction.
    out.sort(key=lambda d: (-d["share"], d["date"], d["id"]))
    return out


def _spent_by_category(conn, span: Span | None) -> dict[str, float]:
    where, args = _span_where(span)
    # Prepaid rows are excluded here and added back by the month they cover, not the day
    # they were paid. The budget side has always prorated — `roll_month_budgets` writes
    # `monthly_cost` — so summing the raw charge made the renewal month read 12x over its
    # cap and the other eleven read a saving, and no single month was ever right.
    sql = (
        f"SELECT category, SUM(amount) AS total FROM expense"
        f" WHERE prepaid_months IS NULL{where} GROUP BY category"
    )
    out = {r["category"]: float(r["total"]) for r in conn.execute(sql, args)}
    keep = _months_filter(span)
    for ym, cat, share in _prepaid_shares(conn):
        if keep is None or ym in keep:
            out[cat] = out.get(cat, 0.0) + share
    return {k: round(v, 2) for k, v in out.items()}


def _budget_by_category(conn, months: list[str]) -> dict[str, float]:
    """Summed across every month in range — one query, not one per month."""
    if months:
        marks = ", ".join("?" for _ in months)
        sql = (
            f"SELECT category, SUM(amount) AS total FROM budget"
            f" WHERE month IN ({marks}) GROUP BY category"
        )
        args: list = list(months)
    else:
        sql = "SELECT category, SUM(amount) AS total FROM budget GROUP BY category"
        args = []
    return {r["category"]: round(float(r["total"]), 2) for r in conn.execute(sql, args)}


def _month_after(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    return f"{y + 1:04d}-01-01" if m == 12 else f"{y:04d}-{m + 1:02d}-01"


def _calendar_progress(month: str, today: str | None) -> tuple[int, int]:
    """How far through the month we are, so budget burn can be read against
    it. 84% of budget spent on day 27 of 31 is fine; on day 12 it is not."""
    y, m = int(month[:4]), int(month[5:7])
    total_days = calendar.monthrange(y, m)[1]
    t = dt.date.fromisoformat(today) if today else dt.date.today()
    t_month = f"{t.year:04d}-{t.month:02d}"
    if t_month == month:
        return t.day, total_days
    return (total_days, total_days) if t_month > month else (0, total_days)


# ── the expenses pane's one query ────────────────────────────────────────
_SORT_COLUMN = {"date": "date", "amount": "amount", "category": "category"}


def _escape_like(text: str) -> str:
    """A description containing % or _ must match literally, not as a wildcard."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def query_expenses(conn, view) -> list[sqlite3.Row]:
    """Every combination of span, sort and the two filters, in one statement.

    Taking a MoneyView rather than eight keyword arguments is deliberate: the
    combinations are the thing that needs testing, and they are only testable as
    a unit if they arrive as one.
    """
    sql = "SELECT * FROM expense WHERE 1=1"
    where, args = _span_where(view.span())
    sql += where

    if view.filter_category:
        sql += " AND category = ?"
        args.append(view.filter_category)
    if view.filter_text:
        needle = f"%{_escape_like(view.filter_text.lower())}%"
        sql += (
            " AND (lower(description) LIKE ? ESCAPE '\\'"
            " OR lower(category) LIKE ? ESCAPE '\\')"
        )
        args.extend([needle, needle])

    column = _SORT_COLUMN.get(view.sort_field)
    if column is None:
        raise MoneyError(f"cannot sort by {view.sort_field!r}")
    direction = "DESC" if view.sort_desc else "ASC"
    sql += f" ORDER BY {column} {direction}, id {direction} LIMIT ?"
    args.append(_LIMIT_CAP)
    return list(conn.execute(sql, args))


def group_expenses(
    rows, *, collapsed: frozenset[str]
) -> list[tuple[str, float, int, list[sqlite3.Row]]]:
    """`(slug, total, count, rows)` ordered by total descending — a grouped view
    answers "where did most of it go". `rows` is empty for a collapsed group, but
    the total and count still show, so collapsing hides detail without hiding
    magnitude."""
    buckets: dict[str, list] = {}
    for r in rows:
        buckets.setdefault(r["category"], []).append(r)
    out = [
        (
            slug,
            round(sum(float(r["amount"]) for r in rs), 2),
            len(rs),
            [] if slug in collapsed else rs,
        )
        for slug, rs in buckets.items()
    ]
    out.sort(key=lambda g: g[1], reverse=True)
    return out
