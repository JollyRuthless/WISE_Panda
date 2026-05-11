import unittest
from pathlib import Path

from engine.aggregator import (
    attach_log_ranges,
    BOSS_FIGHT_HP_THRESHOLD,
    BOSS_DOMINANT_DAMAGE_SHARE,
    BOSS_HP_RATIO_THRESHOLD,
    CURRENT_FIGHT_LABEL,
    EntityKind,
    Fight,
    aggregate_fight,
    build_fights,
    build_mob_damage_breakdown,
    choose_encounter_name,
    elapsed_seconds,
    seconds_between,
    summarize_encounter,
)
from engine.parser import parse_file, parse_line
from ui.live.tracker import LiveFightTracker


def _event(line: str):
    ev = parse_line(line)
    if ev is None:
        raise AssertionError(f"Failed to parse test line: {line}")
    return ev


class MobBreakdownTests(unittest.TestCase):
    def setUp(self):
        self.fight = Fight(index=1, start_time=_event(
            "[10:00:00.000] "
            "[@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
            "[Training Droid {100}:1|(1.00,1.00,1.00,0.00)|(800/1000)] "
            "[Slash {1}] [ApplyEffect {2}: Damage {3}] (200* energy {4})"
        ).timestamp, player_name="Alice")
        self.fight.events = [
            _event(
                "[10:00:00.000] "
                "[@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[Training Droid {100}:1|(1.00,1.00,1.00,0.00)|(800/1000)] "
                "[Slash {1}] [ApplyEffect {2}: Damage {3}] (200* energy {4})"
            ),
            _event(
                "[10:00:01.000] "
                "[@Bob#2|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[Training Droid {100}:1|(1.00,1.00,1.00,0.00)|(500/1000)] "
                "[Shot {5}] [ApplyEffect {2}: Damage {3}] (100 energy {4})"
            ),
            _event(
                "[10:00:02.000] "
                "[@Alice#1/K1-Z3N {200}:9|(0.00,0.00,0.00,0.00)|(700/700)] "
                "[Training Droid {100}:2|(2.00,2.00,2.00,0.00)|(650/1000)] "
                "[Companion Strike {6}] [ApplyEffect {2}: Damage {3}] (50 kinetic {7})"
            ),
            _event(
                "[10:00:03.000] "
                "[@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[Training Droid {100}:2|(2.00,2.00,2.00,0.00)|(500/1000)] "
                "[Slash {1}] [ApplyEffect {2}: Damage {3}] (150 energy {4})"
            ),
            _event(
                "[10:00:04.000] "
                "[@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[Training Droid {100}:1|(1.00,1.00,1.00,0.00)|(0/1000)] "
                "[Finisher {8}] [ApplyEffect {2}: Damage {3}] (50 energy {4})"
            ),
        ]

    def test_builds_grouped_mob_contribution_rows(self):
        rows = build_mob_damage_breakdown(self.fight)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["mob_name"], "Training Droid")
        self.assertEqual(row["npc_entity_id"], "100")
        self.assertEqual(row["instances_seen"], 2)
        self.assertEqual(row["defeats"], 1)
        self.assertEqual(row["total_damage_taken"], 550)
        self.assertEqual(row["top_contributor"], "Alice")
        self.assertAlmostEqual(row["top_share"], 400 / 550)

        contributors = {item["name"]: item for item in row["contributors"]}
        self.assertEqual(contributors["Alice"]["damage"], 400)
        self.assertEqual(contributors["Alice"]["hits"], 3)
        self.assertEqual(contributors["Alice"]["crits"], 1)
        self.assertEqual(contributors["Bob"]["damage"], 100)
        self.assertEqual(contributors["Alice/K1-Z3N"]["damage"], 50)

    def test_can_hide_companion_contributions(self):
        rows = build_mob_damage_breakdown(self.fight, hide_companions=True)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["total_damage_taken"], 500)
        self.assertEqual([item["name"] for item in row["contributors"]], ["Alice", "Bob"])


class EncounterNamingTests(unittest.TestCase):
    def test_attach_log_ranges_makes_live_built_fights_file_backed(self):
        log_text = "\n".join([
            "[10:00:00.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] [] [] "
            "[Event {1}: EnterCombat {2}]",
            "[10:00:01.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
            "[Training Droid {100}:1|(1.00,1.00,1.00,0.00)|(800/1000)] [Slash {3}] "
            "[ApplyEffect {4}: Damage {5}] (200 energy {6})",
            "[10:00:02.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] [] [] "
            "[Event {1}: ExitCombat {7}]",
        ])
        log_path = Path(__file__).parent / "_test_live_ranges.txt"
        try:
            log_path.write_text(log_text, encoding="utf-8")
            events, errors = parse_file(str(log_path))
            self.assertEqual(errors, 0)
            fights = build_fights(events)
            attach_log_ranges(fights, str(log_path))
        finally:
            try:
                log_path.unlink()
            except OSError:
                pass

        self.assertEqual(len(fights), 1)
        self.assertEqual(fights[0]._log_path, str(log_path))
        self.assertEqual(fights[0]._line_start, 0)
        self.assertEqual(fights[0]._line_end, 2)

    def test_choose_encounter_name_prefers_actual_combatant_over_target_only_boss(self):
        events = [
            _event(
                "[10:00:00.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] [] [] "
                "[Event {1}: EnterCombat {2}]"
            ),
            _event(
                "[10:00:01.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[Boss {900}:1|(10.00,10.00,10.00,0.00)|(50000/50000)] [] "
                "[Event {1}: TargetSet {3}]"
            ),
            _event(
                "[10:00:02.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[Add {100}:1|(1.00,1.00,1.00,0.00)|(800/1000)] [Slash {1}] "
                "[ApplyEffect {2}: Damage {3}] (200 energy {4})"
            ),
        ]

        self.assertEqual(choose_encounter_name(events, "Alice"), "Add")

    def test_live_fight_starts_as_current_fight_until_npc_participates(self):
        events = [
            _event(
                "[10:00:00.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] [] [] "
                "[Event {1}: EnterCombat {2}]"
            ),
            _event(
                "[10:00:00.500] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] [=] [Sprint {5}] "
                "[RemoveEffect {6}: Sprint {5}]"
            ),
        ]

        fights = build_fights(events)
        self.assertEqual(len(fights), 1)
        self.assertEqual(fights[0].boss_name, CURRENT_FIGHT_LABEL)

    def test_blank_source_damage_is_bucketed_as_hazard_not_npc(self):
        events = [
            _event(
                "[10:00:00.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] [] [] "
                "[Event {1}: EnterCombat {2}]"
            ),
            _event(
                "[10:00:01.000] [] [@Alice#1|(0.00,0.00,0.00,0.00)|(700/1000)] [Burning Floor {9}] "
                "[ApplyEffect {2}: Damage {3}] (300 elemental {4})"
            ),
            _event(
                "[10:00:02.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(700/1000)] [] [] "
                "[Event {1}: ExitCombat {5}]"
            ),
        ]

        fights = build_fights(events)
        self.assertEqual(len(fights), 1)
        fight = fights[0]
        self.assertIn("Environment", fight.entity_stats)
        self.assertEqual(fight.entity_stats["Environment"].kind, EntityKind.HAZARD)
        self.assertEqual(fight.entity_stats["Environment"].damage_dealt, 300)
        self.assertEqual(fight.entity_stats["Alice"].damage_taken, 300)
        self.assertEqual(fight.boss_name, CURRENT_FIGHT_LABEL)

    def test_summarize_encounter_tracks_top_npc_max_hp(self):
        events = [
            _event(
                "[10:00:00.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[Boss {900}:1|(10.00,10.00,10.00,0.00)|(150000/150000)] [Slash {1}] "
                "[ApplyEffect {2}: Damage {3}] (200 energy {4})"
            ),
            _event(
                "[10:00:01.000] [Boss {900}:1|(10.00,10.00,10.00,0.00)|(149000/150000)] "
                "[@Alice#1|(0.00,0.00,0.00,0.00)|(800/1000)] [Crush {7}] "
                "[ApplyEffect {2}: Damage {3}] (200 kinetic {8})"
            ),
        ]

        summary = summarize_encounter(events, "Alice")
        self.assertIsNotNone(summary)
        self.assertEqual(summary["name"], "Boss")
        self.assertEqual(summary["max_hp"], 150000)
        self.assertEqual(summary["damage_share"], 1.0)
        self.assertEqual(summary["hp_ratio"], float("inf"))


class LiveTrackerMetricTests(unittest.TestCase):
    def test_live_encounter_dps_matches_fight_dps_for_current_fight(self):
        events = [
            _event(
                "[10:00:00.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] [] [] "
                "[Event {1}: EnterCombat {2}]"
            ),
            _event(
                "[10:00:01.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[Training Droid {100}:1|(1.00,1.00,1.00,0.00)|(900/1000)] [Slash {3}] "
                "[ApplyEffect {4}: Damage {5}] (120 energy {6})"
            ),
            _event(
                "[10:00:04.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[Training Droid {100}:1|(1.00,1.00,1.00,0.00)|(700/1000)] [Strike {7}] "
                "[ApplyEffect {4}: Damage {5}] (180 energy {6})"
            ),
        ]

        tracker = LiveFightTracker()
        tracker.push(events)
        live_rows = tracker.snapshot(metric="encounter")
        self.assertEqual(len(live_rows), 1)

        fights = build_fights(events)
        self.assertEqual(len(fights), 1)
        fight = fights[0]

        self.assertAlmostEqual(live_rows[0]["encounter_dps"], fight.dps("Alice"))
        self.assertAlmostEqual(tracker.elapsed, fight.duration_seconds)

    def test_boss_like_flag_uses_dominant_damage_share_and_hp_lead(self):
        events = [
            _event(
                "[10:00:00.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] [] [] "
                "[Event {1}: EnterCombat {2}]"
            ),
            _event(
                f"[10:00:01.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                f"[Boss {{900}}:1|(10.00,10.00,10.00,0.00)|({BOSS_FIGHT_HP_THRESHOLD}/{BOSS_FIGHT_HP_THRESHOLD})] "
                "[Slash {1}] [ApplyEffect {2}: Damage {3}] (200 energy {4})"
            ),
            _event(
                "[10:00:11.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[Boss {900}:1|(10.00,10.00,10.00,0.00)|(99800/100000)] [Slash {1}] "
                "[ApplyEffect {2}: Damage {3}] (200 energy {4})"
            ),
            _event(
                "[10:00:21.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[Boss {900}:1|(10.00,10.00,10.00,0.00)|(99600/100000)] [Slash {1}] "
                "[ApplyEffect {2}: Damage {3}] (200 energy {4})"
            ),
            _event(
                "[10:00:31.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[Boss {900}:1|(10.00,10.00,10.00,0.00)|(99400/100000)] [Slash {1}] "
                "[ApplyEffect {2}: Damage {3}] (200 energy {4})"
            ),
            _event(
                "[10:00:41.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[Boss {900}:1|(10.00,10.00,10.00,0.00)|(99200/100000)] [Slash {1}] "
                "[ApplyEffect {2}: Damage {3}] (200 energy {4})"
            ),
            _event(
                "[10:00:51.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[Boss {900}:1|(10.00,10.00,10.00,0.00)|(99000/100000)] [Slash {1}] "
                "[ApplyEffect {2}: Damage {3}] (200 energy {4})"
            ),
            _event(
                "[10:01:01.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[Boss {900}:1|(10.00,10.00,10.00,0.00)|(98800/100000)] [Slash {1}] "
                "[ApplyEffect {2}: Damage {3}] (200 energy {4})"
            ),
        ]

        fights = build_fights(events)
        self.assertEqual(len(fights), 1)
        self.assertEqual(fights[0].boss_name, "Boss")
        self.assertEqual(fights[0].boss_max_hp, BOSS_FIGHT_HP_THRESHOLD)
        self.assertGreaterEqual(fights[0].boss_damage_share, BOSS_DOMINANT_DAMAGE_SHARE)
        self.assertGreaterEqual(fights[0].boss_hp_ratio, BOSS_HP_RATIO_THRESHOLD)
        self.assertTrue(fights[0].is_boss_like)

    def test_short_dominant_fight_is_not_boss_like(self):
        events = [
            _event(
                "[10:00:00.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] [] [] "
                "[Event {1}: EnterCombat {2}]"
            ),
            _event(
                "[10:00:01.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[Boss {900}:1|(10.00,10.00,10.00,0.00)|(200000/200000)] [Slash {1}] "
                "[ApplyEffect {2}: Damage {3}] (200 energy {4})"
            ),
            _event(
                "[10:00:10.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] [] [] "
                "[Event {1}: ExitCombat {5}]"
            ),
        ]

        fights = build_fights(events)
        self.assertEqual(len(fights), 1)
        self.assertEqual(fights[0].boss_name, "Boss")
        self.assertFalse(fights[0].is_boss_like)

    def test_high_hp_operation_trash_is_not_boss_like_without_dominant_target(self):
        events = [
            _event(
                "[10:00:00.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] [] [] "
                "[Event {1}: EnterCombat {2}]"
            ),
            _event(
                "[10:00:01.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[Strong Add {100}:1|(1.00,1.00,1.00,0.00)|(400000/400000)] [Slash {1}] "
                "[ApplyEffect {2}: Damage {3}] (300 energy {4})"
            ),
            _event(
                "[10:00:01.500] [@Bob#2|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[Strong Add {100}:1|(1.00,1.00,1.00,0.00)|(399700/400000)] [Shot {5}] "
                "[ApplyEffect {2}: Damage {3}] (100 energy {4})"
            ),
            _event(
                "[10:00:02.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[Second Add {200}:1|(2.00,2.00,2.00,0.00)|(350000/350000)] [Slash {1}] "
                "[ApplyEffect {2}: Damage {3}] (260 energy {4})"
            ),
            _event(
                "[10:00:02.500] [@Bob#2|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[Second Add {200}:1|(2.00,2.00,2.00,0.00)|(349740/350000)] [Shot {5}] "
                "[ApplyEffect {2}: Damage {3}] (140 energy {4})"
            ),
            _event(
                "[10:00:03.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] [] [] "
                "[Event {1}: ExitCombat {5}]"
            ),
        ]

        fights = build_fights(events)
        self.assertEqual(len(fights), 1)
        self.assertEqual(fights[0].boss_name, "Strong Add")
        self.assertEqual(fights[0].boss_max_hp, 400000)
        self.assertAlmostEqual(fights[0].boss_damage_share, 0.5)
        self.assertLess(fights[0].boss_hp_ratio, BOSS_HP_RATIO_THRESHOLD)
        self.assertFalse(fights[0].is_boss_like)


class AlternateDpsTests(unittest.TestCase):
    def test_time_helpers_wrap_across_midnight(self):
        start = _event(
            "[23:59:59.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] [] [] "
            "[Event {1}: EnterCombat {2}]"
        ).timestamp
        end = _event(
            "[00:00:01.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] [] [] "
            "[Event {1}: ExitCombat {3}]"
        ).timestamp

        self.assertAlmostEqual(seconds_between(start, end), 2.0)
        self.assertAlmostEqual(elapsed_seconds(start, end), 2.0)

    def test_midnight_fight_keeps_positive_duration_and_offsets(self):
        fight = Fight(index=1, start_time=_event(
            "[23:59:59.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
            "[Boss {900}:1|(10.00,10.00,10.00,0.00)|(100000/100000)] [Slash {1}] "
            "[ApplyEffect {2}: Damage {3}] (100 energy {4})"
        ).timestamp, player_name="Alice")
        fight.end_time = _event(
            "[00:00:01.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
            "[Boss {900}:1|(10.00,10.00,10.00,0.00)|(99800/100000)] [Slash {1}] "
            "[ApplyEffect {2}: Damage {3}] (100 energy {4})"
        ).timestamp
        fight.events = [
            _event(
                "[23:59:59.500] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[Boss {900}:1|(10.00,10.00,10.00,0.00)|(100000/100000)] [Slash {1}] "
                "[ApplyEffect {2}: Damage {3}] (100 energy {4})"
            ),
            _event(
                "[00:00:00.500] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[Boss {900}:1|(10.00,10.00,10.00,0.00)|(99900/100000)] [Slash {1}] "
                "[ApplyEffect {2}: Damage {3}] (100 energy {4})"
            ),
        ]
        fight.boss_name = "Boss"
        aggregate_fight(fight)

        self.assertAlmostEqual(fight.duration_seconds, 2.0)
        self.assertEqual(fight.entity_stats["Alice"].damage_timeline, [(0.5, 100), (1.5, 100)])
        self.assertAlmostEqual(fight.dps("Alice"), 100.0)

    def test_active_dps_uses_only_damage_window(self):
        fight = Fight(index=1, start_time=_event(
            "[10:00:00.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
            "[Boss {900}:1|(10.00,10.00,10.00,0.00)|(100000/100000)] [Slash {1}] "
            "[ApplyEffect {2}: Damage {3}] (100 energy {4})"
        ).timestamp, player_name="Alice")
        fight.end_time = _event(
            "[10:00:20.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
            "[Boss {900}:1|(10.00,10.00,10.00,0.00)|(99800/100000)] [Slash {1}] "
            "[ApplyEffect {2}: Damage {3}] (100 energy {4})"
        ).timestamp
        fight.events = [
            _event(
                "[10:00:05.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[Boss {900}:1|(10.00,10.00,10.00,0.00)|(100000/100000)] [Slash {1}] "
                "[ApplyEffect {2}: Damage {3}] (100 energy {4})"
            ),
            _event(
                "[10:00:15.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[Boss {900}:1|(10.00,10.00,10.00,0.00)|(99900/100000)] [Slash {1}] "
                "[ApplyEffect {2}: Damage {3}] (100 energy {4})"
            ),
        ]
        fight.boss_name = "Boss"
        aggregate_fight(fight)

        self.assertAlmostEqual(fight.dps("Alice"), 10.0)
        self.assertAlmostEqual(fight.active_dps("Alice"), 20.0)

    def test_boss_dps_counts_only_damage_to_primary_boss(self):
        fight = Fight(index=1, start_time=_event(
            "[10:00:00.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
            "[Boss {900}:1|(10.00,10.00,10.00,0.00)|(100000/100000)] [Slash {1}] "
            "[ApplyEffect {2}: Damage {3}] (100 energy {4})"
        ).timestamp, player_name="Alice")
        fight.end_time = _event(
            "[10:00:10.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
            "[Boss {900}:1|(10.00,10.00,10.00,0.00)|(99600/100000)] [Slash {1}] "
            "[ApplyEffect {2}: Damage {3}] (100 energy {4})"
        ).timestamp
        fight.events = [
            _event(
                "[10:00:01.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[Boss {900}:1|(10.00,10.00,10.00,0.00)|(100000/100000)] [Slash {1}] "
                "[ApplyEffect {2}: Damage {3}] (200 energy {4})"
            ),
            _event(
                "[10:00:02.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[Add {100}:1|(1.00,1.00,1.00,0.00)|(1000/1000)] [Slash {1}] "
                "[ApplyEffect {2}: Damage {3}] (300 energy {4})"
            ),
        ]
        aggregate_fight(fight)
        fight.boss_name = "Boss"

        self.assertAlmostEqual(fight.dps("Alice"), 50.0)
        self.assertAlmostEqual(fight.boss_dps("Alice"), 20.0)

    def test_short_fight_dps_and_hps_use_minimum_display_window(self):
        fight = Fight(index=1, start_time=_event(
            "[10:00:00.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
            "[Boss {900}:1|(10.00,10.00,10.00,0.00)|(100000/100000)] [Slash {1}] "
            "[ApplyEffect {2}: Damage {3}] (50000 energy {4})"
        ).timestamp, player_name="Alice")
        fight.end_time = fight.start_time
        fight.events = [
            _event(
                "[10:00:00.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[Boss {900}:1|(10.00,10.00,10.00,0.00)|(100000/100000)] [Slash {1}] "
                "[ApplyEffect {2}: Damage {3}] (50000 energy {4})"
            ),
            _event(
                "[10:00:00.000] [@Alice#1|(0.00,0.00,0.00,0.00)|(1000/1000)] "
                "[@Bob#2|(0.00,0.00,0.00,0.00)|(1000/1000)] [Heal {5}] "
                "[ApplyEffect {2}: Heal {6}] (12000 {7})"
            ),
        ]
        fight.boss_name = "Boss"
        aggregate_fight(fight)

        self.assertAlmostEqual(fight.duration_seconds, 0.001)
        self.assertAlmostEqual(fight.display_duration_seconds, 1.0)
        self.assertAlmostEqual(fight.dps("Alice"), 50000.0)
        self.assertAlmostEqual(fight.active_dps("Alice"), 50000.0)


if __name__ == "__main__":
    unittest.main()
