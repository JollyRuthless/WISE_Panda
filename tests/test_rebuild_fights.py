"""
Tests for rebuild_fights_from_existing_imports.

This is the missing complement to import_combat_log: when the structured-data
schema evolves (new columns, new fields), existing imports were ingested
under the OLD code. This function walks combat_log_imports and re-runs
fight aggregation for each one, refreshing the structured tables without
re-importing raw events.

These tests verify:
  - It actually rebuilds populated tables from cleared state
  - It's safe to run repeatedly
  - It handles missing log files gracefully (skip, not fail)
  - It returns honest counts in the summary
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import storage.encounter_db as encounter_db
# Two-player log used for these tests. Same general format as Phase D-1
# tests — minimal but real-shape SWTOR log lines.
TEST_LOG = "\n".join([
    "[19:00:00.000] [@Karzag#100|(0.00,0.00,0.00,0.00)|(20000/20000)] [] [] [Event {836045448945472}: EnterCombat {836045448945489}]",
    "[19:00:00.500] [@Vossan#101|(0.00,0.00,0.00,0.00)|(20000/20000)] [] [] [Event {836045448945472}: EnterCombat {836045448945489}]",
    "[19:00:01.000] [@Karzag#100|(0.00,0.00,0.00,0.00)|(20000/20000)] [Training Dummy {16000}:1|(0.00,0.00,0.00,0.00)|(50000/50000)] [Tracer Missile {810194895470592}] [Event {836045448945472}: AbilityActivate {836045448945479}]",
    "[19:00:01.100] [@Karzag#100|(0.00,0.00,0.00,0.00)|(20000/20000)] [Training Dummy {16000}:1|(0.00,0.00,0.00,0.00)|(48500/50000)] [Tracer Missile {810194895470592}] [ApplyEffect {836045448945477}: Damage {836045448945501}] (1500 energy {836045448940874}) <1500.0>",
    "[19:00:02.000] [@Vossan#101|(0.00,0.00,0.00,0.00)|(20000/20000)] [Training Dummy {16000}:1|(0.00,0.00,0.00,0.00)|(48500/50000)] [Heatseeker Missiles {811078658654208}] [Event {836045448945472}: AbilityActivate {836045448945479}]",
    "[19:00:02.100] [@Vossan#101|(0.00,0.00,0.00,0.00)|(20000/20000)] [Training Dummy {16000}:1|(0.00,0.00,0.00,0.00)|(46500/50000)] [Heatseeker Missiles {811078658654208}] [ApplyEffect {836045448945477}: Damage {836045448945501}] (2000 energy {836045448940874}) <2000.0>",
    "[19:01:00.000] [@Karzag#100|(0.00,0.00,0.00,0.00)|(20000/20000)] [] [] [Event {836045448945472}: ExitCombat {836045448945490}]",
])


class RebuildFightsTests(unittest.TestCase):
    """Verify rebuild_fights_from_existing_imports correctness."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self._tmp_dir = Path(self._tmp.name)
        self._original_db_path = encounter_db.DB_PATH
        self._original_ledger_path = encounter_db.IMPORT_LEDGER_PATH
        encounter_db.DB_PATH = self._tmp_dir / "test.sqlite3"
        encounter_db.IMPORT_LEDGER_PATH = self._tmp_dir / "test_ledger.json"

        self._log_path = self._tmp_dir / "test_log.txt"
        self._log_path.write_text(TEST_LOG, encoding="utf-8")

        # Seed the DB with one normal import.
        encounter_db.import_combat_log(self._log_path)

    def tearDown(self) -> None:
        encounter_db.DB_PATH = self._original_db_path
        encounter_db.IMPORT_LEDGER_PATH = self._original_ledger_path
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def _structured_data_counts(self) -> tuple[int, int, int]:
        """Return (encounters, per-player rows, per-player ability rows)."""
        with sqlite3.connect(str(encounter_db.DB_PATH)) as conn:
            enc = conn.execute("SELECT COUNT(*) FROM encounters").fetchone()[0]
            pce = conn.execute("SELECT COUNT(*) FROM player_character_encounters").fetchone()[0]
            pcea = conn.execute("SELECT COUNT(*) FROM player_character_encounter_abilities").fetchone()[0]
        return (enc, pce, pcea)

    def _clear_structured_data(self) -> None:
        """Wipe everything that rebuild should be able to regenerate."""
        with sqlite3.connect(str(encounter_db.DB_PATH)) as conn:
            conn.execute("DELETE FROM player_character_encounter_abilities")
            conn.execute("DELETE FROM player_character_encounters")
            conn.execute("DELETE FROM encounters")
            conn.commit()

    def test_rebuild_repopulates_cleared_structured_tables(self):
        """The core promise: clear structured data, rebuild, get it back."""
        # Capture state from the original import for comparison.
        original = self._structured_data_counts()
        self.assertGreater(original[0], 0, "import didn't populate encounters")
        self.assertGreater(original[1], 0, "import didn't populate per-player rows")
        self.assertGreater(original[2], 0, "import didn't populate ability rows")

        # Wipe structured data. Raw events stay.
        self._clear_structured_data()
        self.assertEqual(self._structured_data_counts(), (0, 0, 0))

        # Rebuild.
        summary = encounter_db.rebuild_fights_from_existing_imports()
        self.assertEqual(summary.logs_processed, 1)
        self.assertEqual(summary.logs_skipped, 0)
        self.assertGreaterEqual(summary.fights_succeeded, 1)
        self.assertEqual(summary.fights_failed, 0)

        # Verify counts are restored.
        rebuilt = self._structured_data_counts()
        self.assertEqual(rebuilt, original)

    def test_rebuild_is_idempotent(self):
        """Running rebuild twice produces the same data as running it once."""
        # Initial state from setUp's import.
        first = self._structured_data_counts()

        encounter_db.rebuild_fights_from_existing_imports()
        after_first_rebuild = self._structured_data_counts()
        self.assertEqual(after_first_rebuild, first)

        encounter_db.rebuild_fights_from_existing_imports()
        after_second_rebuild = self._structured_data_counts()
        self.assertEqual(after_second_rebuild, first)

    def test_rebuild_skips_missing_log_files(self):
        """If a log file has been deleted, rebuild skips it gracefully."""
        # Delete the log file from disk. The combat_log_imports row stays.
        self._log_path.unlink()

        # Wipe structured data so we can tell rebuild didn't repopulate.
        self._clear_structured_data()

        summary = encounter_db.rebuild_fights_from_existing_imports()
        self.assertEqual(summary.logs_processed, 0)
        self.assertEqual(summary.logs_skipped, 1)
        self.assertEqual(summary.fights_succeeded, 0)
        # Tables should still be empty since no log was readable.
        self.assertEqual(self._structured_data_counts(), (0, 0, 0))

    def test_rebuild_calls_progress_callback(self):
        """The progress callback fires once per log."""
        calls: list[tuple[int, int]] = []

        def callback(done: int, total: int) -> None:
            calls.append((done, total))

        encounter_db.rebuild_fights_from_existing_imports(progress_callback=callback)

        self.assertEqual(len(calls), 1, f"expected 1 callback, got {calls}")
        self.assertEqual(calls[0], (1, 1))

    def test_rebuild_with_no_imports_returns_zero_counts(self):
        """If combat_log_imports is empty, rebuild does nothing."""
        # Wipe the import ledger row.
        with sqlite3.connect(str(encounter_db.DB_PATH)) as conn:
            conn.execute("DELETE FROM combat_log_imports")
            conn.commit()

        summary = encounter_db.rebuild_fights_from_existing_imports()
        self.assertEqual(summary.logs_processed, 0)
        self.assertEqual(summary.logs_skipped, 0)
        self.assertEqual(summary.fights_succeeded, 0)
        self.assertEqual(summary.fights_failed, 0)


if __name__ == "__main__":
    unittest.main()
