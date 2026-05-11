# The A-10 Plan — Status as of April 28, 2026

This is the project plan after a long working session that took the parser
from "stuck" to "actually useful." Read top-to-bottom on a return visit;
the order is roughly "what is true now → what's next."

---

## What works today

### The database is no longer write-only

The Inspector tab (🔍 in the main window) lets you browse what's stored.
List of encounters, click one to see participants, click a player to see
their abilities. Read-only browsing of historical data is now real.

### Multi-player fights are recorded correctly

Open a 4-person heroic or 8-person op — every player who participated
gets their own row in `player_character_encounters`, with damage, healing,
taunts, interrupts. Companions are correctly separated from their owners.
NPCs are filtered out.

### Three independent ability counts per (player, fight)

The abilities table tracks three things separately:
- `use_count`: in-fight AbilityActivate (button presses during the fight)
- `prebuff_count`: AbilityActivates in the 15s before EnterCombat
- `damage_source_count`: damage events attributed to this ability (catches
  DoT ticks, channels, procs, reflected damage, anything where damage
  appeared without a corresponding fresh button press)

This data reveals patterns that single-count parsers can't show. Examples
from the user's real fights:
- A non-group helper showed prebuff > 0, pressed = 0, damage_source > 0 —
  meaning they pre-cast their DoTs, ran in, and let them tick. That's
  a free "this player wasn't in your group" detector.
- Player Doomside Jones's bleed effects showed pressed = 0, damage_source
  high — bleed DoTs ticking after the activating ability, exactly as
  expected for the Vengeance Juggernaut spec.

### Two parallel ingestion paths, both working

**Pipeline A — `upsert_fight()`:**
Save one fight at a time. Triggered by:
- Right-clicking a fight in the encounter list → "Save to DB"
- Renaming an encounter (existing behavior, unchanged)
- The auto-ingest queue (when `_auto_encounter_ingest_enabled = True`,
  off by default — left in place but unused)

**Pipeline B — `import_combat_log()`:**
Bulk import a log file. Triggered by the Import buttons. Now:
- Writes raw events to `combat_log_events` (as always)
- Also runs fight aggregation against those events (Phase D-1)
- Idempotent at the fight level — re-importing a log refreshes existing
  fights instead of erroring (Phase G)
- Reports back "X new, Y refreshed, Z failed" so the user knows what
  happened on each import

### The user's dream workflow works

1. Live play, watch mode tracking the current log
2. Right-click a fight in the encounter list → "Save to DB"
3. Look it up in the Inspector tab — it's there
4. End of day, "Import All Logs" — saved fights are refreshed, new fights
   are added, no duplicates, no errors

### Schema migrations work

The `db_migrations.py` system handles schema upgrades cleanly:
- v1 → v2 (per-player composite PK)
- v2 → v3 (Phase E ability count columns)
- Both run automatically on app startup, take a backup before changes,
  no-op if already applied, idempotent on re-runs

### Inspector has a "Rebuild Structured Data" button

When schema or aggregation logic changes, the Rebuild button walks every
imported log and re-runs fight aggregation against them. Existing per-fight
rows are replaced with fresh data using current code. Raw events are
untouched. This is how we evolve the structured data without re-importing.

---

## What's tested

95 tests across 7 test files, all green:

| File | Tests | What it covers |
|---|---:|---|
| `test_db_migrations.py` | 22 | Schema migration v1→v2→v3, backups, idempotency |
| `test_cohort.py` | 38 | Query layer for finding fights and player history |
| `test_phase_b_ingestion.py` | 11 | Multi-player fights via `upsert_fight` |
| `test_phase_d1_import.py` | 7 | Bulk import populates per-player tables |
| `test_phase_e_ability_counts.py` | 6 | pressed / prebuff / damage_source columns |
| `test_rebuild_fights.py` | 5 | Inspector's rebuild button |
| `test_phase_g_idempotency.py` | 6 | Re-import doesn't duplicate; live-then-bulk works |

Run them all with:
```
py -m unittest test_db_migrations test_cohort test_phase_b_ingestion \
    test_phase_d1_import test_phase_e_ability_counts test_rebuild_fights \
    test_phase_g_idempotency
```

---

## What's NOT done yet — the remaining roadmap

These are in priority order for the next working session, not strict
dependency order. Skip around if something else feels more important.

### Phase C — Class detection

The Class column in the Inspector is empty for every player. Currently
`_class_name_from_fight()` is a stub that returns `""`.

The plan: vote across abilities used. Each class/spec has a fingerprint
of signature abilities (Tracer Missile = Mercenary Arsenal, Force Lightning =
Sorcerer, Ravage = Marauder/Juggernaut, etc.). Aggregate across the player's
ability list, pick the class with the most signature hits. Set a confidence
threshold below which we say "Unknown" rather than guess.

Visible payoff: every Inspector row populates with class info. Becomes
filterable in Phase F.

### Phase F — Find-a-Fight tab

The user's actual product vision. A dedicated search tab that queries the
DB by:
- Encounter name (boss)
- Player class
- Player name
- Combinations of the above

Returns a list of matching fights. Click one → loads it in the analysis
tabs the same way opening a log file does.

This is what makes the database useful for coaching: "Show me every
Mercenary fight against Apex Vanguard" → click to compare your data
against theirs.

The query layer (`cohort.py`) is already built. Phase F is mostly UI work
on top of an already-solid query API.

### Bulk import all historical logs

Now that the DB is trustworthy, fill it up. Just keep clicking
"Import All Logs" with the user's archive of past sessions. Each import is
a no-op if already imported, otherwise pulls in a new corpus of fights.

Low-risk, high-reward. Makes Phase F immediately useful when it ships.

### Phase H — Cross-fight context inside existing tabs

After Phase F: bring DB context into the Compare/Abilities tabs. When the
user is looking at a fight in Compare, silently query the DB for "other
Juggernauts who fought this boss" and show their numbers next to yours.

This is the "parser as class coach" vision finally manifesting in the
existing analysis UI, not just a separate tab.

---

## Architectural decisions worth remembering

### The combat log is the source of truth, not the database

The DB is a derived view of structured data extracted from logs on disk.
If the DB ever gets corrupted, the user can wipe it and re-import. The
Rebuild button works the same way: re-derive structured data from logs
that are still in `combat_log_imports`.

### Three ingestion levels

Raw event → Fight aggregation → Per-player structured data. Each level is
its own table. Re-running aggregation is cheap (no log re-parse). This
is what makes the Rebuild button possible.

### `encounter_key` is the fight identity

Format: `log_path|line_start|line_end|start_time`. Two fights from the
same log at the same line range and timestamp are the same fight. The
DB enforces uniqueness via this key. Migrations preserve the format.

### Phase G killed the file-level duplicate block

`DuplicateCombatLogImportError` is no longer raised by `import_combat_log`.
The class is still defined and imported (backward-compatibility), but
nothing throws it. Re-importing a log just refreshes its fights. This
is what enables the live-save-then-bulk-import workflow.

---

## Files in the project that matter for this work

| File | What it does |
|---|---|
| `encounter_db.py` | All DB interactions: import, upsert, queries, helpers |
| `db_migrations.py` | v1→v2→v3 schema migrations, idempotent |
| `cohort.py` | Public query API: find_fights, find_player_history, build_cohort |
| `aggregator.py` | Parses log events into Fight objects with entity_stats |
| `parser.py` | Line-level parsing: regex, parse_line, parse_entity |
| `ui/main_window.py` | Main window, encounter list, fight list, right-click menu |
| `ui/tabs/inspector.py` | DB Inspector tab — encounters/players/abilities browser |
| `ui/features.py` | Tab feature registry — adds Inspector to tab list |

---

## Things to NOT do (lessons from this session)

### Don't claim Phase X is "done" without a real-data integration test

Phase B looked done after synthetic-fixture tests passed. It worked on
synthetic data and failed on real logs because the synthetic fixtures
used integer coordinates (real logs use floats) and used `[AbilityActivate
{id}]` instead of `[Event {id}: AbilityActivate {id}]`. Each phase needs
to be exercised end-to-end on a real log file before being declared
complete.

### Don't trust assumptions about what UI buttons do

Repeatedly during this session, I assumed "open log" wrote to the database
when it actually didn't, and that "Import" went through `upsert_fight`
when it actually went through a separate code path that bypassed it. Always
trace the actual code path of a UI action before writing code that depends
on it.

### Don't preserve data that's derived from a source of truth

The original plan tried to preserve existing DB rows during a schema
migration. That added complexity (Phase D backfill) and made testing
harder because we couldn't tell new behavior from old leftover data. The
clean-rebuild approach (wipe DB → re-import from logs) is dramatically
simpler. Apply this thinking elsewhere if the situation comes up.

### Don't suppress useful Qt signals

The `blockSignals(True)` around the bulk-add loop in
`_refresh_fight_list_labels` is a real performance optimization, but it
silently broke the "auto-select on log load" behavior because
`currentRowChanged` never fired. Fix: explicitly call the handler after
unblocking. Pattern to watch for elsewhere — anywhere we use blockSignals.

---

## Where to pick up next

**Easiest visible win:** Phase C (class detection). Empty Class columns
fill in. Quick payoff for the time invested.

**Most impactful for the original product vision:** Phase F (Find-a-Fight).
Plumbs the existing query API into a dedicated search UI. Unlocks the
coaching use case.

**Lowest risk corpus building:** bulk-import historical logs. Don't even
need code changes — just keep clicking Import All Logs against archives
of past sessions until the DB is full of real data.

Pick whichever feels right when you sit down with this next.
