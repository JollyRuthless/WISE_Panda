"""
Tests for cohort.py — the cohort/history query layer.

Strategy: spin up a fresh temp SQLite that mirrors the real schema, populate
it with hand-built fixtures that cover the cases we care about, and assert
that the public query functions return what we expect.

We don't import the production DB. We monkeypatch DB_PATH on encounter_db
(which cohort reuses via _connect_db) to point at the temp DB.
"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


# We import cohort lazily (after the patch) so its module-level imports
# resolve _connect_db against the patched DB_PATH the first time it's used.
import storage.encounter_db as encounter_db
import storage.cohort as cohort
# ─── Schema fixture (copied minimally from encounter_db.init_db) ─────────────
#
# We copy only the tables cohort.py reads from. Keeping this list short means
# a schema change in encounter_db that affects cohort will surface as a test
# failure here, not silently.

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
        PRIMARY KEY (encounter_key, character_id)
    )
    """,
    """
    CREATE TABLE player_character_encounter_abilities (
        encounter_key TEXT NOT NULL,
        character_id INTEGER NOT NULL,
        ability_name TEXT NOT NULL,
        ability_id TEXT NOT NULL DEFAULT '',
        use_count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(encounter_key, character_id, ability_id, ability_name)
    )
    """,
]


def _build_temp_schema(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        for stmt in SCHEMA_SQL:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def _make_key(log_path: str, line_start: int, line_end: int, ts: str) -> str:
    """Mirror encounter_db.encounter_key_for() format."""
    return f"{log_path}|{line_start}|{line_end}|{ts}"


class CohortTestCase(unittest.TestCase):
    """Base: build a temp DB, redirect cohort to it, populate fixtures."""

    def setUp(self) -> None:
        # ignore_cleanup_errors=True so Windows doesn't choke on lingering
        # SQLite file handles when the temp dir is removed.
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "test.sqlite3"
        _build_temp_schema(self.db_path)

        # Redirect the production helper to our temp DB. cohort.py uses
        # encounter_db._connect_db, which reads DB_PATH each call.
        self._db_path_patch = mock.patch.object(encounter_db, "DB_PATH", self.db_path)
        self._db_path_patch.start()

        self._populate()

    def tearDown(self) -> None:
        self._db_path_patch.stop()
        # Force a GC pass so any dangling sqlite3.Connection objects release
        # their OS file handles before TemporaryDirectory tries to delete them.
        # On Windows, an open handle blocks file deletion.
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    # Fixture data ────────────────────────────────────────────────────────────

    def _populate(self) -> None:
        """
        Populate a small but realistic set of fights:
          - 2 bosses (Apex Vanguard, Kanoth) on multiple nights
          - 4 characters: 2 Mercs (one is the user), 1 Operative, 1 Juggernaut
          - One fight has 2 Mercs (same-class peer present)
          - Most fights have just one Merc
          - Each fight has ability counts so cohort_benchmark can aggregate
        """
        today = date.today()
        d_today = today.isoformat()
        d_yest = (today - timedelta(days=1)).isoformat()
        d_old = (today - timedelta(days=120)).isoformat()  # outside default windows

        with sqlite3.connect(str(self.db_path)) as conn:
            # Characters
            chars = [
                ("Karzag",  "Mercenary",  d_old,  d_today),  # the user
                ("Vossan",  "Mercenary",  d_yest, d_today),  # peer Merc
                ("Tyrven",  "Operative",  d_old,  d_today),  # healer
                ("Daskar",  "Juggernaut", d_old,  d_today),  # tank
            ]
            for name, klass, first, last in chars:
                conn.execute(
                    "INSERT INTO player_characters "
                    "(character_name, class_name, first_seen_date, last_seen_date, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (name, klass, first, last, last),
                )

            # Map name -> id
            id_of = {
                row[1]: row[0] for row in
                conn.execute("SELECT character_id, character_name FROM player_characters")
            }

            # Fights. Each tuple: (encounter_key, name, date, log_path, recorded_by, participants)
            # participants: list of (character_name, damage, healing, taunts, interrupts, abilities_dict)
            fights = [
                # Tonight's Apex Vanguard — 2 Mercs! same-class peer present
                (
                    _make_key("/logs/2026-04-27.txt", 1000, 2500, "21:14:00"),
                    "Apex Vanguard", d_today, "/logs/2026-04-27.txt", "Karzag",
                    [
                        ("Karzag",  900_000, 0,       0, 1, {"Heatseeker Missiles": 6, "Tracer Missile": 18, "Rail Shot": 12}),
                        ("Vossan", 1_200_000, 0,       0, 2, {"Heatseeker Missiles": 9, "Tracer Missile": 22, "Rail Shot": 18}),
                        ("Tyrven",        0, 1_400_000, 0, 0, {"Kolto Probe": 14, "Surgical Probe": 30}),
                        ("Daskar",  450_000, 0,       8, 4, {"Smash": 18, "Force Charge": 4}),
                    ],
                ),
                # Yesterday's Apex Vanguard — only 1 Merc (Karzag, solo)
                (
                    _make_key("/logs/2026-04-26.txt", 800, 2200, "20:48:00"),
                    "Apex Vanguard", d_yest, "/logs/2026-04-26.txt", "Karzag",
                    [
                        ("Karzag",  850_000, 0,       0, 1, {"Heatseeker Missiles": 5, "Tracer Missile": 17, "Rail Shot": 10}),
                        ("Tyrven",        0, 1_350_000, 0, 0, {"Kolto Probe": 13, "Surgical Probe": 28}),
                        ("Daskar",  430_000, 0,       7, 3, {"Smash": 17, "Force Charge": 4}),
                    ],
                ),
                # Old Apex Vanguard fight — Vossan only. Outside the 30-day window.
                (
                    _make_key("/logs/old.txt", 100, 1500, "19:00:00"),
                    "Apex Vanguard", d_old, "/logs/old.txt", "Vossan",
                    [
                        ("Vossan", 1_100_000, 0, 0, 2, {"Heatseeker Missiles": 8, "Tracer Missile": 20, "Rail Shot": 16}),
                    ],
                ),
                # Tonight's Kanoth — only Karzag's Merc. No peers.
                (
                    _make_key("/logs/2026-04-27.txt", 3000, 4200, "21:55:00"),
                    "Kanoth", d_today, "/logs/2026-04-27.txt", "Karzag",
                    [
                        ("Karzag",  700_000, 0, 0, 0, {"Heatseeker Missiles": 4, "Tracer Missile": 14}),
                        ("Daskar",  380_000, 0, 6, 2, {"Smash": 14}),
                    ],
                ),
                # Trash fight (very short line range -> short estimated duration)
                (
                    _make_key("/logs/2026-04-27.txt", 5000, 5050, "22:01:00"),
                    "Trash Pull", d_today, "/logs/2026-04-27.txt", "Karzag",
                    [
                        ("Karzag", 50_000, 0, 0, 0, {"Tracer Missile": 3}),
                    ],
                ),
            ]

            for ekey, name, edate, log_path, recorded_by, participants in fights:
                conn.execute(
                    "INSERT INTO encounters "
                    "(encounter_key, encounter_name, encounter_date, log_path, recorded_by, "
                    " biggest_hit_amount, biggest_hit_by, biggest_hit_ability, "
                    " biggest_heal_amount, biggest_heal_by, biggest_heal_ability, "
                    " deaths_json, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 0, '', '', 0, '', '', '{}', ?)",
                    (ekey, name, edate, log_path, recorded_by, edate),
                )
                for char_name, dmg, heal, taunts, interrupts, abilities in participants:
                    cid = id_of[char_name]
                    conn.execute(
                        "INSERT INTO player_character_encounters "
                        "(encounter_key, character_id, encounter_date, damage_done, healing_done, taunts, interrupts) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (ekey, cid, edate, dmg, heal, taunts, interrupts),
                    )
                    for ab_name, count in abilities.items():
                        conn.execute(
                            "INSERT INTO player_character_encounter_abilities "
                            "(encounter_key, character_id, ability_name, ability_id, use_count) "
                            "VALUES (?, ?, ?, '', ?)",
                            (ekey, cid, ab_name, count),
                        )
            conn.commit()


# ─── Tests ────────────────────────────────────────────────────────────────────


class ParseEncounterKeyTests(unittest.TestCase):
    """Pure parsing — no DB, no fixtures."""

    def test_well_formed_key_round_trips(self):
        key = "/path/to/log.txt|100|2500|21:14:00"
        log_path, ls, le, ts = cohort.parse_encounter_key(key)
        self.assertEqual(log_path, "/path/to/log.txt")
        self.assertEqual(ls, 100)
        self.assertEqual(le, 2500)
        self.assertEqual(ts, "21:14:00")

    def test_path_with_pipe_in_extra_fields_is_safe(self):
        # Extra "|" past the 4th field shouldn't break us. We only need the
        # first 4 fields. Real keys don't contain pipes in the path because
        # encounter_key_for builds them — but be defensive.
        key = "/path/with|pipe|0|0|21:14:00"
        log_path, ls, le, ts = cohort.parse_encounter_key(key)
        # First field is just up to first pipe — it's fine that extras are ignored.
        self.assertEqual(log_path, "/path/with")
        self.assertEqual(ls, 0)
        self.assertEqual(le, 0)

    def test_malformed_key_returns_sentinels_not_exception(self):
        log_path, ls, le, ts = cohort.parse_encounter_key("garbage")
        self.assertEqual((log_path, ls, le, ts), ("", 0, 0, ""))

    def test_non_integer_lines_dont_crash(self):
        log_path, ls, le, ts = cohort.parse_encounter_key("/log.txt|abc|xyz|t")
        self.assertEqual(ls, 0)
        self.assertEqual(le, 0)


class FindFightsTests(CohortTestCase):

    def test_no_filters_returns_all_fights_newest_first(self):
        results = cohort.find_fights(cohort.FightFilters())
        # 5 fights total in the fixture
        self.assertEqual(len(results), 5)
        # Newest dates first (today's fights before yesterday's before old)
        dates = [r.encounter_date for r in results]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_filter_by_encounter_name_substring(self):
        results = cohort.find_fights(cohort.FightFilters(encounter_name_contains="Apex"))
        self.assertEqual(len(results), 3)
        for r in results:
            self.assertIn("Apex", r.encounter_name)

    def test_filter_by_player_name(self):
        # Vossan only appears in 2 of the 5 fights
        results = cohort.find_fights(cohort.FightFilters(player_name_contains="Vossan"))
        self.assertEqual(len(results), 2)

    def test_filter_by_class(self):
        # Mercenary appears in 4 fights (3 Apex + 1 Kanoth + 1 trash, all have a Merc)
        # Wait — let me recount the fixture: Karzag is in 4 fights, Vossan in 2.
        # Total distinct fights with a Merc: Apex×3 + Kanoth×1 + Trash×1 = 5 fights.
        results = cohort.find_fights(cohort.FightFilters(class_name="Mercenary"))
        self.assertEqual(len(results), 5)

    def test_filter_by_class_returns_empty_for_unknown_class(self):
        results = cohort.find_fights(cohort.FightFilters(class_name="Sorcerer"))
        self.assertEqual(results, [])

    def test_require_same_class_peer_finds_only_2plus_class_fights(self):
        # Only the first Apex Vanguard has 2 Mercs in the same fight.
        results = cohort.find_fights(cohort.FightFilters(require_same_class_peer=True))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].encounter_name, "Apex Vanguard")

    def test_combined_filters_AND_together(self):
        # Apex Vanguard + Mercenary class + same-class-peer => only the tonight fight
        results = cohort.find_fights(cohort.FightFilters(
            encounter_name_contains="Apex",
            class_name="Mercenary",
            require_same_class_peer=True,
        ))
        self.assertEqual(len(results), 1)

    def test_date_range_filter(self):
        today = date.today().isoformat()
        results = cohort.find_fights(cohort.FightFilters(date_from=today, date_to=today))
        # 3 fights happened today: tonight's Apex, Kanoth, Trash
        self.assertEqual(len(results), 3)

    def test_min_duration_drops_short_fights(self):
        # Trash fight has line range 5000..5050 = 50 lines / 5 = 10s estimated.
        # Set min=30s, trash should be excluded.
        results = cohort.find_fights(cohort.FightFilters(min_duration_seconds=30))
        names = [r.encounter_name for r in results]
        self.assertNotIn("Trash Pull", names)

    def test_limit_caps_results(self):
        results = cohort.find_fights(cohort.FightFilters(limit=2))
        self.assertEqual(len(results), 2)

    def test_returned_fight_ref_has_parsed_line_range(self):
        results = cohort.find_fights(cohort.FightFilters(encounter_name_contains="Kanoth"))
        self.assertEqual(len(results), 1)
        ref = results[0]
        self.assertEqual(ref.line_start, 3000)
        self.assertEqual(ref.line_end, 4200)
        self.assertGreater(ref.duration_estimate, 0)

    def test_log_filename_property(self):
        results = cohort.find_fights(cohort.FightFilters(encounter_name_contains="Kanoth"))
        self.assertEqual(results[0].log_filename, "2026-04-27.txt")


class FindPlayerHistoryTests(CohortTestCase):

    def test_find_history_for_known_player(self):
        results = cohort.find_player_history("Karzag")
        # Karzag is in tonight's Apex, yesterday's Apex, Kanoth, Trash = 4 fights
        self.assertEqual(len(results), 4)

    def test_find_history_filtered_by_encounter(self):
        results = cohort.find_player_history("Karzag", encounter_name="Apex Vanguard")
        self.assertEqual(len(results), 2)
        for r in results:
            self.assertEqual(r.encounter_name, "Apex Vanguard")

    def test_unknown_player_returns_empty(self):
        results = cohort.find_player_history("Nobody")
        self.assertEqual(results, [])

    def test_player_match_is_case_insensitive(self):
        results = cohort.find_player_history("KARZAG")
        self.assertEqual(len(results), 4)


class ListParticipantsTests(CohortTestCase):

    def test_lists_all_players_in_a_fight(self):
        # First Apex Vanguard has Karzag, Vossan, Tyrven, Daskar
        refs = cohort.find_fights(cohort.FightFilters(
            encounter_name_contains="Apex",
            require_same_class_peer=True,
        ))
        self.assertEqual(len(refs), 1)
        participants = cohort.list_participants_in_fight(refs[0].encounter_key)
        names = sorted(p.character_name for p in participants)
        self.assertEqual(names, ["Daskar", "Karzag", "Tyrven", "Vossan"])

    def test_participants_sorted_by_damage_descending(self):
        refs = cohort.find_fights(cohort.FightFilters(
            encounter_name_contains="Apex",
            require_same_class_peer=True,
        ))
        participants = cohort.list_participants_in_fight(refs[0].encounter_key)
        damages = [p.damage_done for p in participants]
        self.assertEqual(damages, sorted(damages, reverse=True))


class BuildCohortTests(CohortTestCase):

    def test_cohort_for_mercenary_apex_vanguard(self):
        c = cohort.build_cohort("Mercenary", "Apex Vanguard")
        # Karzag (today + yesterday) + Vossan (today + old) = 4 (player, fight) pairs
        self.assertEqual(c.sample_size, 4)
        self.assertTrue(c.is_meaningful)
        for f in c.fights:
            self.assertEqual(f.class_name.lower(), "mercenary")

    def test_cohort_with_days_back_excludes_old_fights(self):
        # Vossan's old fight (120 days ago) should be excluded with days_back=30
        c = cohort.build_cohort("Mercenary", "Apex Vanguard", days_back=30)
        # Karzag today + Karzag yesterday + Vossan today = 3 (excludes old Vossan)
        self.assertEqual(c.sample_size, 3)

    def test_cohort_with_min_damage_drops_low_performances(self):
        c = cohort.build_cohort("Mercenary", "Apex Vanguard", min_damage=1_000_000)
        # Only Vossan's two fights cleared 1M damage
        self.assertEqual(c.sample_size, 2)
        for f in c.fights:
            self.assertEqual(f.character_name, "Vossan")

    def test_unknown_combo_returns_empty_cohort(self):
        c = cohort.build_cohort("Sorcerer", "Apex Vanguard")
        self.assertEqual(c.sample_size, 0)
        self.assertFalse(c.is_meaningful)


class CohortBenchmarkTests(CohortTestCase):

    def test_empty_cohort_returns_safe_profile(self):
        empty = cohort.Cohort(class_name="Sorcerer", encounter_name="Foo")
        b = cohort.cohort_benchmark(empty)
        self.assertEqual(b.sample_size, 0)
        self.assertFalse(b.is_meaningful)
        self.assertEqual(b.damage_done, 0.0)

    def test_median_benchmark(self):
        c = cohort.build_cohort("Mercenary", "Apex Vanguard")
        b = cohort.cohort_benchmark(c, mode="median")
        self.assertEqual(b.mode, "median")
        self.assertEqual(b.sample_size, 4)
        self.assertTrue(b.is_meaningful)
        # Damage values across the 4 (Merc, Apex Vanguard) pairs:
        #   Karzag today: 900_000, Vossan today: 1_200_000,
        #   Karzag yest:  850_000, Vossan old:  1_100_000
        # Sorted: 850k, 900k, 1.1M, 1.2M  → median = (900k + 1.1M)/2 = 1_000_000
        self.assertEqual(b.damage_done, 1_000_000.0)

    def test_top1_benchmark(self):
        c = cohort.build_cohort("Mercenary", "Apex Vanguard")
        b = cohort.cohort_benchmark(c, mode="top1")
        self.assertEqual(b.damage_done, 1_200_000.0)

    def test_top25_benchmark_picks_high_percentile(self):
        c = cohort.build_cohort("Mercenary", "Apex Vanguard")
        b = cohort.cohort_benchmark(c, mode="top25")
        # 75th percentile of [850k, 900k, 1.1M, 1.2M]: idx=round(0.75*3)=2 → 1.1M
        self.assertEqual(b.damage_done, 1_100_000.0)

    def test_named_benchmark_picks_one_player(self):
        c = cohort.build_cohort("Mercenary", "Apex Vanguard")
        b = cohort.cohort_benchmark(c, mode="named", named_player="Vossan")
        self.assertEqual(b.sample_size, 2)  # Vossan has 2 Apex fights in cohort
        # Median of Vossan's [1.2M, 1.1M] = 1_150_000
        self.assertEqual(b.damage_done, 1_150_000.0)

    def test_named_benchmark_with_unknown_player_is_safe(self):
        c = cohort.build_cohort("Mercenary", "Apex Vanguard")
        b = cohort.cohort_benchmark(c, mode="named", named_player="Ghost")
        self.assertEqual(b.sample_size, 0)
        self.assertFalse(b.is_meaningful)

    def test_named_benchmark_without_player_raises(self):
        c = cohort.build_cohort("Mercenary", "Apex Vanguard")
        with self.assertRaises(ValueError):
            cohort.cohort_benchmark(c, mode="named")

    def test_ability_use_counts_aggregated_correctly(self):
        c = cohort.build_cohort("Mercenary", "Apex Vanguard")
        b = cohort.cohort_benchmark(c, mode="median")
        # Heatseeker Missiles uses across the 4 Merc-on-Apex samples:
        #   Karzag today: 6, Vossan today: 9, Karzag yest: 5, Vossan old: 8
        # Sorted: [5, 6, 8, 9] → median = 7
        self.assertIn("Heatseeker Missiles", b.ability_use_counts)
        self.assertEqual(b.ability_use_counts["Heatseeker Missiles"], 7.0)

    def test_label_includes_class_encounter_and_sample_size(self):
        c = cohort.build_cohort("Mercenary", "Apex Vanguard")
        b = cohort.cohort_benchmark(c, mode="median")
        self.assertIn("Mercenary", b.label)
        self.assertIn("Apex Vanguard", b.label)
        self.assertIn("n=4", b.label)

    def test_meaningfulness_threshold_is_three(self):
        c = cohort.build_cohort("Mercenary", "Apex Vanguard", min_damage=1_000_000)
        # Only 2 fights pass — should be flagged as not meaningful
        self.assertEqual(c.sample_size, 2)
        b = cohort.cohort_benchmark(c, mode="median")
        self.assertFalse(b.is_meaningful)


class ListKnownNamesTests(CohortTestCase):

    def test_list_known_encounter_names_orders_by_frequency(self):
        names = cohort.list_known_encounter_names()
        # Apex Vanguard appears 3x, Kanoth 1x, Trash Pull 1x
        # Apex must come first.
        self.assertEqual(names[0], "Apex Vanguard")

    def test_list_known_class_names(self):
        names = cohort.list_known_class_names()
        self.assertIn("Mercenary", names)
        self.assertIn("Operative", names)
        self.assertIn("Juggernaut", names)


if __name__ == "__main__":
    unittest.main()
