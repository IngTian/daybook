import sqlite3

import pytest

from daylogs.__main__ import main


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("DAYLOGS_HOME", str(tmp_path))
    return tmp_path


def _seed_weight(kg=70.0):
    from daylogs.body import add_weight
    from daylogs.config import load_config
    from daylogs.db import connect, ensure_schema

    cfg = load_config()
    conn = connect(cfg.db_path)
    ensure_schema(conn)
    add_weight(conn, kg=kg, date="2026-08-27", at=1)
    conn.close()


def test_version(capsys):
    assert main(["--version"]) == 0
    assert "daylogs" in capsys.readouterr().out.lower()


def test_unknown_command_is_an_error():
    assert main(["nonsense"]) != 0


def test_summary_prints_generated_content(capsys, monkeypatch):
    async def fake_runner(system_prompt, user_prompt, *, timeout_sec, model=None):
        return "## Body\n\nAll steady."

    monkeypatch.setattr("daylogs.__main__.claude.run_oneshot_text", fake_runner)
    assert main(["summary"]) == 0
    assert "All steady." in capsys.readouterr().out


def test_summary_honours_explicit_date(capsys, monkeypatch):
    async def fake_runner(system_prompt, user_prompt, *, timeout_sec, model=None):
        assert '"target_date": "2026-08-20"' in user_prompt
        return "dated"

    monkeypatch.setattr("daylogs.__main__.claude.run_oneshot_text", fake_runner)
    assert main(["summary", "--date", "2026-08-20"]) == 0
    assert "dated" in capsys.readouterr().out


def test_summary_persists_so_a_second_run_can_read_it(monkeypatch):
    async def fake_runner(system_prompt, user_prompt, *, timeout_sec, model=None):
        return "persisted"

    monkeypatch.setattr("daylogs.__main__.claude.run_oneshot_text", fake_runner)
    assert main(["summary", "--date", "2026-08-20"]) == 0

    from daylogs.config import load_config
    from daylogs.db import connect
    from daylogs.summary import get_report

    conn = connect(load_config().db_path)
    assert get_report(conn, "2026-08-20")["content"] == "persisted"
    conn.close()


def test_summary_reports_failure_with_nonzero_exit(capsys, monkeypatch):
    from daylogs.claude import ClaudeError

    async def boom(*a, **k):
        raise ClaudeError("no claude on PATH")

    monkeypatch.setattr("daylogs.__main__.claude.run_oneshot_text", boom)
    assert main(["summary"]) != 0
    assert "no claude" in capsys.readouterr().err


def test_summary_rejects_a_bad_date(capsys):
    assert main(["summary", "--date", "20-08-2026"]) == 2
    assert "YYYY-MM-DD" in capsys.readouterr().err


def test_backup_writes_a_readable_copy(tmp_path, capsys):
    _seed_weight()
    dest = tmp_path / "backups"
    assert main(["backup", str(dest)]) == 0
    out = capsys.readouterr().out.strip()
    copies = list(dest.glob("daylogs-*.db"))
    assert len(copies) == 1
    assert str(copies[0]) in out
    c = sqlite3.connect(copies[0])
    assert c.execute("SELECT count(*) FROM weight").fetchone()[0] == 1
    c.close()


def test_backup_creates_the_destination_directory(tmp_path):
    assert main(["backup", str(tmp_path / "deep" / "nested")]) == 0
    assert (tmp_path / "deep" / "nested").is_dir()


def test_backup_is_repeatable_same_day(tmp_path):
    dest = tmp_path / "b"
    assert main(["backup", str(dest)]) == 0
    assert main(["backup", str(dest)]) == 0
    assert len(list(dest.glob("daylogs-*.db"))) == 1


def test_running_a_command_creates_the_schema(tmp_path):
    assert main(["backup", str(tmp_path / "b")]) == 0
    from daylogs.config import load_config
    from daylogs.db import TABLES

    conn = sqlite3.connect(load_config().db_path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert set(TABLES) <= names


# ── day export ────────────────────────────────────────────────────────────


def test_export_writes_a_csv_per_table(tmp_path, capsys):
    import csv

    _seed_weight(kg=78.2)
    dest = tmp_path / "out"
    assert main(["export", str(dest)]) == 0
    cap = capsys.readouterr()

    dirs = list(dest.glob("daylogs-export-*"))
    assert len(dirs) == 1, f"expected one dated directory, got {dirs}"
    written = sorted(p.name for p in dirs[0].glob("*.csv"))
    assert written == [
        "activity.csv", "budget.csv", "expense.csv", "food.csv",
        "recurring.csv", "report.csv", "weight.csv",
    ], written

    with (dirs[0] / "weight.csv").open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1 and rows[0]["kg"] == "78.2"

    assert str(dirs[0]) == cap.out.strip(), (
        "stdout must be exactly the directory, so $(day export …) is usable"
    )


def test_export_reports_row_counts_on_stderr_not_stdout(tmp_path, capsys):
    """The path is the machine-readable output; the counts are for the human
    watching. Mixing them would break `cd "$(day export ~/Drive)"`."""
    _seed_weight()
    assert main(["export", str(tmp_path / "out")]) == 0
    cap = capsys.readouterr()
    assert "weight" in cap.err and "1" in cap.err, f"no counts on stderr: {cap.err!r}"
    assert "weight" not in cap.out, f"counts leaked into stdout: {cap.out!r}"
    assert cap.out.count("\n") == 1, f"stdout should be one line, got {cap.out!r}"


def test_export_creates_the_destination_directory(tmp_path):
    assert main(["export", str(tmp_path / "deep" / "nested")]) == 0
    assert list((tmp_path / "deep" / "nested").glob("daylogs-export-*"))


def test_export_is_repeatable_same_day(tmp_path):
    dest = tmp_path / "e"
    assert main(["export", str(dest)]) == 0
    assert main(["export", str(dest)]) == 0
    assert len(list(dest.glob("daylogs-export-*"))) == 1, "a second export made a second directory"


def test_export_on_an_empty_database_still_writes_every_header(tmp_path):
    """Nothing logged yet is not an error, and an empty export that silently wrote
    no files would look identical to a broken one."""
    import csv

    dest = tmp_path / "out"
    assert main(["export", str(dest)]) == 0
    d = next(iter(dest.glob("daylogs-export-*")))
    files = sorted(d.glob("*.csv"))
    assert len(files) == 7, "a new table must appear in export without touching the exporter"
    for f in files:
        with f.open(newline="") as fh:
            header = next(csv.reader(fh))
        assert header, f"{f.name} has no header row"


def test_export_reports_an_unusable_destination_without_a_traceback(tmp_path, capsys):
    """A typo in the path is a user error, not a crash. `summary` already answers
    those with a message and a non-zero exit; a brand-new command should not be the
    one that prints fifteen lines of pathlib internals instead.

    (`backup` has the same rough edge and is deliberately left alone here — it is
    pre-existing, and its exit code is already correct.)
    """
    blocker = tmp_path / "a-file-not-a-dir"
    blocker.write_text("x")
    assert main(["export", str(blocker)]) == 1
    cap = capsys.readouterr()
    assert "Traceback" not in cap.err, f"a traceback reached the user:\n{cap.err}"
    assert str(blocker) in cap.err, f"the failing path was not named: {cap.err!r}"
    assert cap.out == "", f"a failed export still printed a path: {cap.out!r}"


# ── the daybook -> daylogs migration guard ────────────────────────────────


def test_a_pre_rename_data_root_refuses_to_start_and_names_the_fix(tmp_path, monkeypatch, capsys):
    """Starting fresh beside an old data root would silently hide the history.

    The rename moved the default root from ~/Documents/daybook to
    ~/Documents/daylogs. If the new root is absent, `connect` would happily
    create an empty database and the app would open with none of the user's
    entries — indistinguishable from data loss, from the user's side.
    """
    legacy = tmp_path / "daybook"
    legacy.mkdir()
    (legacy / "daybook.db").write_bytes(b"")
    new_root = tmp_path / "daylogs"
    monkeypatch.setenv("DAYLOGS_HOME", str(new_root))

    # `export` rather than no-args: it runs the same load_config -> guard ->
    # connect path a bare `day` does, but returns instead of launching a TUI, so
    # a broken guard fails the assertion rather than hanging the suite.
    assert main(["export", str(tmp_path / "out")]) == 1
    err = capsys.readouterr().err
    assert str(legacy) in err, f"the old path was not named: {err!r}"
    assert "mv" in err, f"the fix was not spelled out: {err!r}"
    assert not new_root.exists(), "refusing to start must not create the new root"


def test_the_guard_stays_out_of_the_way_once_the_move_has_happened(tmp_path, monkeypatch):
    """A legacy directory that is merely *present* must not block a migrated user.

    Deliberately not `--version`, which returns before the config is even loaded
    and would pass whatever the guard did.
    """
    legacy = tmp_path / "daybook"
    legacy.mkdir()
    (legacy / "daybook.db").write_bytes(b"")
    new_root = tmp_path / "daylogs"
    new_root.mkdir()
    monkeypatch.setenv("DAYLOGS_HOME", str(new_root))

    assert main(["export", str(tmp_path / "out")]) == 0


def test_day_summary_picks_yesterday_in_the_configured_zone(tmp_path, monkeypatch):
    """`cfg` was already in hand and unused for this, so the CLI dated its target by the
    *machine's* clock. With `timezone` pinned away from the machine — which the README
    advertises for travelling — cron wrote a report dated a day the app never looks for:
    the app then spent a second `claude -p` call, and in one direction the CLI summarised
    the configured zone's *today*, the half-finished day `summary.py` exists to avoid.
    """
    import datetime as dt
    from zoneinfo import ZoneInfo

    from daylogs import __main__ as cli

    (tmp_path / "config.toml").write_text('timezone = "Pacific/Kiritimati"\n')
    seen = {}

    async def fake_generate(conn, cfg, *, date, runner):
        seen["date"] = date
        return "ok"

    monkeypatch.setattr(cli.summary, "generate", fake_generate)
    monkeypatch.setenv("DAYLOGS_HOME", str(tmp_path))

    # Kiritimati is UTC+14, so its "today" is ahead of UTC's for most of the day; the
    # target must be *its* yesterday, whatever zone the machine is in.
    #
    # Read either side of the call and accept both. This is the one test left that consults a
    # live clock — `cli.main` has no `now` seam and adding one to production to serve a test
    # is not worth it — so a run that straddles midnight in Kiritimati would otherwise
    # compare two different days and fail roughly once in a blue moon, unreproducibly.
    def kiritimati_yesterday() -> str:
        day = dt.datetime.now(ZoneInfo("Pacific/Kiritimati")).date()
        return (day - dt.timedelta(days=1)).isoformat()

    before = kiritimati_yesterday()
    assert cli.main(["summary"]) == 0
    assert seen["date"] in {before, kiritimati_yesterday()}, (
        f"dated by the machine, not the config: {seen}"
    )
