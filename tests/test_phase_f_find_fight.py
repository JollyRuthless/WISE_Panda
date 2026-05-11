"""
Tests for Phase F — Find-a-Fight functionality.

These exercise the cohort.py query layer that powers the Find tab. The UI
layer itself isn't tested here (Qt widget testing is its own rabbit hole
and brittle in CI). What we DO test:
  - FightFilters carries discipline_name correctly
  - find_fights filters by class, by discipline, by both
  - find_fights prefers per-fight class data over per-character
  - list_known_disciplines respects class_name filter
  - empty filters return everything (up to the limit)
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import storage.cohort as cohort
import storage.encounter_db as encounter_db
# Reuse the same SCHEMA_SQL pattern from the Phase C tests — full schema
# including the v4 columns. Keeping this inline rather than importing
# avoids the cross-test dependency.

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


def _seed(conn, encounter_key: str, encounter_name: str, encounter_date: str,
          players: list[tuple[str, str, str, str, int]]) -> None:
    """
    Insert one encounter and its players.

    `players` is a list of:
      (character_name, per_character_class, per_fight_class,
       per_fight_discipline, damage)
    The split between per-character and per-fight class is what
    Phase F's preference logic exercises.
    """
    conn.execute(
        "INSERT INTO encounters "
        "(encounter_key, encounter_name, encounter_date, log_path, recorded_by, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (encounter_key, encounter_name, encounter_date, "log.txt", "Recorder",
         f"{encounter_date}T00:00:00"),
    )
    for name, pc_class, pf_class, pf_disc, dmg in players:
        # player_characters row (one per character, ON CONFLICT no-op)
        cur = conn.execute(
            "SELECT character_id FROM player_characters WHERE character_name = ?",
            (name,),
        ).fetchone()
        if cur is None:
            conn.execute(
                "INSERT INTO player_characters "
                "(character_name, class_name, first_seen_date, last_seen_date, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, pc_class, encounter_date, encounter_date,
                 f"{encounter_date}T00:00:00"),
            )
            cid = conn.execute(
                "SELECT character_id FROM player_characters WHERE character_name = ?",
                (name,),
            ).fetchone()[0]
        else:
            cid = cur[0]
        # player_character_encounters row (per-fight)
        conn.execute(
            "INSERT INTO player_character_encounters "
            "(encounter_key, character_id, encounter_date, damage_done, "
            " class_name, discipline_name, class_evidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (encounter_key, cid, encounter_date, dmg,
             pf_class, pf_disc,
             "declared:DisciplineChanged" if pf_disc else ""),
        )


# ─── FightFilters dataclass ────────────────────────────────────────────────


class FightFiltersTests(unittest.TestCase):
    def test_filters_default_discipline_name_is_none(self):
        f = cohort.FightFilters()
        self.assertIsNone(f.discipline_name)

    def test_filters_accepts_discipline_name(self):
        f = cohort.FightFilters(class_name="Operative", discipline_name="Lethality")
        self.assertEqual(f.class_name, "Operative")
        self.assertEqual(f.discipline_name, "Lethality")


# ─── find_fights with class + discipline ────────────────────────────────────


class FindFightsTests(unittest.TestCase):
    """End-to-end tests against a real SQLite DB."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "test.sqlite3"
        _build_db(self.db_path)
        self._patch_db = mock.patch.object(encounter_db, "DB_PATH", self.db_path)
        self._patch_db.start()
        # cohort uses encounter_db._connect_db, which reads DB_PATH at
        # call time — patching encounter_db.DB_PATH covers cohort too.

    def tearDown(self) -> None:
        self._patch_db.stop()
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def _seed_three_fights(self) -> None:
        """
        Three fights against Apex Vanguard, varied participants:

        Fight 1: 2026-04-01 — Holt (Operative/Lethality), Mante (Commando/Gunnery)
        Fight 2: 2026-04-15 — Holt (Operative/Concealment) — same player,
                              different discipline (he respec'd between)
        Fight 3: 2026-04-20 — Mante (Commando/Combat Medic), Doomside (Juggernaut/Vengeance)
        """
        with sqlite3.connect(str(self.db_path)) as conn:
            _seed(conn, "log.txt|10|100|2026-04-01T20:00:00",
                  "Apex Vanguard", "2026-04-01",
                  [
                      ("Holt",  "Operative", "Operative", "Lethality",     500_000),
                      ("Mante", "Commando",  "Commando",  "Gunnery",       400_000),
                  ])
            _seed(conn, "log.txt|110|200|2026-04-15T20:00:00",
                  "Apex Vanguard", "2026-04-15",
                  [
                      ("Holt",  "Operative", "Operative", "Concealment",   600_000),
                  ])
            _seed(conn, "log.txt|210|300|2026-04-20T20:00:00",
                  "Apex Vanguard", "2026-04-20",
                  [
                      ("Mante",     "Commando",   "Commando",   "Combat Medic", 100_000),
                      ("Doomside",  "Juggernaut", "Juggernaut", "Vengeance",    700_000),
                  ])
            conn.commit()

    def test_no_filters_returns_everything(self):
        self._seed_three_fights()
        results = cohort.find_fights(cohort.FightFilters())
        self.assertEqual(len(results), 3)
        # Newest first
        self.assertEqual(results[0].encounter_date, "2026-04-20")
        self.assertEqual(results[2].encounter_date, "2026-04-01")

    def test_class_only_filter(self):
        self._seed_three_fights()
        results = cohort.find_fights(cohort.FightFilters(class_name="Operative"))
        # Only fights 1 and 2 have an Operative
        self.assertEqual(len(results), 2)
        dates = {r.encounter_date for r in results}
        self.assertEqual(dates, {"2026-04-01", "2026-04-15"})

    def test_class_plus_discipline_filter(self):
        self._seed_three_fights()
        # Operative/Lethality should match only fight 1 — Holt was Lethality
        # there but Concealment in fight 2.
        results = cohort.find_fights(cohort.FightFilters(
            class_name="Operative",
            discipline_name="Lethality",
        ))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].encounter_date, "2026-04-01")

    def test_class_plus_different_discipline(self):
        self._seed_three_fights()
        # Operative/Concealment matches fight 2, not fight 1.
        results = cohort.find_fights(cohort.FightFilters(
            class_name="Operative",
            discipline_name="Concealment",
        ))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].encounter_date, "2026-04-15")

    def test_discipline_only_no_class(self):
        self._seed_three_fights()
        # "Vengeance" — only Doomside in fight 3
        results = cohort.find_fights(cohort.FightFilters(
            discipline_name="Vengeance",
        ))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].encounter_date, "2026-04-20")

    def test_date_range_filter(self):
        self._seed_three_fights()
        results = cohort.find_fights(cohort.FightFilters(
            date_from="2026-04-10",
            date_to="2026-04-18",
        ))
        # Only fight 2 falls in this window
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].encounter_date, "2026-04-15")

    def test_class_with_no_matching_discipline_returns_empty(self):
        self._seed_three_fights()
        # No Operative fought as Medicine in our seed.
        results = cohort.find_fights(cohort.FightFilters(
            class_name="Operative",
            discipline_name="Medicine",
        ))
        self.assertEqual(len(results), 0)

    def test_per_fight_data_overrides_per_character(self):
        """
        Phase F-specific behavior: when a player's per-character class is
        Sorcerer (legacy) but their per-fight class+discipline is set to
        Assassin/Hatred (Phase C+ data for this specific fight), filtering
        for "Assassin" should match — we trust the per-fight detection.

        This case happens for old fights that got rebuilt after Phase C
        landed: the per-character row was set first (legacy) and the
        per-fight row got upgraded later.
        """
        with sqlite3.connect(str(self.db_path)) as conn:
            _seed(conn, "log.txt|10|100|2026-04-01T20:00:00",
                  "Some Boss", "2026-04-01",
                  # per-character: Sorcerer (legacy "most recent")
                  # per-fight:     Assassin / Hatred (specific to this fight)
                  [("Multispec", "Sorcerer", "Assassin", "Hatred", 100_000)])
            conn.commit()

        # Filter by per-fight class — should match.
        results = cohort.find_fights(cohort.FightFilters(class_name="Assassin"))
        self.assertEqual(len(results), 1)

        # Filter by per-character class — should NOT match (per-fight trumps).
        results = cohort.find_fights(cohort.FightFilters(class_name="Sorcerer"))
        self.assertEqual(len(results), 0)


# ─── list_known_disciplines ─────────────────────────────────────────────────


class ListKnownDisciplinesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "test.sqlite3"
        _build_db(self.db_path)
        self._patch_db = mock.patch.object(encounter_db, "DB_PATH", self.db_path)
        self._patch_db.start()

        # Seed varied class/discipline pairs
        with sqlite3.connect(str(self.db_path)) as conn:
            _seed(conn, "log.txt|10|100|2026-04-01T20:00:00", "Boss", "2026-04-01",
                  [
                      ("A", "Operative",  "Operative",  "Lethality",     100),
                      ("B", "Operative",  "Operative",  "Concealment",   100),
                      ("C", "Operative",  "Operative",  "Medicine",      100),
                      ("D", "Mercenary",  "Mercenary",  "Arsenal",       100),
                      ("E", "Mercenary",  "Mercenary",  "Bodyguard",     100),
                  ])
            conn.commit()

    def tearDown(self) -> None:
        self._patch_db.stop()
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def test_all_disciplines_when_no_class(self):
        result = cohort.list_known_disciplines()
        self.assertEqual(set(result), {
            "Lethality", "Concealment", "Medicine", "Arsenal", "Bodyguard",
        })
        # Should be alphabetically sorted
        self.assertEqual(result, sorted(result))

    def test_filtered_by_class(self):
        result = cohort.list_known_disciplines(class_name="Operative")
        self.assertEqual(set(result), {"Lethality", "Concealment", "Medicine"})

    def test_unknown_class_returns_empty(self):
        result = cohort.list_known_disciplines(class_name="NotAClass")
        self.assertEqual(result, [])

    def test_class_match_is_case_insensitive(self):
        result = cohort.list_known_disciplines(class_name="OPERATIVE")
        self.assertEqual(set(result), {"Lethality", "Concealment", "Medicine"})


if __name__ == "__main__":
    unittest.main()
