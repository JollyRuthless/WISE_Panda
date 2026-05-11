"""
Tests for engine/server_list.py — the SWTOR server list loader.

Covers:
- Missing file is created from built-in defaults
- Valid file is read correctly
- Corrupt file (bad JSON) falls back to defaults without overwriting
- Empty servers list falls back without overwriting
- Display name formatting
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import server_list
from engine.server_list import ServerInfo, format_display_name


class TestServerListLoader(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._path = Path(self._tmpdir.name) / "swtor_servers.json"
        self._patcher = patch.object(server_list, "DATA_FILE", self._path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def test_missing_file_is_created_from_defaults(self):
        self.assertFalse(self._path.exists())
        servers = server_list.load_servers()
        # File now exists
        self.assertTrue(self._path.exists())
        # And contains the built-in defaults
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        self.assertIn("servers", payload)
        names_on_disk = [row["name"] for row in payload["servers"]]
        self.assertIn("Star Forge", names_on_disk)
        self.assertIn("The Leviathan", names_on_disk)
        # And what we returned matches
        self.assertEqual(len(servers), len(payload["servers"]))
        self.assertEqual(servers[0].name, names_on_disk[0])

    def test_valid_file_is_read(self):
        # Write a custom file with one extra fake server
        self._path.write_text(json.dumps({
            "servers": [
                {"name": "Test Server", "region": "Test Region", "region_short": "TST"},
            ],
        }), encoding="utf-8")
        servers = server_list.load_servers()
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0].name, "Test Server")
        self.assertEqual(servers[0].region_short, "TST")

    def test_corrupt_file_falls_back_and_does_not_overwrite(self):
        # Write deliberately bad JSON
        original_content = "{this is not valid json,}"
        self._path.write_text(original_content, encoding="utf-8")
        servers = server_list.load_servers()
        # Got the built-in list
        self.assertTrue(any(s.name == "Star Forge" for s in servers))
        # File on disk was NOT clobbered (user might be mid-edit)
        self.assertEqual(self._path.read_text(encoding="utf-8"), original_content)

    def test_empty_servers_list_falls_back(self):
        self._path.write_text(json.dumps({"servers": []}), encoding="utf-8")
        servers = server_list.load_servers()
        # Built-in list returned
        self.assertTrue(any(s.name == "Star Forge" for s in servers))

    def test_entries_missing_name_are_skipped(self):
        self._path.write_text(json.dumps({
            "servers": [
                {"name": "Valid One", "region": "R", "region_short": "R"},
                {"region": "no name field"},        # invalid
                {"name": "", "region": "empty"},    # invalid
                {"name": "Valid Two", "region": "", "region_short": ""},
            ],
        }), encoding="utf-8")
        servers = server_list.load_servers()
        names = [s.name for s in servers]
        self.assertEqual(names, ["Valid One", "Valid Two"])


class TestFormatDisplayName(unittest.TestCase):
    def test_with_short_region(self):
        info = ServerInfo(name="Star Forge", region="North America", region_short="NA")
        self.assertEqual(format_display_name(info), "Star Forge (NA)")

    def test_without_short_region(self):
        info = ServerInfo(name="Some Server", region="Some Region", region_short="")
        self.assertEqual(format_display_name(info), "Some Server")


if __name__ == "__main__":
    unittest.main()
