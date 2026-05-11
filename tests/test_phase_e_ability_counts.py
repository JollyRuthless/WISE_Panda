"""
Tests for Phase E — pressed / prebuff / damage_source ability count breakdown.

Phase E expands the per-fight ability table to record three independent counts
per (player, ability):

  - use_count: AbilityActivates the player did during the fight
  - prebuff_count: AbilityActivates in the 15s before EnterCombat
  - damage_source_count: damage events in the fight that named this ability

These three counts answer different questions and shouldn't be conflated.
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import storage.encounter_db as encounter_db
# ─── Synthetic log file content ──────────────────────────────────────────────
#
# Two players. One pre-casts a bleed before the fight starts (prebuff),
# the other presses abilities during the fight. The bleed continues to tick
# damage during the fight (damage_source without pressed).

PHASE_E_LOG = "\n".join([
    # Pre-fight: Karzag presses Affliction at 18:59:50 (10 seconds before fight)
    "[18:59:50.000] [@Karzag#100|(0.00,0.00,0.00,0.00)|(20000/20000)] [Training Dummy {16000}:1|(0.00,0.00,0.00,0.00)|(50000/50000)] [Affliction {800000000000001}] [Event {836045448945472}: AbilityActivate {836045448945479}]",
    # Fight starts at 19:00:00
    "[19:00:00.000] [@Karzag#100|(0.00,0.00,0.00,0.00)|(20000/20000)] [] [] [Event {836045448945472}: EnterCombat {836045448945489}]",
    "[19:00:00.500] [@Vossan#101|(0.00,0.00,0.00,0.00)|(20000/20000)] [] [] [Event {836045448945472}: EnterCombat {836045448945489}]",
    # Affliction ticks twice during the fight (damage_source without pressed in fight)
    "[19:00:01.000] [@Karzag#100|(0.00,0.00,0.00,0.00)|(20000/20000)] [Training Dummy {16000}:1|(0.00,0.00,0.00,0.00)|(49000/50000)] [Affliction {800000000000001}] [ApplyEffect {836045448945477}: Damage {836045448945501}] (1000 internal {836045448940874}) <1000.0>",
    "[19:00:04.000] [@Karzag#100|(0.00,0.00,0.00,0.00)|(20000/20000)] [Training Dummy {16000}:1|(0.00,0.00,0.00,0.00)|(48000/50000)] [Affliction {800000000000001}] [ApplyEffect {836045448945477}: Damage {836045448945501}] (1000 internal {836045448940874}) <1000.0>",
    # Vossan presses Heatseeker Missiles in-fight, both pressed AND damage_source
    "[19:00:02.000] [@Vossan#101|(0.00,0.00,0.00,0.00)|(20000/20000)] [Training Dummy {16000}:1|(0.00,0.00,0.00,0.00)|(48000/50000)] [Heatseeker Missiles {811078658654208}] [Event {836045448945472}: AbilityActivate {836045448945479}]",
    "[19:00:02.100] [@Vossan#101|(0.00,0.00,0.00,0.00)|(20000/20000)] [Training Dummy {16000}:1|(0.00,0.00,0.00,0.00)|(46000/50000)] [Heatseeker Missiles {811078658654208}] [ApplyEffect {836045448945477}: Damage {836045448945501}] (2000 energy {836045448940874}) <2000.0>",
    # Karzag presses one in-fight ability (Force Lightning). Hits twice.
    "[19:00:05.000] [@Karzag#100|(0.00,0.00,0.00,0.00)|(20000/20000)] [Training Dummy {16000}:1|(0.00,0.00,0.00,0.00)|(46000/50000)] [Force Lightning {800000000000002}] [Event {836045448945472}: AbilityActivate {836045448945479}]",
    "[19:00:05.100] [@Karzag#100|(0.00,0.00,0.00,0.00)|(20000/20000)] [Training Dummy {16000}:1|(0.00,0.00,0.00,0.00)|(45500/50000)] [Force Lightning {800000000000002}] [ApplyEffect {836045448945477}: Damage {836045448945501}] (500 energy {836045448940874}) <500.0>",
    "[19:00:05.500] [@Karzag#100|(0.00,0.00,0.00,0.00)|(20000/20000)] [Training Dummy {16000}:1|(0.00,0.00,0.00,0.00)|(45000/50000)] [Force Lightning {800000000000002}] [ApplyEffect {836045448945477}: Damage {836045448945501}] (500 energy {836045448940874}) <500.0>",
    # Fight ends
    "[19:01:00.000] [@Karzag#100|(0.00,0.00,0.00,0.00)|(20000/20000)] [] [] [Event {836045448945472}: ExitCombat {836045448945490}]",
])


class PhaseEAbilityCountsTests(unittest.TestCase):
    """End-to-end: import a log and verify pressed/prebuff/damage_source columns."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self._tmp_dir = Path(self._tmp.name)
        self._original_db_path = encounter_db.DB_PATH
        self._original_ledger_path = encounter_db.IMPORT_LEDGER_PATH
        encounter_db.DB_PATH = self._tmp_dir / "test.sqlite3"
        encounter_db.IMPORT_LEDGER_PATH = self._tmp_dir / "test_ledger.json"

        self._log_path = self._tmp_dir / "phase_e_log.txt"
        self._log_path.write_text(PHASE_E_LOG, encoding="utf-8")

    def tearDown(self) -> None:
        encounter_db.DB_PATH = self._original_db_path
        encounter_db.IMPORT_LEDGER_PATH = self._original_ledger_path
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def _query_abilities(self, conn, player_name: str):
        return conn.execute(
            "SELECT pcea.ability_name, pcea.use_count, pcea.prebuff_count, pcea.damage_source_count "
            "FROM player_character_encounter_abilities pcea "
            "JOIN player_characters pc ON pc.character_id = pcea.character_id "
            "WHERE pc.character_name = ? "
            "ORDER BY pcea.ability_name",
            (player_name,),
        ).fetchall()

    def test_prebuff_is_recorded_when_player_casts_before_fight(self):
        """Karzag pre-cast Affliction 10s before EnterCombat."""
        encounter_db.import_combat_log(self._log_path)
        with sqlite3.connect(str(encounter_db.DB_PATH)) as conn:
            rows = self._query_abilities(conn, "Karzag")

        # Find the Affliction row
        affliction = [r for r in rows if r[0] == "Affliction"]
        self.assertEqual(len(affliction), 1, f"Expected one Affliction row, got {rows}")
        ability_name, pressed, prebuff, damage_source = affliction[0]
        # Affliction: pressed=0 (no in-fight cast), prebuff=1 (one pre-cast),
        # damage_source=2 (two ticks during fight)
        self.assertEqual(pressed, 0)
        self.assertEqual(prebuff, 1)
        self.assertEqual(damage_source, 2)

    def test_pressed_and_damage_source_match_for_normal_ability(self):
        """Vossan pressed Heatseeker once; it dealt damage once."""
        encounter_db.import_combat_log(self._log_path)
        with sqlite3.connect(str(encounter_db.DB_PATH)) as conn:
            rows = self._query_abilities(conn, "Vossan")
        heatseeker = [r for r in rows if r[0] == "Heatseeker Missiles"]
        self.assertEqual(len(heatseeker), 1)
        ability_name, pressed, prebuff, damage_source = heatseeker[0]
        self.assertEqual(pressed, 1)
        self.assertEqual(prebuff, 0)
        self.assertEqual(damage_source, 1)

    def test_channeled_ability_has_more_damage_than_presses(self):
        """Karzag pressed Force Lightning once; it ticked damage twice."""
        encounter_db.import_combat_log(self._log_path)
        with sqlite3.connect(str(encounter_db.DB_PATH)) as conn:
            rows = self._query_abilities(conn, "Karzag")
        fl = [r for r in rows if r[0] == "Force Lightning"]
        self.assertEqual(len(fl), 1)
        ability_name, pressed, prebuff, damage_source = fl[0]
        self.assertEqual(pressed, 1)
        self.assertEqual(prebuff, 0)
        self.assertEqual(damage_source, 2)

    def test_pre_buff_outside_window_is_excluded(self):
        """Casts more than 15s before EnterCombat should not count as prebuff."""
        # Build a log where Affliction is cast 30s before — too far back.
        old_log = "\n".join([
            "[18:59:30.000] [@Karzag#100|(0.00,0.00,0.00,0.00)|(20000/20000)] [Training Dummy {16000}:1|(0.00,0.00,0.00,0.00)|(50000/50000)] [Affliction {800000000000001}] [Event {836045448945472}: AbilityActivate {836045448945479}]",
            "[19:00:00.000] [@Karzag#100|(0.00,0.00,0.00,0.00)|(20000/20000)] [] [] [Event {836045448945472}: EnterCombat {836045448945489}]",
            "[19:00:01.000] [@Karzag#100|(0.00,0.00,0.00,0.00)|(20000/20000)] [Training Dummy {16000}:1|(0.00,0.00,0.00,0.00)|(49000/50000)] [Affliction {800000000000001}] [ApplyEffect {836045448945477}: Damage {836045448945501}] (1000 internal {836045448940874}) <1000.0>",
            "[19:01:00.000] [@Karzag#100|(0.00,0.00,0.00,0.00)|(20000/20000)] [] [] [Event {836045448945472}: ExitCombat {836045448945490}]",
        ])
        log_path = self._tmp_dir / "old_buff.txt"
        log_path.write_text(old_log, encoding="utf-8")
        encounter_db.import_combat_log(log_path)
        with sqlite3.connect(str(encounter_db.DB_PATH)) as conn:
            rows = self._query_abilities(conn, "Karzag")
        affliction = [r for r in rows if r[0] == "Affliction"]
        # The cast at -30s is outside the 15s window; prebuff should be 0.
        # The DoT still ticked once during the fight; damage_source should be 1.
        self.assertEqual(len(affliction), 1)
        ability_name, pressed, prebuff, damage_source = affliction[0]
        self.assertEqual(prebuff, 0, "Cast 30s before EnterCombat should not count as prebuff")
        self.assertEqual(damage_source, 1)

    def test_zero_count_rows_are_not_persisted(self):
        """An ability with all three counts at 0 should not appear at all."""
        encounter_db.import_combat_log(self._log_path)
        with sqlite3.connect(str(encounter_db.DB_PATH)) as conn:
            rows = conn.execute(
                "SELECT COUNT(*) FROM player_character_encounter_abilities "
                "WHERE use_count = 0 AND prebuff_count = 0 AND damage_source_count = 0"
            ).fetchone()
        self.assertEqual(rows[0], 0)

    def test_use_count_still_means_in_fight_presses(self):
        """Backwards compatibility: use_count must still be in-fight presses only."""
        encounter_db.import_combat_log(self._log_path)
        with sqlite3.connect(str(encounter_db.DB_PATH)) as conn:
            rows = self._query_abilities(conn, "Karzag")
        # Karzag pressed Force Lightning once in-fight; use_count must be 1.
        # Karzag's Affliction prebuff doesn't count toward use_count.
        affliction = [r for r in rows if r[0] == "Affliction"][0]
        self.assertEqual(affliction[1], 0)  # Affliction use_count = 0

        fl = [r for r in rows if r[0] == "Force Lightning"][0]
        self.assertEqual(fl[1], 1)  # FL use_count = 1


if __name__ == "__main__":
    unittest.main()
