"""
tests/test_threat_row_status.py — Phase 4c tests for the threat-panel
row color-status logic.

Tests the pure-function helpers in engine/threat_status.py that grade
a snapshot row as green/yellow/red. No PyQt6 required.
"""

import unittest

from engine.threat_status import (
    _perspective_status,
    threat_row_status,
    THREAT_NEGATIVE_RATE,
    THREAT_SMALL_GAP_ABS,
    THREAT_SMALL_GAP_PCT,
    THREAT_IMMINENT_SECONDS,
)


# ── _perspective_status: single perspective grading ──────────────────────────

class PerspectiveStatusTests(unittest.TestCase):
    def test_red_when_imminent_and_negative_rate(self):
        # time_left < 5s AND rate < -10 → red.
        status = _perspective_status(
            gap=200.0, closing_rate=-50.0, time_left=2.0,
            reference_threat=10_000.0,
        )
        self.assertEqual(status, "red")

    def test_not_red_when_time_left_imminent_but_rate_not_negative_enough(self):
        # time_left looks scary, but rate is barely negative.
        # We don't want to fire red on noise.
        status = _perspective_status(
            gap=200.0, closing_rate=-1.0, time_left=2.0,
            reference_threat=10_000.0,
        )
        self.assertNotEqual(status, "red")

    def test_not_red_when_time_left_none(self):
        # No overtake predicted at all → never red.
        status = _perspective_status(
            gap=200.0, closing_rate=-50.0, time_left=None,
            reference_threat=10_000.0,
        )
        self.assertNotEqual(status, "red")

    def test_yellow_when_gap_smaller_than_abs_threshold(self):
        # Absolute gap under 500, rate fine → yellow on the gap signal.
        status = _perspective_status(
            gap=100.0, closing_rate=0.0, time_left=None,
            reference_threat=100_000.0,
        )
        self.assertEqual(status, "yellow")

    def test_yellow_when_gap_smaller_than_pct_threshold(self):
        # Big absolute gap, but tank's threat is huge so 5% threshold is bigger.
        # gap=1000, tank=100_000 → gap is 1% of tank's threat → yellow.
        status = _perspective_status(
            gap=1000.0, closing_rate=0.0, time_left=None,
            reference_threat=100_000.0,
        )
        self.assertEqual(status, "yellow")

    def test_yellow_when_rate_meaningfully_negative(self):
        # Gap is fine (large) but rate is closing.
        status = _perspective_status(
            gap=5000.0, closing_rate=-25.0, time_left=None,
            reference_threat=10_000.0,
        )
        self.assertEqual(status, "yellow")

    def test_green_when_everything_is_fine(self):
        status = _perspective_status(
            gap=5000.0, closing_rate=0.0, time_left=None,
            reference_threat=100_000.0,
        )
        self.assertEqual(status, "green")

    def test_green_when_rate_positive(self):
        # Gap is widening — definitely safe.
        status = _perspective_status(
            gap=5000.0, closing_rate=50.0, time_left=None,
            reference_threat=10_000.0,
        )
        self.assertEqual(status, "green")

    def test_zero_reference_threat_skips_percentage_check(self):
        # When the reference player has no threat, we only check the
        # absolute threshold. The percentage check would divide by zero
        # in spirit; skip it.
        status = _perspective_status(
            gap=600.0, closing_rate=0.0, time_left=None,
            reference_threat=0.0,
        )
        # 600 > THREAT_SMALL_GAP_ABS (500), no rate signal → green.
        self.assertEqual(status, "green")


# ── _threat_row_status: worst-of-two combining ───────────────────────────────

class ThreatRowStatusTests(unittest.TestCase):
    def _row(self, **kw):
        """Convenience: build a snapshot-like dict with safe defaults."""
        base = {
            "tank_threat": 10_000.0,
            "second_threat": 5_000.0,
            "your_threat": 5_000.0,
            "dps_gap": 5_000.0,
            "dps_closing_rate": 0.0,
            "dps_time_left": None,
            "tank_gap": 5_000.0,
            "tank_closing_rate": 0.0,
            "tank_time_left": None,
        }
        base.update(kw)
        return base

    def test_green_when_both_perspectives_safe(self):
        self.assertEqual(threat_row_status(self._row()), "green")

    def test_yellow_when_dps_perspective_warns_only(self):
        row = self._row(dps_gap=100.0)  # small gap → yellow on DPS side
        self.assertEqual(threat_row_status(row), "yellow")

    def test_yellow_when_tank_perspective_warns_only(self):
        row = self._row(tank_closing_rate=-30.0)  # tank losing grip
        self.assertEqual(threat_row_status(row), "yellow")

    def test_red_when_either_perspective_is_red(self):
        # DPS about to pull — red regardless of tank side.
        row = self._row(
            dps_gap=200.0, dps_closing_rate=-50.0, dps_time_left=2.0,
        )
        self.assertEqual(threat_row_status(row), "red")

    def test_red_beats_yellow(self):
        # DPS yellow, tank red → row is red.
        row = self._row(
            dps_gap=200.0,  # yellow on DPS side (small gap)
            tank_gap=200.0, tank_closing_rate=-50.0, tank_time_left=1.0,
        )
        self.assertEqual(threat_row_status(row), "red")

    def test_zero_tank_threat_forces_dps_green(self):
        # Nobody has tank threat → no DPS-side comparison possible.
        row = self._row(tank_threat=0.0, dps_gap=10.0, dps_closing_rate=-100.0)
        # DPS side is forced green; tank side defaults to green.
        self.assertEqual(threat_row_status(row), "green")

    def test_zero_second_threat_forces_tank_green(self):
        # No second-place player → no tank-side comparison.
        row = self._row(second_threat=0.0,
                        tank_gap=10.0, tank_closing_rate=-100.0)
        self.assertEqual(threat_row_status(row), "green")


# ── Threshold sanity checks ──────────────────────────────────────────────────

class ThresholdSanityTests(unittest.TestCase):
    """Guard against future accidental threshold edits that flip the
    meaning of the color signals. If someone changes the sign or scale
    of one of these constants the assertion will catch it immediately."""

    def test_negative_rate_is_negative(self):
        self.assertLess(THREAT_NEGATIVE_RATE, 0)

    def test_small_gap_pct_is_between_zero_and_one(self):
        self.assertGreater(THREAT_SMALL_GAP_PCT, 0)
        self.assertLess(THREAT_SMALL_GAP_PCT, 1)

    def test_imminent_seconds_is_short_positive(self):
        self.assertGreater(THREAT_IMMINENT_SECONDS, 0)
        self.assertLess(THREAT_IMMINENT_SECONDS, 30)


if __name__ == "__main__":
    unittest.main()
