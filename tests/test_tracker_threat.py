"""
tests/test_tracker_threat.py — Phase 4a tests for the predictive threat math.

Covers:
  - _rate_from_samples: pure-function math for threat-per-second
  - _time_to_zero: pure-function math for time-to-overtake prediction
  - LiveFightTracker.threat_panel_snapshot: integration through synthetic
    LogEvents pushed at the tracker

These tests don't need PyQt6 — the tracker is pure Python by design (see
tracker.py module docstring).
"""

import unittest
from datetime import time

from engine.parser import LogEvent, Entity, DamageResult, NamedThing
from ui.live.tracker import (
    LiveFightTracker,
    _rate_from_samples,
    _time_to_zero,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts(seconds_after_midnight: float) -> time:
    """Build a datetime.time from seconds-after-midnight. Keeps the test
    fixtures readable: _ts(0.0), _ts(5.5), _ts(10.0) etc."""
    total_microsec = int(seconds_after_midnight * 1_000_000)
    sec, usec = divmod(total_microsec, 1_000_000)
    mins, sec = divmod(sec, 60)
    hours, mins = divmod(mins, 60)
    return time(hour=hours, minute=mins, second=sec, microsecond=usec)


def _player_entity(name: str) -> Entity:
    """Make an Entity that the tracker will treat as a player."""
    return Entity(player=name, player_id="x")


def _npc_entity(name: str, entity_id: str = "npc-1", maxhp: int = 100_000) -> Entity:
    """Make an Entity that the tracker will treat as an NPC."""
    return Entity(npc=name, npc_entity_id=entity_id, maxhp=maxhp)


def _damage_event(source: Entity, target: Entity, amount: int, threat: float,
                  timestamp: time) -> LogEvent:
    """Build a synthetic damage event with a threat value, matching what
    the parser would produce from a real log line."""
    detail = NamedThing(name="Damage", id="dmg")
    result = DamageResult(
        amount=amount, is_crit=False, overheal=None, dmg_type="energy",
        result="hit", absorbed=None, threat=threat,
    )
    ability = NamedThing(name="Test Strike", id="ab-1")
    return LogEvent(
        timestamp=timestamp,
        source=source, target=target,
        ability=ability,
        effect_type="ApplyEffect", effect_name="Damage", effect_id="eid",
        effect_detail=detail,
        result=result,
    )


def _enter_combat_event(timestamp: time) -> LogEvent:
    """Build a synthetic enter-combat event."""
    return LogEvent(
        timestamp=timestamp,
        source=Entity(is_empty=True), target=Entity(is_empty=True),
        ability=None,
        effect_type="Event", effect_name="EnterCombat", effect_id="ec",
        effect_detail=None,
    )


# ── Pure helpers ──────────────────────────────────────────────────────────────

class RateFromSamplesTests(unittest.TestCase):
    def test_returns_zero_for_empty_samples(self):
        self.assertEqual(_rate_from_samples([]), 0.0)

    def test_returns_zero_for_one_sample(self):
        self.assertEqual(_rate_from_samples([(_ts(0), 100.0)]), 0.0)

    def test_returns_zero_for_zero_time_span(self):
        # Two samples at the same timestamp — rate undefined, must be 0.
        self.assertEqual(
            _rate_from_samples([(_ts(0), 100.0), (_ts(0), 200.0)]),
            0.0,
        )

    def test_basic_positive_rate(self):
        # 100 threat over 5 seconds = 20 threat/sec.
        rate = _rate_from_samples([(_ts(0), 0.0), (_ts(5), 100.0)])
        self.assertAlmostEqual(rate, 20.0)

    def test_uses_first_and_last_only(self):
        # Intermediate samples don't change the result (rate uses
        # earliest and latest values only).
        rate = _rate_from_samples([
            (_ts(0), 0.0),
            (_ts(1), 50.0),
            (_ts(2), 60.0),  # spike that doesn't matter
            (_ts(5), 100.0),
        ])
        self.assertAlmostEqual(rate, 20.0)


class TimeToZeroTests(unittest.TestCase):
    def test_none_when_gap_already_zero_or_negative(self):
        self.assertIsNone(_time_to_zero(0.0, -10.0, cap=60.0))
        self.assertIsNone(_time_to_zero(-5.0, -10.0, cap=60.0))

    def test_none_when_rate_zero(self):
        self.assertIsNone(_time_to_zero(100.0, 0.0, cap=60.0))

    def test_none_when_rate_positive(self):
        # Positive rate = gap is widening, no overtake predicted.
        self.assertIsNone(_time_to_zero(100.0, 10.0, cap=60.0))

    def test_none_when_prediction_exceeds_cap(self):
        # gap=1000, rate=-10 → time = 100s, beyond cap of 60.
        self.assertIsNone(_time_to_zero(1000.0, -10.0, cap=60.0))

    def test_basic_prediction(self):
        # gap=100, rate=-20 → time = 5s.
        self.assertAlmostEqual(_time_to_zero(100.0, -20.0, cap=60.0), 5.0)

    def test_at_cap_boundary(self):
        # gap=120, rate=-2 → time = 60s, exactly at cap → returns 60s.
        self.assertAlmostEqual(_time_to_zero(120.0, -2.0, cap=60.0), 60.0)


# ── Integration through the tracker ───────────────────────────────────────────

class ThreatPanelSnapshotTests(unittest.TestCase):
    def test_empty_when_no_npcs_engaged(self):
        tracker = LiveFightTracker()
        tracker.push([_enter_combat_event(_ts(0))])
        self.assertEqual(tracker.threat_panel_snapshot(), [])

    def test_single_player_single_npc_basics(self):
        """One player attacking one NPC. The 'tank' identity heuristic
        picks that player (highest cumulative threat = only player).
        DPS gap is zero (you ARE the tank in this case)."""
        tracker = LiveFightTracker()
        tracker.push([_enter_combat_event(_ts(0))])
        you = _player_entity("Jolly")
        boss = _npc_entity("Boss")
        # Three damage events spaced over 4 seconds, threat=100 each.
        tracker.push([_damage_event(you, boss, 50, 100.0, _ts(1))])
        tracker.push([_damage_event(you, boss, 50, 100.0, _ts(3))])
        tracker.push([_damage_event(you, boss, 50, 100.0, _ts(5))])

        rows = tracker.threat_panel_snapshot()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["name"], "Boss")
        self.assertEqual(row["tank_name"], "Jolly")  # heuristic picks the only player
        # Player is the tank in this case: your_threat == tank_threat.
        self.assertAlmostEqual(row["your_threat"], 300.0)
        self.assertAlmostEqual(row["tank_threat"], 300.0)
        # No second-place player.
        self.assertEqual(row["second_name"], "")
        self.assertAlmostEqual(row["second_threat"], 0.0)
        # DPS gap = 0; not a useful prediction. time_left = None.
        self.assertAlmostEqual(row["dps_gap"], 0.0)
        self.assertIsNone(row["dps_time_left"])

    def test_tank_identity_is_player_with_highest_cumulative_threat(self):
        """Tank identity heuristic: the player with the most threat
        across all engaged NPCs wins, regardless of which NPC."""
        tracker = LiveFightTracker()
        tracker.push([_enter_combat_event(_ts(0))])
        tank = _player_entity("Tank")
        dps  = _player_entity("DPS")
        boss = _npc_entity("Boss", entity_id="boss-1")

        # Tank generates 1000 threat over the fight; DPS generates 600.
        tracker.push([_damage_event(tank, boss, 100, 1000.0, _ts(1))])
        tracker.push([_damage_event(dps,  boss, 100, 600.0,  _ts(2))])

        rows = tracker.threat_panel_snapshot()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tank_name"], "Tank")
        self.assertEqual(rows[0]["second_name"], "DPS")
        self.assertAlmostEqual(rows[0]["tank_threat"], 1000.0)
        self.assertAlmostEqual(rows[0]["second_threat"], 600.0)
        # Tank gap = tank - second = 400.
        self.assertAlmostEqual(rows[0]["tank_gap"], 400.0)

    def test_dps_gap_and_closing_rate(self):
        """DPS perspective: tank is ahead but DPS is gaining faster.
        Gap should be positive, closing rate negative, time_left finite."""
        tracker = LiveFightTracker()
        # Pin the local player identity. In real play, this is discovered
        # from the first event referencing the local character. In tests
        # we set it explicitly so the snapshot's "your_*" fields read
        # from the player we mean.
        tracker.player_name = "Jolly"
        tracker.push([_enter_combat_event(_ts(0))])
        tank = _player_entity("Tank")
        you  = _player_entity("Jolly")
        boss = _npc_entity("Boss")

        # Setup: tank gets 500 threat upfront so it's ahead.
        tracker.push([_damage_event(tank, boss, 100, 500.0, _ts(1))])
        tracker.push([_damage_event(you,  boss, 100, 200.0, _ts(1))])

        # Now over the next 4 seconds:
        # Tank: 50 threat/sec  (50 at t=2, 50 at t=3, 50 at t=4, 50 at t=5)
        # You:  100 threat/sec (100 at t=2, 100 at t=3, 100 at t=4, 100 at t=5)
        # You are gaining 50 threat/sec faster than the tank.
        # Gap at t=5: tank=700, you=600 → gap=100.
        # Closing rate of gap = tank_rate - your_rate = 50 - 100 = -50 thr/sec.
        # Predicted overtake: 100 / 50 = 2 seconds.
        for t_sec in (2, 3, 4, 5):
            tracker.push([_damage_event(tank, boss, 50, 50.0,  _ts(t_sec))])
            tracker.push([_damage_event(you,  boss, 50, 100.0, _ts(t_sec))])

        rows = tracker.threat_panel_snapshot()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["tank_name"], "Tank")
        self.assertAlmostEqual(row["tank_threat"], 700.0)
        self.assertAlmostEqual(row["your_threat"], 600.0)
        self.assertAlmostEqual(row["dps_gap"], 100.0)
        # Rate signs: tank_rate ~50/sec, your_rate ~100/sec, closing_rate
        # ~ -50/sec. Allow some looseness because the rolling window
        # bounds and sample count depend on event spacing.
        self.assertLess(row["dps_closing_rate"], 0.0,
                        "DPS should be closing on tank (negative rate)")
        # Predicted time-to-overtake should be a small positive number
        # in single-digit seconds (the test math says exactly 2s,
        # tolerate some drift from rolling-window edge effects).
        self.assertIsNotNone(row["dps_time_left"])
        self.assertGreater(row["dps_time_left"], 0.5)
        self.assertLess(row["dps_time_left"], 10.0)

    def test_time_left_none_when_dps_is_falling_behind(self):
        """If the DPS is generating threat slower than the tank, the
        gap widens and there's no overtake prediction."""
        tracker = LiveFightTracker()
        tracker.player_name = "Jolly"
        tracker.push([_enter_combat_event(_ts(0))])
        tank = _player_entity("Tank")
        you  = _player_entity("Jolly")
        boss = _npc_entity("Boss")

        tracker.push([_damage_event(tank, boss, 100, 200.0, _ts(1))])
        tracker.push([_damage_event(you,  boss, 100, 100.0, _ts(1))])
        # Tank gains 100/sec; you gain 50/sec — gap widens.
        for t_sec in (2, 3, 4):
            tracker.push([_damage_event(tank, boss, 50, 100.0, _ts(t_sec))])
            tracker.push([_damage_event(you,  boss, 50, 50.0,  _ts(t_sec))])

        rows = tracker.threat_panel_snapshot()
        row = rows[0]
        self.assertGreater(row["dps_gap"], 0.0)
        # tank_rate > your_rate → closing_rate = tank_rate - your_rate > 0
        # → gap is widening → no overtake predicted.
        self.assertGreaterEqual(row["dps_closing_rate"], 0.0)
        self.assertIsNone(row["dps_time_left"])

    def test_multiple_npcs_sorted_by_danger_score(self):
        """Two NPCs, one with a tight gap and one with a wide gap. The
        tight-gap NPC should be at the top of the sorted list (highest
        danger)."""
        tracker = LiveFightTracker()
        tracker.player_name = "Jolly"
        tracker.push([_enter_combat_event(_ts(0))])
        tank = _player_entity("Tank")
        you  = _player_entity("Jolly")
        boss_a = _npc_entity("BossA", entity_id="a")
        boss_b = _npc_entity("BossB", entity_id="b")

        # On Boss A: tight gap (50).
        tracker.push([_damage_event(tank, boss_a, 100, 1000.0, _ts(1))])
        tracker.push([_damage_event(you,  boss_a, 100, 950.0,  _ts(1))])

        # On Boss B: wide gap (500).
        tracker.push([_damage_event(tank, boss_b, 100, 1000.0, _ts(1))])
        tracker.push([_damage_event(you,  boss_b, 100, 500.0,  _ts(1))])

        rows = tracker.threat_panel_snapshot()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["name"], "BossA",
                         "Smallest-gap NPC should be at the top")
        self.assertEqual(rows[1]["name"], "BossB")

    def test_dead_npcs_excluded(self):
        """An NPC marked is_dead should not appear in the snapshot."""
        tracker = LiveFightTracker()
        tracker.push([_enter_combat_event(_ts(0))])
        you = _player_entity("Jolly")
        boss = _npc_entity("Boss", maxhp=100)
        tracker.push([_damage_event(you, boss, 50, 100.0, _ts(1))])
        # Mark it dead by pushing a damage event where target has hp=0.
        dead_npc = Entity(npc="Boss", npc_entity_id="npc-1", maxhp=100, hp=0)
        tracker.push([_damage_event(you, dead_npc, 50, 100.0, _ts(2))])

        rows = tracker.threat_panel_snapshot()
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
