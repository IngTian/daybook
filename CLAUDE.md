# daylogs — Claude Code context

A single-user terminal app for three things: weight, food, and expenses, plus
one daily summary. Keeping it small is the point, not a side effect: every
feature has to survive daily use, and every layer of complexity has to justify
itself against a simpler version.

## What daylogs is not

Say so plainly if a request drifts into any of these — they were all
deliberately cut, and adding one back is a scope decision, not a detail:

- **No income, no cash balance, no net worth, no projection.** Money answers
  one question: where did it go this month, and am I inside the budget. A
  negative `expense.amount` is a refund, not income.
- No investments, holdings, market data, journal, intel, weather, token
  dashboards, productivity tracking, or exercise logging.
- No HTTP server, no web UI, no MCP server, no workflow engine, no scheduler.
- No fourth feature. Three tabs.

## Layout

```
daylogs/
  config.py   tomllib config + update_config/add_category; DAYLOGS_HOME overrides the root
  db.py       connect + seven-table schema; _ADD_COLUMNS for the one thing DDL cannot do
  categories.py  constant category tuple, extensible via config.toml
  sigil.py    tokeniser: `!` category, `@` time, `~` note, `=` kcal, `#` cycle
  complete.py tab completion for `!` and `#` vocabularies (no Textual import)
  parse.py    pure grammar parsers on top of sigil.py, and the shared amount/limit rules
  horizon.py  the one time-window concept: Span, Axis, resolve_goto
  moneyview.py  all Money tab state as one value with named transitions
  body.py     weight, food, activity; trend windows; Mifflin-St Jeor BMR,
              activity factor -> TDEE ("burn"), BMI, per-day net series, restamp
  money.py    expenses, recurring, budgets, summarize_span
  claude.py   three `claude -p` subprocess wrappers
  photo.py    clipboard (osascript) / inbox / path acquisition
  estimate.py photo-or-text → calorie estimate
  summary.py  payload + one claude call + persist
  export.py   one CSV per table, table list derived from the schema
  markup.py   legacy <num>/<warn> tags → markdown, for reports already stored
  fmt.py      hhmm / human_date — shared by the data and UI layers
  undo.py     in-memory ring buffer of row pre-images (deletes and edits)
  log.py      rotating file log under ~/.daylogs/logs/
  tui/        app shell, footer prompt, three tabs, pure text widgets
              tab order: Day · Body · Money; Day's scope id is still `summary`
              Body sub-views: weight · food · activity (each name is its table)
    keymap.py   every key, as data — generates bindings, footer and help
    hints.py    every prompt's example and grammar, as data
    common.py   PanelTab: on_resize + panel_width, shared by the two big tabs
    chart.py    braille line charts (no Textual import)
    widgets.py  bars, sparklines, colours, markup escaping (no Textual import)
    progress.py the in-progress popup: one line per running claude call
    themes.py   theme names from Textual, and `T`'s live-preview picker
  __main__.py `day`, `day summary`, `day backup`
```

Keep this map current. It is the first thing a reader (or a fresh session) uses to
find anything, and three modules were once missing from it because each round
appended prose where it was convenient rather than editing the map.

## Invariants

- **`daylogs/tui/keymap.py` is the only place keys are declared.** The bindings,
  the contextual footer, and the `?` overlay are all generated from `KEYMAP`.
  Never hand-write a key hint — that is how a footer starts describing keys that
  aren't bound. Tests enforce no duplicate `(key, scope)` and no tab key
  shadowing an app key.
- **The digit keys are bound to named actions, not tab positions.** `Key("2", …,
  "show_body", …)` calls `show_scope("body")`, so moving a pane without moving its
  digit leaves a tab labelled 3 that `2` jumps to. `tuple(_TAB_OF)` carries the order
  the arrow keys walk and a test asserts it matches the `TabPane` order — change all
  three together or the test will tell you.
- **Measured facts are encoded in `Key` flags; don't "simplify" them away — in
  either direction.** `priority=True` on `tab`/`shift+tab` because an ordinary App
  binding loses to the Screen's focus-next. `bind=False` on `enter` because a
  focused DataTable converts it to `RowSelected` before any binding sees it.
  Priority on a printable key would steal `/`, `+`, `-` and `enter` from the
  prompt — verified, and it breaks entry outright. And `left`/`right` are
  deliberately **not** priority: a `cursor_type="row"` DataTable does not claim
  them, so a plain binding already fires with the table focused, while a focused
  Input keeps its cursor movement. Making them priority breaks arrow keys inside a
  line you are typing *and* lets tabs switch behind the `?` overlay — two tests
  fail on it, which is the intended tripwire.
  **A plain binding only wins if something on the active tab is holding focus, so every
  tab's `focus_default` has to focus something.** Day's returned `None` and nothing on it
  was focusable, so `AUTO_FOCUS = "*"` gave focus to TabbedContent's own `ContentTabs`,
  whose `left`/`right` bindings are *also* not priority and therefore beat the App's: the
  arrow moved `active_tab_id` without `show_scope` running, leaving the footer describing
  Day with Body on screen and the arriving table unfocused, so row keys and `enter` did
  nothing. Pressing `1` did not clear it — that calls `focus_default` — so it healed only
  after a `2`/`3`, which is why every test in `test_tui_nav.py` missed it: they all press a
  digit first. Day now focuses `#summary-scroll`, which is also what pages the read.
- **`MoneyView` is the only Money tab state.** Horizon, pane, sort, filters and
  grouping travel as one value with named transitions, because as separate flags
  they are sixteen untested combinations. `anchor` is a **date** (the right-hand
  edge of the span), not a month.
  **A write reveals the row it wrote, in both directions** — `view.reveal(date)`, not
  `anchor = max(anchor, date)`. `max` only ever moved the edge forward, so a backdated
  expense under the default MTD horizon was written and then never shown: the pane omitted
  it, the header total omitted it, and the toast quoted a category total that excluded the
  amount just booked, all three agreeing on the wrong thing while the comment on that line
  claimed the row was now "inside whatever horizon is active". `anchor = date` is enough
  because every horizon resolves to a span *ending* on the anchor — pinned across
  `HORIZONS`, so a future horizon that broke that property would fail rather than strand
  rows. It moves only when the date is genuinely outside the span, or an August row would
  drag a YTD view off September for no reason; `t` is the one keypress home.
- **Two named weight concepts, and they are not interchangeable.** `latest_weight` is
  "what do I weigh now" — the WEIGHT header's headline, which states the reading's clock
  time so it reads as a reading rather than as *the* number, and the BMI beside it.
  `morning_weight` is the day's **first** reading, taken fasted: the trend line, the
  7d/30d deltas and the digest. One function served both and it was `latest_weight`, so
  the collapse kept `MAX(measured_at)`. On real data every twice-weighed day was measured
  early and again mid-morning, so latest-wins took the low end of every day, hid the high
  reading entirely once the window passed `3d`, and made `summary.py`'s prompt false — it
  states `weight_kg` is the morning weigh-in "before any of the food listed" while being
  handed the mid-morning one. `next_morning_kg` was mislabelled the same way. The
  headline and the trend therefore disagree on a twice-weighed day, on purpose.
- **A chart's y-extent describes the window, not the points it drew.** `frame_chart`
  takes `low`/`high` for the weight chart, which plots one point per day while the window
  may hold several readings per day — fitting to the drawn points labelled the top of a
  week as 81.85 with an 82.65 inside it, the same defect as v1 labelling min/max of the
  last 30 entries as though they were the window. The line then does not touch the panel
  edges, which is the honest picture: the value defining the edge is not on screen.
- **One timezone, and `fmt` takes it as an argument.** `cfg.timezone` defaults to the
  *machine's* zone (`config.system_timezone`, from `TZ` then the `/etc/localtime`
  symlink, else UTC) and is always a real IANA name, so every reader can do
  `ZoneInfo(cfg.timezone)` with no None branch. It used to default to the literal
  `"America/Toronto"` while `fmt.hhmm` read the machine — so on any other machine an
  edit's prefill round-trip moved the row by the offset, reachable from a plain `enter`
  on a food row. `fmt.hhmm`/`fmt.wall` and `body.restamp` therefore **require** the
  zone: a default is precisely how that hid, because every call site looked right. A
  zone *name*, not a tzinfo, because DST has to resolve per timestamp — a captured
  offset renders a January reading with August's rules.
  `date` stays authoritative over `ate_at` when the two disagree after a zone change
  (Aug 27 in Toronto is Aug 28 in Kiritimati), so the render is asserted to be *stable*
  rather than instant-preserving: it settles once instead of creeping on every edit.
- **An unlogged day is not a missing day — it is an ordinary one.** You log a day when it was
  *out of the ordinary*; a day with no activity row takes the profile baseline, which is
  what the owner confirmed he relies on. So there is no partial-logging machinery and no
  "did you forget" nagging, and none should be added: there is nothing to forget. The code
  has always said this — `body.py`'s "otherwise the profile baseline, which is the common
  case and needs no input", `db.py`'s "an ordinary day needs no entry at all",
  `summary.py`'s "`profile` is the user's ordinary day and is not news".
  This bullet used to open "activities are assumed to be logged faithfully and in full, every
  day", which was false and read as a demand for daily discipline nobody had agreed to: 8
  rows exist across the table's whole life. An audit priced that as an abandoned feature; the
  table was 27 days old and its first row lands on the day it shipped, so the number measured
  the column's age, not anyone's intent.
  **The second clause was true and stays**: the inference is handed the whole day, not the
  new entry. `body_tab` re-queries `list_activity(conn, date=r.date)` and passes
  `activities=[*logged, r.description]` because a PAL is not additive — that re-query looks
  redundant beside the description the worker already holds, and this is where it is recorded
  as deliberate.
  Do **not** describe activity as "sparse": that word is reserved here for the opposite
  treatment. A foodless day is *absent* from `kcal_series_between`/`net_series_between`
  rather than counted as a fast; an activity-less day is *kept*, at `day_baseline`.
- **The activity factor is never defaulted, and `body.day_tdee` is the only place
  BMR is multiplied by it.** Assuming `desk` for an unset profile would raise
  maintenance 20% and silently restate every figure on screen and in every digest
  already written, so "no factor" is a real state in which every calorie number sits
  against resting BMR exactly as it did before. Several surfaces read the burn — the
  ENERGY panel, the Day tab's BODY block, `summary.build_payload`, and the write toasts —
  and each separate composition is another chance for one of them to measure the same day
  against a different baseline. The FOOD header used to be on that list and no longer is:
  it describes the window of rows beneath it, which is why the day's balance is stated
  once, in the panel. `resolved_factor` also returns *where* the factor came
  from, because that reaches the screen: a multiplier rescales every calorie
  judgement for its day, so it must not arrive with nothing to make you doubt it.
  The level keywords are standard PAL bands **re-described** to mean "a day with
  nothing logged" — the textbook labels bake habitual exercise in, and using those
  while also logging hard days counts the exercise twice.
- **A failed activity inference still records the activity, with a NULL factor.**
  The description is the user's data and the multiplier is a guess; losing the first
  because the second failed is the worse outcome. That NULL is the state `db.py` and
  `resolved_factor` already describe — the day falls back to the profile baseline,
  not to resting BMR. It is also why a factor is **not clearable** through the edit
  prompt: `_update` drops None, so a line with no `=` leaves the stored factor
  alone, which is what keeps a factorless row's description fixable at all.
- **Only the entry path infers a factor; an edit never does.** Fixing a typo in a
  description must not silently re-roll the number the whole day is measured
  against. The activity worker also has its **own** `@work` group: sharing the food
  estimate's would mean logging a gym session cancelled a meal estimate still
  running.
- **Net over a window is computed per day, never as one subtraction.** A factor
  describes a single day, so `avg_in − today's burn` lets one gym session restate a
  whole month. `body.net_series_between` pairs each day's intake with that day's own
  burn and skips days that lack either — a day before the first weigh-in has no BMR
  to scale, and showing its intake as net reads as an enormous surplus.
- **`horizon.py` is the single time-window concept**, shared by the Body chart and
  the Money tab. Never add a second one: v2 had chart windows on Body and ranges
  on Money, so `+` meant different things per tab and neither offered MTD or YTD.
  Spend filters by **date**, not by month — filtering by month made a one-week
  horizon silently cover the whole month.
- **On Body the window governs all three tables, not just the chart.** `+`/`-` moved the
  chart and the weight table and did nothing at all to the food and activity tables — one
  row at `1d` and the same one row at `all` — while the chart above the food table went on
  plotting weight. Every table now takes `since`/`until` from `span`, and `1d` selects
  exactly the rows the old per-day view did, because `horizon.resolve("1d")` gives
  `start == end == the anchor` and labels it with the day. `body.list_food`/`list_activity` keep a `date=` mode
  for `summary.build_payload` and the Day tab, which read a day out loud and want it
  chronological; the window is newest-first *throughout*, within a day as well as across
  days, because it is a log you scroll and the last thing you ate belongs on top. It was
  `stamp ASC` within a day for a while so `1d` matched the digest's order — an equivalence
  nobody checks row by row, against a table that visibly changed direction halfway down. Asking for both raises
  rather than resolving — two questions with two orders, and picking one silently would
  make a call site's intent unreadable.
- **`d`/`k` sort the Body tables, and `k` is one key because each table has one number.**
  Same idiom as Money's `d`/`c`/`k`: a different field switches and resets to descending,
  the same key again flips direction. `k` sorts by kg on weight, kcal on food and factor on
  activity — so its `KEYMAP` label has to stay generic ("by value"), because the table is
  static data and the sub-view is not, and the footer's state row names the actual column
  instead. `body._order_by` builds every window's ORDER BY, so the three sub-views cannot
  drift apart, and it keeps date and clock as the tiebreak so equal numbers hold a stable
  order. The sort name is validated because it arrives from a keypress; the column names it
  maps to are literals in `body.py` and never reach SQL from outside. The `date=` mode takes
  no sort at all — the digest reads a day forwards, full stop. `tab` keeps the sort, the way
  Money keeps it across panes.
- **Three tables over one window means three headers of one shape**: what you are looking
  at, the window, how many rows are in it, and — for food — the intake those rows sum to.
  The day's own energy balance is stated **once**, in the ENERGY panel, which already
  carried every figure of it. That is one fewer surface reading `burn`, which is the point:
  each extra one is another chance for two panels to measure the same day against two
  baselines. The no-factor guidance moved to the panel with it, beside the resting-BMR
  number it explains — the panel showed that fallback with no word about it, because the
  words were in a header that now describes a window.
- **A dated series is plotted against its dates, never its index.** `horizon.axis`
  resolves the plot extent and `braille_line(positions=…)` places each point.
  Index spacing rescales the x-axis to the *sample count*, so two weigh-ins a day
  apart drew a smooth month-long climb across a month-wide panel. Tick labels come
  from the axis too — from the data they printed "Aug 27 / Aug 28 / Aug 28".
- **Escape anything non-literal before it reaches markup**, with `widgets.esc`.
  Textual's content markup wants a **backslash**; `rich.markup.escape` is the wrong
  tool because it leaves a bare `[` alone, and `[` is itself a key — its footer
  hint rendered as `[[/] prev`. Filter text is user input and needs this too.
- **Colour is emphasis, never the only signal.** `widgets.GOOD/BAD/WARN` are
  palette hues, not the ANSI names — bare `red`/`green` resolve to `#ff0000` and
  `#008000`, harsh and near-unreadable on this background. An overrun still carries
  `⚠`, a weight change still carries its arrow. Weight colour assumes down-is-good
  because there is no goal weight to compare against.
- **Markup goes on *after* width arithmetic.** The bar builders return plain text
  and callers colour whole finished lines; colouring first makes `len()` count
  colour codes and silently sheds real content. DataTable cells use `rich.Text`,
  not markup, because the table measures columns from the cell's own render.
  **`widgets.shade` is the one exception, and only because it changes nothing that is
  printed.** A bar whose *segments* mean different things needs part of one line styled, so
  `shade` substitutes markup around an already-measured run — `Content.from_markup(out).plain`
  is asserted byte-identical to the input, which is what keeps the builders' arithmetic
  correct. Textual's markup **nests**, so this composes with the `mark()` the caller wraps
  around the whole row: a budget line ends up with its status colour over the full line and
  `dim` over the glyph run inside it, verified as two spans rather than assumed. It handles
  the first contiguous run, which is all the builders emit. Do not reach for it to colour
  anything the builders did not mark with a glyph of its own.
- **The TREND panel plots one series at a time, and only weight fits its own range.**
  `c` cycles weight · intake · net over the window `+`/`-` already control; the two are
  independent, so zooming must not reset the series. Weight is self-fitted because
  anchored at zero a 70–75 kg band is a flat line at the top of the panel; both calorie
  series pass `include_zero`, because a magnitude fitted to its own minimum reads as a
  climb from nothing and a *signed* net fitted to itself draws the same line whether it
  is a deficit or a surplus. The zero marker is a `┼` on the y-axis, never braille dots
  — dots among the data are indistinguishable from data — and it appears only when zero
  falls strictly inside the extent, because otherwise the extent label already says
  `0`.
- **A sparkline of a magnitude scales `from_zero`.** Fitting to the series' own
  min made six months of level rent land on the lowest glyph, so the
  second-largest spend category rendered as an empty floor. Weight, which only
  means anything relative to itself, keeps min–max.
- **The footer is four rows and generated from `KINDS`.** Row 1 is the tab's state, then
  one row per group: write+danger / view / nav. It was two rows, with the three groups
  sharing row 2 separated by three spaces — the grouping existed but you had to find it
  inside the line, and on Body that line was **189 cells**, which is the wall the grouping
  was meant to prevent. Fixed at four rather than `auto`: a height that follows the active
  scope reflows the table above it on every tab switch, and a frame that holds still is
  worth more than the row a short scope would save.
  **Shedding is per line now.** Each group drops hints from its own tail until that line
  fits, because separate lines no longer compete for width — and the old flat rule broke
  the moment they were split, since it shed from the end of the whole key list, so an
  over-wide *first* group could not shrink until every later group was empty. Measured:
  Money at 40 columns rendered a 48-cell write row above an empty nav row. If even the
  pinned keys do not fit, the line is dropped rather than overflowed, because a line wider
  than the terminal folds and silently steals a row from the table. Stacking also *sheds
  less*: at 120 columns Body used to drop six hints and now drops none.
  It still drops any key the active scope has no handler for, asking the app's own resolver
  so the footer and the dispatcher cannot disagree.
- **`config.toml` is written by two functions that write to opposite ends, on
  purpose.** `update_config` inserts scalars **before the first table header**;
  `add_category` appends a `[[category]]` block **after everything**. TOML's scoping
  is positional and both failures are silent, because the file still parses either
  way: a scalar after `[[category]]` is a field of that table and is simply never read
  again, and a table header among the scalars swallows every scalar below it. Both
  edit the file as text so a hand-written comment survives. Neither is the general
  case of the other — don't unify them.
- **`b` prefills from the selected category, and only on the categories pane.** Editing
  a budget otherwise meant reading the amount off the pane and retyping the line;
  `upsert_budget` is keyed on `(month, name)`, so a changed number always replaced the
  line and what was missing was seeing the current one. It writes to
  `view.months()[-1]` — the month **on screen**, not today's, since `[` walks the
  anchor back — and the toast states that month. A group header on the expenses pane is
  the only other row carrying a slug, and it deliberately prefills nothing: that pane
  shows spend, and one key meaning two things depending on whether `G` is on is the
  cost. `budget` is `UNIQUE(month, name)`, not `(month, category)`, so a category can
  hold several lines; `money.budget_line` offers the newest, and the pane keeps summing
  all of them.
- **A payment can cover several months, and every *total* counts it by the months it
  covers.** `#N` on an expense line sets `expense.prepaid_months`; the row is stored once, at
  the real amount and date, and `_prepaid_shares` spreads `amount/N` across the N calendar
  months from its own. This is not a new opinion about accounting — the budget side has
  always prorated (`roll_month_budgets` writes `monthly_cost`), so summing the raw charge
  made the renewal month read 240.00 against a 20.00 cap while the other eleven read a
  saving. It netted out over a year and no single month was right, which is a problem for a
  tab whose whole question is "am I inside the budget this month".
  **Totals prorate; lists do not.** `_spent_by_category` and the six-month history exclude
  `prepaid_months IS NOT NULL` from their SQL sum and add the shares back by covered month.
  The expenses pane and the digest's day list keep the real charge, because
  those answer a cash question — 240.00 is what left the account, and a list of payments
  showing a twelfth of one would be lying about the row. The pane marks it `#N` so the two
  readings do not look like a contradiction, and `render_expense` round-trips the token so a
  displayed marker stays editable.
  **But the pane also pins the shares it is being counted at, above the payments.** Marking
  the charge `#12` only helps in the month the charge is *in*; in the other eleven the share
  arrives with no row at all, so the second month's header read 20.00 over a list that summed
  to nothing, and the charge's own month read 20.00 over a row saying 240.00. Both are the
  same missing sentence — which payments are being counted here, and for how much — and "the
  marker stops that reading as a contradiction" was only ever true one month in twelve.
  `money.prepaid_inflows(conn, span)` is the one place that answers it: one entry per
  **charge**, not per covered month, since twelve lines for one subscription would bury the
  payments the pane exists for. Its `share` is **unrounded** for the same reason
  `_prepaid_shares` is — round per line and add, and the rows are a cent short of a header
  that rounds once at the end, which is the original defect wearing a smaller hat. It
  filters through `_months_filter`, so a second month rule cannot put the list and the total
  back into disagreement by a different route.
  The rows are `⇢` **and** faint, never faint alone, because these are the one place on the
  pane where the amount is not what left the account. They are **inert** — `_ids` takes `-1`
  like a group header, so `enter`, `x` and the fold all skip them; a share is arithmetic over
  a charge in another month, and arming an edit from one would write to the wrong month. The
  charge's own date is on the line because that is where `g` has to take you. Pinned above
  rather than sorted or grouped in: a share has no date of its own to sort by, and inside a
  category group it would fold into a total that answers a cash question.
  **Both panels split the fill the same way**, because `spent` is two different quantities
  added together and the panels are where the number is actually read: a category showed
  `30.00 / 50.00` as one green bar with 20.00 of it proration. `CategorySpend.prorated`
  carries the split and comes from `_prorated_by_category`, which sums **`prepaid_inflows`**
  rather than re-walking `_prepaid_shares` — the panel's segment and the rows the pane pins
  are then the same arithmetic, and two readers of one question is exactly how a total and
  its own list came to disagree in the first place.
  The segment is a **glyph** (`PRORATED_GLYPH`), with colour added afterwards by `shade`. Not
  a hue on its own: the budget row already carries a status colour that is load-bearing, and
  a segment distinguished by hue would both fight it and break "colour is never the only
  signal". `_prorated_cells` measures on the bar's own scale, clamps to the drawn `filled`
  (a share can exceed it when an over-budget fill is clamped, and spilling past would
  overstate the spend), gives a non-zero share **at least one cell** — same lesson as the
  `from_zero` sparkline, absent is a different claim from small — and draws nothing for a
  negative share, since a part-to-whole has no negative slice.
  The glyph is **named in the panel title, and only when one is drawn**, so a month with no
  prepayment carries no legend it does not need. Asked per panel, because the two genuinely
  disagree: a category with spend and no cap draws no fill to segment, so BUDGET vs SPENT
  can have nothing to explain while WHERE IT WENT is 100% prorated. Budget proration itself
  is untouched — `roll_month_budgets` still writes `monthly_cost`, and the bar's extent is
  still the cap; what changed is that prorated spend no longer eats it invisibly.
  A covered month counts if the span touches it at all — deliberately the same rule
  `_budget_by_category` already uses, since budgets are stored per calendar month and summed
  over `span.months()` however much of each month the span covers. That is what makes a
  monthly figure comparable to a monthly cap, and it inherits the `1w` mismatch already
  recorded for budgets. Shares are summed unrounded and rounded once at the end, so twelve
  months of a 240.01 charge add back to 240.01 rather than drifting a cent a month.
  `parse` rejects `#1` and `money._check_months` rejects it again: a payment covering one
  month *is* an ordinary expense, and a second representation of the plain case is a branch
  in every reader. `0` is the clearing value on `update_expense`, for the same reason `""` is
  the note's — it drops None to tell "not mentioned" from "set to nothing".
- **Adding a column is the one thing `CREATE TABLE IF NOT EXISTS` cannot do.**
  `db._ADD_COLUMNS` is a list of `(table, column, decl)` ALTERed in on every open, guarded by
  `PRAGMA table_info` so it is idempotent. Still not a migration framework, and it must stay
  a list rather than grow into one: the columns are nullable with no default, which is what
  makes the ALTER cheap and leaves old rows meaningful (NULL = "an ordinary expense"). A
  change that needs to *rewrite* rows is where this stops being enough, and that is a
  decision to take deliberately rather than by appending here.
- **An empty state names the fix.** A month nobody rolled has no budget rows, and
  "0.00 budget / 1,234.00 over" is true, useless, and reads as stale data. It says
  what `r` would do instead. `money.pending_roll` must agree with
  `roll_month_budgets` or the header promises what the key won't deliver.
- **No widget carries a width the terminal has to be wide enough for.** The `?` overlay
  was `width: 92`, and a fixed width is still *laid out* at 92 on a narrower screen rather
  than shrinking: at 60 columns its right-hand column sat at x=46..89, so half the keys
  were clipped off the edge with nothing on screen to say so — on the one screen the
  README promises cannot be out of date, while the footer advertises `?` at every width.
  It is `width: 1fr; max-width: 92` now, and under `-narrow` the two key lists stack
  exactly as `.panel-row` does and for the same reason: a 32-cell line does not fit in
  half of a 60-column box, and a truncated label cannot be read at all while a scrolled
  one is only further down. Geometry is what the tests assert, because a clipped widget
  still reports its full content — which is why the tests reading `.content` sat beside
  this defect for a release.
- **Panel content sizes itself from the panel**, via `content_size.width`. A
  hardcoded width wider than the panel wraps every row and doubles its height.
  `content_size` is 0 during `on_mount`, so the app re-renders once via
  `call_after_refresh`, and each widget handles its own `on_resize` — `Resize` is
  delivered to widgets, **not** to the App, so an App-level handler never fires.
  `PanelTab.on_resize` reloads **only on a width change, and only if the last build
  measured rather than guessed**. The width half is the entire reason the handler exists: a
  height change rebuilds nothing, and rebuilding anyway calls `_fill_table`, whose
  `table.clear(columns=True)` resets the DataTable cursor to row 0. The `_used_fallback`
  half is why a width alone is not enough — `refresh_tabs()` (`u`, `h`, `n`) builds the
  off-screen tabs before they are laid out, so `panel_width` measures 0 and takes the
  46-column `_FALLBACK`; the Resize that would fix it arrives at the same *tab* width, and a
  width-only guard dropped it. The flag is cleared *before* the reload so `panel_width` can
  set it again for that build.
  **`show_scope` defers its reload with `call_after_refresh` for the same reason, and does
  not rely on the guard to save it.** Reloading a pane that has not been laid out yet drew
  every visit after the first at 46 columns inside a 96-column panel; correcting that from
  the following `Resize` is a *race* — it held locally and failed on CI, where the event is
  often not delivered before the next thing happens, so one test passed on one PR and failed
  on the next with identical code. A Textual callback fires deterministically; a terminal
  event does not. There is a test asserting the return visit is correct with every `Resize`
  dropped, because "it works when the event arrives" is what was not good enough.
  Everything in the bottom container shortens the tab by appearing — the prompt (recorded
  as a known wart for a while), the in-progress popup, the theme picker — so without the
  guard, starting a photo estimate threw away the food row you had selected. Test it by
  calling `on_resize` with a synthetic `Resize`, not through `pilot.resize_terminal`:
  `pause()` returns before the event is delivered under the suite's 2 ms idle granularity,
  so the end-to-end version passed locally with the guard removed and failed on CI.
- **The summary renders through Textual's `Markdown` widget.** Feed it markdown,
  not Rich markup; the prompt is instructed to emit plain markdown and no LaTeX,
  which no terminal renders. `markup.py` only converts legacy `<num>`/`<warn>`
  tags in reports already stored.
- **Part-to-whole is ranked bars, not a pie.** A terminal has ~8 distinguishable
  fill glyphs against 9 categories, so a pie collides and still needs a legend to
  read amounts.
- **A category can net negative, and every renderer has to survive it.** Refunds
  are first-class (`amount < 0`), so a reimbursed bill can outweigh a month's
  charges. Three separate bugs came from assuming otherwise: shares divided by the
  *signed* total summed past 100%, an unclamped bar width went to -44 of 14 cells
  and pushed the amounts off the panel, and the panels filtered `spent > 0` so the
  refund vanished while the header still showed the net. Shares are of **gross**
  spend; a negative row keeps its amount and shows no share.
- **`chart.py` and `widgets.py` import no Textual.** They are pure functions and
  unit-test as such.
- **A prompt belongs to the tab that opened it, not the tab on screen.**
  `InlinePrompt.owner` is a scope id, defaulting to the active one. Two prompts are opened
  by a *worker* rather than a keypress — `confirm food` and `confirm activity`, arriving up
  to a minute later — and the popup exists precisely so you can look at another tab while
  you wait. Routed by the active tab, the answer went to a tab with no branch for that
  label and no `else`: `enter` closed the prompt and wrote **nothing**, with no toast and no
  way back, since `show_scope` refuses to switch while a prompt is open. A logged activity
  vanishing is what the NULL-factor invariant exists to prevent, and this lost it on the
  success path. The two async call sites pass `owner=self.id`; a keypress-opened prompt
  cannot be mis-routed, because the tab cannot change while it is open. The *answer* routes
  by owner and *focus* goes to the active tab — focusing a hidden tab's table is not a
  thing.
- **Work in flight is announced by the popup, app-level, and nowhere else.** A
  `claude -p` call runs for seconds to a minute, so the indicator has to outlive a
  toast — that part was already true as a `"   estimating…"` suffix on the FOOD header
  and `"   generating…"` on SUMMARY. What was wrong is that both lived on the tab that
  started the work: pressing `3` erased every trace of a running estimate and the answer
  arrived later as a prompt with no explanation. `app.begin_work(key, label, timeout)` /
  `end_work(key)` is the only way to say it now, and a tab that grows a second indicator
  is reintroducing the bug. Keyed, not a boolean: a food estimate, an activity inference
  and the daily read can all be in flight at once, and the first two have separate worker
  groups on purpose. Elapsed is shown against the call's own budget (`18s / 60s`) because
  with animations off a static word cannot say "still alive"; the timer exists only while
  a job does. It lives in `#bottom` with the prompt and the footer rather than floating,
  so it pushes content up instead of covering the row you are reading, and costs no rows
  when hidden. `summary.generate` takes `on_attempt` because each retry gets the *whole*
  timeout, so the clock has to restart per attempt or the second attempt reads "200s / 120s"
  — larger than the budget it is shown against.
- **Animations stay off** (`self.animation_level = "none"`), because off is never
  slower and nothing here is worth animating. This used to cite "383 ms → 127 ms per tab
  switch" as measured fact while `test_tui_perf.py` said 106 and `app.py` said both in one
  sentence; the figure is now stated only in `test_tui_perf.py`, next to what that harness
  can actually re-derive. Don't restore a number here that no test can check. It is an
  *instance* attribute in textual 8.2; a class attribute named `ANIMATION_LEVEL` is a
  silent no-op.
- **The data layer is fast; don't "optimise" it.** `summarize_month` over a
  few hundred expenses is 0.15 ms and a full tab reload is under 0.6 ms. If
  something feels slow, measure before touching queries — last time the entire
  cost turned out to be a UI animation.
- **No business logic in `daylogs/tui/`.** Tabs render and handle keys.
  Arithmetic belongs in `body.py` / `money.py` / `summary.py`, with tests
  there. A tab that grows a calculation is a bug worth rejecting.
- **`T` previews; it does not ask you to name a theme.** The picker applies each theme
  on `←`/`→`, `enter` keeps it, `esc` restores whatever was in effect when it opened. It
  replaced a text prompt whose recorded justification was that cycling 21 themes "would
  take up to 21 presses to get back to one you liked" — which assumed you know the name
  you want, when the whole difficulty is that a name tells you nothing about how it looks
  against charts and bars that are deliberately *not* themed. `esc` is the one-keypress
  answer to that objection. It is a focused widget in the bottom container and **not** a
  `ModalScreen`, because a modal covers the interface being previewed; tabs stay reachable
  by digit and focus returns to the picker, since the charts and the summary are where the
  difference shows. `←`/`→` are claimed in `on_key` with `stop()` + `prevent_default()`,
  the same route `InlinePrompt` takes for `escape` — which is *why* those keys are
  deliberately not priority bindings. `themes.check` went with the prompt: every name the
  picker can produce came out of `names()`, and a validator with no caller rots.
  Because its exit runs from `on_key`, it has to be **told** when something else takes the
  keyboard: `InlinePrompt.open` calls `picker.cancel()`. Otherwise any write key left it
  displayed but deaf — `esc` no longer restoring the theme its own subtitle named, the next
  digit press re-stealing focus from the table, and an `enter` aimed at a row writing the
  preview to `config.toml`. `cancel` posts no `Cancelled` message on purpose: that is what
  returns focus to the tab, and the prompt that just took focus must keep it. A second `T`
  mid-preview is a no-op, because `open` re-anchors what `esc` restores and reopening threw
  away the one-keypress way back.
  The README's picker box is **generated, not drawn** — a test pins it to `themes.strip`.
  Hand-drawn art of a live widget is what `tools/screenshots.py` exists to prevent, and the
  first version of that box had a border one column short of its sides, a truncated name
  `strip` cannot emit, and a neighbourhood of the list the cursor does not produce.
- **Every prompt declares a hint in `daylogs/tui/hints.py`**, and a test greps the
  `prompt.open("…")` call sites to fail when one doesn't. `profile` shipped with a
  working grammar and no way to discover it because the label *was* the placeholder,
  so it vanished on the first keystroke and there was nowhere to put an example. The
  three slots are now: border title = label, placeholder = a copyable example
  (parsed by a test, so it must be valid), border subtitle = the grammar. An error
  takes the subtitle, not the title — the label stays useful while you read it.
- **One grammar, in `parse.py`, on top of `sigil.py`.** A field is marked by a
  sigil at the start of a token, so nothing is scavenged out of free text — which
  is what lets the same line serve entry *and* edit prefill. The previous grammar
  hunted for a category slug and a time token anywhere in the line, and silently
  stored the wrong category whenever a description contained a category word.
  `parse(render(row)) == row` is a tested property, over the corpus of inputs that
  broke the old grammar.
- **`sigil.py` and `complete.py` import no Textual**, like `chart.py` and
  `widgets.py`. Completion is a pure function of (line, cursor, vocabulary); the
  widget only decides when to call it.
- **A prompt's sigils are data on its `Hint`.** Vocabularies resolve at call time
  because `config.toml` can add categories, so a frozen literal would go stale.
- **A prompt that exists to correct a row must arm that row before it opens.** `fix
  category` is the only one, and it did not: it re-enters `_submit_expense`, which asks
  `_take_editing("expense")` for a row id, and `_editing` was only ever set by the
  `enter`-on-a-row path — so the id came back None and the "fix" **INSERTed a second
  expense**. One 12.40 lunch became 24.80 booked, and the duplicate `other` row sits on a
  pane the app does not open on, so the month total, the budget delta, the burn bar and the
  Day tab's MONEY panel were all wrong with nothing on screen disagreeing. `u` could not
  reach it either, because an add pushes no pre-image. `money_tab.py` now arms
  `("expense", new_id)` before `prompt.open`, which makes the refile an edit like any
  other. Escaping still leaves the row filed under `other`, because `cancel_editing`
  clears the slot — record-now-classify-later is the point of the fallback and survives.
  It shipped for four versions because
  `test_fixing_the_category_does_not_loop_forever` asserted `["other", "restaurant"]`: a
  test named for the re-prompt loop, freezing the duplicate as expected behaviour. Its
  hint was wrong in the same direction — example `restaurant`, which `parse_expense`
  rejects outright, and `sigils=("",)`, so the completion offered a bare word that lands
  in the *description* and falls back to `other` a second time. `fix category` was in
  `test_hints.py`'s `NO_PARSER` as "a bare category", which is exactly the lie that file
  already records about `profile`, and it is what left an impossible example unchecked.
  A correcting prompt's grammar is the grammar of the thing it corrects.
- **An edit line carries the columns its table displays, plus expense's write-only
  note, minus four read-only columns named below.** What you can see is what you can
  edit. Columns with no visible representation stay out of reach entirely — `created_at`
  must survive an edit, so it is in neither the table nor the
  line.
  **A *confirm* line carries the `@` it will write to, for the same reason an edit line
  does: it is re-parsed, and the grammar resolves an absent `@` to now.** `f pizza and
  salad @09-04/19:30` was offered back as `pizza and salad =850`, so accepting it wrote
  today at whatever the clock said when Claude answered — two days' net figures wrong at
  once, the meal added to one and missing from the other, with nothing on screen
  contradicting either. The *failure* paths never had this, because they write the parsed
  row directly, so the same keypress was right when Claude was down and wrong when it
  answered. `_offer`'s `when` is optional rather than always-on because
  `_run_image_estimate` has no typed line: a photo really is being estimated now, and a
  fabricated stamp on a confirm line reads as a claim about when you ate. `measured_at` used to be on that list — the weight table showed only a date, so
  a day weighed twice was two identical-looking rows while the invisible column decided
  which one the trend used and which the headline showed. It is a `time` column now, and
  `render_weigh` emits `@date/HH:MM` so it can be edited. The recorded objection was that
  re-deriving the stamp "would shave the seconds off every edit"; that is exactly what
  `body.restamp` exists to prevent — its own docstring names weight as the motivating
  case — and it had simply never been wired up.
  `body_tab._restamp_for` is the one rule **all three** edit paths use: a line naming a time
  restamps to it; a line naming only a **date** keeps the row's own clock time and moves it
  to that date; a line naming neither changes nothing, because `restamp` then compares equal
  and returns None. It said "should use" while only weight did, and the other two failed in
  the two opposite directions it exists to prevent — food left `ate_at` on the old day, and
  activity restamped from the parsed instant, so a date-only edit moved `logged_at` to
  *now's* clock, on the column `resolved_factor` uses as its tie-breaker. Both reproduced,
  both routed through the helper, one parametrized test across all three. The middle case is why the helper exists — the grammar resolves a
  missing time to *now*, so restamping from the parsed instant moved a 07:05 reading to
  whenever the edit happened, while keeping the stored stamp left it on the old day and
  inverted `morning_weight`/`latest_weight`. `restamp` compares the whole local **minute**,
  date included; comparing `%H:%M` alone is what made a date-only move look like nothing
  had changed.
  Expense's `~note` is currently write-only (settable, faithfully
  round-tripped through the edit prefill, displayed nowhere). An edit writes only
  the fields it parsed. The submitted line is authoritative: drop the note words
  and the note is cleared; submit unchanged and the note survives.
  **Four** displayed columns are deliberately not in an edit line, each for its own
  reason, and they are the whole list — this said "the one displayed column" while naming
  recurring's `on`, which is how a reader was left to rediscover the other three.
  Recurring's `on` is toggled by `o` instead: a boolean's entire edit is a toggle, and as
  a field every recurring line would carry a token that reads "on" almost always. It
  stayed unreachable for four versions — the column rendered `yes` for every row forever
  while `roll_month_budgets` filtered on a flag nothing could set — so the exception is
  written down here rather than left to be rediscovered as a bug. Recurring's `monthly` is
  derived from cost and cycle and recomputed whenever either moves, so a field for it
  would be a second, editable answer to a question already answered. And food's and
  activity's `src` (`lab`/`est`) is provenance: it says whether the number was typed or
  came out of a `claude -p` estimate, which is a fact about how the row came to exist
  rather than a property of the day, and the digest reads it. An edit may fix the
  description or the number; it may not restate where they came from.
- **`update_recurring` is keyed by id, and nothing may edit through
  `upsert_recurring`.** That resolves conflicts on `name`, so a rename matches
  nothing and INSERTs a second row; both then look active and the next
  `roll_month_budgets` writes two budget lines for one subscription. `monthly_cost`
  is a stored derived column and is recomputed whenever cost or cycle moves.
  `active` is absent from the ON CONFLICT clause for the same class of reason:
  `s` is add-*or-update* and its grammar cannot express the flag, so
  `active = excluded.active` meant every re-add carried the parameter's `True`
  default — raising a paused subscription's price un-paused it and the next roll
  charged for it. `note` is still overwritten there, on purpose: the recurring
  grammar has no `~note`, so that column is unreachable in both directions and
  fixing half of it would be pretending.
- **Pausing means "not from now on", never "this never happened".** A budget line
  already rolled for the month stays when you pause the item — you may have paid it —
  and next month's roll simply omits it. Same stance `_rename_rolled_budgets` takes on
  a deleted item's line, and the reason there is no un-roll.
- **Undo is an upsert on the primary key**, so one statement means "restore this row
  to these values" — covering a delete (row gone → insert) and an edit (row present
  → update) without the stack needing to know which. A plain INSERT raised on an
  edit's pre-image.
  That covers the only conflict the stack itself causes; it does **not** cover another
  unique index refusing the row. `recurring.name` is one — rename an item, let a new item
  take the freed name, and the pre-image collides — and resolving *that* would mean
  deleting the new item to make room. So **a failed undo is a no-op, not a consumed one**:
  the `except` pushes the pre-image back before it notifies. It used to pop and drop, which
  left the rename un-undone *and* sent the next `u` one entry deeper — measured: depth 2 → 1
  across a `u` that changed nothing, then a deleted expense resurrected under "restored
  expense row" while the rename became permanently unreachable. `u` now stays parked on an
  entry it cannot apply until the collision is resolved, and that is the trade: a stuck key
  beats a silent write to the wrong row.
- **A comma is a thousands separator, never a decimal point.** `parse.to_amount`
  rejects a decimal comma before stripping thousands commas, so `12,40` is
  rejected with a suggestion rather than silently becoming 1240. A $12.40 lunch
  typed as `12,40` would have been recorded as $1,240.00; weights escaped only
  because 782 kg trips a plausibility check expenses have no equivalent of.
- **The app owns prompt-error policy.** Tabs let `ParseError`/`MoneyError`/
  `BodyError`/`PhotoError`/`ViewError` propagate; the app re-opens the prompt
  with the text intact — **and with the row it was editing.** `_take_editing` consumes the
  armed id on read, so a rejection raised *after* that read (food's "kcal is required", a
  recurring name clash) left the retry to INSERT: a duplicate food row, and a second active
  recurring item through `upsert_recurring`, which is precisely what the by-id edit path
  exists to stop. `on_input_submitted` snapshots `tab._editing` before dispatching and
  restores it in the `except`, but only when something *was* armed — `_submit_expense` arms
  the `fix category` slot mid-flight, and writing None over that would disarm a row the
  handler had just armed. A retry is the same submission, so it has to still be the same
  edit. Abandonment is unaffected: escape and an empty submit both still clear through
  `cancel_editing`. A tab that catches these errors discards what the user typed.
- **`claude -p` runners are always injected.** `DaylogsApp` takes
  `runner_text` / `runner_json` / `runner_image`; services take `runner=`. No
  test may spawn a subprocess.
- **Parsers stay pure.** `parse.py` takes `now` as an argument. No test result
  may depend on when the suite runs.
  **That rule is not self-enforcing, and it has failed six times.** The live shape of the
  failure: a test seeds a dated row and reads it back through a window anchored on *today*,
  so the calendar alone eventually moves the fixture out of view — `enter` then selects
  nothing, no edit is armed, and the test passes while asserting nothing until the day it
  fails. On 2026-09-28 eight weight tests broke at once, on a comment that had predicted
  the row would leave Body's 1m window "on 2026-09-26".
  **So the harness clock is frozen: `conftest.FROZEN_NOW`, and `make_app` applies it unless a
  test passes `now=`.** Pinning per test was the documented cure and it is the remembering
  that failed — 190 tests still read the wall clock while the rule sat in this file. A default
  inverts it: a test now opts *in* to a date instead of having to opt out of the real one.
  `tests/test_harness.py` holds it down, including a grep asserting nothing builds `DaylogsApp`
  outside the fixture, because one default covers every test only while that stays true.
  Explicit pins stay where they exist — several are about a timezone, not a date.
  The date is mid-month on purpose (verified green frozen there, so nothing was bent to fit
  it): on a month edge a future test doing "a month later" lands on `horizon.shift`'s
  day-clamping branch by accident. And fixture dates written **relative to the real clock**
  would be worse than the bomb, not better: the row stays "two days old" while the calendar
  month it lands in changes, so it straddles `_covered_months` and `roll_month_budgets` on
  some run dates and not others — passing ~28 days a month and failing ~2, unreproducibly.
  Relative to `FROZEN_NOW` is the pattern that reads as intent and still resolves identically
  forever.
  Pinning the viewing date (`body.viewing_date = DAY`) also still works and anchors the span
  directly.
  CI still runs **nightly**, now for the reason that survives the frozen clock:
  `dependencies = ["textual>=8.2,<9"]` with no lockfile, so CI resolves the newest matching
  release every run and a Textual point release can break `main` with nothing pushed — which
  matters here because several recorded facts are about Textual *internals* (the private
  `textual._wait.SLEEP_GRANULARITY`, `animation_level` being an instance attribute, what
  `Static.content` hands back). The original reason stands as history: the last green run on
  main was thirteen days old while main was red.
  One test consults a live clock on purpose — `test_cli.py`'s Kiritimati case, since
  `cli.main` has no `now` seam and adding one to production to serve a test is not worth it.
  It reads the clock either side of the call and accepts both days, so a run straddling
  midnight there cannot flake.
- **stdlib `sqlite3`, not an ORM.** Measured: the SQLAlchemy/SQLModel stack
  cost 220 ms of import time for zero remaining consumers.
- **`PRAGMA journal_mode=DELETE`, never WAL.** WAL sidecars sync independently
  under iCloud and corrupt the DB on the receiving device.
- **Hard delete plus session undo.** No `deleted_at` columns, no trash
  service.
- Amounts are positive for a spend. Dates are `TEXT` `YYYY-MM-DD`, local.
  Timestamps are `INTEGER` unix seconds.
- Single runtime dependency: `textual`. Adding another needs a reason that
  beats the standard library.

## Working on it

- Branch `claude/<topic>`; never commit implementation work to `main`.
  Squash-merge one slice per PR. Wait for CI green before merging.
- Gate every change on `pytest -q` **and** `ruff check .`.
- `docs/` is gitignored — a local paper trail (specs, plans, closing notes),
  not part of the repo.
- **The repo is written open-source-clean.** It **is** published — public on GitHub and
  on PyPI since 0.2.0 — so this is no longer a precaution about a possible future. No real
  weights, amounts, names, or `/Users/<name>/` paths in
  tracked files. Note the sdist carries the full source, so anything committed is public
  whatever the GitHub setting says. Category slugs and `America/Toronto` are feature-domain
  values, not personal data. Fixtures use round, obviously-fake numbers.
- Verify dependency changes in a clean env, not just the local `.venv`. A
  dependency that happens to be installed locally but is missing from
  `pyproject.toml` only fails once CI builds from scratch.
- **Local setup is uv.** `uv venv --python 3.13` plus `uv pip install -e '.[dev]'`, so the
  gate is `.venv/bin/pytest` and `.venv/bin/ruff check .` — there is no conda env any more,
  and a hardcoded interpreter path is how a session ends up testing a Python that no longer
  exists. CI stays on plain pip with 3.12, which is deliberate: the two paths check each
  other, and 3.12 is what `requires-python` promises. The suite passes on both.
  The daily `day` is a **separate** `uv tool install daylogs` — the published release, not
  this checkout. An editable install doubling as the daily tool means whatever branch is
  checked out is what records your weight, which during one session meant running a branch
  with a broken `add_expense`.
- Push back on scope creep. This project's whole value is that it stayed
  small.

## Prompt tone

The LLM-facing prompts live in `summary.py` (`_VOICE` + `_TASK`) and
`estimate.py`. The summary voice is **objective and tactful** — direct about
what mattered, honest about misses, no grading, no manager-speak, loud on real
wins. It is tuned; preserve it when editing.
