"""Behaviour shared by the two tabs that draw width-sensitive panels.

Both concerns here are the same concern — a panel's contents are built to its own
measured width, so they have to be rebuilt when that width changes — and both were
written twice (three times for the width probe, with three different floor values
and the same magic fallback).

Kept to exactly that. `focus_default`, the cursor-bounds check and the prompt
dispatch chain are also near-identical between the tabs, and are deliberately
*not* here: they are one or two lines each, and a one-line override that you can
read in place beats an inherited one you have to go and find.
"""

from __future__ import annotations

from textual.containers import Vertical

# Wide enough to be worth drawing into before the first layout has happened.
_FALLBACK = 46


class PanelTab(Vertical):
    """A tab whose panels size their own contents."""

    # The width the panels were last built for. A class attribute so no subclass has to
    # remember to call a constructor; the first resize never matches it.
    _built_for_width = -1
    # Whether that build had to guess. `panel_width` sets it whenever it falls back, and
    # the guard below refuses to latch a guessed width — see `on_resize`.
    _used_fallback = True

    def on_resize(self, event) -> None:
        """`Resize` is delivered to widgets, not to the App, so an App-level handler
        never fires — each tab has to ask for its own redraw.

        Only on a **width** change, which is the whole reason this handler exists: a
        panel's contents are built to its own measured width. Height rebuilds nothing,
        and rebuilding anyway is not merely wasted work — `reload` calls `_fill_table`,
        which does `table.clear(columns=True)`, so the DataTable cursor goes back to row
        0. Everything in the bottom container changes the tab's height by appearing:
        opening any prompt did it (documented as pre-existing, and it is this), and so
        does the in-progress popup, which is how a photo estimate started throwing away
        the food row you had selected.

        A width alone is not enough to latch on, though, and the first version of this
        guard was wrong for exactly that reason. `show_scope` reloads the tab it just
        switched to *before* that pane is laid out, so `panel_width` measures 0 and takes
        the 46-column `_FALLBACK`; the Resize that would correct it then arrives at the
        same *tab* width and was dropped, so the guessed build stuck. From the second
        visit onward every chart and bar drew at 46 columns inside a 96-column panel and
        only healed on the next key that happened to reload. `refresh_tabs()` — `u`, `h`,
        `n` — has the same shape for the two tabs that are not on screen. So a build that
        had to guess is never treated as settled.

        Known, separate, and older than this guard: the reload happens *during* the
        resize, when a panel's `content_size` still holds the previous layout's width. So
        the contents are built to the width the panel is about to stop having — dragging
        120 columns to 80 measured a 76-column panel and drew a 36-column chart in it,
        until the next keypress reloaded the tab. `call_after_refresh(self.reload)` fixes
        it in a hand-run app and could not be made to fail-then-pass reliably in the test
        harness, so it is written down here rather than shipped on a coin flip.
        """
        if event.size.width == self._built_for_width and not self._used_fallback:
            return
        self._built_for_width = event.size.width
        # Cleared before the reload, so `panel_width` can set it again for THIS build.
        self._used_fallback = False
        self.reload()

    def panel_width(self, selector: str, *, minimum: int) -> int:
        """The usable width inside a panel, measured from the panel itself.

        A constant wider than the panel makes every row wrap, which doubles the
        panel's height and looks broken. `content_size` is 0 before the first
        layout, hence the fallback and the app's one `call_after_refresh` redraw.

        Falling back is recorded rather than silent: `on_resize` needs to know that the
        width it is about to latch was a guess, or it discards the very Resize that would
        replace the guess with a measurement. Sticky across the panels of one build — any
        panel guessing makes the whole build a guess, which is the conservative direction.
        """
        try:
            avail = self.query_one(selector).content_size.width
        except Exception:  # before the first layout there is no size
            avail = 0
        if not avail:
            self._used_fallback = True
        return max(minimum, avail or _FALLBACK)
