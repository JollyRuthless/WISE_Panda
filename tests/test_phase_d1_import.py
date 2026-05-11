"""
Tests for Phase D-1 — import_combat_log writes per-player encounter rows.

Phase D-1 is the change that makes the bulk-import path (Pipeline B) call
upsert_fight after writing raw events. Before this change, importing a
log only populated combat_log_events; after this change, it also
populates encounters, player_character_encounters, and
player_character_encounter_abilities.

This is the test that should have existed before we ever called Phase B
"done." It exercises the actual user-facing import flow end to end with
a real (tiny) log file on disk.
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import storage.encounter_db as encounter_db
# ─── Synthetic log file content ──────────────────────────────────────────────
#
# A minimal SWTOR combat log that the existing parser will accept. We include:
#   - 1 EnterCombat line for each player
#   - a few damage events from each player against an NPC
#   - 1 ExitCombat line
#
# The format is the real SWTOR log format. parse_line() in parser.py is the
# source of truth for what's valid; this fixture matches lines we've seen
# in that codebase's other tests.

SYNTHETIC_LOG = "\n".join([
    # Two players ramp up against a Training Dummy. The recorder is Karzag.
    # Real SWTOR format for these is `[Event {id}: <name> {id}]`.
    "[19:00:00.000] [@Karzag#100|(0.00,0.00,0.00,0.00)|(20000/20000)] [] [] [Event {836045448945472}: EnterCombat {836045448945489}]",
    "[19:00:00.500] [@Vossan#101|(0.00,0.00,0.00,0.00)|(20000/20000)] [] [] [Event {836045448945472}: EnterCombat {836045448945489}]",
    # Karzag fires Tracer Missile at the dummy
    "[19:00:01.000] [@Karzag#100|(0.00,0.00,0.00,0.00)|(20000/20000)] [Training Dummy {16000}:1|(0.00,0.00,0.00,0.00)|(50000/50000)] [Tracer Missile {810194895470592}] [Event {836045448945472}: AbilityActivate {836045448945479}]",
    "[19:00:01.100] [@Karzag#100|(0.00,0.00,0.00,0.00)|(20000/20000)] [Training Dummy {16000}:1|(0.00,0.00,0.00,0.00)|(48500/50000)] [Tracer Missile {810194895470592}] [ApplyEffect {836045448945477}: Damage {836045448945501}] (1500 energy {836045448940874}) <1500.0>",
    "[19:00:02.000] [@Karzag#100|(0.00,0.00,0.00,0.00)|(20000/20000)] [Training Dummy {16000}:1|(0.00,0.00,0.00,0.00)|(48500/50000)] [Tracer Missile {810194895470592}] [Event {836045448945472}: AbilityActivate {836045448945479}]",
    "[19:00:02.100] [@Karzag#100|(0.00,0.00,0.00,0.00)|(20000/20000)] [Training Dummy {16000}:1|(0.00,0.00,0.00,0.00)|(47000/50000)] [Tracer Missile {810194895470592}] [ApplyEffect {836045448945477}: Damage {836045448945501}] (1500 energy {836045448940874}) <1500.0>",
    # Vossan fires Heatseeker Missiles at the dummy
    "[19:00:03.000] [@Vossan#101|(0.00,0.00,0.00,0.00)|(20000/20000)] [Training Dummy {16000}:1|(0.00,0.00,0.00,0.00)|(47000/50000)] [Heatseeker Missiles {811078658654208}] [Event {836045448945472}: AbilityActivate {836045448945479}]",
    "[19:00:03.100] [@Vossan#101|(0.00,0.00,0.00,0.00)|(20000/20000)] [Training Dummy {16000}:1|(0.00,0.00,0.00,0.00)|(45000/50000)] [Heatseeker Missiles {811078658654208}] [ApplyEffect {836045448945477}: Damage {836045448945501}] (2000 energy {836045448940874}) <2000.0>",
    "[19:00:04.000] [@Vossan#101|(0.00,0.00,0.00,0.00)|(20000/20000)] [Training Dummy {16000}:1|(0.00,0.00,0.00,0.00)|(45000/50000)] [Heatseeker Missiles {811078658654208}] [Event {836045448945472}: AbilityActivate {836045448945479}]",
    "[19:00:04.100] [@Vossan#101|(0.00,0.00,0.00,0.00)|(20000/20000)] [Training Dummy {16000}:1|(0.00,0.00,0.00,0.00)|(43000/50000)] [Heatseeker Missiles {811078658654208}] [ApplyEffect {836045448945477}: Damage {836045448945501}] (2000 energy {836045448940874}) <2000.0>",
    # ExitCombat closes the fight
    "[19:01:00.000] [@Karzag#100|(0.00,0.00,0.00,0.00)|(20000/20000)] [] [] [Event {836045448945472}: ExitCombat {836045448945490}]",
])


class ImportCombatLogPhaseD1Tests(unittest.TestCase):
    """The promise of Phase D-1: importing a log populates per-player tables."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self._tmp_dir = Path(self._tmp.name)

        # Redirect DB_PATH to a fresh test database. init_db (called from
        # import_combat_log) will create the schema.
        self._original_db_path = encounter_db.DB_PATH
        self._original_ledger_path = encounter_db.IMPORT_LEDGER_PATH
        encounter_db.DB_PATH = self._tmp_dir / "test.sqlite3"
        encounter_db.IMPORT_LEDGER_PATH = self._tmp_dir / "test_ledger.json"

        # Write the synthetic log file.
        self._log_path = self._tmp_dir / "synthetic_combat_log.txt"
        self._log_path.write_text(SYNTHETIC_LOG, encoding="utf-8")

    def tearDown(self) -> None:
        encounter_db.DB_PATH = self._original_db_path
        encounter_db.IMPORT_LEDGER_PATH = self._original_ledger_path
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(encounter_db.DB_PATH))

    # ── Tests ────────────────────────────────────────────────────────────────

    def test_import_creates_combat_log_events_rows(self):
        """Sanity: the existing import behavior still works."""
        encounter_db.import_combat_log(self._log_path)
        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM combat_log_events").fetchone()[0]
        # 11 lines total; some events for each ability cast
        self.assertGreater(count, 0)

    def test_import_creates_encounter_rows(self):
        """Phase D-1: import should create encounter summary rows."""
        encounter_db.import_combat_log(self._log_path)
        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM encounters").fetchone()[0]
        # The synthetic log has one fight (one EnterCombat / ExitCombat pair)
        self.assertEqual(count, 1)

    def test_import_creates_per_player_encounter_rows_for_both_players(self):
        """Phase D-1: BOTH players should appear in player_character_encounters."""
        encounter_db.import_combat_log(self._log_path)
        with self._connect() as conn:
            names = sorted(
                row[0] for row in conn.execute(
                    "SELECT pc.character_name FROM player_character_encounters pce "
                    "JOIN player_characters pc ON pc.character_id = pce.character_id"
                )
            )
        # Both Karzag and Vossan participated. Both must be there.
        self.assertEqual(names, ["Karzag", "Vossan"])

    def test_import_creates_player_characters_rows(self):
        encounter_db.import_combat_log(self._log_path)
        with self._connect() as conn:
            names = sorted(
                row[0] for row in conn.execute(
                    "SELECT character_name FROM player_characters"
                )
            )
        self.assertEqual(names, ["Karzag", "Vossan"])

    def test_import_creates_per_player_ability_rows(self):
        """Phase D-1: ability counts per player should be recorded."""
        encounter_db.import_combat_log(self._log_path)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT pc.character_name, pcea.ability_name, pcea.use_count "
                "FROM player_character_encounter_abilities pcea "
                "JOIN player_characters pc ON pc.character_id = pcea.character_id "
                "ORDER BY pc.character_name, pcea.ability_name"
            ).fetchall()
        # Karzag pressed Tracer Missile twice. Vossan pressed Heatseeker twice.
        self.assertEqual(rows, [
            ("Karzag", "Tracer Missile", 2),
            ("Vossan", "Heatseeker Missiles", 2),
        ])

    def test_import_records_damage_done_per_player(self):
        encounter_db.import_combat_log(self._log_path)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT pc.character_name, pce.damage_done "
                "FROM player_character_encounters pce "
                "JOIN player_characters pc ON pc.character_id = pce.character_id "
                "ORDER BY pc.character_name"
            ).fetchall()
        # Karzag: 2 hits × 1500 = 3000
        # Vossan: 2 hits × 2000 = 4000
        self.assertEqual(rows, [("Karzag", 3000), ("Vossan", 4000)])


class ImportFailureToleranceTests(unittest.TestCase):
    """A bad log shouldn't crash the entire import."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self._tmp_dir = Path(self._tmp.name)
        self._original_db_path = encounter_db.DB_PATH
        self._original_ledger_path = encounter_db.IMPORT_LEDGER_PATH
        encounter_db.DB_PATH = self._tmp_dir / "test.sqlite3"
        encounter_db.IMPORT_LEDGER_PATH = self._tmp_dir / "test_ledger.json"

    def tearDown(self) -> None:
        encounter_db.DB_PATH = self._original_db_path
        encounter_db.IMPORT_LEDGER_PATH = self._original_ledger_path
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def test_log_with_no_combat_imports_without_error(self):
        """A log with no EnterCombat/ExitCombat is valid input — no fights."""
        log = self._tmp_dir / "no_combat.txt"
        log.write_text(
            # Just an AreaEntered line — no combat at all
            "[18:10:41.742] [@Karzag#100|(0,0,0,0)|(20000/20000)] [] [] "
            "[AreaEntered {836045448953664}: Imperial Fleet {137438989504}] () <0>",
            encoding="utf-8",
        )
        # Should not raise
        encounter_db.import_combat_log(log)

        # Events still got written (the AreaEntered line is one row)
        with sqlite3.connect(str(encounter_db.DB_PATH)) as conn:
            event_count = conn.execute(
                "SELECT COUNT(*) FROM combat_log_events"
            ).fetchone()[0]
            encounter_count = conn.execute(
                "SELECT COUNT(*) FROM encounters"
            ).fetchone()[0]
        self.assertGreaterEqual(event_count, 1)
        # No combat → no encounters
        self.assertEqual(encounter_count, 0)


if __name__ == "__main__":
    unittest.main()
