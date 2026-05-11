"""
Tests for the Roster app's backend layer.

What's covered:
  - cohort.list_known_players() — counts, most-played-class resolution,
    last-seen date, name-contains filter, empty DB, fallback to per-character
    class when no per-fight data exists
  - ui_roster.roles — the lookup table covers every parser-known spec, no
    role drift between the parser and the role module, basic role_for()

UI panels not tested here. Same reason Phase F/H skipped Qt widget tests:
brittle in CI, not the bug-finding bottleneck.
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import engine.class_detection as class_detection
import storage.cohort as cohort
import storage.encounter_db as encounter_db
from ui_roster.roles import (
    ROLE_BY_SPEC,
    ROLE_DPS,
    ROLE_HEALER,
    ROLE_TANK,
    ROLE_UNKNOWN,
    all_classes_for_role,
    role_for,
)


# ─── Schema fixture ──────────────────────────────────────────────────────────

SCHEMA_SQL = [
    """
    CREATE TABLE encounters (
        encounter_key TEXT PRIMARY KEY,
        encounter_name TEXT NOT NULL,
        encounter_date TEXT NOT NULL,
        log_path TEXT NOT NULL,
        recorded_by TEXT,
        biggest_hit_amount INTEGER NOT NULL DEFAULT 0,
        biggest_hit_by TEXT,
        biggest_hit_ability TEXT,
        biggest_heal_amount INTEGER NOT NULL DEFAULT 0,
        biggest_heal_by TEXT,
        biggest_heal_ability TEXT,
        deaths_json TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE player_characters (
        character_id INTEGER PRIMARY KEY AUTOINCREMENT,
        character_name TEXT NOT NULL COLLATE NOCASE UNIQUE,
        class_name TEXT NOT NULL DEFAULT '',
        first_seen_date TEXT NOT NULL,
        last_seen_date TEXT NOT NULL,
        total_damage_done INTEGER NOT NULL DEFAULT 0,
        total_healing_done INTEGER NOT NULL DEFAULT 0,
        total_taunts INTEGER NOT NULL DEFAULT 0,
        total_interrupts INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE player_character_encounters (
        encounter_key TEXT NOT NULL,
        character_id INTEGER NOT NULL,
        encounter_date TEXT NOT NULL,
        damage_done INTEGER NOT NULL DEFAULT 0,
        healing_done INTEGER NOT NULL DEFAULT 0,
        taunts INTEGER NOT NULL DEFAULT 0,
        interrupts INTEGER NOT NULL DEFAULT 0,
        class_name TEXT NOT NULL DEFAULT '',
        discipline_name TEXT NOT NULL DEFAULT '',
        class_evidence TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (encounter_key, character_id)
    )
    """,
]


def _build_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        for stmt in SCHEMA_SQL:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def _seed_player(conn, name: str, pc_class: str, last_date: str = "2026-04-01") -> int:
    conn.execute(
        "INSERT INTO player_characters "
        "(character_name, class_name, first_seen_date, last_seen_date, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, pc_class, "2026-01-01", last_date, last_date + "T00:00:00"),
    )
    return int(conn.execute(
        "SELECT character_id FROM player_characters WHERE character_name = ?",
        (name,),
    ).fetchone()[0])


def _seed_fight(
    conn,
    encounter_key: str,
    encounter_name: str,
    encounter_date: str,
    participants: list[tuple[int, str]],  # (character_id, per_fight_class)
) -> None:
    conn.execute(
        "INSERT INTO encounters "
        "(encounter_key, encounter_name, encounter_date, log_path, "
        " recorded_by, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (encounter_key, encounter_name, encounter_date, "log.txt", "Test",
         encounter_date + "T00:00:00"),
    )
    for cid, pf_class in participants:
        conn.execute(
            "INSERT INTO player_character_encounters "
            "(encounter_key, character_id, encounter_date, class_name) "
            "VALUES (?, ?, ?, ?)",
            (encounter_key, cid, encounter_date, pf_class),
        )


# ─── list_known_players ──────────────────────────────────────────────────────


class ListKnownPlayersTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "test.sqlite3"
        _build_db(self.db_path)
        self._patch_db = mock.patch.object(encounter_db, "DB_PATH", self.db_path)
        self._patch_db.start()

    def tearDown(self) -> None:
        self._patch_db.stop()
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def test_empty_db_returns_empty_list(self):
        self.assertEqual(cohort.list_known_players(), [])

    def test_player_with_no_fights_appears_with_zero_count(self):
        """A player_characters row without any encounters still shows up."""
        with sqlite3.connect(str(self.db_path)) as conn:
            _seed_player(conn, "Lonely", "Mercenary")
            conn.commit()

        results = cohort.list_known_players()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].character_name, "Lonely")
        self.assertEqual(results[0].fight_count, 0)
        self.assertEqual(results[0].most_played_class, "Mercenary")
        # last_seen_date is empty when there are no encounters
        self.assertEqual(results[0].last_seen_date, "")

    def test_fight_count_and_last_seen(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            cid = _seed_player(conn, "Karzag", "Mercenary")
            _seed_fight(conn, "log|0|100|2026-04-01T20", "Boss A", "2026-04-01",
                        [(cid, "Arsenal")])
            _seed_fight(conn, "log|100|200|2026-04-15T20", "Boss A", "2026-04-15",
                        [(cid, "Arsenal")])
            _seed_fight(conn, "log|200|300|2026-04-20T20", "Boss B", "2026-04-20",
                        [(cid, "Arsenal")])
            conn.commit()

        results = cohort.list_known_players()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].fight_count, 3)
        self.assertEqual(results[0].last_seen_date, "2026-04-20")

    def test_most_played_class_uses_per_fight_data(self):
        """When per-fight class is set, prefer it over per-character."""
        with sqlite3.connect(str(self.db_path)) as conn:
            # Karzag's per-character class is Mercenary (mistakenly), but his
            # per-fight class is consistently Operative. Most-played should be
            # Operative.
            cid = _seed_player(conn, "Karzag", "Mercenary")
            for i in range(5):
                _seed_fight(
                    conn, f"log|{i*100}|{i*100+50}|2026-04-{i+1:02d}",
                    "Boss A", f"2026-04-{i+1:02d}",
                    [(cid, "Operative")],
                )
            conn.commit()

        results = cohort.list_known_players()
        self.assertEqual(results[0].most_played_class, "Operative")

    def test_most_played_class_picks_majority(self):
        """When per-fight class varies across fights, pick the most common."""
        with sqlite3.connect(str(self.db_path)) as conn:
            cid = _seed_player(conn, "Karzag", "Mercenary")
            # 3 Mercenary fights, 1 Operative — Mercenary wins
            for i in range(3):
                _seed_fight(
                    conn, f"log|{i*100}|{i*100+50}|2026-04-{i+1:02d}",
                    "Boss A", f"2026-04-{i+1:02d}",
                    [(cid, "Mercenary")],
                )
            _seed_fight(
                conn, "log|999|1050|2026-04-09",
                "Boss A", "2026-04-09",
                [(cid, "Operative")],
            )
            conn.commit()

        results = cohort.list_known_players()
        self.assertEqual(results[0].most_played_class, "Mercenary")

    def test_most_played_class_falls_back_to_per_character(self):
        """When no per-fight class data exists, use pc.class_name."""
        with sqlite3.connect(str(self.db_path)) as conn:
            cid = _seed_player(conn, "OldFighter", "Sorcerer")
            # Old fight ingested before Phase C — pce.class_name is empty
            _seed_fight(
                conn, "log|0|100|2026-04-01",
                "Boss A", "2026-04-01",
                [(cid, "")],
            )
            conn.commit()

        results = cohort.list_known_players()
        self.assertEqual(results[0].most_played_class, "Sorcerer")

    def test_name_contains_filter_case_insensitive(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            _seed_player(conn, "Karzag", "Mercenary")
            _seed_player(conn, "Doomside", "Juggernaut")
            _seed_player(conn, "Vossan", "Mercenary")
            conn.commit()

        # "do" should match Doomside but case-insensitively.
        results = cohort.list_known_players(name_contains="do")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].character_name, "Doomside")

        # Empty string is "no filter."
        results = cohort.list_known_players(name_contains="")
        self.assertEqual(len(results), 3)

    def test_sort_order_fight_count_descending(self):
        """Most active player surfaces first."""
        with sqlite3.connect(str(self.db_path)) as conn:
            casual = _seed_player(conn, "Casual", "Sorcerer")
            heavy = _seed_player(conn, "Heavy", "Sorcerer")
            _seed_fight(conn, "log|0|10|2026-04-01", "Boss", "2026-04-01",
                        [(casual, "Madness")])
            for i in range(5):
                _seed_fight(
                    conn, f"log|{i*100}|{i*100+50}|2026-04-{i+10:02d}",
                    "Boss", f"2026-04-{i+10:02d}",
                    [(heavy, "Madness")],
                )
            conn.commit()

        results = cohort.list_known_players()
        self.assertEqual(results[0].character_name, "Heavy")
        self.assertEqual(results[1].character_name, "Casual")

    def test_limit_caps_results(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            for i in range(10):
                _seed_player(conn, f"Player{i:02d}", "Sage")
            conn.commit()

        results = cohort.list_known_players(limit=3)
        self.assertEqual(len(results), 3)


# ─── ui_roster.roles ─────────────────────────────────────────────────────────


class RolesTableTests(unittest.TestCase):
    """
    The roles table MUST cover every (class, discipline) pair that the parser
    can produce. If class_detection.py adds a new discipline, this test fails
    until roles.py is updated.
    """

    def test_no_drift_between_parser_and_roles(self):
        """The parser's pairs and the roles table's pairs are identical."""
        parser_pairs = set()
        for klass, entries in class_detection._FINGERPRINT_TABLE_BY_CLASS.items():
            for _, disc, _ in entries:
                if disc:
                    parser_pairs.add((klass, disc))

        roles_pairs = set(ROLE_BY_SPEC.keys())

        missing_in_roles = parser_pairs - roles_pairs
        extra_in_roles = roles_pairs - parser_pairs

        self.assertEqual(
            missing_in_roles, set(),
            f"Parser knows specs missing from roles.py: {missing_in_roles}",
        )
        self.assertEqual(
            extra_in_roles, set(),
            f"roles.py has specs the parser doesn't know about: {extra_in_roles}",
        )

    def test_total_count_is_48(self):
        """16 advanced classes × 3 disciplines = 48."""
        self.assertEqual(len(ROLE_BY_SPEC), 48)

    def test_role_distribution(self):
        """6 tank specs (3 × 2 mirrors), 6 healer specs, 36 DPS specs."""
        self.assertEqual(len(all_classes_for_role(ROLE_TANK)), 6)
        self.assertEqual(len(all_classes_for_role(ROLE_HEALER)), 6)
        self.assertEqual(len(all_classes_for_role(ROLE_DPS)), 36)


class RoleForTests(unittest.TestCase):
    def test_known_tanks(self):
        self.assertEqual(role_for("Juggernaut", "Immortal"), ROLE_TANK)
        self.assertEqual(role_for("Guardian", "Defense"), ROLE_TANK)
        self.assertEqual(role_for("Powertech", "Shield Tech"), ROLE_TANK)
        self.assertEqual(role_for("Vanguard", "Shield Specialist"), ROLE_TANK)
        self.assertEqual(role_for("Assassin", "Darkness"), ROLE_TANK)
        self.assertEqual(role_for("Shadow", "Kinetic Combat"), ROLE_TANK)

    def test_known_healers(self):
        self.assertEqual(role_for("Mercenary", "Bodyguard"), ROLE_HEALER)
        self.assertEqual(role_for("Commando", "Combat Medic"), ROLE_HEALER)
        self.assertEqual(role_for("Operative", "Medicine"), ROLE_HEALER)
        self.assertEqual(role_for("Scoundrel", "Sawbones"), ROLE_HEALER)
        self.assertEqual(role_for("Sorcerer", "Corruption"), ROLE_HEALER)
        self.assertEqual(role_for("Sage", "Seer"), ROLE_HEALER)

    def test_known_dps_sample(self):
        self.assertEqual(role_for("Marauder", "Annihilation"), ROLE_DPS)
        self.assertEqual(role_for("Sniper", "Marksmanship"), ROLE_DPS)
        self.assertEqual(role_for("Operative", "Lethality"), ROLE_DPS)
        self.assertEqual(role_for("Juggernaut", "Vengeance"), ROLE_DPS)

    def test_empty_inputs_yield_unknown(self):
        self.assertEqual(role_for("", ""), ROLE_UNKNOWN)
        self.assertEqual(role_for("Juggernaut", ""), ROLE_UNKNOWN)
        self.assertEqual(role_for("", "Vengeance"), ROLE_UNKNOWN)

    def test_unknown_pair_yields_unknown(self):
        self.assertEqual(role_for("Sith Lord", "Awesomeness"), ROLE_UNKNOWN)
        # Class right, discipline wrong
        self.assertEqual(role_for("Juggernaut", "Madness"), ROLE_UNKNOWN)
        # Discipline right, class wrong
        self.assertEqual(role_for("Mercenary", "Vengeance"), ROLE_UNKNOWN)


if __name__ == "__main__":
    unittest.main()
