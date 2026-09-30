#!/usr/bin/env python3
"""Render the README's three screenshots from a live app.

    .venv/bin/python tools/screenshots.py            # assets/{day,body,money}.png
    .venv/bin/python tools/screenshots.py --social   # assets/social-preview.png

The venv's interpreter, not a bare `python`: this drives a real app, so it needs `textual`
and an importable `daylogs`.

Writes `assets/{day,body,money}.png`. Run it after any change that moves the
layout, and commit what it produces.

The README used to carry hand-drawn ASCII of the Day tab. It drifted twice: the
box borders fell out of alignment because `▼` and `🐄` are double-width and the
padding was counted in characters, and an earlier version advertised a key that
was never bound. Both are the same bug — an illustration maintained by hand
cannot be verified against the thing it illustrates. Rendering from the real app
makes that class of error impossible.

Everything here is synthetic and deliberately round. This repo is public, and a
screenshot is the one artifact where real numbers would be published verbatim.

Output is deterministic: the clock is pinned, and `report.generated_at` is
overwritten with a fixed stamp because `upsert_report` stamps `time.time()` and
takes no injection point. Two consecutive runs produce identical files, so a
regeneration that shows a diff means the UI actually changed.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from daylogs.body import add_food, add_weight  # noqa: E402
from daylogs.config import load_config  # noqa: E402
from daylogs.db import connect, ensure_schema  # noqa: E402
from daylogs.money import add_expense, upsert_budget, upsert_recurring  # noqa: E402
from daylogs.summary import upsert_report  # noqa: E402
from daylogs.tui.app import DaylogsApp  # noqa: E402

ASSETS = ROOT / "assets"
TZ = ZoneInfo("America/Toronto")
NOW = dt.datetime(2026, 8, 30, 9, 30, tzinfo=TZ)
TODAY = NOW.date().isoformat()
REPORT_DATE = "2026-08-29"
GENERATED_AT = int(dt.datetime(2026, 8, 30, 6, 12, tzinfo=TZ).timestamp())

# 100x30 fits the two side-by-side panels without triggering the narrow layout,
# and stays legible when GitHub scales the image down to the README's width.
SIZE = (100, 30)

# GitHub's social preview card wants 1280x640 — a 2:1 aspect, which nothing in the README
# uses. A terminal cell in these SVGs is ~14 x 29.5 px, so 120 x 28 renders 1680 x 826,
# an aspect of 2.03: close enough that cropping to exactly 2:1 loses a couple of pixels
# rather than a row of content. 120 columns also stays clear of the 100-column `-narrow`
# breakpoint, so the two panels sit side by side instead of stacking.
#
# Without this image every link to the repo — a message, a post, a search result — renders
# a card with no picture, which for a TUI is the whole pitch thrown away.
SOCIAL_SIZE = (120, 28)
SOCIAL_WIDTH = 1280

WEIGHTS = [71.9, 71.7, 71.6, 71.4, 71.5, 71.2, 71.1]

FOOD = [
    ("yogurt and berries", 320, "labeled", (8, 5)),
    ("chicken salad", 610, "estimated", (12, 40)),
    ("almonds", 180, "labeled", (15, 20)),
]

# Two categories deliberately over budget, so the ⚠ glyph and the over-budget
# colour both appear. A screenshot of an all-green month shows neither.
EXPENSES = [
    ("housing", 1400.00, "rent", 1),
    ("grocery", 84.10, "weekly shop", 4),
    ("grocery", 62.40, "market", 12),
    ("grocery", 71.25, "weekly shop", 19),
    ("restaurant", 48.00, "dinner out", 8),
    ("restaurant", 31.50, "lunch", 19),
    ("restaurant", 52.00, "dinner out", 26),
    ("transport", 120.00, "transit pass", 2),
    ("utilities", 95.00, "hydro", 6),
    ("subscriptions", 20.99, "streaming", 1),
    ("entertainment", 54.00, "cinema", 15),
    ("education", 39.00, "book", 22),
]

RECURRING = [("Streaming", "subscriptions", 20.99), ("Transit", "transport", 120.00)]

BUDGETS = [
    ("Rent", "housing", 1400.00),
    ("Grocery", "grocery", 400.00),
    ("Dining", "restaurant", 120.00),
    ("Utilities", "utilities", 90.00),
    ("Transport", "transport", 130.00),
    ("Fun", "entertainment", 60.00),
]

REPORT = """## Body

Weight is down 0.8 across the week, and three of the last four days came in under
maintenance. The pattern is steady rather than sharp, which is the kind that holds.

## Money

Groceries are tracking under budget with two days left. Utilities went over by 5.00 --
the hydro bill landed early, so it is a timing artifact rather than a real overrun.
"""

# `activity` is set so the screenshots show the ENERGY panel as it reads with a
# maintenance figure — the common case, and the whole point of the setting. It is a
# profile baseline rather than a logged activity row deliberately: an `a`-logged
# factor would advertise a flow this release does not ship.
CONFIG = """timezone   = "America/Toronto"
height_cm  = 175.0
sex        = "male"
birthday   = "1995-04-12"
activity   = "light"
"""


def seed(root: Path):
    """Build a throwaway data root and return an open connection to it."""
    (root / "config.toml").write_text(CONFIG)
    cfg = load_config(root)
    conn = connect(cfg.db_path)
    ensure_schema(conn)

    for offset, kg in enumerate(WEIGHTS):
        day = NOW.date() - dt.timedelta(days=len(WEIGHTS) - 1 - offset)
        at = int(dt.datetime(day.year, day.month, day.day, 7, 5, tzinfo=TZ).timestamp())
        add_weight(conn, kg=kg, date=day.isoformat(), at=at)

    for description, kcal, source, (hour, minute) in FOOD:
        at = int(NOW.replace(hour=hour, minute=minute).timestamp())
        add_food(conn, description=description, kcal=kcal, source=source, date=TODAY, at=at)

    for category, amount, description, day in EXPENSES:
        add_expense(
            conn,
            amount=amount,
            description=description,
            category=category,
            date=f"2026-08-{day:02d}",
            cfg=cfg,
        )

    for name, category, cost in RECURRING:
        upsert_recurring(conn, name=name, cost=cost, cycle="monthly", category=category, cfg=cfg)

    for name, category, amount in BUDGETS:
        upsert_budget(
            conn, month="2026-08", name=name, category=category, amount=amount, cfg=cfg
        )

    upsert_report(conn, date=REPORT_DATE, content=REPORT)
    conn.execute("UPDATE report SET generated_at = ?", (GENERATED_AT,))
    return cfg, conn


async def shoot(cfg, conn, out: Path) -> list[Path]:
    app = DaylogsApp(cfg, conn, now=lambda: NOW)
    written = []
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        for key, name in (("1", "day"), ("2", "body"), ("3", "money")):
            await pilot.press(key)
            # Two frames: one for the tab switch, one for the tab's own reload.
            await pilot.pause()
            await pilot.pause()
            app.save_screenshot(f"{name}.svg", path=str(out))
            written.append(out / f"{name}.svg")
    return written


def _svg_aspect(svg: Path) -> float | None:
    """Width / height from the root viewBox, for un-letterboxing.

    Rich's root `<svg>` carries no width or height attributes — only a viewBox.
    The first `width=` in the file is an inner rect a couple of thousand
    characters in, and using it silently gives the wrong aspect.
    """
    match = re.search(r'<svg[^>]*viewBox="0 0 ([0-9.]+) ([0-9.]+)"', svg.read_text()[:4000])
    if not match:
        return None
    width, height = float(match.group(1)), float(match.group(2))
    return width / height if height else None


def _crop_to_aspect(png: Path, aspect: float) -> None:
    """Trim a fitted-to-square render back to the content, centred.

    `qlmanage -s N` fits into an N x N box and pads the short axis, so a 5:3
    screenshot arrives with a third of its height as empty margin. sips is macOS
    built-in and crops from the centre, which is where the padding leaves the
    content. Derived from the SVG's own declared size rather than assuming a
    particular output height.
    """
    probe = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(png)],
        capture_output=True,
        text=True,
    )
    dims = {
        key.strip(): int(value)
        for key, _, value in (line.partition(":") for line in probe.stdout.splitlines())
        if value.strip().isdigit()
    }
    width, height = dims.get("pixelWidth"), dims.get("pixelHeight")
    if not width or not height:
        return
    target = round(width / aspect)
    if target >= height:  # already tight, or taller than we expected
        return
    subprocess.run(["sips", "-c", str(target), str(width), str(png)], capture_output=True)


async def shoot_social(cfg, conn, out: Path) -> Path:
    """The Day tab alone, at the social card's aspect. It is the tab that shows both
    panels and the generated read, i.e. the most of the app in one frame."""
    app = DaylogsApp(cfg, conn, now=lambda: NOW)
    async with app.run_test(size=SOCIAL_SIZE) as pilot:
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause()
        await pilot.pause()
        app.save_screenshot("social-preview.svg", path=str(out))
    return out / "social-preview.svg"


def to_png(svg: Path, png: Path, width: int = 1400) -> bool:
    """Convert the SVG, using whichever converter this machine has.

    The README embeds PNG rather than the SVG, even though the SVG is smaller and
    sharper, because Textual writes the screen as `<text>` elements against
    `font-family: Fira Code, monospace` and embeds no font. A viewer without that
    font substitutes its own metrics, and the glyphs whose advance widths differ
    most are box-drawing and `▼` — precisely the ones whose misalignment in the
    old hand-drawn ASCII this script exists to fix. A raster cannot come apart
    that way.
    """
    candidates = [
        ["rsvg-convert", "-w", str(width), str(svg), "-o", str(png)],
        ["cairosvg", str(svg), "-o", str(png), "--output-width", str(width)],
        # macOS, needs no extra install.
        ["qlmanage", "-t", "-s", str(width), "-o", str(svg.parent), str(svg)],
    ]
    for cmd in candidates:
        # Each candidate is tried until one *works*, not until one exists: a
        # `cairosvg` on PATH whose libcairo is missing installs and resolves
        # fine and then fails at the call, and stopping there would skip a
        # converter that does work.
        if not shutil.which(cmd[0]):
            continue
        if subprocess.run(cmd, capture_output=True).returncode != 0:
            continue
        # qlmanage names its output after the whole filename: day.svg.png, and
        # writes it beside the SVG rather than where we want it.
        stamped = svg.parent / f"{svg.name}.png"
        if stamped.exists():
            stamped.replace(png)
        if not png.exists():
            continue
        if cmd[0] == "qlmanage" and shutil.which("sips"):
            aspect = _svg_aspect(svg)
            if aspect:
                _crop_to_aspect(png, aspect)
        return True
    return False


def main() -> int:
    social_only = "--social" in sys.argv
    ASSETS.mkdir(exist_ok=True)
    # The SVG is an intermediate, not an artifact: only the PNG the README
    # embeds is tracked, so the SVGs are rendered into a directory that goes
    # away and never show up as untracked noise after a regeneration.
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        cfg, conn = seed(work)
        try:
            if social_only:
                svgs = [asyncio.run(shoot_social(cfg, conn, work))]
            else:
                svgs = asyncio.run(shoot(cfg, conn, work))
        finally:
            conn.close()

        failed = []
        for svg in svgs:
            png = ASSETS / f"{svg.stem}.png"
            width = SOCIAL_WIDTH if svg.stem == "social-preview" else 1400
            if to_png(svg, png, width=width):
                if svg.stem == "social-preview":
                    # Exactly 2:1. GitHub letterboxes anything else, which puts grey bars
                    # through the middle of the card.
                    _crop_to_aspect(png, 2.0)
                print(f"  {png.relative_to(ROOT)}")
            else:
                failed.append(png.name)

    if failed:
        print(
            f"\nno converter for {', '.join(failed)}.\n"
            "Install one of: rsvg-convert (brew install librsvg), cairosvg, "
            "or run on macOS, where qlmanage is built in.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
