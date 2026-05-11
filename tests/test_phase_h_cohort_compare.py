"""
Tests for Phase H — Cohort Compare functionality.

Two surfaces are tested here:

1. cohort.build_cohort(discipline_name=...) — the discipline filter added in
   Phase H. Mirrors find_fights' per-fight-class preference: when pce.class_name
   is set, match against it. When pce.class_name is empty (old fights), fall
   back to per-character pc.class_name. Discipline ALWAYS comes from per-fight
   pce.discipline_name.

2. cohort.cohort_durations() — fast and precise modes. Fast uses the line-range
   estimate. Precise queries combat_log_events for MIN/MAX timestamp_text.

The Qt tab itself (ui/tabs/cohort_compare.py) is not tested here. Same reason
Phase F skipped Qt widget tests: brittle in CI, not the bug-finding bottleneck.
The view logic is thin enough to verify by hand.
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import storage.cohort as cohort
import storage.encounter_db as encounter_db
# ─── Schema fixture ──────────────────────────────────────────────────────────
#
# Mirrors the production schema for the tables Phase H reads from. We include
# combat_log_imports and combat_log_events here because cohort_durations
# queries both. Phase F's fixture didn't need them.

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
    """
    CREATE TABLE player_character_encounter_abilities (
        encounter_key TEXT NOT NULL,
        character_id INTEGER NOT NULL,
        ability_name TEXT NOT NULL,
        ability_id TEXT NOT NULL DEFAULT '',
        use_count INTEGER NOT NULL DEFAULT 0,
        prebuff_count INTEGER NOT NULL DEFAULT 0,
        damage_source_count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(encounter_key, character_id, ability_id, ability_name)
    )
    """,
    """
    CREATE TABLE combat_log_imports (
        import_id INTEGER PRIMARY KEY AUTOINCREMENT,
        log_path TEXT NOT NULL UNIQUE,
        file_name TEXT NOT NULL,
        file_size INTEGER NOT NULL DEFAULT 0,
        file_modified_at TEXT NOT NULL DEFAULT '',
        imported_at TEXT NOT NULL,
        line_count INTEGER NOT NULL DEFAULT 0,
        parsed_line_count INTEGER NOT NULL DEFAULT 0,
        parse_error_count INTEGER NOT NULL DEFAULT 0,
        source_character_name TEXT NOT NULL DEFAULT '',
        source_class_name TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE combat_log_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        import_id INTEGER NOT NULL,
        line_number INTEGER NOT NULL,
        timestamp_text TEXT NOT NULL DEFAULT '',
        UNIQUE(import_id, line_number)
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


def _make_key(log_path: str, line_start: int, line_end: int, ts: str) -> str:
    """Mirrors encounter_db.encounter_key_for() format."""
    return f"{log_path}|{line_start}|{line_end}|{ts}"


def _seed_player(conn, name: str, pc_class: str, encounter_date: str) -> int:
    """Insert a player_characters row if missing, return character_id."""
    cur = conn.execute(
        "SELECT character_id FROM player_characters WHERE character_name = ?",
        (name,),
    ).fetchone()
    if cur is not None:
        return int(cur[0])
    conn.execute(
        "INSERT INTO player_characters "
        "(character_name, class_name, first_seen_date, last_seen_date, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, pc_class, encounter_date, encounter_date,
         f"{encounter_date}T00:00:00"),
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
    log_path: str,
    participants: list[tuple[str, str, str, str, int, int]],
) -> None:
    """
    Insert one encounter and its participants.

    participants tuple: (character_name, pc_class, pf_class, pf_disc,
                         damage, healing)
    """
    conn.execute(
        "INSERT INTO encounters "
        "(encounter_key, encounter_name, encounter_date, log_path, "
        " recorded_by, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (encounter_key, encounter_name, encounter_date, log_path,
         participants[0][0] if participants else "",
         f"{encounter_date}T00:00:00"),
    )
    for name, pc_class, pf_class, pf_disc, dmg, heal in participants:
        cid = _seed_player(conn, name, pc_class, encounter_date)
        conn.execute(
            "INSERT INTO player_character_encounters "
            "(encounter_key, character_id, encounter_date, "
            " damage_done, healing_done, "
            " class_name, discipline_name, class_evidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (encounter_key, cid, encounter_date, dmg, heal,
             pf_class, pf_disc,
             "declared:DisciplineChanged" if pf_disc else ""),
        )


# ─── build_cohort discipline filter ──────────────────────────────────────────


class BuildCohortDisciplineTests(unittest.TestCase):
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

    def _seed_apex_vanguard(self) -> None:
        """
        Three Apex Vanguard fights, three different specs:
          1. Holt — Operative/Lethality
          2. Holt — Operative/Concealment (respec on a later night)
          3. Mante — Operative/Medicine (different player, healer spec)
        """
        with sqlite3.connect(str(self.db_path)) as conn:
            _seed_fight(
                conn,
                _make_key("log.txt", 10, 100, "2026-04-01T20:00:00"),
                "Apex Vanguard", "2026-04-01", "log.txt",
                [("Holt", "Operative", "Operative", "Lethality",   500_000, 0)],
            )
            _seed_fight(
                conn,
                _make_key("log.txt", 110, 200, "2026-04-15T20:00:00"),
                "Apex Vanguard", "2026-04-15", "log.txt",
                [("Holt", "Operative", "Operative", "Concealment", 600_000, 0)],
            )
            _seed_fight(
                conn,
                _make_key("log.txt", 210, 300, "2026-04-20T20:00:00"),
                "Apex Vanguard", "2026-04-20", "log.txt",
                [("Mante", "Operative", "Operative", "Medicine",   100_000, 800_000)],
            )
            conn.commit()

    def test_class_only_returns_all_three(self):
        """Without a discipline filter, all three Operatives match."""
        self._seed_apex_vanguard()
        c = cohort.build_cohort("Operative", "Apex Vanguard")
        self.assertEqual(c.sample_size, 3)

    def test_discipline_lethality_filters_to_one(self):
        """Discipline filter narrows to the single Lethality fight."""
        self._seed_apex_vanguard()
        c = cohort.build_cohort(
            "Operative", "Apex Vanguard", discipline_name="Lethality",
        )
        self.assertEqual(c.sample_size, 1)
        self.assertEqual(c.fights[0].character_name, "Holt")
        self.assertEqual(c.fights[0].discipline_name, "Lethality")

    def test_discipline_concealment_filters_to_one(self):
        self._seed_apex_vanguard()
        c = cohort.build_cohort(
            "Operative", "Apex Vanguard", discipline_name="Concealment",
        )
        self.assertEqual(c.sample_size, 1)
        self.assertEqual(c.fights[0].discipline_name, "Concealment")

    def test_discipline_unmatched_returns_empty(self):
        """Discipline that doesn't appear in the data returns an empty cohort."""
        self._seed_apex_vanguard()
        c = cohort.build_cohort(
            "Operative", "Apex Vanguard", discipline_name="Sawbones",
        )
        self.assertEqual(c.sample_size, 0)
        # Empty cohort should still report the right encounter/class.
        self.assertEqual(c.encounter_name, "Apex Vanguard")
        self.assertEqual(c.class_name, "Operative")

    def test_discipline_case_insensitive(self):
        """Discipline matching is COLLATE NOCASE — same behaviour as class."""
        self._seed_apex_vanguard()
        c = cohort.build_cohort(
            "Operative", "Apex Vanguard", discipline_name="lethality",
        )
        self.assertEqual(c.sample_size, 1)

    def test_discipline_falls_back_to_per_character_class(self):
        """
        If pce.class_name is empty (old fight ingested before Phase C), the
        match should fall back to pc.class_name. This mirrors find_fights.
        """
        with sqlite3.connect(str(self.db_path)) as conn:
            # Old fight: pce.class_name='' but pc.class_name='Operative'.
            # pce.discipline_name was somehow populated (maybe by a Phase C
            # backfill). The cohort match needs to find this fight.
            _seed_fight(
                conn,
                _make_key("log.txt", 10, 100, "2026-04-01T20:00:00"),
                "Apex Vanguard", "2026-04-01", "log.txt",
                [("Holt", "Operative", "", "Lethality", 500_000, 0)],
            )
            conn.commit()

        c = cohort.build_cohort(
            "Operative", "Apex Vanguard", discipline_name="Lethality",
        )
        self.assertEqual(c.sample_size, 1)
        self.assertEqual(c.fights[0].character_name, "Holt")

    def test_class_only_still_uses_per_fight_when_present(self):
        """
        Class-only matching (no discipline) prefers pce.class_name, with
        fallback to pc.class_name when the per-fight column is empty. Mirrors
        find_fights.
        """
        with sqlite3.connect(str(self.db_path)) as conn:
            # Player whose per-character class has been mis-detected as
            # Mercenary, but per-fight (Phase C) correctly says Operative.
            _seed_fight(
                conn,
                _make_key("log.txt", 10, 100, "2026-04-01T20:00:00"),
                "Apex Vanguard", "2026-04-01", "log.txt",
                [("Holt", "Mercenary", "Operative", "Lethality", 500_000, 0)],
            )
            conn.commit()

        # Filtering for Operative should find this fight via pce.class_name.
        c = cohort.build_cohort("Operative", "Apex Vanguard")
        self.assertEqual(c.sample_size, 1)
        self.assertEqual(c.fights[0].character_name, "Holt")
        # Filtering for Mercenary should NOT — per-fight overrides.
        c = cohort.build_cohort("Mercenary", "Apex Vanguard")
        self.assertEqual(c.sample_size, 0)


# ─── cohort_durations: fast (estimate) and precise (event-driven) ───────────


class CohortDurationsTests(unittest.TestCase):
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

    def test_empty_input_returns_empty_dict(self):
        self.assertEqual(cohort.cohort_durations([]), {})
        self.assertEqual(cohort.cohort_durations([""]), {})

    def test_fast_path_uses_line_range_estimate(self):
        """precise=False returns the line-range estimate (lines/5)."""
        key = _make_key("log.txt", 100, 600, "2026-04-01T20:00:00")
        result = cohort.cohort_durations([key], precise=False)
        self.assertIn(key, result)
        # 500 lines / 5 lines-per-second = 100 seconds.
        self.assertAlmostEqual(result[key], 100.0, places=3)

    def test_precise_path_uses_event_timestamps(self):
        """
        precise=True (default) computes elapsed seconds from MIN/MAX
        timestamp_text in combat_log_events for the fight's line range.
        """
        log_path = "log.txt"
        key = _make_key(log_path, 100, 600, "2026-04-01T20:00:00")
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO combat_log_imports "
                "(log_path, file_name, imported_at) VALUES (?, ?, ?)",
                (log_path, "log.txt", "2026-04-01T20:00:00"),
            )
            import_id = conn.execute(
                "SELECT import_id FROM combat_log_imports WHERE log_path = ?",
                (log_path,),
            ).fetchone()[0]
            # Two events at the boundaries of the fight, 45.500 seconds apart.
            conn.execute(
                "INSERT INTO combat_log_events "
                "(import_id, line_number, timestamp_text) VALUES (?, ?, ?)",
                (import_id, 100, "[20:00:00.000]"),
            )
            conn.execute(
                "INSERT INTO combat_log_events "
                "(import_id, line_number, timestamp_text) VALUES (?, ?, ?)",
                (import_id, 600, "[20:00:45.500]"),
            )
            conn.commit()

        result = cohort.cohort_durations([key], precise=True)
        self.assertIn(key, result)
        self.assertAlmostEqual(result[key], 45.5, places=3)

    def test_precise_falls_back_to_estimate_when_no_events(self):
        """
        If a fight's log was never imported (no combat_log_imports row), the
        precise path can't run — we fall back to the line-range estimate
        rather than omit the key.
        """
        key = _make_key("missing.txt", 0, 1000, "2026-04-01T20:00:00")
        result = cohort.cohort_durations([key], precise=True)
        self.assertIn(key, result)
        # 1000 lines / 5 = 200 seconds estimate.
        self.assertAlmostEqual(result[key], 200.0, places=3)

    def test_precise_one_query_per_log(self):
        """
        Two fights from the same log should result in one import_id lookup,
        not two. We can't easily count queries, but we can assert the answer
        is correct for both fights with shared infrastructure.
        """
        log_path = "shared.txt"
        key_a = _make_key(log_path, 100, 200, "2026-04-01T20:00:00")
        key_b = _make_key(log_path, 300, 400, "2026-04-01T21:00:00")
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO combat_log_imports "
                "(log_path, file_name, imported_at) VALUES (?, ?, ?)",
                (log_path, "shared.txt", "2026-04-01T20:00:00"),
            )
            import_id = conn.execute(
                "SELECT import_id FROM combat_log_imports WHERE log_path = ?",
                (log_path,),
            ).fetchone()[0]
            for line, ts in [
                (100, "[20:00:00.000]"),
                (200, "[20:00:30.000]"),
                (300, "[21:00:00.000]"),
                (400, "[21:00:10.250]"),
            ]:
                conn.execute(
                    "INSERT INTO combat_log_events "
                    "(import_id, line_number, timestamp_text) VALUES (?, ?, ?)",
                    (import_id, line, ts),
                )
            conn.commit()

        result = cohort.cohort_durations([key_a, key_b])
        self.assertAlmostEqual(result[key_a], 30.0, places=3)
        self.assertAlmostEqual(result[key_b], 10.25, places=3)

    def test_malformed_key_yields_zero_estimate(self):
        """A junk key parses to (0,0) line range — estimate = 0."""
        result = cohort.cohort_durations(["not-a-real-key"], precise=False)
        self.assertEqual(result, {"not-a-real-key": 0.0})

    def test_seconds_between_timestamp_text_handles_missing_brackets(self):
        """
        The internal helper should accept '20:00:00' as well as
        '[20:00:00.000]'. Future-proofing against data drift.
        """
        from storage.cohort import _seconds_between_timestamp_text
        self.assertAlmostEqual(
            _seconds_between_timestamp_text("[20:00:00.000]", "[20:00:01.500]"),
            1.5, places=3,
        )
        self.assertAlmostEqual(
            _seconds_between_timestamp_text("20:00:00", "20:00:02"),
            2.0, places=3,
        )

    def test_seconds_between_timestamp_text_returns_none_on_garbage(self):
        from storage.cohort import _seconds_between_timestamp_text
        self.assertIsNone(_seconds_between_timestamp_text("hello", "[20:00:00]"))
        self.assertIsNone(_seconds_between_timestamp_text("[20:00:00]", "world"))


if __name__ == "__main__":
    unittest.main()
