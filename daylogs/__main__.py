"""Entry point.

No arguments launches the TUI. The subcommands exist so the summary and a
backup can run headless from cron without any code change — and because a
single-file SQLite database that only exists in one place is one accident away
from being no database at all.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from daylogs import __version__, claude, export, summary
from daylogs.config import load_config
from daylogs.db import connect, ensure_schema
from daylogs.log import setup_logging
from daylogs.money import MoneyError, check_date


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="day", description="a personal daily log: weight, food, expenses"
    )
    parser.add_argument("--version", action="store_true", help="print version and exit")
    sub = parser.add_subparsers(dest="cmd")

    p_sum = sub.add_parser("summary", help="generate the daily summary and print it")
    p_sum.add_argument("--date", help="YYYY-MM-DD; defaults to yesterday")

    p_bak = sub.add_parser("backup", help="write a consistent copy of the database")
    p_bak.add_argument("dest", help="destination directory")

    p_exp = sub.add_parser("export", help="write one CSV per table, readable anywhere")
    p_exp.add_argument("dest", help="destination directory")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code) if e.code else 0

    if args.version:
        print(f"daylogs {__version__}")
        return 0

    cfg = load_config()
    setup_logging()
    conn = connect(cfg.db_path)
    ensure_schema(conn)

    try:
        if args.cmd == "summary":
            return _summary(conn, cfg, args.date)
        if args.cmd == "backup":
            return _backup(conn, Path(args.dest).expanduser())
        if args.cmd == "export":
            return _export(conn, Path(args.dest).expanduser())
        return _tui(conn, cfg)
    finally:
        conn.close()


def _summary(conn, cfg, date: str | None) -> int:
    # The configured zone, not the machine's. `cfg` was already in hand and unused for
    # this: with `timezone` pinned away from the machine — which the README advertises —
    # cron wrote a report dated a day the app never looks for, so the app spent a second
    # `claude -p` call, and in one direction the CLI summarised the configured zone's
    # *today*: the half-finished day this module's docstring exists to avoid.
    today = dt.datetime.now(ZoneInfo(cfg.timezone)).date().isoformat()
    target = date or summary.target_date(today)
    try:
        check_date(target)
    except MoneyError as e:
        print(str(e), file=sys.stderr)
        return 2
    try:
        content = asyncio.run(
            summary.generate(conn, cfg, date=target, runner=claude.run_oneshot_text)
        )
    except Exception as e:  # top-level CLI boundary
        print(str(e), file=sys.stderr)
        return 1
    print(content)
    return 0


def _backup(conn, dest: Path) -> int:
    """VACUUM INTO writes a consistent single-file copy without stopping the
    app — exactly what a cron backup to a synced folder needs."""
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"daylogs-{dt.date.today().isoformat()}.db"
    out.unlink(missing_ok=True)
    conn.execute("VACUUM INTO ?", (str(out),))
    print(out)
    return 0


def _export(conn, dest: Path) -> int:
    """One CSV per table, so the data is readable without daylogs.

    stdout is *only* the directory, so `cd "$(day export ~/Drive)"` works; the
    per-table counts go to stderr, where a human sees them and a script does not
    have to parse around them.
    """
    try:
        written = export.export_csv(conn, dest)
    except OSError as e:
        # A path you cannot write to is a typo, not a crash. `summary` already
        # answers user error with a message and a non-zero exit; printing pathlib's
        # traceback instead would just be the newest command being the rudest.
        print(f"cannot export to {dest}: {e.strerror or e}", file=sys.stderr)
        return 1
    counts = export.row_counts(conn)
    for path in written:
        print(f"{path.name:16} {counts[path.stem]:>7} rows", file=sys.stderr)
    print(written[0].parent if written else dest)
    return 0


def _tui(conn, cfg) -> int:
    from daylogs.tui.app import DaylogsApp

    DaylogsApp(cfg, conn).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
