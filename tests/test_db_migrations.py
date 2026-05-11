"""
Tests for db_migrations.py — the schema migration module.

Strategy: build a temp SQLite that has the OLD schema (the one with the
single-column primary keys), populate it with realistic data, run the
migration, verify:

  - The new schema is in place
  - All old data is preserved exactly (row count + values)
  - The new schema can now accept multiple players per fight
  - Running the migration a second time is a no-op (idempotent)
  - The backup file was created
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import storage.db_migrations as db_migrations
# ─── Fixture: build the OLD schema ────────────────────────────────────────────

OLD_SCHEMA_SQL = [
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
    # OLD shape — encounter_key alone is the PRIMARY KEY.
    """
    CREATE TABLE player_character_encounters (
        encounter_key TEXT PRIMARY KEY,
        character_id INTEGER NOT NULL,
        encounter_date TEXT NOT NULL,
        damage_done INTEGER NOT NULL DEFAULT 0,
        healing_done INTEGER NOT NULL DEFAULT 0,
        taunts INTEGER NOT NULL DEFAULT 0,
        interrupts INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(character_id) REFERENCES player_characters(character_id) ON DELETE CASCADE
    )
    """,
    # OLD shape — character_id is missing from the PRIMARY KEY.
    """
    CREATE TABLE player_character_encounter_abilities (
        encounter_key TEXT NOT NULL,
        character_id INTEGER NOT NULL,
        ability_name TEXT NOT NULL,
        ability_id TEXT NOT NULL DEFAULT '',
        use_count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(encounter_key, ability_id, ability_name),
        FOREIGN KEY(character_id) REFERENCES player_characters(character_id) ON DELETE CASCADE
    )
    """,
    # The original index on the abilities table.
    """
    CREATE INDEX idx_player_character_encounter_abilities_character_id
    ON player_character_encounter_abilities(character_id)
    """,
    # The original index on the encounters table.
    """
    CREATE INDEX idx_player_character_encounters_character_id
    ON player_character_encounters(character_id)
    """,
]


def _build_old_db(db_path: Path) -> None:
    """Create a database with the pre-migration schema and some data in it."""
    conn = sqlite3.connect(str(db_path))
    try:
        for stmt in OLD_SCHEMA_SQL:
            conn.execute(stmt)

        # One known character, one known fight, with realistic-looking values.
        # The old schema only allowed one player per fight, so we put the
        # recorder in there and that's it.
        conn.execute(
            "INSERT INTO player_characters "
            "(character_id, character_name, class_name, first_seen_date, "
            " last_seen_date, total_damage_done, total_healing_done, "
            " total_taunts, total_interrupts, updated_at) "
            "VALUES (1, 'Karzag', 'Mercenary', '2026-04-01', '2026-04-27', "
            "        900000, 0, 0, 1, '2026-04-27')"
        )
        conn.execute(
            "INSERT INTO player_character_encounters "
            "(encounter_key, character_id, encounter_date, damage_done, "
            " healing_done, taunts, interrupts) "
            "VALUES ('/logs/run.txt|100|2500|21:14:00', 1, '2026-04-27', "
            "        900000, 0, 0, 1)"
        )
        # Three abilities used in that fight by Karzag.
        for ab_id, ab_name, count in [
            ("810194895470592", "Tracer Missile", 18),
            ("811078658654208", "Heatseeker Missiles", 6),
            ("811117313359872", "Rail Shot", 12),
        ]:
            conn.execute(
                "INSERT INTO player_character_encounter_abilities "
                "(encounter_key, character_id, ability_name, ability_id, use_count) "
                "VALUES ('/logs/run.txt|100|2500|21:14:00', 1, ?, ?, ?)",
                (ab_name, ab_id, count),
            )
        conn.commit()
    finally:
        conn.close()


# ─── Tests ────────────────────────────────────────────────────────────────────


class MigrationDetectionTests(unittest.TestCase):
    """The detection helpers should accurately read schema state."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "test.sqlite3"
        _build_old_db(self.db_path)

    def tearDown(self) -> None:
        import gc
        gc.collect()
        # Windows holds SQLite file handles open longer than Linux. Swallow
        # any cleanup error — the temp file will be removed by the OS later.
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def test_old_schema_is_detected_as_not_yet_migrated(self):
        self.assertFalse(db_migrations._migration_v2_already_applied(self.db_path))

    def test_new_schema_is_detected_as_already_migrated(self):
        # Run the migration to get to the new shape, then check.
        db_migrations._migration_v2_per_player_per_fight(self.db_path)
        self.assertTrue(db_migrations._migration_v2_already_applied(self.db_path))

    def test_primary_key_columns_reads_old_pce_correctly(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            cols = db_migrations._primary_key_columns(conn, "player_character_encounters")
        self.assertEqual(cols, ["encounter_key"])

    def test_primary_key_columns_reads_old_pcea_correctly(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            cols = db_migrations._primary_key_columns(
                conn, "player_character_encounter_abilities"
            )
        # Old schema: encounter_key, ability_id, ability_name (in that order)
        self.assertEqual(cols, ["encounter_key", "ability_id", "ability_name"])

    def test_table_exists_helper(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            self.assertTrue(db_migrations._table_exists(conn, "player_characters"))
            self.assertFalse(db_migrations._table_exists(conn, "no_such_table"))


class MigrationApplyTests(unittest.TestCase):
    """The migration should upgrade the schema and preserve data."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "test.sqlite3"
        _build_old_db(self.db_path)

    def tearDown(self) -> None:
        import gc
        gc.collect()
        # Windows holds SQLite file handles open longer than Linux. Swallow
        # any cleanup error — the temp file will be removed by the OS later.
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def test_migration_changes_pce_primary_key(self):
        db_migrations._migration_v2_per_player_per_fight(self.db_path)
        with sqlite3.connect(str(self.db_path)) as conn:
            cols = db_migrations._primary_key_columns(conn, "player_character_encounters")
        self.assertEqual(set(cols), {"encounter_key", "character_id"})

    def test_migration_changes_pcea_primary_key(self):
        db_migrations._migration_v2_per_player_per_fight(self.db_path)
        with sqlite3.connect(str(self.db_path)) as conn:
            cols = db_migrations._primary_key_columns(
                conn, "player_character_encounter_abilities"
            )
        self.assertEqual(
            set(cols),
            {"encounter_key", "character_id", "ability_id", "ability_name"},
        )

    def test_migration_preserves_pce_rows(self):
        # Snapshot before
        with sqlite3.connect(str(self.db_path)) as conn:
            before = conn.execute(
                "SELECT encounter_key, character_id, encounter_date, "
                "       damage_done, healing_done, taunts, interrupts "
                "FROM player_character_encounters ORDER BY encounter_key"
            ).fetchall()

        db_migrations._migration_v2_per_player_per_fight(self.db_path)

        with sqlite3.connect(str(self.db_path)) as conn:
            after = conn.execute(
                "SELECT encounter_key, character_id, encounter_date, "
                "       damage_done, healing_done, taunts, interrupts "
                "FROM player_character_encounters ORDER BY encounter_key"
            ).fetchall()

        self.assertEqual(before, after)
        self.assertEqual(len(after), 1)

    def test_migration_preserves_pcea_rows(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            before = conn.execute(
                "SELECT encounter_key, character_id, ability_name, ability_id, use_count "
                "FROM player_character_encounter_abilities "
                "ORDER BY ability_name"
            ).fetchall()

        db_migrations._migration_v2_per_player_per_fight(self.db_path)

        with sqlite3.connect(str(self.db_path)) as conn:
            after = conn.execute(
                "SELECT encounter_key, character_id, ability_name, ability_id, use_count "
                "FROM player_character_encounter_abilities "
                "ORDER BY ability_name"
            ).fetchall()

        self.assertEqual(before, after)
        self.assertEqual(len(after), 3)

    def test_migration_preserves_unrelated_tables(self):
        # player_characters should be untouched.
        with sqlite3.connect(str(self.db_path)) as conn:
            before = conn.execute(
                "SELECT character_id, character_name, class_name FROM player_characters"
            ).fetchall()

        db_migrations._migration_v2_per_player_per_fight(self.db_path)

        with sqlite3.connect(str(self.db_path)) as conn:
            after = conn.execute(
                "SELECT character_id, character_name, class_name FROM player_characters"
            ).fetchall()

        self.assertEqual(before, after)

    def test_indexes_are_recreated(self):
        """Joins on character_id need the index. Verify it survives migration."""
        db_migrations._migration_v2_per_player_per_fight(self.db_path)

        with sqlite3.connect(str(self.db_path)) as conn:
            indexes = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
        self.assertIn("idx_player_character_encounters_character_id", indexes)
        self.assertIn("idx_player_character_encounter_abilities_character_id", indexes)

    def test_old_temp_tables_are_dropped(self):
        """The migration should leave no _old_* tables behind."""
        db_migrations._migration_v2_per_player_per_fight(self.db_path)

        with sqlite3.connect(str(self.db_path)) as conn:
            tables = [
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ]
        for t in tables:
            self.assertFalse(t.startswith("_old_"), f"Leftover table: {t}")


class MigrationCapabilityTests(unittest.TestCase):
    """The new schema should actually do what the old one couldn't."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "test.sqlite3"
        _build_old_db(self.db_path)
        db_migrations._migration_v2_per_player_per_fight(self.db_path)

        # Add a second character so we can test multi-player inserts.
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO player_characters "
                "(character_id, character_name, class_name, first_seen_date, "
                " last_seen_date, total_damage_done, total_healing_done, "
                " total_taunts, total_interrupts, updated_at) "
                "VALUES (2, 'Vossan', 'Mercenary', '2026-04-15', '2026-04-27', "
                "        1200000, 0, 0, 2, '2026-04-27')"
            )
            conn.commit()

    def tearDown(self) -> None:
        import gc
        gc.collect()
        # Windows holds SQLite file handles open longer than Linux. Swallow
        # any cleanup error — the temp file will be removed by the OS later.
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def test_can_insert_multiple_players_in_same_fight(self):
        """The schema bug we set out to fix. This is the proof."""
        with sqlite3.connect(str(self.db_path)) as conn:
            # The same encounter_key as Karzag's existing row, but a different
            # character_id. Old schema would reject this with UNIQUE constraint
            # failure — the whole reason we did this migration.
            conn.execute(
                "INSERT INTO player_character_encounters "
                "(encounter_key, character_id, encounter_date, "
                " damage_done, healing_done, taunts, interrupts) "
                "VALUES ('/logs/run.txt|100|2500|21:14:00', 2, '2026-04-27', "
                "        1200000, 0, 0, 2)"
            )
            conn.commit()

            count = conn.execute(
                "SELECT COUNT(*) FROM player_character_encounters "
                "WHERE encounter_key = '/logs/run.txt|100|2500|21:14:00'"
            ).fetchone()[0]

        self.assertEqual(count, 2)

    def test_can_insert_two_players_using_same_ability(self):
        """Both Mercs use Tracer Missile — must store both rows separately."""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO player_character_encounter_abilities "
                "(encounter_key, character_id, ability_name, ability_id, use_count) "
                "VALUES ('/logs/run.txt|100|2500|21:14:00', 2, "
                "        'Tracer Missile', '810194895470592', 22)"
            )
            conn.commit()

            rows = conn.execute(
                "SELECT character_id, use_count "
                "FROM player_character_encounter_abilities "
                "WHERE encounter_key = '/logs/run.txt|100|2500|21:14:00' "
                "  AND ability_name = 'Tracer Missile' "
                "ORDER BY character_id"
            ).fetchall()

        # Karzag (character_id=1, 18 uses) and Vossan (character_id=2, 22 uses)
        self.assertEqual(rows, [(1, 18), (2, 22)])

    def test_same_player_same_ability_same_fight_still_collides(self):
        """The PK still enforces uniqueness within a (fight, player, ability) triple."""
        with sqlite3.connect(str(self.db_path)) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                # Karzag using Tracer Missile in his fight already exists from
                # the fixture data. Trying to insert a duplicate must fail.
                conn.execute(
                    "INSERT INTO player_character_encounter_abilities "
                    "(encounter_key, character_id, ability_name, ability_id, use_count) "
                    "VALUES ('/logs/run.txt|100|2500|21:14:00', 1, "
                    "        'Tracer Missile', '810194895470592', 99)"
                )


class MigrationIdempotencyTests(unittest.TestCase):
    """Running migrations twice must not corrupt or duplicate data."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "test.sqlite3"
        _build_old_db(self.db_path)

    def tearDown(self) -> None:
        import gc
        gc.collect()
        # Windows holds SQLite file handles open longer than Linux. Swallow
        # any cleanup error — the temp file will be removed by the OS later.
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def test_second_run_is_a_noop(self):
        first = db_migrations._migration_v2_per_player_per_fight(self.db_path)
        second = db_migrations._migration_v2_per_player_per_fight(self.db_path)
        self.assertIsNotNone(first)   # First run did something
        self.assertIsNone(second)     # Second run did nothing

    def test_data_survives_a_second_run(self):
        db_migrations._migration_v2_per_player_per_fight(self.db_path)
        with sqlite3.connect(str(self.db_path)) as conn:
            mid_count = conn.execute(
                "SELECT COUNT(*) FROM player_character_encounter_abilities"
            ).fetchone()[0]

        db_migrations._migration_v2_per_player_per_fight(self.db_path)

        with sqlite3.connect(str(self.db_path)) as conn:
            final_count = conn.execute(
                "SELECT COUNT(*) FROM player_character_encounter_abilities"
            ).fetchone()[0]
        self.assertEqual(mid_count, final_count)

    def test_run_pending_migrations_returns_messages_first_time(self):
        msgs = db_migrations.run_pending_migrations(self.db_path)
        # Three migrations now: v2 (per-player schema), v3 (ability count
        # columns), and v4 (per-fight class / discipline / evidence). All
        # three run on the freshly-migrated tables in sequence.
        self.assertEqual(len(msgs), 3)
        self.assertIn("v2", msgs[0])
        self.assertIn("v3", msgs[1])
        self.assertIn("v4", msgs[2])

    def test_run_pending_migrations_silent_on_second_call(self):
        db_migrations.run_pending_migrations(self.db_path)
        msgs = db_migrations.run_pending_migrations(self.db_path)
        self.assertEqual(msgs, [])


class MigrationBackupTests(unittest.TestCase):
    """The migration must take a backup before doing destructive work."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "test.sqlite3"
        _build_old_db(self.db_path)

    def tearDown(self) -> None:
        import gc
        gc.collect()
        # Windows holds SQLite file handles open longer than Linux. Swallow
        # any cleanup error — the temp file will be removed by the OS later.
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def test_backup_file_is_created(self):
        db_migrations._migration_v2_per_player_per_fight(self.db_path)
        backups = list(self.db_path.parent.glob("test.before_v2_*.sqlite3"))
        self.assertEqual(len(backups), 1)

    def test_backup_contains_old_schema(self):
        db_migrations._migration_v2_per_player_per_fight(self.db_path)
        backup = next(self.db_path.parent.glob("test.before_v2_*.sqlite3"))
        with sqlite3.connect(str(backup)) as conn:
            cols = db_migrations._primary_key_columns(conn, "player_character_encounters")
        # The backup should still have the OLD schema — that's its purpose.
        self.assertEqual(cols, ["encounter_key"])

    def test_no_backup_on_fresh_database(self):
        """A nonexistent DB path triggers no backup (nothing to migrate)."""
        fresh_path = Path(self._tmp.name) / "doesnt_exist.sqlite3"
        msgs = db_migrations.run_pending_migrations(fresh_path)
        self.assertEqual(msgs, [])
        # No backup file should have appeared.
        backups = list(self._tmp.name and Path(self._tmp.name).glob("doesnt_exist*"))
        self.assertEqual(backups, [])


if __name__ == "__main__":
    unittest.main()
