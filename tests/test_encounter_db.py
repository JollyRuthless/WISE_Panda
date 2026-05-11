from datetime import time
from pathlib import Path
import shutil
import sqlite3
import unittest
from unittest.mock import patch

import storage.encounter_db as encounter_db
from engine.aggregator import EntityKind, EntityStats, Fight
from engine.parser_core import Entity, LogEvent, NamedThing


class EncounterDbCharacterTests(unittest.TestCase):
    def setUp(self):
        self._original_db_path = encounter_db.DB_PATH
        self._temp_dir = Path(__file__).parent / "_test_tmp_encounter_db"
        shutil.rmtree(self._temp_dir, ignore_errors=True)
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        self._test_db_path = self._temp_dir / "encounter_history.sqlite3"
        encounter_db.DB_PATH = self._test_db_path
        encounter_db.init_db()

    def tearDown(self):
        encounter_db.DB_PATH = self._original_db_path
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def test_player_character_rollup_sums_fights_and_tracks_dates(self):
        first = self._fight(
            player_name="Venturus Pounce",
            encounter_date="2026-04-01",
            line_start=10,
            damage_done=1234,
            healing_done=200,
        )
        second = self._fight(
            player_name="Venturus Pounce",
            encounter_date="2026-04-03",
            line_start=20,
            damage_done=3456,
            healing_done=400,
            start_time=time(9, 5, 0),
        )

        with patch("storage.encounter_db.analyse_tank") as mock_analyse_tank:
            mock_analyse_tank.side_effect = [
                type("Metrics", (), {"taunt_count": 2, "interrupt_count": 1})(),
                type("Metrics", (), {"taunt_count": 3, "interrupt_count": 4})(),
            ]
            encounter_db.upsert_fight(first)
            encounter_db.upsert_fight(second)

        rows = encounter_db.list_player_characters()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertGreater(row.character_id, 0)
        self.assertEqual(row.character_name, "Venturus Pounce")
        self.assertEqual(row.class_name, "")
        self.assertEqual(row.first_seen_date, "2026-04-01")
        self.assertEqual(row.last_seen_date, "2026-04-03")
        self.assertEqual(row.total_damage_done, 4690)
        self.assertEqual(row.total_healing_done, 600)
        self.assertEqual(row.total_taunts, 5)
        self.assertEqual(row.total_interrupts, 5)

    def test_player_character_rollup_is_idempotent_for_same_encounter(self):
        fight = self._fight(
            player_name="Doomside",
            encounter_date="2026-04-05",
            line_start=30,
            damage_done=777,
            healing_done=111,
        )

        with patch("storage.encounter_db.analyse_tank") as mock_analyse_tank:
            mock_analyse_tank.return_value = type(
                "Metrics", (), {"taunt_count": 1, "interrupt_count": 2}
            )()
            encounter_db.upsert_fight(fight)
            encounter_db.upsert_fight(fight)

        rows = encounter_db.list_player_characters()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.character_name, "Doomside")
        self.assertEqual(row.total_damage_done, 777)
        self.assertEqual(row.total_healing_done, 111)
        self.assertEqual(row.total_taunts, 1)
        self.assertEqual(row.total_interrupts, 2)

    def test_seed_player_characters_from_logs_reads_header_name_and_class(self):
        log_dir = self._temp_dir / "CombatLogs"
        log_dir.mkdir()
        (log_dir / "combat_2026-04-13_17_10_34_729183.txt").write_text(
            "\n".join([
                "[17:10:41.742] [@Lorika Ransack#686859837826583|(4834.27,4840.36,694.05,-139.46)|(1/381000)] [] [] [AreaEntered {836045448953664}: Imperial Fleet {137438989504}] (he3000) <v7.0.0b>",
                "[17:10:41.742] [@Lorika Ransack#686859837826583|(4834.27,4840.36,694.05,-139.46)|(1/381000)] [] [] [DisciplineChanged {836045448953665}: Operative {16140905232405801950}/Concealment {2031339142381595}]",
            ]),
            encoding="utf-8",
        )
        (log_dir / "combat_2026-04-14_17_11_00_864794.txt").write_text(
            "\n".join([
                "[17:11:08.747] [@Holt Hexxen#690844084234794|(4648.53,4705.14,710.02,86.68)|(1/3838)] [] [] [AreaEntered {836045448953664}: Imperial Fleet {137438989504}] (he3000) <v7.0.0b>",
                "[17:11:08.747] [@Holt Hexxen#690844084234794|(4648.53,4705.14,710.02,86.68)|(1/3838)] [] [] [DisciplineChanged {836045448953665}: Operative {16140905232405801950}/Lethality {2031339142381593}]",
            ]),
            encoding="utf-8",
        )

        seeded = encounter_db.seed_player_characters_from_logs(log_dir)

        self.assertEqual(seeded, 2)
        rows = encounter_db.list_player_characters()
        self.assertEqual(
            [(row.character_name, row.class_name, row.first_seen_date, row.last_seen_date) for row in rows],
            [
                ("Holt Hexxen", "Operative", "2026-04-14", "2026-04-14"),
                ("Lorika Ransack", "Operative", "2026-04-13", "2026-04-13"),
            ],
        )

    def test_seed_player_character_from_log_updates_seen_range_for_existing_name(self):
        log_one = self._temp_dir / "combat_2026-04-13_17_10_34_729183.txt"
        log_two = self._temp_dir / "combat_2026-04-15_17_10_34_729183.txt"
        for path in (log_one, log_two):
            path.write_text(
                "\n".join([
                    f"[17:10:41.742] [@Kincade Jones#686859837826583|(4834.27,4840.36,694.05,-139.46)|(1/381000)] [] [] [AreaEntered {{836045448953664}}: Imperial Fleet {{137438989504}}] (he3000) <v7.0.0b>",
                    f"[17:10:41.742] [@Kincade Jones#686859837826583|(4834.27,4840.36,694.05,-139.46)|(1/381000)] [] [] [DisciplineChanged {{836045448953665}}: Sniper {{16140905232405801950}}/Marksmanship {{2031339142381595}}]",
                ]),
                encoding="utf-8",
            )

        self.assertTrue(encounter_db.seed_player_character_from_log(log_two))
        self.assertTrue(encounter_db.seed_player_character_from_log(log_one))

        rows = encounter_db.list_player_characters()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.character_name, "Kincade Jones")
        self.assertEqual(row.class_name, "Sniper")
        self.assertEqual(row.first_seen_date, "2026-04-13")
        self.assertEqual(row.last_seen_date, "2026-04-15")

    def test_player_character_abilities_roll_up_ability_uses(self):
        first = self._fight(
            player_name="Venturus Pounce",
            encounter_date="2026-04-01",
            line_start=10,
            damage_done=1234,
            healing_done=200,
        )
        first.events = [
            self._ability_activate("Venturus Pounce", "Rail Shot", "1001"),
            self._ability_activate("Venturus Pounce", "Rail Shot", "1001"),
            self._ability_activate("Venturus Pounce", "Rapid Shots", "1002"),
            self._ability_activate("Somebody Else", "Rail Shot", "1001"),
        ]
        second = self._fight(
            player_name="Venturus Pounce",
            encounter_date="2026-04-03",
            line_start=20,
            damage_done=3456,
            healing_done=400,
            start_time=time(9, 5, 0),
        )
        second.events = [
            self._ability_activate("Venturus Pounce", "Rail Shot", "1001"),
            self._ability_activate("Venturus Pounce", "Electro Dart", "1003"),
        ]

        with patch("storage.encounter_db.analyse_tank") as mock_analyse_tank:
            mock_analyse_tank.side_effect = [
                type("Metrics", (), {"taunt_count": 0, "interrupt_count": 0})(),
                type("Metrics", (), {"taunt_count": 0, "interrupt_count": 0})(),
            ]
            encounter_db.upsert_fight(first)
            encounter_db.upsert_fight(second)

        rows = encounter_db.list_player_character_abilities("Venturus Pounce")
        self.assertEqual(
            [(row.ability_name, row.ability_id, row.total_uses) for row in rows],
            [
                ("Electro Dart", "1003", 1),
                ("Rail Shot", "1001", 3),
                ("Rapid Shots", "1002", 1),
            ],
        )

    def test_player_character_abilities_are_idempotent_for_same_encounter(self):
        fight = self._fight(
            player_name="Doomside",
            encounter_date="2026-04-05",
            line_start=30,
            damage_done=777,
            healing_done=111,
        )
        fight.events = [
            self._ability_activate("Doomside", "Sundering Assault", "2001"),
            self._ability_activate("Doomside", "Sundering Assault", "2001"),
        ]

        with patch("storage.encounter_db.analyse_tank") as mock_analyse_tank:
            mock_analyse_tank.return_value = type(
                "Metrics", (), {"taunt_count": 0, "interrupt_count": 0}
            )()
            encounter_db.upsert_fight(fight)
            encounter_db.upsert_fight(fight)

        rows = encounter_db.list_player_character_abilities("Doomside")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].ability_name, "Sundering Assault")
        self.assertEqual(rows[0].ability_id, "2001")
        self.assertEqual(rows[0].total_uses, 2)

    def test_import_combat_log_persists_raw_and_parsed_event_rows(self):
        log_path = self._temp_dir / "combat_2026-04-15_18_10_00_000001.txt"
        log_path.write_text(
            "\n".join([
                "[18:10:41.742] [@Lorika Ransack#686859837826583|(4834.27,4840.36,694.05,-139.46)|(381000/381000)] [Training Dummy {111}:1|(0.00,0.00,0.00,0.00)|(1000/1000)] [Rail Shot {1001}] [Event {1}: Damage {2}] (1234* kinetic {3} -hit {4}) <55.0>",
                "this is not a parseable combat line",
            ]),
            encoding="utf-8",
        )

        summary = encounter_db.import_combat_log(log_path)

        self.assertEqual(summary.file_name, log_path.name)
        self.assertEqual(summary.line_count, 2)
        self.assertEqual(summary.parsed_line_count, 1)
        self.assertEqual(summary.parse_error_count, 1)
        self.assertEqual(summary.source_character_name, "Lorika Ransack")

        with sqlite3.connect(self._test_db_path) as conn:
            conn.row_factory = sqlite3.Row
            imported = conn.execute(
                "SELECT file_name, line_count, parsed_line_count, parse_error_count, source_character_name FROM combat_log_imports"
            ).fetchone()
            self.assertIsNotNone(imported)
            self.assertEqual(imported["file_name"], log_path.name)
            self.assertEqual(imported["line_count"], 2)
            self.assertEqual(imported["parsed_line_count"], 1)
            self.assertEqual(imported["parse_error_count"], 1)
            self.assertEqual(imported["source_character_name"], "Lorika Ransack")

            rows = conn.execute(
                """
                SELECT line_number, parse_status, raw_line, source_name, target_name, ability_name, result_amount, result_is_crit
                FROM combat_log_events
                ORDER BY line_number
                """
            ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual((rows[0]["line_number"], rows[0]["parse_status"]), (1, "parsed"))
            self.assertEqual(rows[0]["source_name"], "Lorika Ransack")
            self.assertEqual(rows[0]["target_name"], "Training Dummy")
            self.assertEqual(rows[0]["ability_name"], "Rail Shot")
            self.assertEqual(rows[0]["result_amount"], 1234)
            self.assertEqual(rows[0]["result_is_crit"], 1)
            self.assertEqual((rows[1]["line_number"], rows[1]["parse_status"]), (2, "parse_error"))
            self.assertEqual(rows[1]["raw_line"], "this is not a parseable combat line")

    def test_import_combat_log_replaces_rows_for_same_file(self):
        log_path = self._temp_dir / "combat_2026-04-15_18_10_00_000002.txt"
        log_path.write_text(
            "[18:10:41.742] [@Lorika Ransack#686859837826583|(4834.27,4840.36,694.05,-139.46)|(381000/381000)] [] [] [AreaEntered {836045448953664}: Imperial Fleet {137438989504}] (he3000) <v7.0.0b>\n",
            encoding="utf-8",
        )
        first = encounter_db.import_combat_log(log_path)

        log_path.write_text(
            "\n".join([
                "[18:10:41.742] [@Lorika Ransack#686859837826583|(4834.27,4840.36,694.05,-139.46)|(381000/381000)] [] [] [AreaEntered {836045448953664}: Imperial Fleet {137438989504}] (he3000) <v7.0.0b>",
                "[18:10:42.100] [@Lorika Ransack#686859837826583|(4834.27,4840.36,694.05,-139.46)|(381000/381000)] [Training Dummy {111}:1|(0.00,0.00,0.00,0.00)|(1000/1000)] [Rapid Shots {1002}] [Event {1}: Damage {2}] (600 energy {3} -hit {4}) <22.0>",
            ]),
            encoding="utf-8",
        )
        second = encounter_db.import_combat_log(log_path)

        self.assertEqual(first.import_id, second.import_id)
        self.assertEqual(second.line_count, 2)
        self.assertEqual(second.parsed_line_count, 2)

        with sqlite3.connect(self._test_db_path) as conn:
            row = conn.execute("SELECT COUNT(*) FROM combat_log_imports").fetchone()
            self.assertEqual(row[0], 1)
            row = conn.execute("SELECT COUNT(*) FROM combat_log_events").fetchone()
            self.assertEqual(row[0], 2)

    def _fight(
        self,
        *,
        player_name: str,
        encounter_date: str,
        line_start: int,
        damage_done: int,
        healing_done: int,
        start_time: time = time(9, 0, 0),
    ) -> Fight:
        fight = Fight(
            index=1,
            start_time=start_time,
            end_time=time(start_time.hour, start_time.minute, min(start_time.second + 15, 59)),
            player_name=player_name,
            _log_path=str(Path(f"CombatLog_{encounter_date}_120000.txt")),
            _line_start=line_start,
            _line_end=line_start - 1,
        )
        fight.entity_stats[player_name] = EntityStats(
            name=player_name,
            kind=EntityKind.PLAYER,
            damage_dealt=damage_done,
            healing_done=healing_done,
        )
        return fight

    def _ability_activate(self, player_name: str, ability_name: str, ability_id: str) -> LogEvent:
        return LogEvent(
            timestamp=time(9, 0, 0),
            source=Entity(player=player_name),
            target=Entity(is_empty=True),
            ability=NamedThing(name=ability_name, id=ability_id),
            effect_type="Event",
            effect_name="AbilityActivate",
            effect_id="1",
            effect_detail=NamedThing(name="AbilityActivate", id="1"),
        )


if __name__ == "__main__":
    unittest.main()
