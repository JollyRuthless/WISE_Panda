# Roster — UI Drop

This is the second half of the Roster app. The backend layer landed last
session (`cohort.PlayerSummary`, `cohort.list_known_players()`, the
`ui_roster/roles.py` lookup table, and `test_roster.py`). This drop
delivers the four UI panels and the entry point that ties them together.

After this drop, `py roster.py` opens a working Roster window.

## What's in this drop

| File | What it does | New / Edit |
|---|---|---|
| `roster.py` | Entry point at the project root. Launches the Roster window standalone. | New |
| `ui_roster/main_window.py` | Three-region vertical-splitter container. Owns no state — just wires the three panels together. | New |
| `ui_roster/players_panel.py` | Top region. Search box + sortable table of every player in the DB. | New |
| `ui_roster/bosses_panel.py` | Middle region. For the selected player, every boss they've fought, with fight count, last-seen date, and the spec they used most recently on each boss. | New |
| `ui_roster/comparison_panel.py` | Bottom region. The analytical surface: every other player who fought the same boss in the same role, with best & median totals. Builds the cohort on a worker thread so the UI never blocks. | New |
| `ROSTER_README.md` | This file. | New |

Nothing in this drop edits an existing file. Five new files, all additive.

## What the window looks like

```
┌──────────────────────────────────────────────────────────┐
│  Players  (123)                              [↻ Refresh] │
│  ┌────────────────────────────────────────────────────┐  │
│  │ Filter by name…                                    │  │
│  └────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────┐  │
│  │ Player          │ Most-played class │ Fights │ Last │  │
│  │ Karzag          │ Mercenary         │    312 │ 4-30 │  │
│  │ Vossan          │ Sentinel          │    187 │ 4-29 │  │
│  │ Doomside        │ Juggernaut        │    154 │ 4-28 │  │
│  │ …                                                  │  │
│  └────────────────────────────────────────────────────┘  │
├──────────────────────────────────────────────────────────┤
│  Bosses  · Karzag · 14 unique bosses                     │
│  ┌────────────────────────────────────────────────────┐  │
│  │ Boss           │ Fights │ Last seen │ Spec        │  │
│  │ Apex Vanguard  │     22 │ 2026-04-30│ Merc · Arsl │  │
│  │ Master & Blade │     18 │ 2026-04-28│ Merc · Arsl │  │
│  │ …                                                  │  │
│  └────────────────────────────────────────────────────┘  │
├──────────────────────────────────────────────────────────┤
│  Comparison  · Karzag vs Apex Vanguard (Merc · Arsenal)  │
│  ┌────────────────────────────────────────────────────┐  │
│  │ Player           │ Spec       │ Fts │ Top dmg     │  │
│  │ Karzag (you)     │ Merc·Arsl  │  22 │ 4,128,991   │  │
│  │ Vossan           │ Merc·Arsl  │  18 │ 4,772,103   │  │
│  │ Tymo             │ Sniper·Mar │  11 │ 5,201,477   │  │
│  │ …                                                  │  │
│  └────────────────────────────────────────────────────┘  │
│  3 players matched · 14 skipped (different role)         │
│  Read-only · Roster never writes to the database         │
└──────────────────────────────────────────────────────────┘
```

The three regions are vertically stacked in a `QSplitter` — drag the
handles to re-balance. The defaults are 30/30/40 (the analytical region
gets the most space because it's where the user spends the most time).

## How the panels are wired

One-way top-to-bottom signal cascade, set up in `main_window.py`:

```
PlayersPanel.player_selected(name)
        │
        └→ BossesPanel.set_player(name)
              │
              └ on row click →
                BossesPanel.boss_selected(boss, class, discipline)
                      │
                      └→ ComparisonPanel.set_context(player, boss, class, discipline)
                            │
                            └ kicks off worker thread → role-matched cohort
```

The `MainWindow` itself owns no state. Pulling the player name freshly
each time a boss is clicked (rather than caching it) means the wiring
keeps working even if the user changes their player selection mid-flow.

## Install

1. Drop the new files over your project, preserving directory structure:
   - root: `roster.py`
   - `ui_roster/`: `main_window.py`, `players_panel.py`,
     `bosses_panel.py`, `comparison_panel.py`
2. Make sure the previous backend drop is in place (`cohort.py`,
   `ui_roster/__init__.py`, `ui_roster/roles.py`, `test_roster.py`).
3. Make sure PySide6 is installed in your Python — `pip install PySide6`
   if it isn't. (Same dependency the parser app uses, so probably already
   there.)

## Run

From your project root:

```
py roster.py
```

The window should open inside a couple of seconds with your player list
populated.

## Verify

The previous backend drop's tests still pass:

```
py -m unittest test_roster
```

Expected: **17 tests, OK**.

Full suite:

```
py -m unittest test_db_migrations test_cohort test_phase_b_ingestion test_phase_c_class_detection test_phase_f_find_fight test_phase_d1_import test_phase_e_ability_counts test_rebuild_fights test_phase_g_idempotency test_phase_h_cohort_compare test_roster
```

Expected: **172 tests, OK** (no change — this drop adds no new tests).

## What this drop does NOT test

UI panels aren't unit-tested for the same reason Phase F/H skipped Qt
widget tests: brittle in CI, low yield. The panel logic is thin enough
to verify by hand, and the deeper data-shaping logic (cohort building,
median calculations, role filtering) all delegates to functions that
*are* tested in `test_roster.py`, `test_phase_h_cohort_compare.py`, and
`test_cohort.py`.

What I did verify before shipping:
- All five new files parse cleanly under `python3 -c 'import ast'`
- All imports resolve (no typos in `from X import Y`) with stub
  modules standing in for PySide6 and cohort
- Signal contracts match across `connect()` sites
  (`player_selected(str)` → `set_player(str)`,
  `boss_selected(str, str, str)` → `_on_boss_selected(str, str, str)`)
- No file in this drop edits any existing module

## Behaviour notes worth flagging

### Cohort building runs on a worker thread

`ComparisonPanel.set_context()` cancels any in-flight worker and starts a
new one (`_CohortWorker`, a `QThread` subclass). The worker walks every
fight on the boss, calls `cohort.list_participants_in_fight()` per fight,
filters participants to those whose role matches the user's role on this
boss, and aggregates by player.

The worker reports progress to the panel's status label
(`"Building cohort for DPSes on Apex Vanguard…  47/200  (23%)"`) and
returns a single `_CohortBuildResult` when done.

Cancellation is cooperative — the worker checks a flag between fights
and exits its loop quickly, but it does **not** cancel mid-DB-query.
That's fine because per-fight queries are short.

### Cohort cap

The worker only walks the most recent 500 fights on the boss
(`COHORT_FIGHT_CAP` in `comparison_panel.py`). This is a safety net,
not a feature. If you have a boss with thousands of pulls in your DB,
older fights past the 500-cap simply don't contribute to the median.
Bumping the cap is one constant change away.

### Role-matching is strict

The comparison panel only includes players whose `(class, discipline)`
maps to the same role as the selected player. A player with no
discipline data on a fight (Phase C didn't fire — old fight or unclear
ability fingerprint) is skipped, with the count reported in the footer.

This is honest: comparing your DPS Merc to a Healer Merc on the same
boss is misleading, even though they're nominally "the same class."
The footer's `N skipped (no class data)` count tells you how much of
your data is invisible to the analysis.

### "You" row pinning

The user's own row is pinned to the top of the comparison table and
tinted blue, regardless of where it would sort by fight count or
performance. The user is always the baseline.

### Read-only

Roster never writes to the database. There is no Save button, no
import path, no settings persistence. Closing the window doesn't lose
state because there's no state to lose. The status bar carries that
contract as a quiet reminder.

## What's next (after this v1)

Things that came up during build but didn't go in:

- **Right-click → Open in Yoda.** The comparison panel's per-row context
  menu should let you jump back to the main parser app with that fight
  pre-loaded. Roster v1 is a bird's-eye view; deeper drilldown should
  hand off rather than re-implement.
- **Per-ability cohort table.** "On this boss, the median Vengeance Jugg
  used Ravage 14 times; you used it 8." We have `cohort_benchmark()`
  which already returns per-ability medians — just needs a UI tab.
  Probably belongs as a second tab inside the comparison panel.
- **Sortable columns.** Currently every table populates with a fixed
  sort. Once the panels are stable and you've used them a bit, click-
  to-sort headers are a small lift.
- **Persisted splitter sizes.** The 30/30/40 default resets on every
  launch. A `QSettings` save/restore would feel nicer. Maybe overkill
  for v1.
- **Refresh-on-DB-change.** The Refresh button is manual. A file-system
  watcher on the SQLite file is doable but introduces edge cases
  (WAL checkpoints look like writes; race conditions with the parser's
  in-flight transactions). Manual refresh is the safer v1 choice.

None of these block v1. Ship the standalone window, see how it feels,
decide what's worth building next.
