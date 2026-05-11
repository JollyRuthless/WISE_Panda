"""
Tests for Phase G — fight-level idempotency in import_combat_log.

Phase G removes the file-level duplicate-import block. Importing a log a
second time is now legal and just refreshes the data using current code.
The summary distinguishes:

  - fights_total: total fights found in the log
  - fights_new: didn't have an encounters row before this import
  - fights_refreshed: encounters row already existed; was updated
  - fights_failed: could not be aggregated/upserted

These tests verify both the "first time" and "second time" behaviors,
and the live-then-bulk workflow that Phase G is designed to enable.
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import storage.encounter_db as encounter_db
# Same general format as Phase D-1 test fixture. One fight, two players.
TEST_LOG = "\n".join([
    "[19:00:00.000] [@Karzag#100|(0.00,0.00,0.00,0.00)|(20000/20000)] [] [] [Event {836045448945472}: EnterCombat {836045448945489}]",
    "[19:00:00.500] [@Vossan#101|(0.00,0.00,0.00,0.00)|(20000/20000)] [] [] [Event {836045448945472}: EnterCombat {836045448945489}]",
    "[19:00:01.000] [@Karzag#100|(0.00,0.00,0.00,0.00)|(20000/20000)] [Training Dummy {16000}:1|(0.00,0.00,0.00,0.00)|(50000/50000)] [Tracer Missile {810194895470592}] [Event {836045448945472}: AbilityActivate {836045448945479}]",
    "[19:00:01.100] [@Karzag#100|(0.00,0.00,0.00,0.00)|(20000/20000)] [Training Dummy {16000}:1|(0.00,0.00,0.00,0.00)|(48500/50000)] [Tracer Missile {810194895470592}] [ApplyEffect {836045448945477}: Damage {836045448945501}] (1500 energy {836045448940874}) <1500.0>",
    "[19:00:02.000] [@Vossan#101|(0.00,0.00,0.00,0.00)|(20000/20000)] [Training Dummy {16000}:1|(0.00,0.00,0.00,0.00)|(48500/50000)] [Heatseeker Missiles {811078658654208}] [Event {836045448945472}: AbilityActivate {836045448945479}]",
    "[19:00:02.100] [@Vossan#101|(0.00,0.00,0.00,0.00)|(20000/20000)] [Training Dummy {16000}:1|(0.00,0.00,0.00,0.00)|(46500/50000)] [Heatseeker Missiles {811078658654208}] [ApplyEffect {836045448945477}: Damage {836045448945501}] (2000 energy {836045448940874}) <2000.0>",
    "[19:01:00.000] [@Karzag#100|(0.00,0.00,0.00,0.00)|(20000/20000)] [] [] [Event {836045448945472}: ExitCombat {836045448945490}]",
])


class PhaseGIdempotencyTests(unittest.TestCase):
    """Bulk import is now idempotent at fight level."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self._tmp_dir = Path(self._tmp.name)
        self._original_db_path = encounter_db.DB_PATH
        self._original_ledger_path = encounter_db.IMPORT_LEDGER_PATH
        encounter_db.DB_PATH = self._tmp_dir / "test.sqlite3"
        encounter_db.IMPORT_LEDGER_PATH = self._tmp_dir / "test_ledger.json"

        self._log_path = self._tmp_dir / "test_log.txt"
        self._log_path.write_text(TEST_LOG, encoding="utf-8")

    def tearDown(self) -> None:
        encounter_db.DB_PATH = self._original_db_path
        encounter_db.IMPORT_LEDGER_PATH = self._original_ledger_path
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def test_first_import_reports_one_new_fight(self):
        summary = encounter_db.import_combat_log(self._log_path)
        self.assertEqual(summary.fights_total, 1)
        self.assertEqual(summary.fights_new, 1)
        self.assertEqual(summary.fights_refreshed, 0)
        self.assertEqual(summary.fights_failed, 0)

    def test_second_import_reports_one_refreshed_fight(self):
        encounter_db.import_combat_log(self._log_path)

        # Second import: same log, no changes. Must not raise.
        summary = encounter_db.import_combat_log(self._log_path)
        self.assertEqual(summary.fights_total, 1)
        self.assertEqual(summary.fights_new, 0)
        self.assertEqual(summary.fights_refreshed, 1)
        self.assertEqual(summary.fights_failed, 0)

    def test_second_import_does_not_duplicate_encounter_rows(self):
        encounter_db.import_combat_log(self._log_path)
        encounter_db.import_combat_log(self._log_path)

        with sqlite3.connect(str(encounter_db.DB_PATH)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM encounters").fetchone()[0]
        self.assertEqual(count, 1, "second import created duplicate encounter rows")

    def test_second_import_does_not_duplicate_per_player_rows(self):
        encounter_db.import_combat_log(self._log_path)

        with sqlite3.connect(str(encounter_db.DB_PATH)) as conn:
            first_count = conn.execute(
                "SELECT COUNT(*) FROM player_character_encounters"
            ).fetchone()[0]

        encounter_db.import_combat_log(self._log_path)

        with sqlite3.connect(str(encounter_db.DB_PATH)) as conn:
            second_count = conn.execute(
                "SELECT COUNT(*) FROM player_character_encounters"
            ).fetchone()[0]

        self.assertEqual(first_count, second_count)

    def test_no_duplicate_import_error_is_raised(self):
        """The hard block from Phase D-1 is gone."""
        encounter_db.import_combat_log(self._log_path)
        # If this raised DuplicateCombatLogImportError, the test would fail.
        try:
            encounter_db.import_combat_log(self._log_path)
        except encounter_db.DuplicateCombatLogImportError:
            self.fail("DuplicateCombatLogImportError should no longer be raised")


class PhaseGLiveThenBulkTests(unittest.TestCase):
    """The live-save-then-bulk-import workflow is the whole point."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self._tmp_dir = Path(self._tmp.name)
        self._original_db_path = encounter_db.DB_PATH
        self._original_ledger_path = encounter_db.IMPORT_LEDGER_PATH
        encounter_db.DB_PATH = self._tmp_dir / "test.sqlite3"
        encounter_db.IMPORT_LEDGER_PATH = self._tmp_dir / "test_ledger.json"

        self._log_path = self._tmp_dir / "live.txt"
        self._log_path.write_text(TEST_LOG, encoding="utf-8")

    def tearDown(self) -> None:
        encounter_db.DB_PATH = self._original_db_path
        encounter_db.IMPORT_LEDGER_PATH = self._original_ledger_path
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def test_live_save_then_bulk_import_reports_refreshed_not_new(self):
        """
        Simulates the user's workflow:
          1. While playing, right-click a fight, choose "Save to DB"
             (this calls upsert_fight on a Fight built from scan_fights)
          2. Hours later, "Import All Logs" runs against the same log file
          3. The bulk import should see the fight already exists and report
             it as refreshed, not new
        """
        # Step 1: simulate the live "Save to DB" action by calling the same
        # pieces it would call: scan_fights + ensure_loaded + upsert_fight.
        from engine.aggregator import resolve_fight_names, scan_fights

        encounter_db.init_db()  # need DB initialized for upsert_fight to work
        fights = scan_fights(str(self._log_path))
        self.assertEqual(len(fights), 1)
        resolve_fight_names(str(self._log_path), fights)
        fights[0].ensure_loaded()
        encounter_db.upsert_fight(fights[0])

        # Step 2: bulk import the whole log
        summary = encounter_db.import_combat_log(self._log_path)

        # Step 3: should be 1 refreshed, 0 new
        self.assertEqual(summary.fights_total, 1)
        self.assertEqual(summary.fights_new, 0)
        self.assertEqual(summary.fights_refreshed, 1)


if __name__ == "__main__":
    unittest.main()
