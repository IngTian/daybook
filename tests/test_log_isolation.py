"""The suite must not write to the owner's real log directory.

Found while adversarially re-checking an audit proposal about `log.py`: two verdicts disagreed
about whether `log_dir`'s unused `root=` parameter was load-bearing, and checking which was
right surfaced that neither mattered — the suite had been appending to the live
`~/.daylogs/logs/daylogs.log` all along. `tests/test_cli.py` calls `main([...])` twenty times
and each one runs `setup_logging()`, which resolves `Path.home()`.

Nothing failed, which is why it lasted: a test polluting a file nobody asserts on is invisible
until someone measures the mtime.
"""

import logging
import pathlib

from daylogs import log
from daylogs.__main__ import main


def test_log_dir_is_redirected_away_from_home(tmp_path):
    """The autouse fixture in conftest is what holds this; this is its tripwire.

    Asserted on the resolved path rather than on a side effect, because the failure mode is a
    write to a path outside `tmp_path` — which a test cannot safely provoke in order to check.
    """
    d = log.log_dir()
    assert str(d).startswith(str(tmp_path.parent.parent)) or "pytest" in str(d), d
    assert pathlib.Path.home() not in d.parents, (
        f"log_dir still resolves under the real home: {d}"
    )


def test_running_the_cli_writes_its_log_under_tmp(tmp_path, monkeypatch, capsys):
    """`setup_logging` is called from `main` after arg-parsing, so any CLI test triggers it.

    `export` rather than a bare run: it goes through the same `load_config` -> `setup_logging`
    -> `connect` path without launching a TUI.
    """
    monkeypatch.setenv("DAYLOGS_HOME", str(tmp_path / "home"))
    assert main(["export", str(tmp_path / "out")]) == 0
    written = list((log.log_dir()).glob("*.log"))
    assert written, f"no log written under {log.log_dir()}"
    assert pathlib.Path.home() not in written[0].parents, written[0]


def test_the_root_handler_does_not_point_at_the_real_log(tmp_path):
    """`setup_logging` does `root_logger.handlers = [handler]`, so a leaked handler keeps
    writing to whatever it was built with for the rest of the session — the reason this is
    patched at `log_dir` rather than cleaned up afterwards."""
    log.setup_logging()
    targets = [
        pathlib.Path(h.baseFilename)
        for h in logging.getLogger().handlers
        if hasattr(h, "baseFilename")
    ]
    assert targets, "setup_logging installed no file handler"
    for t in targets:
        assert pathlib.Path.home() not in t.parents, t
