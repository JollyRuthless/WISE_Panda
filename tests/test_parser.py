import unittest
from pathlib import Path
from unittest import mock

from engine.parser import _open_log, parse_line


class ParserTests(unittest.TestCase):
    def test_open_log_only_samples_bytes_for_encoding_detection(self):
        path = Path(__file__).parent / "_test_encoding_sample.txt"
        payload = ("A" * 20000).encode("utf-8")
        path.write_bytes(payload)
        read_sizes: list[int] = []
        original_open = open

        def tracking_open(*args, **kwargs):
            handle = original_open(*args, **kwargs)
            mode = args[1] if len(args) > 1 else kwargs.get("mode", "r")
            if "b" in mode:
                original_read = handle.read

                def tracked_read(size=-1):
                    read_sizes.append(size)
                    return original_read(size)

                handle.read = tracked_read
            return handle

        try:
            with mock.patch("builtins.open", side_effect=tracking_open):
                with _open_log(str(path)) as handle:
                    self.assertEqual(handle.read(1), "A")
        finally:
            try:
                path.unlink()
            except OSError:
                pass

        self.assertIn(16384, read_sizes)
        self.assertNotIn(-1, read_sizes)

    def test_parses_spend_event_into_spend_amount(self):
        line = (
            "[12:41:47.009] [@Nathrakh] [@Nathrakh] [] "
            "[Spend {836045448945473}: Force {836045448938502}] (45)"
        )
        ev = parse_line(line)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.effect_type, "Spend")
        self.assertEqual(ev.effect_name, "Force")
        self.assertIsNone(ev.restore_amount)
        self.assertEqual(ev.spend_amount, 45.0)

    def test_parses_restore_event_into_restore_amount(self):
        line = (
            "[12:41:47.010] [@Nathrakh] [@Nathrakh] [] "
            "[Restore {836045448945474}: Force {836045448938502}] (8)"
        )
        ev = parse_line(line)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.effect_type, "Restore")
        self.assertEqual(ev.restore_amount, 8.0)
        self.assertIsNone(ev.spend_amount)

    def test_treats_resist_as_avoidance(self):
        line = (
            "[16:06:38.136] [Dread Guard Acolyte {3267194506969088}:5023040388242] [@Regent] "
            "[shiv {811787473649664}] [ApplyEffect {836045448945477}: Damage {836045448945501}] "
            "(0 -resist {836045448945510}) <1451>"
        )
        ev = parse_line(line)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.result.result, "resist")
        self.assertTrue(ev.result.is_miss)

    def test_treats_zero_shield_as_avoidance_but_keeps_partial_shields_as_hits(self):
        zero_line = (
            "[16:03:28.609] [selgh Kap'gohe {3310406172934144}:5023040126440] [@Regent] "
            "[Force Scream {1660266852909056}] [ApplyEffect {836045448945477}: Damage {836045448945501}] "
            "(0 -shield {836045448945509}) <1029>"
        )
        zero_ev = parse_line(zero_line)
        self.assertIsNotNone(zero_ev)
        self.assertEqual(zero_ev.result.result, "shield")
        self.assertTrue(zero_ev.result.is_miss)

        partial_line = (
            "[16:03:28.610] [selgh Kap'gohe {3310406172934144}:5023040126440] [@Regent] "
            "[Force Scream {1660266852909056}] [ApplyEffect {836045448945477}: Damage {836045448945501}] "
            "(123 energy {836045448945509} -shield {836045448945509}) <1029>"
        )
        partial_ev = parse_line(partial_line)
        self.assertIsNotNone(partial_ev)
        self.assertEqual(partial_ev.result.result, "shield")
        self.assertFalse(partial_ev.result.is_miss)

    def test_handles_blank_result_marker(self):
        line = (
            "[16:08:56.871] [sorna Taros {3267181622067200}:5023040100176] [@Regent] "
            "[Hammer Shot {811860488093696}] [ApplyEffect {836045448945477}: Damage {836045448945501}] "
            "(0 -) <172>"
        )
        ev = parse_line(line)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.result.result, "")
        self.assertTrue(ev.result.is_miss)

    def test_parses_reflected_damage_variant(self):
        line = (
            "[19:46:52.216] [@Kincade Jones#690562598556051|(152.02,639.03,-147.78,178.32)|(411134/411134)] "
            "[Refactored Battle Droid {3943866604453888}:7666000979620|(151.95,656.12,-151.19,41.29)|(329180/4078620)] "
            "[Responsive Safeguards {4085553280581632}] [ApplyEffect {836045448945477}: Damage {836045448945501}] "
            "(1212 kinetic {836045448940873}(reflected {836045448953649}))"
        )
        ev = parse_line(line)
        self.assertIsNotNone(ev)
        self.assertIsNotNone(ev.result)
        self.assertEqual(ev.result.amount, 1212)
        self.assertEqual(ev.result.dmg_type, "kinetic")
        self.assertFalse(ev.result.is_miss)


if __name__ == "__main__":
    unittest.main()
