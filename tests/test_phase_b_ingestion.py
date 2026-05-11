"""
Tests for Phase B — multi-player ingestion.

Strategy: build a synthetic Fight object with several player participants
(plus a companion and an NPC for negative cases), run upsert_fight against
a temp DB, and assert that every player lands in the per-fight tables with
correct totals and ability counts.

We exercise the real upsert_fight path, not just the helpers, because that's
what catches integration bugs between the helpers and the surrounding SQL.
"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import storage.encounter_db as encounter_db
from engine.aggregator import (
    AbilityStats,
    EntityKind,
    EntityStats,
    Fight,
)
from engine.parser import Entity, LogEvent, NamedThing


# ─── Helpers for building synthetic events ────────────────────────────────────


def _player(name: str) -> Entity:
    """An Entity that represents a player named `name`."""
    return Entity(player=name)


def _companion(owner: str, comp_name: str) -> Entity:
    """An Entity that represents a player's companion."""
    return Entity(player=owner, companion=comp_name)


def _npc(name: str) -> Entity:
    """An Entity that represents an NPC."""
    return Entity(npc=name)


def _ability_activate(
    player_name: str,
    ability_name: str,
    ability_id: str,
    ts: time,
) -> LogEvent:
    """Build a minimal LogEvent representing a player pressing an ability."""
    return LogEvent(
        timestamp=ts,
        source=_player(player_name),
        target=_npc("Training Dummy"),
        ability=NamedThing(name=ability_name, id=ability_id),
        effect_type="Event",
        effect_name="AbilityActivate",
        effect_id="987000",
        effect_detail=NamedThing(name="Activate", id="987001"),
    )


def _companion_ability(
    owner: str,
    comp_name: str,
    ability_name: str,
    ability_id: str,
    ts: time,
) -> LogEvent:
    """Companion casting an ability — should NOT be counted as the owner's."""
    return LogEvent(
        timestamp=ts,
        source=_companion(owner, comp_name),
        target=_npc("Training Dummy"),
        ability=NamedThing(name=ability_name, id=ability_id),
        effect_type="Event",
        effect_name="AbilityActivate",
        effect_id="987000",
        effect_detail=NamedThing(name="Activate", id="987001"),
    )


# ─── Building a fully-formed Fight for the test ──────────────────────────────


def _build_two_player_fight(log_path: Path) -> Fight:
    """
    Build a Fight with:
      - Recorder Karzag (PLAYER) — 900k damage
      - Group member Vossan (GROUP_MEMBER) — 1.2M damage
      - A companion that should NOT be counted as a player
      - An NPC target (should be excluded from player ingestion)

    Both players use Tracer Missile, but with different counts. This is the
    primary regression scenario — the OLD schema couldn't store this.

    log_path is the on-disk file the fight references. summarize_fight
    reads from it; we don't need real combat-log lines, just an existing
    file (an empty one is fine — no biggest hit, no deaths, that's OK).
    """
    fight = Fight(
        index=1,
        start_time=time(21, 14, 0),
        end_time=time(21, 18, 0),
        player_name="Karzag",
        boss_name="Apex Vanguard",
        custom_name=None,
        _log_path=str(log_path),
        _line_start=0,
        _line_end=0,
        _loaded=True,
    )

    # Populate entity_stats directly — what aggregate_fight would have
    # produced. Doing it this way makes the test fast and deterministic.
    karzag = EntityStats(name="Karzag", kind=EntityKind.PLAYER, damage_dealt=900_000)
    karzag.abilities_damage["Tracer Missile"] = AbilityStats(
        name="Tracer Missile", hits=18, total_amount=900_000
    )
    fight.entity_stats["Karzag"] = karzag

    vossan = EntityStats(name="Vossan", kind=EntityKind.GROUP_MEMBER, damage_dealt=1_200_000)
    vossan.abilities_damage["Tracer Missile"] = AbilityStats(
        name="Tracer Missile", hits=22, total_amount=1_200_000
    )
    fight.entity_stats["Vossan"] = vossan

    # A companion belonging to Karzag — has damage but should NOT become its
    # own player_characters row, and its ability uses should NOT count toward
    # Karzag's ability totals.
    treek = EntityStats(name="Karzag/Treek", kind=EntityKind.COMPANION, damage_dealt=80_000)
    fight.entity_stats["Karzag/Treek"] = treek

    # The NPC. Damage taken by it doesn't matter for player ingestion.
    boss = EntityStats(name="Apex Vanguard", kind=EntityKind.NPC, damage_taken=2_180_000)
    fight.entity_stats["Apex Vanguard"] = boss

    # Now build event timeline. Both players activate Tracer Missile at
    # different counts. Karzag's companion casts Companion Heal — this must
    # not be attributed to Karzag.
    base_ts_seconds = 0
    events: list[LogEvent] = []

    # Karzag: 18 Tracer Missiles + 6 Heatseeker Missiles
    for i in range(18):
        events.append(_ability_activate("Karzag", "Tracer Missile", "810194895470592",
                                         time(21, 14, i % 60)))
    for i in range(6):
        events.append(_ability_activate("Karzag", "Heatseeker Missiles", "811078658654208",
                                         time(21, 15, i * 5 % 60)))

    # Vossan: 22 Tracer Missiles + 9 Heatseeker Missiles
    for i in range(22):
        events.append(_ability_activate("Vossan", "Tracer Missile", "810194895470592",
                                         time(21, 14, i % 60)))
    for i in range(9):
        events.append(_ability_activate("Vossan", "Heatseeker Missiles", "811078658654208",
                                         time(21, 15, (i * 4) % 60)))

    # Companion casts — these should be ignored.
    for i in range(5):
        events.append(_companion_ability("Karzag", "Treek", "Companion Heal", "999000001",
                                          time(21, 16, i)))

    fight.events = events
    return fight


def _build_zero_damage_player_fight(log_path: Path) -> Fight:
    """
    A fight where one "player" entity exists in entity_stats but did literally
    nothing measurable. They should be filtered out of ingestion — empty
    player_characters rows pollute downstream cohort queries.
    """
    fight = Fight(
        index=1,
        start_time=time(20, 0, 0),
        end_time=time(20, 5, 0),
        player_name="Karzag",
        boss_name="Some Boss",
        _log_path=str(log_path),
        _line_start=0,
        _line_end=0,
        _loaded=True,
    )
    fight.entity_stats["Karzag"] = EntityStats(
        name="Karzag", kind=EntityKind.PLAYER, damage_dealt=500_000
    )
    fight.entity_stats["Ghost"] = EntityStats(
        name="Ghost", kind=EntityKind.GROUP_MEMBER  # all zeros
    )
    fight.events = [
        _ability_activate("Karzag", "Tracer Missile", "810194895470592", time(20, 1, 0)),
    ]
    return fight


# ─── Schema setup mirroring the real init_db (post-migration shape) ──────────


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
        PRIMARY KEY (encounter_key, character_id),
        FOREIGN KEY(character_id) REFERENCES player_characters(character_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE player_character_abilities (
        character_ability_id INTEGER PRIMARY KEY AUTOINCREMENT,
        character_id INTEGER NOT NULL,
        ability_name TEXT NOT NULL,
        ability_id TEXT NOT NULL DEFAULT '',
        total_uses INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        UNIQUE(character_id, ability_id, ability_name),
        FOREIGN KEY(character_id) REFERENCES player_characters(character_id) ON DELETE CASCADE
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
        PRIMARY KEY(encounter_key, character_id, ability_id, ability_name),
        FOREIGN KEY(character_id) REFERENCES player_characters(character_id) ON DELETE CASCADE
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


# ─── Tests ────────────────────────────────────────────────────────────────────


class MultiPlayerIngestionTests(unittest.TestCase):
    """The core Phase B promise: every player in a fight lands in the DB."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "test.sqlite3"
        _build_db(self.db_path)

        # summarize_fight() reads the on-disk log file. We don't need real
        # combat lines — an empty file produces an empty summary, which is
        # fine for ingestion testing.
        self.log_path = Path(self._tmp.name) / "synthetic.log"
        self.log_path.write_text("", encoding="utf-8")

        self._patch = mock.patch.object(encounter_db, "DB_PATH", self.db_path)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def test_both_players_land_in_player_characters(self):
        encounter_db.upsert_fight(_build_two_player_fight(self.log_path))

        with sqlite3.connect(str(self.db_path)) as conn:
            names = sorted(
                row[0] for row in conn.execute("SELECT character_name FROM player_characters")
            )
        # Karzag and Vossan should both be there. The companion (Karzag/Treek)
        # and the NPC (Apex Vanguard) should NOT.
        self.assertEqual(names, ["Karzag", "Vossan"])

    def test_both_players_have_per_fight_rows(self):
        fight = _build_two_player_fight(self.log_path)
        encounter_db.upsert_fight(fight)

        with sqlite3.connect(str(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT pc.character_name, pce.damage_done "
                "FROM player_character_encounters pce "
                "JOIN player_characters pc ON pc.character_id = pce.character_id "
                "ORDER BY pc.character_name"
            ).fetchall()
        # Two rows: Karzag at 900k, Vossan at 1.2M.
        self.assertEqual(rows, [("Karzag", 900_000), ("Vossan", 1_200_000)])

    def test_companion_is_not_persisted_as_a_player(self):
        encounter_db.upsert_fight(_build_two_player_fight(self.log_path))

        with sqlite3.connect(str(self.db_path)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM player_characters WHERE character_name LIKE '%Treek%'"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_npc_is_not_persisted_as_a_player(self):
        encounter_db.upsert_fight(_build_two_player_fight(self.log_path))

        with sqlite3.connect(str(self.db_path)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM player_characters WHERE character_name = 'Apex Vanguard'"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_per_player_ability_counts_are_correct(self):
        encounter_db.upsert_fight(_build_two_player_fight(self.log_path))

        with sqlite3.connect(str(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT pc.character_name, pcea.ability_name, pcea.use_count "
                "FROM player_character_encounter_abilities pcea "
                "JOIN player_characters pc ON pc.character_id = pcea.character_id "
                "ORDER BY pc.character_name, pcea.ability_name"
            ).fetchall()

        # Expected: 4 rows total (2 players × 2 abilities each).
        # Karzag: 6 Heatseeker, 18 Tracer
        # Vossan: 9 Heatseeker, 22 Tracer
        self.assertEqual(rows, [
            ("Karzag", "Heatseeker Missiles", 6),
            ("Karzag", "Tracer Missile", 18),
            ("Vossan", "Heatseeker Missiles", 9),
            ("Vossan", "Tracer Missile", 22),
        ])

    def test_companion_casts_do_not_count_for_owner(self):
        """Karzag's companion casts Companion Heal — must not appear under Karzag."""
        encounter_db.upsert_fight(_build_two_player_fight(self.log_path))

        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM player_character_encounter_abilities "
                "WHERE ability_name = 'Companion Heal'"
            ).fetchone()
        self.assertEqual(row[0], 0)

    def test_lifetime_totals_are_set_per_player(self):
        encounter_db.upsert_fight(_build_two_player_fight(self.log_path))

        with sqlite3.connect(str(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT character_name, total_damage_done "
                "FROM player_characters ORDER BY character_name"
            ).fetchall()
        # The lifetime totals are recomputed from per-encounter rows.
        # With one fight, lifetime == that fight's contribution.
        self.assertEqual(rows, [("Karzag", 900_000), ("Vossan", 1_200_000)])

    def test_lifetime_ability_rollup_per_player(self):
        encounter_db.upsert_fight(_build_two_player_fight(self.log_path))

        with sqlite3.connect(str(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT pc.character_name, pca.ability_name, pca.total_uses "
                "FROM player_character_abilities pca "
                "JOIN player_characters pc ON pc.character_id = pca.character_id "
                "ORDER BY pc.character_name, pca.ability_name"
            ).fetchall()
        self.assertEqual(rows, [
            ("Karzag", "Heatseeker Missiles", 6),
            ("Karzag", "Tracer Missile", 18),
            ("Vossan", "Heatseeker Missiles", 9),
            ("Vossan", "Tracer Missile", 22),
        ])


class ParticipationFilterTests(unittest.TestCase):
    """Empty-totals players shouldn't pollute the DB."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "test.sqlite3"
        _build_db(self.db_path)
        self.log_path = Path(self._tmp.name) / "synthetic.log"
        self.log_path.write_text("", encoding="utf-8")
        self._patch = mock.patch.object(encounter_db, "DB_PATH", self.db_path)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def test_zero_stats_player_is_skipped(self):
        encounter_db.upsert_fight(_build_zero_damage_player_fight(self.log_path))

        with sqlite3.connect(str(self.db_path)) as conn:
            names = [
                row[0] for row in conn.execute(
                    "SELECT character_name FROM player_characters ORDER BY character_name"
                )
            ]
        # Karzag did damage; Ghost did nothing. Ghost must be filtered out.
        self.assertEqual(names, ["Karzag"])


class IdempotencyTests(unittest.TestCase):
    """Re-ingesting the same fight must not duplicate or corrupt rows."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "test.sqlite3"
        _build_db(self.db_path)
        self.log_path = Path(self._tmp.name) / "synthetic.log"
        self.log_path.write_text("", encoding="utf-8")
        self._patch = mock.patch.object(encounter_db, "DB_PATH", self.db_path)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def test_double_upsert_does_not_duplicate_rows(self):
        fight = _build_two_player_fight(self.log_path)
        encounter_db.upsert_fight(fight)
        encounter_db.upsert_fight(fight)

        with sqlite3.connect(str(self.db_path)) as conn:
            pce_count = conn.execute(
                "SELECT COUNT(*) FROM player_character_encounters"
            ).fetchone()[0]
            pcea_count = conn.execute(
                "SELECT COUNT(*) FROM player_character_encounter_abilities"
            ).fetchone()[0]
            pc_count = conn.execute(
                "SELECT COUNT(*) FROM player_characters"
            ).fetchone()[0]

        # 2 players × 1 fight = 2 player_character_encounters rows.
        self.assertEqual(pce_count, 2)
        # 2 players × 2 abilities = 4 ability rows.
        self.assertEqual(pcea_count, 4)
        # 2 distinct players.
        self.assertEqual(pc_count, 2)

    def test_double_upsert_preserves_correct_totals(self):
        fight = _build_two_player_fight(self.log_path)
        encounter_db.upsert_fight(fight)
        encounter_db.upsert_fight(fight)

        with sqlite3.connect(str(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT character_name, total_damage_done "
                "FROM player_characters ORDER BY character_name"
            ).fetchall()
        # Lifetime totals shouldn't have been doubled — the upsert replaces,
        # not adds.
        self.assertEqual(rows, [("Karzag", 900_000), ("Vossan", 1_200_000)])


if __name__ == "__main__":
    unittest.main()
