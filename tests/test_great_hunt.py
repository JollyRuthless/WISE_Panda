import unittest
from pathlib import Path

import engine.great_hunt as great_hunt
class GreatHuntLocationTests(unittest.TestCase):
    def setUp(self):
        self._original_data_file = great_hunt.DATA_FILE
        self._test_data_file = Path(__file__).parent / "_test_great_hunt_data.json"
        self._test_db_file = self._test_data_file.with_suffix(".sqlite3")
        for path in (self._test_data_file, self._test_db_file):
            try:
                path.unlink()
            except OSError:
                pass
        great_hunt.DATA_FILE = self._test_data_file

    def tearDown(self):
        great_hunt.DATA_FILE = self._original_data_file
        for path in (self._test_data_file, self._test_db_file):
            try:
                path.unlink()
            except OSError:
                pass

    def test_infers_last_area_entered_as_zone_fallback(self):
        log_text = "\n".join([
            "[09:16:22.713] [@Venturus Pounce#1|(1.00,2.00,3.00,4.00)|(1/10)] [] [] [AreaEntered {1}: Imperial Fleet {2}] (he3000) <v7.0.0b>",
            "[09:16:25.000] [@Venturus Pounce#1|(1.00,2.00,3.00,4.00)|(1/10)] [] [] [Event {3}: EnterCombat {4}]",
            "[09:16:30.000] [@Venturus Pounce#1|(1.00,2.00,3.00,4.00)|(1/10)] [] [] [AreaEntered {1}: Dromund Kaas {5}] (he3000) <v7.0.0b>",
        ])
        log_path = Path(__file__).parent / "_test_great_hunt_location.txt"
        try:
            log_path.write_text(log_text, encoding="utf-8")
            result = great_hunt.infer_location_fields(str(log_path), line_end=2)
        finally:
            try:
                log_path.unlink()
            except OSError:
                pass

        self.assertEqual(result["detected_area_name"], "Dromund Kaas")
        self.assertEqual(result["location_name"], "Dromund Kaas")
        self.assertEqual(result["zone_name"], "")
        self.assertEqual(result["instance_name"], "")

    def test_complete_annotation_matches_same_location_and_all_mob_classifications(self):
        original_data_file = great_hunt.DATA_FILE
        test_data_file = Path(__file__).parent / "_test_great_hunt_data.json"
        great_hunt.DATA_FILE = test_data_file
        great_hunt.save_data({"reference_rows": [], "annotations": {}})
        try:
            great_hunt.save_annotation("old-fight", {
                "fight": {
                    "location_name": "Dromund Kaas",
                    "zone_name": "Heroic 2+ Personal Challenge",
                    "instance_name": "Open World",
                },
                "mobs": {
                    "Mandalorian Beast Slayer|1117318562185216": {"classification": "Elite"},
                    "Mandalorian Trophy Hunter|1116940605063168": {"classification": "Elite"},
                },
            })

            self.assertTrue(great_hunt.has_complete_annotation(
                [
                    "Mandalorian Beast Slayer|1117318562185216",
                    "Mandalorian Trophy Hunter|1116940605063168",
                ],
                {
                    "location_name": "Dromund Kaas",
                    "zone_name": "Heroic 2+ Personal Challenge",
                    "instance_name": "Open World",
                },
                "new-fight",
            ))
        finally:
            great_hunt.DATA_FILE = original_data_file
            try:
                test_data_file.unlink()
            except OSError:
                pass

    def test_clear_annotations_preserves_reference_rows(self):
        original_data_file = great_hunt.DATA_FILE
        test_data_file = Path(__file__).parent / "_test_great_hunt_data.json"
        great_hunt.DATA_FILE = test_data_file
        great_hunt.save_data({
            "reference_rows": [{"kind": "zone", "value": "Sanctuary", "parent": "Dromund Kaas"}],
            "annotations": {"old-fight": {"fight": {}, "mobs": {}}},
        })
        try:
            great_hunt.clear_annotations()
            payload = great_hunt.load_data()
            self.assertEqual(payload["annotations"], {})
            self.assertEqual(
                payload["reference_rows"],
                [{"kind": "zone", "value": "Sanctuary", "parent": "Dromund Kaas"}],
            )
        finally:
            great_hunt.DATA_FILE = original_data_file
            try:
                test_data_file.unlink()
            except OSError:
                pass

    def test_contextual_choices_reuse_saved_data_for_matching_location_only(self):
        original_data_file = great_hunt.DATA_FILE
        test_data_file = Path(__file__).parent / "_test_great_hunt_data.json"
        great_hunt.DATA_FILE = test_data_file
        great_hunt.save_data({"reference_rows": [], "annotations": {}})
        try:
            great_hunt.save_annotation("dromund-fight", {
                "fight": {
                    "location_name": "Dromund Kaas",
                    "zone_name": "Sanctuary",
                    "location_type": "Open World",
                    "quest_name": "Heroic 2+ Personal Challenge",
                },
                "mobs": {},
            })
            great_hunt.save_annotation("fleet-fight", {
                "fight": {
                    "location_name": "Imperial Fleet",
                    "zone_name": "Supplies",
                    "location_type": "Open World",
                    "quest_name": "Fleet Errand",
                },
                "mobs": {},
            })

            self.assertEqual(
                great_hunt.get_contextual_choices("zone", location="Dromund Kaas"),
                ["Sanctuary"],
            )
            self.assertEqual(
                great_hunt.get_contextual_choices("quest", location="Dromund Kaas", zone="Sanctuary"),
                ["Heroic 2+ Personal Challenge"],
            )
            self.assertEqual(
                great_hunt.get_contextual_choices("location_type", location="Dromund Kaas", zone="Sanctuary"),
                ["Open World"],
            )
        finally:
            great_hunt.DATA_FILE = original_data_file
            try:
                test_data_file.unlink()
            except OSError:
                pass

    def test_recent_context_value_returns_latest_matching_zone(self):
        original_data_file = great_hunt.DATA_FILE
        test_data_file = Path(__file__).parent / "_test_great_hunt_data.json"
        great_hunt.DATA_FILE = test_data_file
        great_hunt.save_data({"reference_rows": [], "annotations": {}})
        try:
            great_hunt.save_annotation("first-fight", {
                "fight": {
                    "location_name": "Dromund Kaas",
                    "zone_name": "Sanctuary",
                },
                "mobs": {},
            })
            great_hunt.save_annotation("second-fight", {
                "fight": {
                    "location_name": "Dromund Kaas",
                    "zone_name": "The Unfinished Colossus",
                },
                "mobs": {},
            })
            great_hunt.save_annotation("fleet-fight", {
                "fight": {
                    "location_name": "Imperial Fleet",
                    "zone_name": "Supplies",
                },
                "mobs": {},
            })

            self.assertEqual(
                great_hunt.get_contextual_choices("zone", location="Dromund Kaas"),
                ["Sanctuary", "The Unfinished Colossus"],
            )
            self.assertEqual(
                great_hunt.get_recent_context_value("zone", location="Dromund Kaas"),
                "The Unfinished Colossus",
            )
        finally:
            great_hunt.DATA_FILE = original_data_file
            try:
                test_data_file.unlink()
            except OSError:
                pass

    def test_contextual_choices_fall_back_to_location_when_zone_has_no_values(self):
        original_data_file = great_hunt.DATA_FILE
        test_data_file = Path(__file__).parent / "_test_great_hunt_data.json"
        great_hunt.DATA_FILE = test_data_file
        great_hunt.save_data({"reference_rows": [], "annotations": {}})
        try:
            great_hunt.save_annotation("quest-fight", {
                "fight": {
                    "location_name": "Dromund Kaas",
                    "zone_name": "Sanctuary",
                    "location_type": "Open World",
                    "quest_name": "Heroic 2+ Personal Challenge",
                },
                "mobs": {},
            })

            self.assertEqual(
                great_hunt.get_contextual_choices("quest", location="Dromund Kaas", zone="Apple"),
                ["Heroic 2+ Personal Challenge"],
            )
            self.assertEqual(
                great_hunt.get_contextual_choices("location_type", location="Dromund Kaas", zone="Apple"),
                ["Open World"],
            )
        finally:
            great_hunt.DATA_FILE = original_data_file
            try:
                test_data_file.unlink()
            except OSError:
                pass

    def test_contextual_quest_choices_can_be_location_scoped(self):
        original_data_file = great_hunt.DATA_FILE
        test_data_file = Path(__file__).parent / "_test_great_hunt_data.json"
        great_hunt.DATA_FILE = test_data_file
        great_hunt.save_data({"reference_rows": [], "annotations": {}})
        try:
            great_hunt.save_annotation("personal-challenge", {
                "fight": {
                    "location_name": "Dromund Kaas",
                    "zone_name": "Sanctuary",
                    "quest_name": "Heroic 2+ Personal Challenge",
                },
                "mobs": {},
            })
            great_hunt.save_annotation("saving-face", {
                "fight": {
                    "location_name": "Dromund Kaas",
                    "zone_name": "The Unfinished Colossus",
                    "quest_name": "Heroic 2+ Saving Face",
                },
                "mobs": {},
            })

            self.assertEqual(
                great_hunt.get_contextual_choices("quest", location="Dromund Kaas"),
                ["Heroic 2+ Personal Challenge", "Heroic 2+ Saving Face"],
            )
        finally:
            great_hunt.DATA_FILE = original_data_file
            try:
                test_data_file.unlink()
            except OSError:
                pass

    def test_list_annotation_entries_flattens_saved_mobs(self):
        original_data_file = great_hunt.DATA_FILE
        test_data_file = Path(__file__).parent / "_test_great_hunt_data.json"
        great_hunt.DATA_FILE = test_data_file
        great_hunt.save_data({"reference_rows": [], "annotations": {}})
        try:
            great_hunt.save_annotation("old-fight", {
                "fight": {
                    "location_name": "Dromund Kaas",
                    "zone_name": "Heroic 2+ Personal Challenge",
                    "instance_name": "Open World",
                    "character_name": "Venturus Pounce",
                    "fight_label": "#2 - Mandalorian Beast Slayer  (0:46)",
                },
                "mobs": {
                    "Mandalorian Beast Slayer|1117318562185216": {
                        "mob_name": "Mandalorian Beast Slayer",
                        "npc_entity_id": "1117318562185216",
                        "classification": "Elite (Gold)",
                        "max_hp_seen": 2875,
                        "instances_seen": 2,
                    },
                },
            })

            rows = great_hunt.list_annotation_entries()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["npc_entity_id"], "1117318562185216")
            self.assertEqual(rows[0]["mob_name"], "Mandalorian Beast Slayer")
            self.assertEqual(rows[0]["classification"], "Elite (Gold)")
            self.assertEqual(rows[0]["location"], "Dromund Kaas")
            self.assertEqual(rows[0]["zone"], "Heroic 2+ Personal Challenge")
            self.assertEqual(rows[0]["location_type"], "Open World")
            self.assertEqual(rows[0]["instance_name"], "")
            self.assertEqual(rows[0]["quest_name"], "")
            self.assertEqual(rows[0]["character_name"], "Venturus Pounce")
            self.assertEqual(rows[0]["mob_count"], "2")
            self.assertEqual(rows[0]["conflict"], "")
        finally:
            great_hunt.DATA_FILE = original_data_file
            try:
                test_data_file.unlink()
            except OSError:
                pass

    def test_list_annotation_entries_marks_conflicting_npc_id_data_and_keeps_first_character(self):
        original_data_file = great_hunt.DATA_FILE
        test_data_file = Path(__file__).parent / "_test_great_hunt_data.json"
        great_hunt.DATA_FILE = test_data_file
        great_hunt.save_data({"reference_rows": [], "annotations": {}})
        try:
            great_hunt.save_annotation("fight-one", {
                "fight": {"location_name": "Dromund Kaas", "character_name": "First Finder"},
                "mobs": {
                    "Mandalorian Beast Slayer|1117318562185216": {
                        "mob_name": "Mandalorian Beast Slayer",
                        "npc_entity_id": "1117318562185216",
                        "classification": "Elite (Gold)",
                        "instances_seen": 1,
                    },
                },
            })
            great_hunt.save_annotation("fight-two", {
                "fight": {"location_name": "Dromund Kaas", "character_name": "Later Finder"},
                "mobs": {
                    "Mandalorian Beast Slayer|1117318562185216": {
                        "mob_name": "Mandalorian Beast Slayer",
                        "npc_entity_id": "1117318562185216",
                        "classification": "Champion",
                        "instances_seen": 1,
                    },
                },
            })

            rows = great_hunt.list_annotation_entries()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["npc_entity_id"], "1117318562185216")
            self.assertEqual(rows[0]["classification"], "Elite (Gold)")
            self.assertEqual(rows[0]["character_name"], "First Finder")
            self.assertEqual(rows[0]["conflict"], "Type")
        finally:
            great_hunt.DATA_FILE = original_data_file
            try:
                test_data_file.unlink()
            except OSError:
                pass

    def test_complete_annotation_requires_every_mob_classification(self):
        original_data_file = great_hunt.DATA_FILE
        test_data_file = Path(__file__).parent / "_test_great_hunt_data.json"
        great_hunt.DATA_FILE = test_data_file
        great_hunt.save_data({"reference_rows": [], "annotations": {}})
        try:
            great_hunt.save_annotation("old-fight", {
                "fight": {"location_name": "Dromund Kaas"},
                "mobs": {
                    "Mandalorian Beast Slayer|1117318562185216": {"classification": "Elite"},
                    "Mandalorian Trophy Hunter|1116940605063168": {"classification": ""},
                },
            })

            self.assertFalse(great_hunt.has_complete_annotation(
                [
                    "Mandalorian Beast Slayer|1117318562185216",
                    "Mandalorian Trophy Hunter|1116940605063168",
                ],
                {"location_name": "Dromund Kaas"},
            ))
        finally:
            great_hunt.DATA_FILE = original_data_file
            try:
                test_data_file.unlink()
            except OSError:
                pass

    def test_complete_annotation_requires_zone_and_location_type(self):
        original_data_file = great_hunt.DATA_FILE
        test_data_file = Path(__file__).parent / "_test_great_hunt_data.json"
        great_hunt.DATA_FILE = test_data_file
        great_hunt.save_data({"reference_rows": [], "annotations": {}})
        try:
            great_hunt.save_annotation("old-fight", {
                "fight": {"location_name": "Dromund Kaas"},
                "mobs": {
                    "Mandalorian Beast Slayer|1117318562185216": {"classification": "Elite"},
                },
            })

            self.assertFalse(great_hunt.has_complete_annotation(
                ["Mandalorian Beast Slayer|1117318562185216"],
                {"location_name": "Dromund Kaas"},
            ))
        finally:
            great_hunt.DATA_FILE = original_data_file
            try:
                test_data_file.unlink()
            except OSError:
                pass

    def test_complete_annotation_allows_blank_instance_and_quest_name(self):
        original_data_file = great_hunt.DATA_FILE
        test_data_file = Path(__file__).parent / "_test_great_hunt_data.json"
        great_hunt.DATA_FILE = test_data_file
        great_hunt.save_data({"reference_rows": [], "annotations": {}})
        try:
            great_hunt.save_annotation("old-fight", {
                "fight": {
                    "location_name": "Dromund Kaas",
                    "zone_name": "Heroic 2+ Personal Challenge",
                    "location_type": "Open World",
                    "instance_name": "",
                    "quest_name": "",
                },
                "mobs": {
                    "Mandalorian Beast Slayer|1117318562185216": {"classification": "Elite"},
                },
            })

            self.assertTrue(great_hunt.has_complete_annotation(
                ["Mandalorian Beast Slayer|1117318562185216"],
                {
                    "location_name": "Dromund Kaas",
                    "zone_name": "Heroic 2+ Personal Challenge",
                    "location_type": "Open World",
                },
            ))
        finally:
            great_hunt.DATA_FILE = original_data_file
            try:
                test_data_file.unlink()
            except OSError:
                pass

    def test_select_classification_is_not_complete(self):
        original_data_file = great_hunt.DATA_FILE
        test_data_file = Path(__file__).parent / "_test_great_hunt_data.json"
        great_hunt.DATA_FILE = test_data_file
        great_hunt.save_data({"reference_rows": [], "annotations": {}})
        try:
            great_hunt.save_annotation("old-fight", {
                "fight": {"location_name": "Dromund Kaas"},
                "mobs": {
                    "Mandalorian Beast Slayer|1117318562185216": {"classification": "Select"},
                },
            })

            self.assertFalse(great_hunt.has_complete_annotation(
                ["Mandalorian Beast Slayer|1117318562185216"],
                {"location_name": "Dromund Kaas"},
            ))
        finally:
            great_hunt.DATA_FILE = original_data_file
            try:
                test_data_file.unlink()
            except OSError:
                pass

    def test_known_mob_classifications_prefills_partial_mixed_encounter(self):
        original_data_file = great_hunt.DATA_FILE
        test_data_file = Path(__file__).parent / "_test_great_hunt_data.json"
        great_hunt.DATA_FILE = test_data_file
        great_hunt.save_data({"reference_rows": [], "annotations": {}})
        try:
            great_hunt.save_annotation("old-fight", {
                "fight": {"location_name": "Dromund Kaas"},
                "mobs": {
                    "Mandalorian Beast Slayer|1117318562185216": {"classification": "Elite"},
                },
            })

            self.assertEqual(
                great_hunt.known_mob_classifications(
                    [
                        "Mandalorian Beast Slayer|1117318562185216",
                        "Brand New Mob|999",
                    ],
                    {"location_name": "Dromund Kaas"},
                    "new-fight",
                ),
                {"Mandalorian Beast Slayer|1117318562185216": "Elite"},
            )
        finally:
            great_hunt.DATA_FILE = original_data_file
            try:
                test_data_file.unlink()
            except OSError:
                pass

    def test_classification_for_npc_uses_saved_entry(self):
        original_data_file = great_hunt.DATA_FILE
        test_data_file = Path(__file__).parent / "_test_great_hunt_data.json"
        great_hunt.DATA_FILE = test_data_file
        great_hunt.save_data({"reference_rows": [], "annotations": {}, "entries": {}})
        try:
            great_hunt.update_entry("1117318562185216", {"classification": "Elite (Gold)"})

            self.assertEqual(great_hunt.classification_for_npc("1117318562185216"), "Elite (Gold)")
            self.assertEqual(great_hunt.classification_for_npc("999"), "")
            self.assertEqual(great_hunt.classification_for_npc(""), "")
        finally:
            great_hunt.DATA_FILE = original_data_file
            try:
                test_data_file.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
