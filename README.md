# W.I.S.E. Panda

W.I.S.E. Panda is a SWTOR combat log parser, live overlay tool, and database-backed combat analysis app built with Python and PyQt6.

The project started as a fight-analysis parser, but it now supports a broader workflow:

- load and analyze a single combat log
- import many combat logs into SQLite for historical comparison
- build a database of your own characters and the other players you have seen
- maintain roster metadata such as guild, legacy, friend status, and notes
- use live overlays while playing
- launch from a dashboard instead of dropping straight into a DPS-first screen

## What The App Does

The app currently has four major modes of use.

1. Session analysis

Load a combat log file and inspect fights in the Overview, Abilities, Rotation, Compare, Training, and Raw Log tabs.

2. Historical data

Import one log or an entire combat-log folder into SQLite. The app stores raw event rows plus cached summary tables for imported characters and seen players.

3. Roster and profile management

Use the Characters and Seen Players tools to review who appears in your logs, what classes and abilities they were seen with, and maintain your own profile fields and rich-text notes.

4. Live mode

Watch an active combat log in real time with the live battle overlay and threat board.

## Current Highlights

- Home dashboard with quick actions and database/session summary cards
- Encounter list plus tabbed fight analysis
- Overview tab with combatant breakdown and companion/NPC filters
- Ability, raw log, rotation, compare, DPS training, tank training, and healer training tabs
- Database import flow for one log or all logs in a folder
- Import history viewer with event preview and CSV export
- Imported character profiles built from the database
- Seen Players roster built from imported logs
- Editable player metadata:
  - Legacy
  - Guild
  - Friend
  - Rich-text player notes
- Duplicate-import protection
- Live battle overlay
- Live threat board
- Great Hunt tools and reference-data support

## Project Shape

The app is now organized around a lighter shell:

- `main.py`
  App startup only
- `ui/main_window.py`
  Top-level wiring and orchestration
- `ui/features.py`
  Declarative tab registration
- `ui/tabs/`
  Main shell tabs such as Dashboard, Overview, Abilities, and Raw Log
- `ui/dialogs/`
  Non-modal tools such as Characters, Import History, Seen Players, and Player Notes
- `encounter_db.py`
  SQLite schema, import pipeline, cache tables, and data access helpers
- `aggregator.py`
  Fight detection and combat aggregation
- `analysis.py`
  Higher-level analysis logic
- `ui/live/`
  Watch mode overlays and live tracker

The goal is modular growth: new features should plug into the shell cleanly instead of bloating `main.py`.

## Database Model

The app uses SQLite as a working database, not just as a cache dump.

Important stored layers include:

- raw imported combat logs
- raw imported combat-log event rows
- imported player-character cache tables
- seen-player cache tables
- encounter summaries
- player-character and ability summaries from processed fights

This means the app does not need to rebuild everything from raw logs every time a screen opens. Import once, then read from stored tables.

Primary local data files:

- `encounter_history.sqlite3`
  Main application database
- `combat_log_imports.json`
  JSON ledger of imported logs
- `great_hunt_data.sqlite3`
  Great Hunt reference/working data

## Requirements

- Windows
- Python 3.11+ recommended
- PyQt6

Install dependencies:

```bash
pip install -r requirements.txt
```

## Run The App

Standard launch:

```bash
python main.py
```

Launch with a combat log path:

```bash
python main.py "C:/Users/YourName/Documents/Star Wars - The Old Republic/CombatLogs/combat_2026-01-13_12_00_00_000000.txt"
```

If `python` is not on your PATH on Windows, use:

```bash
py -3 main.py
```

## SWTOR Combat Log Location

Typical Windows path:

```text
C:\Users\<YourName>\Documents\Star Wars - The Old Republic\CombatLogs\
```

Make sure combat logging is enabled in game:

- `Escape`
- `Preferences`
- `Combat Logging`
- enable combat logging

## Recommended Workflow

### Quick single-log review

1. Start the app
2. Use the Home dashboard or toolbar to open a combat log
3. Select a fight from the encounter list
4. Inspect Overview, Abilities, Rotation, Compare, or Training tabs

### Build historical data

1. Use `Import Log To DB` for one file or `Import All Logs` for a folder
2. Open `Import History` to verify imports
3. Open `Characters` to review your imported character profiles
4. Open `Seen Players` to build out your roster metadata and notes

### Use live mode

1. Open a current combat log
2. Turn on `Watch Mode`
3. Open the battle window and threat board if desired

## Main Files

```text
main.py
aggregator.py
analysis.py
parser.py
parser_core.py
training_tabs.py
encounter_db.py
great_hunt.py
ui/
assets/
```

## Tests

The repo includes test files for core parser and database logic, including:

- `test_parser.py`
- `test_aggregator.py`
- `test_great_hunt.py`
- `test_encounter_db.py`

## Notes

- The SQLite database can become very large if you import a lot of logs.
- Imported log data is now part of the application’s normal workflow, so `encounter_history.sqlite3` should be treated as important project data.
- Temporary scratch DB files and `__pycache__` folders are safe to clean up, but the main database is not.

## Status

This project is actively evolving from a parser-first combat tool into a broader combat-history and roster-analysis application.

The codebase already has the beginnings of a modular shell, and the next features should continue moving toward:

- feature modules with clear contracts
- table-first historical workflows
- UI tools that sit on top of stored data rather than rebuilding state repeatedly
