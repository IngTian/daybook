"""Your data, in files anything can read.

`day backup` writes a copy only daylogs can open. That is the right tool for
losing your laptop and the wrong one for looking at three years of spend in a
spreadsheet, so this writes one CSV per table instead.

CSV rather than JSON because every one of the seven tables is flat scalars, so
JSON's type fidelity would buy almost nothing, while "double-click it" is the
most likely thing anyone actually does with an export. The one real cost is that
CSV cannot tell NULL from an empty string — a note that was never written and a
note that was written empty come out the same. Documented rather than worked
around, because inventing a sentinel would be worse.

One file per table, mirroring the schema, rather than a denormalised sheet per
subject: weight and food have different grains — one reading against several
meals a day — so joining them would be a reporting decision this module has no
business making. The daily summary is where the human-readable job is done.

Not an importer. Round-tripping an export back in raises real conflict questions
and the one-shot importer this project already retired is the cautionary tale.
"""

from __future__ import annotations

import csv
import datetime as dt
import sqlite3
from pathlib import Path

from daylogs.db import table_names


def export_csv(
    conn: sqlite3.Connection,
    dest: Path | str,
    *,
    today: dt.date | None = None,
) -> list[Path]:
    """Write one CSV per table under a dated directory. Returns what it wrote.

    The date goes on the directory rather than the filenames — seven files cannot
    each carry it the way `backup`'s single file does — so exporting twice on
    different days keeps both snapshots, and twice on the same day replaces it.

    `today` is injected for the same reason the parsers take `now`: no test
    result should depend on when it runs.
    """
    stamp = (today or dt.date.today()).isoformat()
    out_dir = Path(dest).expanduser() / f"daylogs-export-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for table in table_names(conn):
        path = out_dir / f"{table}.csv"
        # rowid, not id: `report` is keyed by date and has no id column, while every
        # table here has a rowid. This is a guarantee rather than a fix — SQLite does
        # return insertion order for a bare SELECT today, so removing the ORDER BY
        # fails no test — but the ordering is what makes two exports of an unchanged
        # database byte-identical, and SQL promises nothing without it.
        # The f-string is safe: `table` comes from `table_names(conn)`, which reads
        # `sqlite_master`, so no caller-supplied text ever reaches this SQL.
        cur = conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
        # newline="" is required by the csv module, not optional tidiness: without
        # it a value containing a newline — every generated summary — is written
        # with \r\n and read back as extra rows on some platforms.
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow([c[0] for c in cur.description])
            writer.writerows(cur)
        written.append(path)
    return written


def row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Rows per table, for the CLI to report what it just wrote.

    The f-string is safe for the same reason as in `export_csv`: `t` comes from
    `table_names(conn)`, which reads `sqlite_master`, never a caller.
    """
    return {
        t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in table_names(conn)
    }
