import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from engine.ability_icons import (
    AbilityIconLibrary,
    encounter_ability_pairs,
    known_ability_ids_by_name,
    normalize_ability_name,
    rename_noid_icons,
)


class AbilityIconLibraryTests(unittest.TestCase):
    TMP_ROOT = Path(__file__).parent / "_test_tmp_ability_icons"

    @classmethod
    def setUpClass(cls):
        cls.TMP_ROOT.mkdir(exist_ok=True)

    def _tmp(self):
        return tempfile.TemporaryDirectory(dir=self.TMP_ROOT)

    def test_resolves_icons_by_id_then_name(self):
        with self._tmp() as tmp:
            icon_dir = Path(tmp)
            by_id = icon_dir / "12345_Rail_Shot.jpg"
            noid = icon_dir / "noid_1_2_Force_Clarity.jpg"
            by_id.write_bytes(b"x")
            noid.write_bytes(b"y")

            library = AbilityIconLibrary(icon_dir)

            self.assertEqual(library.icon_path("Something Else", "12345"), by_id)
            self.assertEqual(library.icon_path("Rail Shot"), by_id)
            self.assertEqual(library.icon_path("Force Clarity"), noid)

    def test_rename_noid_icons_uses_known_id_mapping(self):
        with self._tmp() as tmp:
            icon_dir = Path(tmp)
            old_path = icon_dir / "noid_2_25_Force_Clarity.jpg"
            old_path.write_bytes(b"x")

            result = rename_noid_icons(icon_dir, [("Force Clarity", "98765")])

            new_path = icon_dir / "98765_Force_Clarity.jpg"
            self.assertEqual(result.renamed, [(old_path, new_path)])
            self.assertTrue(new_path.exists())
            self.assertFalse(old_path.exists())

    def test_rename_skips_when_destination_exists(self):
        with self._tmp() as tmp:
            icon_dir = Path(tmp)
            old_path = icon_dir / "noid_1_23_Countermeasures.jpg"
            existing = icon_dir / "3469564776022016_Countermeasures.jpg"
            old_path.write_bytes(b"x")
            existing.write_bytes(b"y")

            result = rename_noid_icons(icon_dir, [("Countermeasures", "3469564776022016")])

            self.assertEqual(result.renamed, [])
            self.assertTrue(old_path.exists())
            self.assertTrue(existing.exists())

    def test_encounter_pairs_drive_noid_rename_without_database(self):
        class Ability:
            def __init__(self, name, ability_id):
                self.name = name
                self.id = ability_id

        class Event:
            def __init__(self, ability):
                self.ability = ability

        class Fight:
            events = [
                Event(Ability("Blade Blitz", "123456")),
                Event(Ability("Blade Blitz", "123456")),
                Event(Ability("Unknown", "")),
            ]

        with self._tmp() as tmp:
            icon_dir = Path(tmp)
            old_path = icon_dir / "noid_1_8_Blade_Blitz.jpg"
            old_path.write_bytes(b"x")

            library = AbilityIconLibrary(icon_dir)
            result = library.rename_noid_icons_for_abilities(encounter_ability_pairs(Fight()))

            new_path = icon_dir / "123456_Blade_Blitz.jpg"
            self.assertEqual(result.renamed, [(old_path, new_path)])
            self.assertEqual(library.icon_path("Blade Blitz", "123456"), new_path)

    def test_known_mappings_merge_abilities_json_and_encounter_db(self):
        with self._tmp() as tmp:
            root = Path(tmp)
            abilities_path = root / "abilities.json"
            db_path = root / "encounter_history.sqlite3"
            abilities_path.write_text(
                json.dumps({"abilities": {"Rail Shot": {"id": "1001"}}}),
                encoding="utf-8",
            )
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(
                    "CREATE TABLE combat_log_events (ability_name TEXT, ability_id TEXT)"
                )
                conn.execute(
                    "INSERT INTO combat_log_events (ability_name, ability_id) VALUES (?, ?)",
                    ("Force Clarity", "2002"),
                )
                conn.commit()
            finally:
                conn.close()

            mappings = known_ability_ids_by_name(abilities_path, db_path)

            self.assertEqual(mappings[normalize_ability_name("Rail Shot")], ("1001", "Rail Shot"))
            self.assertEqual(
                mappings[normalize_ability_name("Force_Clarity")],
                ("2002", "Force Clarity"),
            )


if __name__ == "__main__":
    unittest.main()
