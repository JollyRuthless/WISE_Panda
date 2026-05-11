"""
Tests for Phase C — class & discipline detection.

Strategy:
  1. Pure-function tests on class_detection.detect_class with synthetic
     events. Cover both the DisciplineChanged path and the fingerprint
     fallback. Edge cases: empty events, mirrored class abilities,
     multi-discipline ambiguity, low-signal data.
  2. End-to-end test against a real combat log shipped with the project.
     Validates that the parser's effect_detail_raw plumbing actually
     surfaces the discipline string from real data.
  3. Ingestion test: build a synthetic Fight, run upsert_fight, verify
     class_name / discipline_name / class_evidence land in
     player_character_encounters.

Synthetic events use the same _player / _ability_activate helpers as the
Phase B test, kept inline here to avoid a cross-test import.
"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import engine.class_detection as class_detection
import storage.encounter_db as encounter_db
from engine.aggregator import EntityKind, EntityStats, Fight
from engine.class_detection import ClassDetection, detect_class
from engine.parser import Entity, LogEvent, NamedThing


# ─── Synthetic event helpers ─────────────────────────────────────────────────


def _player(name: str) -> Entity:
    return Entity(player=name)


def _companion(owner: str, comp_name: str) -> Entity:
    return Entity(player=owner, companion=comp_name)


def _npc(name: str) -> Entity:
    return Entity(npc=name)


def _ability_activate(player_name: str, ability_name: str, ability_id: str = "1",
                      ts: time = time(20, 0, 0)) -> LogEvent:
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


def _discipline_changed(player_name: str, class_name: str, discipline_name: str,
                        ts: time = time(20, 0, 0)) -> LogEvent:
    """
    A synthetic DisciplineChanged event. The detector reads
    effect_detail_raw, so we set it to the same shape parse_line would
    produce from a real log:
        "<Class> {id}/<Discipline> {id}"
    """
    raw = f"{class_name} {{1234}}/{discipline_name} {{5678}}"
    return LogEvent(
        timestamp=ts,
        source=_player(player_name),
        target=_npc(""),
        ability=None,
        effect_type="DisciplineChanged",
        effect_name=class_name,
        effect_id="836045448953665",
        effect_detail=NamedThing(name=class_name, id="1234"),
        effect_detail_raw=raw,
    )


# ─── Path 1: DisciplineChanged-driven detection ──────────────────────────────


class DisciplineChangedDetectionTests(unittest.TestCase):
    """When the game declares the spec, we use it. Authoritative path."""

    def test_detects_operative_lethality(self):
        events = [_discipline_changed("Holt Hexxen", "Operative", "Lethality")]
        result = detect_class(events, "Holt Hexxen")
        self.assertEqual(result.class_name, "Operative")
        self.assertEqual(result.discipline_name, "Lethality")
        self.assertEqual(result.confidence, 1.0)
        self.assertEqual(result.evidence, "declared:DisciplineChanged")

    def test_detects_multiword_discipline(self):
        # "Combat Medic", "Innovative Ordnance", "Shield Specialist" all
        # have spaces. The regex must not stop at the first whitespace.
        events = [_discipline_changed("Mante", "Commando", "Combat Medic")]
        result = detect_class(events, "Mante")
        self.assertEqual(result.class_name, "Commando")
        self.assertEqual(result.discipline_name, "Combat Medic")

    def test_detects_innovative_ordnance(self):
        events = [_discipline_changed("X", "Mercenary", "Innovative Ordnance")]
        result = detect_class(events, "X")
        self.assertEqual(result.class_name, "Mercenary")
        self.assertEqual(result.discipline_name, "Innovative Ordnance")

    def test_only_targets_named_player(self):
        # Two players in the same fight, each with their own
        # DisciplineChanged. Detection must isolate by name.
        events = [
            _discipline_changed("Holt", "Operative", "Lethality"),
            _discipline_changed("Doomside", "Juggernaut", "Vengeance"),
        ]
        self.assertEqual(detect_class(events, "Holt").discipline_name, "Lethality")
        self.assertEqual(detect_class(events, "Doomside").discipline_name, "Vengeance")

    def test_empty_events_returns_empty_detection(self):
        result = detect_class([], "Anyone")
        self.assertFalse(result.is_known)
        self.assertEqual(result.class_name, "")
        self.assertEqual(result.confidence, 0.0)

    def test_blank_character_name_returns_empty(self):
        events = [_discipline_changed("Holt", "Operative", "Lethality")]
        result = detect_class(events, "")
        self.assertFalse(result.is_known)

    def test_player_not_in_events_returns_empty(self):
        events = [_discipline_changed("Holt", "Operative", "Lethality")]
        result = detect_class(events, "Stranger")
        self.assertFalse(result.is_known)

    def test_last_discipline_change_wins(self):
        # If a player respec'd between fights but logging captured both
        # events, we want the most recent one. Tests defensive rather
        # than expected behaviour — the game won't let this happen mid-
        # combat, but the parser sees combat-log lines linearly.
        events = [
            _discipline_changed("Holt", "Operative", "Concealment", time(20, 0, 0)),
            _discipline_changed("Holt", "Operative", "Lethality",   time(20, 5, 0)),
        ]
        result = detect_class(events, "Holt")
        self.assertEqual(result.discipline_name, "Lethality")


# ─── Path 2: Ability fingerprinting fallback ────────────────────────────────


class FingerprintFallbackTests(unittest.TestCase):
    """When no DisciplineChanged was captured, vote on signature abilities."""

    def test_arsenal_mercenary_via_fingerprint(self):
        # Tracer Missile is the iconic Arsenal cast (weight 10). Pressing
        # it 18 times is overwhelming evidence.
        events = [
            _ability_activate("Vossan", "Tracer Missile") for _ in range(18)
        ]
        events += [
            _ability_activate("Vossan", "Heatseeker Missiles") for _ in range(6)
        ]
        result = detect_class(events, "Vossan")
        self.assertEqual(result.class_name, "Mercenary")
        self.assertEqual(result.discipline_name, "Arsenal")
        self.assertGreater(result.confidence, 0.5)
        self.assertLessEqual(result.confidence, 0.9)
        self.assertTrue(result.evidence.startswith("voted:"))
        self.assertIn("Tracer Missile", result.evidence)

    def test_lethality_operative_via_fingerprint(self):
        events = [
            _ability_activate("X", "Toxic Blast") for _ in range(10)
        ]
        events += [_ability_activate("X", "Corrosive Dart") for _ in range(15)]
        events += [_ability_activate("X", "Corrosive Assault") for _ in range(8)]
        result = detect_class(events, "X")
        self.assertEqual(result.class_name, "Operative")
        self.assertEqual(result.discipline_name, "Lethality")

    def test_class_only_with_low_discipline_signal(self):
        # Press only class-confirming abilities (weight 2-3, no discipline
        # hint). Should give us Mercenary with empty discipline.
        events = [_ability_activate("Y", "Power Shot") for _ in range(5)]
        events += [_ability_activate("Y", "Rapid Shots") for _ in range(20)]
        result = detect_class(events, "Y")
        self.assertEqual(result.class_name, "Mercenary")
        # Power Shot gives weight 3 only to class — discipline_score never
        # rises above the threshold.
        self.assertEqual(result.discipline_name, "")

    def test_ignores_companion_casts(self):
        # Companions are not the player. Their casts must not contribute
        # to fingerprinting.
        events = [
            LogEvent(
                timestamp=time(20, 0, i),
                source=_companion("Holt", "Mako"),
                target=_npc("Dummy"),
                ability=NamedThing(name="Tracer Missile", id="1"),
                effect_type="Event",
                effect_name="AbilityActivate",
                effect_id="987",
                effect_detail=NamedThing(name="Activate", id="988"),
            )
            for i in range(20)
        ]
        result = detect_class(events, "Holt")
        # No player presses → no fingerprint → empty detection.
        self.assertFalse(result.is_known)

    def test_unknown_abilities_dont_crash(self):
        # An ability that isn't in the fingerprint table contributes
        # nothing. Should silently not vote, not raise.
        events = [_ability_activate("X", "Some Random Ability") for _ in range(50)]
        result = detect_class(events, "X")
        self.assertFalse(result.is_known)

    def test_fingerprint_confidence_below_one(self):
        # A fingerprint result, no matter how strong, never reaches 1.0 —
        # that's reserved for DisciplineChanged-declared. This guarantees
        # that if both signals are available, declared wins.
        events = [_ability_activate("X", "Tracer Missile") for _ in range(100)]
        result = detect_class(events, "X")
        self.assertLess(result.confidence, 1.0)


# ─── Priority: declared beats fingerprinted ──────────────────────────────────


class DeclaredBeatsFingerprintTests(unittest.TestCase):
    """When both signals exist, DisciplineChanged wins."""

    def test_declared_overrides_contradictory_fingerprint(self):
        # Player pressed Tracer Missile (Arsenal Mercenary fingerprint)
        # but also has a DisciplineChanged saying Operative/Lethality —
        # which would never happen in a real fight, but tests the rule.
        events = [_discipline_changed("X", "Operative", "Lethality")]
        events += [_ability_activate("X", "Tracer Missile") for _ in range(20)]
        result = detect_class(events, "X")
        self.assertEqual(result.class_name, "Operative")
        self.assertEqual(result.discipline_name, "Lethality")
        self.assertEqual(result.confidence, 1.0)
        self.assertEqual(result.evidence, "declared:DisciplineChanged")


# ─── End-to-end: real combat log ─────────────────────────────────────────────


class RealCombatLogTests(unittest.TestCase):
    """
    Drive the detector against a real combat log shipped with the project.
    This catches "synthetic-data passed, real-data failed" issues — the
    same trap the A10 plan called out.
    """

    @classmethod
    def setUpClass(cls):
        cls.log_path = Path(__file__).parent / "TEST_Log" / \
            "combat_2026-04-23_18_11_59_192327.txt"
        if not cls.log_path.exists():
            raise unittest.SkipTest(f"Real test log not present: {cls.log_path}")
        from engine.parser import parse_file
        cls.events, _errors = parse_file(str(cls.log_path))

    def test_recording_player_resolves_via_disciplinechanged(self):
        # Holt Hexxen is the recorder in this log file. The first
        # DisciplineChanged in the file declares Operative/Lethality.
        result = detect_class(self.events, "Holt Hexxen")
        self.assertEqual(result.class_name, "Operative")
        self.assertEqual(result.discipline_name, "Lethality")
        self.assertEqual(result.confidence, 1.0)

    def test_other_group_member_also_resolves(self):
        # Hellsa Hexxen — a group member, not the recorder. The fact
        # that DisciplineChanged also fires for group members is the
        # whole reason this approach works for cohort detection.
        result = detect_class(self.events, "Hellsa Hexxen")
        self.assertEqual(result.class_name, "Operative")
        self.assertEqual(result.discipline_name, "Lethality")


# ─── Ingestion: upsert_fight writes the new columns ─────────────────────────


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


def _build_fight_with_disciplinechanged(log_path: Path) -> Fight:
    """One fight, two players, both with DisciplineChanged events."""
    fight = Fight(
        index=1,
        start_time=time(20, 0, 0),
        end_time=time(20, 5, 0),
        player_name="Holt",
        boss_name="Apex Vanguard",
        _log_path=str(log_path),
        _line_start=0,
        _line_end=0,
        _loaded=True,
    )
    fight.entity_stats["Holt"] = EntityStats(
        name="Holt", kind=EntityKind.PLAYER, damage_dealt=900_000
    )
    fight.entity_stats["Vossan"] = EntityStats(
        name="Vossan", kind=EntityKind.GROUP_MEMBER, damage_dealt=1_200_000
    )

    events = [
        _discipline_changed("Holt", "Operative", "Lethality"),
        _discipline_changed("Vossan", "Mercenary", "Arsenal"),
    ]
    # Add a few ability uses to make the participation gate happy. The
    # damage totals are already enough — this is just for realism.
    events += [_ability_activate("Holt", "Toxic Blast", "1", time(20, 1, i)) for i in range(3)]
    events += [_ability_activate("Vossan", "Tracer Missile", "2", time(20, 1, i)) for i in range(5)]
    fight.events = events
    return fight


class UpsertWritesClassDataTests(unittest.TestCase):
    """upsert_fight() must persist class_name, discipline_name, evidence."""

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

    def test_class_data_lands_in_per_fight_row(self):
        encounter_db.upsert_fight(_build_fight_with_disciplinechanged(self.log_path))

        with sqlite3.connect(str(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT pc.character_name, pce.class_name, pce.discipline_name, "
                "pce.class_evidence "
                "FROM player_character_encounters pce "
                "JOIN player_characters pc ON pc.character_id = pce.character_id "
                "ORDER BY pc.character_name"
            ).fetchall()

        self.assertEqual(rows, [
            ("Holt",   "Operative",  "Lethality", "declared:DisciplineChanged"),
            ("Vossan", "Mercenary",  "Arsenal",   "declared:DisciplineChanged"),
        ])

    def test_class_data_also_lands_in_player_characters(self):
        # The existing player_characters.class_name column should still be
        # populated with the detected class — that's the legacy "most
        # recent" view used by the Inspector character list. Discipline
        # is per-fight only.
        encounter_db.upsert_fight(_build_fight_with_disciplinechanged(self.log_path))

        with sqlite3.connect(str(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT character_name, class_name "
                "FROM player_characters ORDER BY character_name"
            ).fetchall()
        self.assertEqual(rows, [("Holt", "Operative"), ("Vossan", "Mercenary")])

    def test_unknown_player_gets_empty_class_data(self):
        # A player with no DisciplineChanged event AND no ability presses
        # at all (just damage stats) gets empty class fields. The "don't
        # overwrite known with unknown" merge policy means any later
        # upsert can fill these in without losing data.
        fight = Fight(
            index=1,
            start_time=time(20, 0, 0),
            end_time=time(20, 5, 0),
            player_name="Mystery",
            _log_path=str(self.log_path),
            _line_start=0,
            _line_end=0,
            _loaded=True,
        )
        fight.entity_stats["Mystery"] = EntityStats(
            name="Mystery", kind=EntityKind.PLAYER, damage_dealt=500_000
        )
        encounter_db.upsert_fight(fight)

        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT pce.class_name, pce.discipline_name, pce.class_evidence "
                "FROM player_character_encounters pce "
                "JOIN player_characters pc ON pc.character_id = pce.character_id "
                "WHERE pc.character_name = 'Mystery'"
            ).fetchone()
        self.assertEqual(row, ("", "", ""))


# ─── Cross-fight discipline inference ───────────────────────────────────────


def _build_fight_with_only_class_no_discipline(
    log_path: Path,
    fight_index: int,
    fight_start: time,
    encounter_key_marker: str,
    character_name: str,
) -> Fight:
    """
    A fight where the named player has class detected (via fingerprint or
    via the legacy player_characters fallback) but no per-fight discipline.
    Used to set up the inference test scenario.
    """
    fight = Fight(
        index=fight_index,
        start_time=fight_start,
        end_time=time(fight_start.hour, fight_start.minute + 5, 0),
        player_name=character_name,
        boss_name=encounter_key_marker,
        _log_path=str(log_path),
        _line_start=0,
        _line_end=0,
        _loaded=True,
    )
    fight.entity_stats[character_name] = EntityStats(
        name=character_name, kind=EntityKind.PLAYER, damage_dealt=300_000
    )
    # No DisciplineChanged, no signature presses — pure-blank scenario.
    fight.events = []
    return fight


class CrossFightInferenceTests(unittest.TestCase):
    """
    Inference rules:
      1. Same character, declared elsewhere -> infer.
      2. Multiple disciplines declared for this character -> abstain.
      3. Voted-evidence other fights -> not eligible (declared only).
      4. Same character_id only.
    """

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "test.sqlite3"
        _build_db(self.db_path)
        self.log_path = Path(self._tmp.name) / "synthetic.log"
        self.log_path.write_text("", encoding="utf-8")
        self._patch = mock.patch.object(encounter_db, "DB_PATH", self.db_path)
        self._patch.start()
        # cohort.py uses encounter_db._connect_db underneath. The patch
        # above should cover it because cohort imports the DB_PATH from
        # encounter_db at call time, but be paranoid and verify.

    def tearDown(self) -> None:
        self._patch.stop()
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def _upsert_declared_fight_for_kiave(self, discipline: str = "Hatred",
                                          fight_idx: int = 1) -> str:
        """
        Helper: create a fight where Kìave is declared via DisciplineChanged.
        Returns the encounter_key.
        """
        fight = Fight(
            index=fight_idx,
            start_time=time(20, fight_idx * 5, 0),
            end_time=time(20, fight_idx * 5 + 4, 0),
            player_name="Kìave",
            boss_name=f"Boss{fight_idx}",
            _log_path=str(self.log_path),
            _line_start=0,
            _line_end=0,
            _loaded=True,
        )
        fight.entity_stats["Kìave"] = EntityStats(
            name="Kìave", kind=EntityKind.PLAYER, damage_dealt=500_000
        )
        fight.events = [_discipline_changed("Kìave", "Assassin", discipline)]
        encounter_db.upsert_fight(fight)
        # The encounter_key generation is deterministic from the fight's
        # log_path + line range + start time. We'll grab it from the DB.
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT pce.encounter_key FROM player_character_encounters pce "
                "JOIN player_characters pc ON pc.character_id = pce.character_id "
                "WHERE pc.character_name = 'Kìave' AND pce.discipline_name = ? "
                "ORDER BY pce.encounter_date DESC LIMIT 1",
                (discipline,),
            ).fetchone()
            return row[0] if row else ""

    def _upsert_blank_fight_for_kiave(self, fight_idx: int = 99) -> str:
        """
        Create a fight where Kìave has class data resolvable but discipline
        blank. We do this by first running the declared fight (so the
        per-character class_name is set), then a fight with no events.
        """
        fight = _build_fight_with_only_class_no_discipline(
            self.log_path,
            fight_index=fight_idx,
            fight_start=time(21, 0, 0),
            encounter_key_marker=f"BlankFight{fight_idx}",
            character_name="Kìave",
        )
        encounter_db.upsert_fight(fight)
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT pce.encounter_key FROM player_character_encounters pce "
                "JOIN player_characters pc ON pc.character_id = pce.character_id "
                "WHERE pc.character_name = 'Kìave' AND pce.discipline_name = '' "
                "ORDER BY pce.encounter_date DESC LIMIT 1",
            ).fetchone()
            return row[0] if row else ""

    def test_inference_fills_in_blank_when_declared_elsewhere(self):
        # 1: declare Kìave as Assassin/Hatred in fight A
        self._upsert_declared_fight_for_kiave(discipline="Hatred", fight_idx=1)
        # 2: create fight B where Kìave has no discipline detected
        blank_key = self._upsert_blank_fight_for_kiave(fight_idx=99)
        self.assertTrue(blank_key, "Setup failed: blank fight didn't get a key")

        # When we list participants of the blank fight, Kìave should now
        # come back with discipline filled in by inference.
        import storage.cohort as cohort
        participants = cohort.list_participants_in_fight(blank_key)
        kiave = next((p for p in participants if p.character_name == "Kìave"), None)
        self.assertIsNotNone(kiave)
        self.assertEqual(kiave.discipline_name, "Hatred")
        self.assertTrue(kiave.class_evidence.startswith("inferred:"))
        self.assertIn("Hatred", kiave.class_evidence)

    def test_inference_abstains_when_character_swapped_disciplines(self):
        # Kìave declared as Hatred in fight A AND as Deception in fight B.
        # In a third blank fight, we should NOT infer either — they swap.
        self._upsert_declared_fight_for_kiave(discipline="Hatred", fight_idx=1)
        self._upsert_declared_fight_for_kiave(discipline="Deception", fight_idx=2)
        blank_key = self._upsert_blank_fight_for_kiave(fight_idx=99)

        import storage.cohort as cohort
        participants = cohort.list_participants_in_fight(blank_key)
        kiave = next((p for p in participants if p.character_name == "Kìave"), None)
        self.assertIsNotNone(kiave)
        # No inference — discipline stays blank rather than guessing.
        self.assertEqual(kiave.discipline_name, "")
        # Evidence stays empty too — we didn't lie about anything.
        self.assertEqual(kiave.class_evidence, "")

    def test_inference_ignores_voted_evidence_in_other_fights(self):
        # Declared fights are eligible inference sources. Voted ones are
        # not — they're already inferences themselves, and chaining
        # inference-on-inference produces cascading errors.
        # We simulate this by hand-inserting a "voted:" row.
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                """
                INSERT INTO player_characters
                  (character_name, class_name, first_seen_date, last_seen_date, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Voter", "Assassin", "2026-04-01", "2026-04-01", "2026-04-01T00:00:00"),
            )
            voter_id = conn.execute(
                "SELECT character_id FROM player_characters WHERE character_name = 'Voter'"
            ).fetchone()[0]
            # A voted-evidence row in some past fight.
            conn.execute(
                """
                INSERT INTO player_character_encounters
                  (encounter_key, character_id, encounter_date,
                   damage_done, healing_done, taunts, interrupts,
                   class_name, discipline_name, class_evidence)
                VALUES (?, ?, ?, 100, 0, 0, 0, ?, ?, ?)
                """,
                ("past_fight_xyz", voter_id, "2026-04-01",
                 "Assassin", "Hatred", "voted:Demolish=12,Death Field=4"),
            )
            conn.execute(
                """
                INSERT INTO player_character_encounters
                  (encounter_key, character_id, encounter_date,
                   damage_done, healing_done, taunts, interrupts,
                   class_name, discipline_name, class_evidence)
                VALUES (?, ?, ?, 100, 0, 0, 0, '', '', '')
                """,
                ("blank_fight_abc", voter_id, "2026-04-02"),
            )
            # encounters table needs a row for the blank fight to be
            # discoverable (the join is inner).
            conn.execute(
                """
                INSERT INTO encounters
                  (encounter_key, encounter_name, encounter_date, log_path,
                   recorded_by, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("blank_fight_abc", "Some Boss", "2026-04-02", "x", "x",
                 "2026-04-02T00:00:00"),
            )
            conn.commit()

        import storage.cohort as cohort
        participants = cohort.list_participants_in_fight("blank_fight_abc")
        voter = next((p for p in participants if p.character_name == "Voter"), None)
        self.assertIsNotNone(voter)
        # Voted source isn't eligible for inference — discipline stays blank.
        self.assertEqual(voter.discipline_name, "")


# ─── Stance detection ───────────────────────────────────────────────────────


def _stance_apply(player_name: str, stance_name: str,
                  ts: time = time(20, 0, 0)) -> LogEvent:
    """A synthetic ApplyEffect event for a stance buff."""
    return LogEvent(
        timestamp=ts,
        source=_player(player_name),
        target=_player(player_name),  # self-applied
        ability=NamedThing(name=stance_name, id="9999"),
        effect_type="ApplyEffect",
        effect_name="ApplyEffect",
        effect_id="836045448945477",
        effect_detail=NamedThing(name=stance_name, id="9999"),
    )


class StanceDetectionTests(unittest.TestCase):
    """Stance buffs are essentially self-declarations of discipline."""

    def test_dark_charge_means_assassin_darkness(self):
        events = [_stance_apply("Kashia", "Dark Charge")]
        result = detect_class(events, "Kashia")
        self.assertEqual(result.class_name, "Assassin")
        self.assertEqual(result.discipline_name, "Darkness")
        self.assertEqual(result.confidence, 1.0)
        self.assertEqual(result.evidence, "stance:Dark Charge")

    def test_acid_blade_means_operative_concealment(self):
        events = [_stance_apply("Stealthy", "Acid Blade")]
        result = detect_class(events, "Stealthy")
        self.assertEqual(result.class_name, "Operative")
        self.assertEqual(result.discipline_name, "Concealment")

    def test_disciplinechanged_takes_precedence_over_stance(self):
        # If both signals are present, declared wins.
        events = [
            _stance_apply("X", "Dark Charge"),
            _discipline_changed("X", "Sorcerer", "Lightning"),
        ]
        result = detect_class(events, "X")
        # Declared (Sorcerer/Lightning) overrides stance (Assassin/Darkness)
        self.assertEqual(result.class_name, "Sorcerer")
        self.assertEqual(result.discipline_name, "Lightning")
        self.assertEqual(result.evidence, "declared:DisciplineChanged")


# ─── Voting on aggregated ability counts ───────────────────────────────────


class AbilityCountsVotingTests(unittest.TestCase):
    """
    The fingerprint vote can take pre-aggregated counts (pressed, prebuff,
    damage_source) instead of just AbilityActivate events. This is critical
    for bystander players whose ability footprint is mostly DoT ticks.
    """

    def test_damage_source_only_can_resolve_discipline(self):
        # Mimics the real Kashia case: zero presses, but multiple
        # discipline-diagnostic damage_source counts.
        ability_counts = {
            ("1", "Wither"):            {"pressed": 0, "prebuff": 0, "damage_source": 3},
            ("2", "Depredating Volts"): {"pressed": 0, "prebuff": 0, "damage_source": 6},
            ("3", "Discharge"):         {"pressed": 0, "prebuff": 0, "damage_source": 4},
            ("4", "Maul"):              {"pressed": 0, "prebuff": 0, "damage_source": 4},
            ("5", "Thrash"):            {"pressed": 0, "prebuff": 0, "damage_source": 16},
        }
        result = detect_class([], "Kashia", ability_counts=ability_counts)
        self.assertEqual(result.class_name, "Assassin")
        self.assertEqual(result.discipline_name, "Darkness")

    def test_prebuff_counts_contribute(self):
        # A Sorcerer who pre-cast Affliction in the 15s before pull and
        # then walked off has 0 presses, 1 prebuff, 0 damage source. We
        # should still vote for them.
        ability_counts = {
            ("1", "Affliction"):    {"pressed": 0, "prebuff": 1, "damage_source": 0},
            ("2", "Force Leech"):   {"pressed": 0, "prebuff": 1, "damage_source": 0},
            ("3", "Force Lightning"): {"pressed": 0, "prebuff": 1, "damage_source": 0},
        }
        result = detect_class([], "Caster", ability_counts=ability_counts)
        self.assertEqual(result.class_name, "Sorcerer")
        self.assertEqual(result.discipline_name, "Madness")

    def test_pure_press_path_still_works_when_counts_none(self):
        # Backwards compatibility: callers passing None for ability_counts
        # fall back to walking events for AbilityActivate.
        events = [_ability_activate("X", "Tracer Missile") for _ in range(15)]
        result = detect_class(events, "X")  # no ability_counts
        self.assertEqual(result.class_name, "Mercenary")
        self.assertEqual(result.discipline_name, "Arsenal")

    def test_damage_source_is_attenuated_vs_presses(self):
        # Pressed weight 1.0 vs damage_source weight 0.3 — a single press
        # of one ability should outvote 2 damage_source ticks of another
        # if their weights are equal.
        ability_counts = {
            # 5 presses of Tracer Missile (Arsenal weight 10) = 50
            ("1", "Tracer Missile"): {"pressed": 5, "prebuff": 0, "damage_source": 0},
            # 5 damage source of Mag Shot (IO weight 10) = 5*10*0.3 = 15
            ("2", "Mag Shot"):       {"pressed": 0, "prebuff": 0, "damage_source": 5},
        }
        result = detect_class([], "X", ability_counts=ability_counts)
        # Arsenal must win — presses outweigh damage_source.
        self.assertEqual(result.discipline_name, "Arsenal")

    def test_maul_no_longer_votes_for_deception(self):
        # Bug in the old table: Maul was tagged Deception. It's actually
        # used by all 3 Assassin disciplines. After the fix, pressing only
        # Maul should give us Assassin (correct) but no specific discipline.
        ability_counts = {
            ("1", "Maul"): {"pressed": 50, "prebuff": 0, "damage_source": 0},
        }
        result = detect_class([], "X", ability_counts=ability_counts)
        self.assertEqual(result.class_name, "Assassin")
        # No specific discipline — Maul is class-only now.
        self.assertEqual(result.discipline_name, "")


if __name__ == "__main__":
    unittest.main()