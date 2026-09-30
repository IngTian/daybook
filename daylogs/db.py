"""SQLite connection + schema. Seven tables, one idempotent DDL block.

No migration framework: the DDL is CREATE TABLE IF NOT EXISTS throughout, so adding
a table is additive on an existing database. The version is stamped in
PRAGMA user_version so a future change has somewhere to hook.

Adding a *column* is the one thing that DDL cannot do, and `_ADD_COLUMNS` is where that
is handled — a guarded `ALTER TABLE ... ADD COLUMN` per column, checked against
`PRAGMA table_info` so it is idempotent and safe to run on every open. Still no
framework: this is a list of columns, not a migration engine, and it stays that way. A
change that needs to *rewrite* rows rather than add a nullable column is the point at
which this stops being enough, and that decision should be made deliberately rather
than by extending this list.

journal_mode=DELETE is deliberate. WAL's -wal/-shm sidecars can sync
independently of the main file under iCloud Drive and corrupt the database on
the receiving device. daylogs is read-heavy; the write cost is noise.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 3  # 2: the activity table. 3: expense.prepaid_months

TABLES: tuple[str, ...] = (
    "weight",
    "food",
    "activity",
    "expense",
    "recurring",
    "budget",
    "report",
)

_DDL = """
CREATE TABLE IF NOT EXISTS weight (
  id          INTEGER PRIMARY KEY,
  date        TEXT    NOT NULL,
  measured_at INTEGER NOT NULL,
  kg          REAL    NOT NULL,
  note        TEXT
);
CREATE INDEX IF NOT EXISTS ix_weight_date ON weight(date);

CREATE TABLE IF NOT EXISTS food (
  id          INTEGER PRIMARY KEY,
  date        TEXT    NOT NULL,
  ate_at      INTEGER NOT NULL,
  description TEXT    NOT NULL,
  kcal        INTEGER NOT NULL,
  source      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_food_date ON food(date);

-- Only days that depart from the profile's baseline get rows here; an ordinary day
-- needs no entry at all.
--
-- `factor` is the whole *day's* PAL as inferred when this row landed, not this
-- activity's own contribution: a PAL multiplier describes a day and is not additive,
-- so "gym" plus "walked" is not 1.375 + 1.2. The day's value is read from the row with
-- the greatest `logged_at` — the same last-reading-wins rule the weight series applies
-- to same-day weigh-ins — which also leaves an audit trail of what was believed when.
-- NULL means no inference landed (no CLI, a timeout), and the day falls back to the
-- profile baseline rather than to resting BMR.
CREATE TABLE IF NOT EXISTS activity (
  id          INTEGER PRIMARY KEY,
  date        TEXT    NOT NULL,
  logged_at   INTEGER NOT NULL,
  description TEXT    NOT NULL,
  factor      REAL,
  source      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_activity_date ON activity(date);

CREATE TABLE IF NOT EXISTS expense (
  id          INTEGER PRIMARY KEY,
  date        TEXT    NOT NULL,
  amount      REAL    NOT NULL,
  description TEXT    NOT NULL,
  category    TEXT    NOT NULL,
  note        TEXT,
  -- How many months this one payment covers; NULL for an ordinary expense. Declared here
  -- *and* in _ADD_COLUMNS: this block is the description of the current schema, so a fresh
  -- database gets the column from CREATE TABLE and the ALTER only ever upgrades an older
  -- one. Leaving it out of here would make the DDL an incomplete picture and put every
  -- fresh database on the migration path.
  prepaid_months INTEGER,
  created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_expense_date ON expense(date);
CREATE INDEX IF NOT EXISTS ix_expense_category ON expense(category);

CREATE TABLE IF NOT EXISTS recurring (
  id           INTEGER PRIMARY KEY,
  name         TEXT    NOT NULL UNIQUE,
  category     TEXT    NOT NULL,
  cost         REAL    NOT NULL,
  cycle        TEXT    NOT NULL,
  monthly_cost REAL    NOT NULL,
  active       INTEGER NOT NULL DEFAULT 1,
  note         TEXT
);

CREATE TABLE IF NOT EXISTS budget (
  id       INTEGER PRIMARY KEY,
  month    TEXT NOT NULL,
  name     TEXT NOT NULL,
  category TEXT NOT NULL,
  amount   REAL NOT NULL,
  source   TEXT NOT NULL,
  note     TEXT,
  UNIQUE(month, name)
);
CREATE INDEX IF NOT EXISTS ix_budget_month ON budget(month);

CREATE TABLE IF NOT EXISTS report (
  date         TEXT PRIMARY KEY,
  content      TEXT    NOT NULL,
  generated_at INTEGER NOT NULL
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    """Open (creating parent dirs as needed) in autocommit mode.

    isolation_level=None means writes land without an explicit commit and
    there is never a half-open transaction to reason about — the right trade
    for a single-user local app with no concurrent writers.
    """
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    # No `PRAGMA foreign_keys=ON`: the schema declares no foreign keys at all
    # (`PRAGMA foreign_key_list` returns nothing for all seven tables), and
    # `categories.py` names "a table, three foreign keys and a service" as the design this
    # app deliberately rejected. The pragma's only other effects govern `ALTER TABLE RENAME`
    # and `DROP TABLE`, neither of which this codebase issues — `_ADD_COLUMNS` only ever
    # ADDs. It read as a safety measure while enforcing nothing; its test asserted the
    # pragma was set rather than that anything was enforced.
    return conn


# Columns added to a table that already exists. `CREATE TABLE IF NOT EXISTS` is a no-op
# on an existing database, so a new column has to be ALTERed in or every reader breaks on
# a database created before it. Nullable and no default, which is what makes the ALTER
# cheap and the old rows meaningful: NULL means "an ordinary expense", not "0 months".
_ADD_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("expense", "prepaid_months", "INTEGER"),
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    for table, column, decl in _ADD_COLUMNS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    # Monotonic, never a bare assignment, because the stamp's whole job is to say which
    # shapes a reader can expect and a bare `PRAGMA user_version=N` can move it *down*.
    # iCloud syncs one database between installs and the daily `day` is a separate
    # `uv tool install` that can lag this checkout, so `ensure_schema` does run against
    # databases newer than the code running it.
    #
    # Be precise about what this buys: it cannot repair the past. A release older than this
    # one runs its own `ensure_schema` with the bare assignment, so an old `day` will still
    # stamp a newer file down — the guard only holds from here forward. What it does
    # guarantee is that no *future* version of this function lies about a file it did not
    # write.
    #
    # And a newer database is not silently accepted. Nothing else reads `user_version`, so
    # without this the one place that knows would notice and say nothing: `_DDL` is
    # `IF NOT EXISTS` and `_ADD_COLUMNS` is additive, so an older reader opens a newer file,
    # no-ops every statement, and then reads and writes a schema it does not know about.
    # Additive-only evolution makes that survivable today rather than safe in principle,
    # which is exactly the kind of thing worth a line in the log rather than silence.
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if SCHEMA_VERSION > current:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    elif current > SCHEMA_VERSION:
        logging.getLogger(__name__).warning(
            "database is at schema %d but this daylogs knows %d — leaving the stamp alone; "
            "columns added by the newer version are invisible here",
            current,
            SCHEMA_VERSION,
        )


def table_names(conn: sqlite3.Connection) -> list[str]:
    """The user's tables, in a stable order.

    Asked of the database rather than kept as a list beside `_DDL`, so a table
    added later cannot be silently left out of an export. `sqlite_%` names are
    SQLite's own bookkeeping (`sqlite_sequence` and friends) — implementation
    detail, not anybody's data.
    """
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    )
    return [r[0] for r in rows]
