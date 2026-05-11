"""
W.I.S.E. Panda — Cohort & History Query Layer
==============================================

The single query layer that powers:
  - Find-a-Fight tab (top-down search)
  - Right-click Deeper View on a player (bottom-up drilldown)
  - Aggregated benchmark coaching (median / top / specific peer)

Design principle: pure read functions over the existing SQLite schema.
This module never writes to the DB. It only asks questions.

Schema notes (what we read from):
  encounters                              -- one row per fight, encounter-level summary
  player_character_encounters             -- per-player per-fight totals
  player_character_encounter_abilities    -- per-player per-fight ability counts
  player_characters                       -- the user's own characters (with class)
  imported_player_characters              -- everyone we've ever seen, with latest class
  combat_log_imports                      -- log files (for path/date)

The encounter_key encodes: "<log_path>|<line_start>|<line_end>|<start_time_iso>"
so we can recover the on-disk location of any stored fight without a schema change.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Iterable, Optional

# Reuse the existing connection helper so we honour the same WAL/timeout policy.
from storage.encounter_db import _connect_db


# ─── Public dataclasses ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class FightRef:
    """
    A lightweight pointer to a stored fight. Enough to display in a results
    list and to reload the fight on demand by parsing the encounter_key.

    This is intentionally not a full Fight — we don't want to pay the cost of
    parsing events for every search hit. Reload on click.
    """
    encounter_key: str
    encounter_name: str
    encounter_date: str           # ISO date "YYYY-MM-DD"
    log_path: str
    recorded_by: str              # local player name on the log
    line_start: int
    line_end: int
    duration_estimate: float      # seconds — see _duration_from_line_range

    @property
    def log_filename(self) -> str:
        return Path(self.log_path).name if self.log_path else ""


@dataclass(frozen=True)
class PlayerInFight:
    """One player's totals in one fight. The atom for cohort building."""
    encounter_key: str
    character_name: str
    class_name: str
    damage_done: int
    healing_done: int
    taunts: int
    interrupts: int
    encounter_date: str
    # Phase C: per-fight class detection. discipline_name and
    # class_evidence come straight from player_character_encounters and
    # are empty for fights ingested before Phase C landed. class_name
    # falls back to the legacy per-character value when the per-fight
    # column is empty, so existing UI doesn't suddenly show "—" for old
    # fights.
    discipline_name: str = ""
    class_evidence: str = ""


@dataclass(frozen=True)
class FightFilters:
    """
    Filters for find_fights(). All fields optional. Matches are AND'd together.

    Keep this dataclass tightly scoped to what the UI exposes — adding a field
    here means adding a control somewhere. Resist the urge to grow it.
    """
    date_from: Optional[str] = None              # ISO date "YYYY-MM-DD"
    date_to: Optional[str] = None
    encounter_name_contains: Optional[str] = None
    player_name_contains: Optional[str] = None   # any participant
    class_name: Optional[str] = None             # exact match on a participant's class
    # Phase F: filter by per-fight discipline. Implementation prefers the
    # per-fight pce.discipline_name (Phase C+) for accuracy. discipline_name
    # is meaningful only when class_name is also set, since "Lethality"
    # without a class context is ambiguous (both Operative and Sniper have
    # discipline names that could collide in theory; in practice they don't,
    # but we still require class+discipline to be paired for clarity).
    discipline_name: Optional[str] = None
    min_duration_seconds: Optional[float] = None
    require_same_class_peer: bool = False        # ≥2 players sharing a class in one fight
    limit: int = 200


@dataclass
class Cohort:
    """A set of (player, fight) pairs that match a cohort definition."""
    class_name: str
    encounter_name: str
    fights: list[PlayerInFight] = field(default_factory=list)

    @property
    def sample_size(self) -> int:
        return len(self.fights)

    @property
    def is_meaningful(self) -> bool:
        """A cohort with <3 samples is too noisy to draw conclusions from."""
        return self.sample_size >= 3


@dataclass(frozen=True)
class PlayerSummary:
    """
    Summary row for the Roster app's player list. One per player_characters
    row, enriched with their most-played per-fight class and activity stats.
    """
    character_name: str
    most_played_class: str   # may be empty string when no class data exists
    fight_count: int
    last_seen_date: str      # ISO "YYYY-MM-DD", may be empty


@dataclass(frozen=True)
class BenchmarkProfile:
    """
    A single set of "what to compare against" values, derived from a cohort.

    The comparison engine consumes one of these the same way it consumes
    a single reference player. That's the whole point: same engine, different
    reference object.
    """
    label: str                           # "Median Mercenary on Apex Vanguard (n=14)"
    sample_size: int
    is_meaningful: bool                  # convenience for UI to show a warning
    mode: str                            # "median" | "top25" | "top1" | "named"

    # Aggregated metrics
    damage_done: float
    healing_done: float
    taunts: float
    interrupts: float

    # Per-ability median use counts: ability_name -> median uses
    ability_use_counts: dict[str, float] = field(default_factory=dict)


# ─── encounter_key parsing ───────────────────────────────────────────────────


def parse_encounter_key(encounter_key: str) -> tuple[str, int, int, str]:
    """
    Recover (log_path, line_start, line_end, start_time_iso) from a key.

    The key was constructed by encounter_db.encounter_key_for() as:
        "<log_path>|<line_start>|<line_end>|<start_time_iso>"

    Returns sentinel values on a malformed key rather than raising, because
    one bad row in the DB shouldn't crash a search.
    """
    parts = encounter_key.split("|")
    if len(parts) < 4:
        return ("", 0, 0, "")
    log_path = parts[0]
    try:
        line_start = int(parts[1])
        line_end = int(parts[2])
    except ValueError:
        line_start = 0
        line_end = 0
    start_time_iso = parts[3]
    return (log_path, line_start, line_end, start_time_iso)


def _duration_from_line_range(line_start: int, line_end: int) -> float:
    """
    Rough duration estimate from line span when we don't have a real duration
    stored. SWTOR logs are ~3-8 lines/sec in active combat. We use 5 as a
    middle-of-road heuristic — it's only used for filtering, not display.

    Real duration is only available after loading the fight from disk.
    """
    span = max(line_end - line_start, 0)
    return span / 5.0


# ─── Find fights (top-down search) ───────────────────────────────────────────


def find_fights(filters: FightFilters) -> list[FightRef]:
    """
    Top-down fight search. Returns at most filters.limit results, newest first.

    This powers the Find-a-Fight tab. Every clause is optional and AND'd.
    """
    sql_parts: list[str] = [
        "SELECT DISTINCT e.encounter_key, e.encounter_name, e.encounter_date, "
        "       e.log_path, e.recorded_by "
        "FROM encounters e "
    ]
    where: list[str] = []
    params: list[object] = []

    # Player filter requires a join to participants. We use a LEFT JOIN style
    # via EXISTS so we don't multiply rows when many players match.
    if filters.player_name_contains:
        where.append(
            "EXISTS (SELECT 1 FROM player_character_encounters pce "
            "        JOIN player_characters pc ON pc.character_id = pce.character_id "
            "        WHERE pce.encounter_key = e.encounter_key "
            "          AND pc.character_name LIKE ? COLLATE NOCASE)"
        )
        params.append(f"%{filters.player_name_contains}%")

    if filters.class_name and filters.discipline_name:
        # Phase F: class + discipline, both required → query per-fight
        # detection data. We accept matches against either:
        #   - per-fight pce.class_name + pce.discipline_name (preferred,
        #     since this is what Phase C populated)
        #   - fallback: per-character pc.class_name with empty per-fight
        #     class data, but only if the discipline filter also matches.
        # In practice the second case is for old fights ingested before
        # Phase C — those have empty discipline so they won't match a
        # discipline filter anyway.
        where.append(
            "EXISTS (SELECT 1 FROM player_character_encounters pce "
            "        JOIN player_characters pc ON pc.character_id = pce.character_id "
            "        WHERE pce.encounter_key = e.encounter_key "
            "          AND ("
            "                (pce.class_name = ? COLLATE NOCASE AND pce.discipline_name = ? COLLATE NOCASE) "
            "                OR (pce.class_name = '' AND pc.class_name = ? COLLATE NOCASE "
            "                    AND pce.discipline_name = ? COLLATE NOCASE)"
            "              ))"
        )
        params.extend([
            filters.class_name, filters.discipline_name,
            filters.class_name, filters.discipline_name,
        ])
    elif filters.class_name:
        # Class only — same per-fight preference as the combined case.
        where.append(
            "EXISTS (SELECT 1 FROM player_character_encounters pce "
            "        JOIN player_characters pc ON pc.character_id = pce.character_id "
            "        WHERE pce.encounter_key = e.encounter_key "
            "          AND ("
            "                pce.class_name = ? COLLATE NOCASE "
            "                OR (pce.class_name = '' AND pc.class_name = ? COLLATE NOCASE)"
            "              ))"
        )
        params.extend([filters.class_name, filters.class_name])
    elif filters.discipline_name:
        # Discipline without class — uncommon but supported. Just match the
        # per-fight discipline column directly.
        where.append(
            "EXISTS (SELECT 1 FROM player_character_encounters pce "
            "        WHERE pce.encounter_key = e.encounter_key "
            "          AND pce.discipline_name = ? COLLATE NOCASE)"
        )
        params.append(filters.discipline_name)

    if filters.encounter_name_contains:
        where.append("e.encounter_name LIKE ? COLLATE NOCASE")
        params.append(f"%{filters.encounter_name_contains}%")

    if filters.date_from:
        where.append("e.encounter_date >= ?")
        params.append(filters.date_from)

    if filters.date_to:
        where.append("e.encounter_date <= ?")
        params.append(filters.date_to)

    if filters.require_same_class_peer:
        # Find encounters where at least one class appears 2+ times among
        # participants. Done as a subquery so it composes with other filters.
        where.append(
            "EXISTS ("
            "   SELECT 1 FROM player_character_encounters pce "
            "   JOIN player_characters pc ON pc.character_id = pce.character_id "
            "   WHERE pce.encounter_key = e.encounter_key "
            "     AND pc.class_name <> '' "
            "   GROUP BY pc.class_name "
            "   HAVING COUNT(*) >= 2"
            ")"
        )

    if where:
        sql_parts.append("WHERE " + " AND ".join(where) + " ")

    # Newest first. encounter_date is ISO so lexical sort = chronological.
    # updated_at as the tiebreaker handles same-day fights in import order.
    sql_parts.append("ORDER BY e.encounter_date DESC, e.updated_at DESC ")
    sql_parts.append("LIMIT ?")
    params.append(int(filters.limit))

    sql = "".join(sql_parts)

    refs: list[FightRef] = []
    with _connect_db() as conn:
        for row in conn.execute(sql, params):
            encounter_key, encounter_name, encounter_date, log_path, recorded_by = row
            _, line_start, line_end, _ = parse_encounter_key(encounter_key)
            duration = _duration_from_line_range(line_start, line_end)

            # Apply min-duration filter in Python — we don't have stored duration.
            # Slight cost to fetching extra rows we'll throw away, but we cap at
            # filters.limit upstream so worst-case is bounded.
            if (
                filters.min_duration_seconds is not None
                and duration < filters.min_duration_seconds
            ):
                continue

            refs.append(FightRef(
                encounter_key=encounter_key,
                encounter_name=encounter_name or "Unknown Encounter",
                encounter_date=encounter_date or "",
                log_path=log_path or "",
                recorded_by=recorded_by or "",
                line_start=line_start,
                line_end=line_end,
                duration_estimate=duration,
            ))
    return refs


# ─── Find a player's history (bottom-up drilldown) ───────────────────────────


def find_player_history(
    player_name: str,
    encounter_name: Optional[str] = None,
    limit: int = 200,
) -> list[FightRef]:
    """
    All fights a given player appeared in, newest first.

    If encounter_name is set, restricts to fights matching that boss — useful
    for "show me every other time I fought Apex Vanguard."

    Player matching is exact (case-insensitive) on character_name. Aliases
    aren't followed here because the seen_player_aliases table doesn't link
    back to player_characters cleanly. If a player has aliases, the caller
    should resolve them before calling this function.
    """
    sql = (
        "SELECT e.encounter_key, e.encounter_name, e.encounter_date, "
        "       e.log_path, e.recorded_by "
        "FROM encounters e "
        "JOIN player_character_encounters pce ON pce.encounter_key = e.encounter_key "
        "JOIN player_characters pc ON pc.character_id = pce.character_id "
        "WHERE pc.character_name = ? COLLATE NOCASE "
    )
    params: list[object] = [player_name]

    if encounter_name:
        sql += "AND e.encounter_name = ? COLLATE NOCASE "
        params.append(encounter_name)

    sql += "ORDER BY e.encounter_date DESC, e.updated_at DESC LIMIT ?"
    params.append(int(limit))

    refs: list[FightRef] = []
    with _connect_db() as conn:
        for row in conn.execute(sql, params):
            encounter_key, enc_name, enc_date, log_path, recorded_by = row
            _, line_start, line_end, _ = parse_encounter_key(encounter_key)
            refs.append(FightRef(
                encounter_key=encounter_key,
                encounter_name=enc_name or "Unknown Encounter",
                encounter_date=enc_date or "",
                log_path=log_path or "",
                recorded_by=recorded_by or "",
                line_start=line_start,
                line_end=line_end,
                duration_estimate=_duration_from_line_range(line_start, line_end),
            ))
    return refs


def list_participants_in_fight(encounter_key: str) -> list[PlayerInFight]:
    """
    Every player recorded in a single fight, with totals.

    Used by the Deeper View "this fight" panel and as the per-fight lookup
    when building cohorts. Cheap — single indexed query.

    Class-name resolution: prefer the per-fight class_name in
    player_character_encounters (Phase C). Fall back to the per-character
    class_name in player_characters when the per-fight column is empty,
    which happens for fights ingested before Phase C landed.

    Phase C+: cross-fight discipline inference. When the per-fight
    discipline_name is empty (player was in this fight but didn't emit
    DisciplineChanged AND didn't press enough signature abilities to vote),
    we look at OTHER fights of the same character. If they were declared
    via DisciplineChanged in another fight at exactly one discipline, we
    fill that in here. The evidence string carries 'inferred:...' so the
    UI can display these differently from declared/voted results.
    """
    sql = (
        "SELECT pce.encounter_key, pc.character_name, pc.character_id, "
        "       CASE WHEN pce.class_name != '' THEN pce.class_name ELSE pc.class_name END, "
        "       pce.damage_done, pce.healing_done, pce.taunts, pce.interrupts, "
        "       pce.encounter_date, "
        "       pce.discipline_name, pce.class_evidence "
        "FROM player_character_encounters pce "
        "JOIN player_characters pc ON pc.character_id = pce.character_id "
        "WHERE pce.encounter_key = ? "
        "ORDER BY pce.damage_done DESC"
    )
    out: list[PlayerInFight] = []
    with _connect_db() as conn:
        rows = list(conn.execute(sql, (encounter_key,)))

        for row in rows:
            (encounter_key_val, character_name, character_id, class_name,
             dmg, heal, taunts, interrupts, enc_date, discipline, evidence) = row

            discipline = discipline or ""
            evidence = evidence or ""

            # Cross-fight inference path. Only fires when:
            #   - the per-fight discipline_name is empty
            #   - the character has been declared elsewhere
            #   - and only at one discipline (no spec swappers)
            if not discipline:
                inferred = _infer_discipline_for_character(
                    conn, int(character_id), str(encounter_key_val), enc_date or ""
                )
                if inferred is not None:
                    discipline, evidence = inferred

            out.append(PlayerInFight(
                encounter_key=encounter_key_val,
                character_name=character_name,
                class_name=class_name or "",
                damage_done=int(dmg or 0),
                healing_done=int(heal or 0),
                taunts=int(taunts or 0),
                interrupts=int(interrupts or 0),
                encounter_date=enc_date or "",
                discipline_name=discipline,
                class_evidence=evidence,
            ))
    return out


def _infer_discipline_for_character(
    conn,
    character_id: int,
    current_encounter_key: str,
    current_encounter_date: str,
) -> Optional[tuple[str, str]]:
    """
    Look at other fights of this character to infer a discipline for the
    current fight, when the current fight has no per-fight detection.

    Safety rules:
      1. Only consider DECLARED fights (evidence starts with 'declared:').
         Voted fights are themselves inferences — chaining inference on
         inference produces cascading errors.
      2. Skip the current fight (don't infer from yourself).
      3. If the character has been declared at MORE THAN ONE discipline,
         don't infer at all. They demonstrably swap specs across sessions
         and we have no way to know which one was active in this fight.
      4. Prefer fights on the same date when ranking by recency. A
         declaration from the same play session is the strongest signal.

    Returns:
      (discipline_name, evidence_string) on success
      None when no safe inference is possible
    """
    rows = conn.execute(
        """
        SELECT discipline_name, encounter_date
        FROM player_character_encounters
        WHERE character_id = ?
          AND encounter_key != ?
          AND class_evidence LIKE 'declared:%'
          AND discipline_name != ''
        """,
        (character_id, current_encounter_key),
    ).fetchall()

    if not rows:
        return None

    # Tally declared disciplines for this character. If more than one
    # discipline appears in their declared history, abstain — they swap.
    distinct = {r[0] for r in rows}
    if len(distinct) != 1:
        return None

    discipline = next(iter(distinct))

    # Count declared fights and same-day fights for the evidence string —
    # gives the user useful "why" when they hover.
    same_day = (
        sum(1 for r in rows if (r[1] or "") == current_encounter_date)
        if current_encounter_date else 0
    )
    n_declared = len(rows)

    if same_day > 0:
        evidence = f"inferred:{discipline} (declared in {same_day} other fight{'s' if same_day != 1 else ''} the same day)"
    else:
        evidence = f"inferred:{discipline} (declared in {n_declared} other fight{'s' if n_declared != 1 else ''} of this character)"

    return discipline, evidence


# ─── Cohort building (the benchmark library at work) ─────────────────────────


def build_cohort(
    class_name: str,
    encounter_name: str,
    *,
    discipline_name: Optional[str] = None,
    days_back: Optional[int] = None,
    min_damage: Optional[int] = None,
) -> Cohort:
    """
    Pull every (player, fight) pair where a player of `class_name` fought
    `encounter_name`. This is the corpus that powers benchmarked coaching.

    Optional narrowing:
      discipline_name — restrict to a single discipline (e.g. "Vengeance" for
                        Juggernaut). Phase H. Mirrors the per-fight preference
                        used by find_fights() — match pce.discipline_name first,
                        and only fall back to per-character class+empty
                        per-fight data when needed.
      days_back       — only fights within the last N days (recency, gear)
      min_damage      — drop trivially short or AFK appearances

    Class matching prefers per-fight class data (pce.class_name from Phase C)
    over per-character (pc.class_name) the same way find_fights does. Old
    fights ingested before Phase C have empty pce.class_name and fall through
    to the per-character match.

    The cohort can include the user's own past performances. That's a feature,
    not a bug: it's how "self vs history" comparison works.
    """
    sql_parts: list[str] = [
        "SELECT pce.encounter_key, pc.character_name, "
        "       CASE WHEN pce.class_name != '' THEN pce.class_name ELSE pc.class_name END, "
        "       pce.damage_done, pce.healing_done, pce.taunts, pce.interrupts, "
        "       pce.encounter_date, "
        "       pce.discipline_name, pce.class_evidence "
        "FROM player_character_encounters pce "
        "JOIN player_characters pc ON pc.character_id = pce.character_id "
        "JOIN encounters e ON e.encounter_key = pce.encounter_key "
        "WHERE e.encounter_name = ? COLLATE NOCASE "
    ]
    params: list[object] = [encounter_name]

    if discipline_name:
        # Both class and discipline must match. Per-fight data preferred,
        # per-character fallback only when per-fight class is blank.
        sql_parts.append(
            "  AND ("
            "        (pce.class_name = ? COLLATE NOCASE AND pce.discipline_name = ? COLLATE NOCASE) "
            "        OR (pce.class_name = '' AND pc.class_name = ? COLLATE NOCASE "
            "            AND pce.discipline_name = ? COLLATE NOCASE)"
            "      ) "
        )
        params.extend([class_name, discipline_name, class_name, discipline_name])
    else:
        # Class only — same per-fight preference, no discipline constraint.
        sql_parts.append(
            "  AND ("
            "        pce.class_name = ? COLLATE NOCASE "
            "        OR (pce.class_name = '' AND pc.class_name = ? COLLATE NOCASE)"
            "      ) "
        )
        params.extend([class_name, class_name])

    if days_back is not None and days_back > 0:
        cutoff = (datetime.now() - timedelta(days=days_back)).date().isoformat()
        sql_parts.append("AND pce.encounter_date >= ? ")
        params.append(cutoff)

    if min_damage is not None and min_damage > 0:
        sql_parts.append("AND pce.damage_done >= ? ")
        params.append(min_damage)

    sql_parts.append("ORDER BY pce.damage_done DESC")
    sql = "".join(sql_parts)

    cohort = Cohort(class_name=class_name, encounter_name=encounter_name)
    with _connect_db() as conn:
        for row in conn.execute(sql, params):
            cohort.fights.append(PlayerInFight(
                encounter_key=row[0],
                character_name=row[1],
                class_name=row[2] or "",
                damage_done=int(row[3] or 0),
                healing_done=int(row[4] or 0),
                taunts=int(row[5] or 0),
                interrupts=int(row[6] or 0),
                encounter_date=row[7] or "",
                discipline_name=row[8] or "",
                class_evidence=row[9] or "",
            ))
    return cohort


def _ability_counts_for_encounters(
    conn: sqlite3.Connection,
    encounter_keys: Iterable[str],
    character_filter: Optional[set[str]] = None,
) -> dict[str, list[int]]:
    """
    For a set of encounter_keys, return ability_name -> [use_count, ...] across
    all (player, fight) pairs in the cohort. Same player in different fights
    contributes multiple samples — that's correct for a per-fight median.

    If character_filter is set, only those character names contribute.
    """
    keys = list(encounter_keys)
    if not keys:
        return {}

    # SQLite has a default parameter limit of 999. Chunk just in case a user
    # has a monster cohort. (At 200 fights/character per cohort this is rare,
    # but we're future-proofing.)
    counts: dict[str, list[int]] = {}
    chunk_size = 500

    for i in range(0, len(keys), chunk_size):
        chunk = keys[i:i + chunk_size]
        placeholders = ",".join("?" * len(chunk))
        sql = (
            "SELECT pcea.ability_name, pcea.use_count, pc.character_name "
            "FROM player_character_encounter_abilities pcea "
            "JOIN player_characters pc ON pc.character_id = pcea.character_id "
            f"WHERE pcea.encounter_key IN ({placeholders})"
        )
        for ability_name, use_count, character_name in conn.execute(sql, chunk):
            if character_filter and character_name not in character_filter:
                continue
            counts.setdefault(ability_name, []).append(int(use_count or 0))

    return counts


def cohort_benchmark(
    cohort: Cohort,
    *,
    mode: str = "median",
    named_player: Optional[str] = None,
) -> BenchmarkProfile:
    """
    Aggregate a cohort into a single comparable profile.

    Modes:
      "median"  — middle of the cohort. Robust to outliers. Default.
      "top25"   — 75th percentile. "What good looks like."
      "top1"    — best in the cohort. "What great looks like."
      "named"   — a specific player's values, when named_player is given.
                  Cohort must contain that player or returns an empty profile.

    The label and sample size are honest: if n=2 we say n=2 and the UI can
    show a "not enough data" warning. We never silently fudge.
    """
    if not cohort.fights:
        return BenchmarkProfile(
            label=f"No data for {cohort.class_name} on {cohort.encounter_name}",
            sample_size=0,
            is_meaningful=False,
            mode=mode,
            damage_done=0.0,
            healing_done=0.0,
            taunts=0.0,
            interrupts=0.0,
        )

    if mode == "named":
        if not named_player:
            raise ValueError("mode='named' requires named_player")
        named_fights = [f for f in cohort.fights if f.character_name.lower() == named_player.lower()]
        if not named_fights:
            return BenchmarkProfile(
                label=f"{named_player} not in cohort",
                sample_size=0,
                is_meaningful=False,
                mode=mode,
                damage_done=0.0,
                healing_done=0.0,
                taunts=0.0,
                interrupts=0.0,
            )
        target = named_fights
        label_prefix = f"{named_player}"
        character_filter: Optional[set[str]] = {named_player}
    else:
        target = list(cohort.fights)
        character_filter = None
        label_prefix = {
            "median": f"Median {cohort.class_name}",
            "top25":  f"Top 25% {cohort.class_name}",
            "top1":   f"Top {cohort.class_name}",
        }.get(mode, f"{mode} {cohort.class_name}")

    # Pick the aggregation function for this mode. Each operates on a list of
    # numbers and returns a single representative value.
    def aggregate(values: list[int | float]) -> float:
        if not values:
            return 0.0
        if mode == "median" or mode == "named":
            # 'named' aggregates across that one player's multiple appearances.
            return float(median(values))
        if mode == "top25":
            ordered = sorted(values)
            # 75th percentile, inclusive nearest-rank. With n=4: index 3 (top).
            idx = max(0, int(round(0.75 * (len(ordered) - 1))))
            return float(ordered[idx])
        if mode == "top1":
            return float(max(values))
        return float(median(values))

    damage = aggregate([f.damage_done for f in target])
    healing = aggregate([f.healing_done for f in target])
    taunts = aggregate([f.taunts for f in target])
    interrupts = aggregate([f.interrupts for f in target])

    # Per-ability aggregation
    encounter_keys = {f.encounter_key for f in target}
    with _connect_db() as conn:
        per_ability = _ability_counts_for_encounters(conn, encounter_keys, character_filter)

    ability_aggregates: dict[str, float] = {}
    for ability_name, samples in per_ability.items():
        ability_aggregates[ability_name] = aggregate(samples)

    sample_size = len(target)
    label = f"{label_prefix} on {cohort.encounter_name} (n={sample_size})"

    return BenchmarkProfile(
        label=label,
        sample_size=sample_size,
        is_meaningful=sample_size >= 3,
        mode=mode,
        damage_done=damage,
        healing_done=healing,
        taunts=taunts,
        interrupts=interrupts,
        ability_use_counts=ability_aggregates,
    )


# ─── Phase H: per-fight duration lookup for normalized rates ────────────────


def cohort_durations(
    encounter_keys: Iterable[str],
    *,
    precise: bool = True,
) -> dict[str, float]:
    """
    Return {encounter_key: duration_seconds} for the given keys.

    Two modes:
      precise=False — fast path. Uses the line-range estimate from
                      _duration_from_line_range. ~5x lines/sec heuristic.
                      Wrong by up to 30%. Good enough for filtering or quick
                      checks.
      precise=True  — slow path. Queries combat_log_events for the actual
                      MIN/MAX timestamp_text of the fight's line range.
                      Costs one query per distinct log file in the input.
                      Use this for damage/min and healing/min calculations
                      where accuracy matters.

    Keys we can't resolve (missing log import, malformed key, fight whose
    raw events were never imported) get the estimate as a fallback rather
    than being omitted. The UI never has to handle "missing" durations.

    Phase H v1 uses precise=True. The fast path is exposed for any future
    place where speed matters more than accuracy.
    """
    keys = [k for k in encounter_keys if k]
    if not keys:
        return {}

    # First pass — line-range estimates for everyone. This is our floor;
    # precise lookups overwrite it where they succeed.
    durations: dict[str, float] = {}
    parsed: dict[str, tuple[str, int, int]] = {}  # key -> (log_path, line_start, line_end)
    for key in keys:
        log_path, line_start, line_end, _ = parse_encounter_key(key)
        durations[key] = _duration_from_line_range(line_start, line_end)
        parsed[key] = (log_path, line_start, line_end)

    if not precise:
        return durations

    # Group by log_path so we can do one MIN/MAX query per log instead of
    # per fight. Logs with many fights amortize the import_id lookup.
    by_log: dict[str, list[tuple[str, int, int]]] = {}
    for key, (log_path, line_start, line_end) in parsed.items():
        if not log_path or line_end <= line_start:
            continue
        by_log.setdefault(log_path, []).append((key, line_start, line_end))

    if not by_log:
        return durations

    with _connect_db() as conn:
        for log_path, fight_ranges in by_log.items():
            # Resolve import_id once per log. Skip if log isn't imported —
            # the line-range estimate stays.
            row = conn.execute(
                "SELECT import_id FROM combat_log_imports WHERE log_path = ?",
                (log_path,),
            ).fetchone()
            if not row:
                continue
            import_id = int(row[0])

            for key, line_start, line_end in fight_ranges:
                # MIN/MAX timestamp_text within the fight's line range.
                # timestamp_text is the bracketed prefix from the log line,
                # e.g. "[19:47:46.233]" — lexical order matches chronological
                # within a single log day, which is the only span we care
                # about (combat logs roll over per-day).
                ts_row = conn.execute(
                    "SELECT MIN(timestamp_text), MAX(timestamp_text) "
                    "FROM combat_log_events "
                    "WHERE import_id = ? AND line_number BETWEEN ? AND ? "
                    "  AND timestamp_text <> ''",
                    (import_id, line_start, line_end),
                ).fetchone()
                if not ts_row or not ts_row[0] or not ts_row[1]:
                    continue
                seconds = _seconds_between_timestamp_text(ts_row[0], ts_row[1])
                if seconds is not None and seconds >= 0:
                    durations[key] = seconds

    return durations


def _seconds_between_timestamp_text(start_ts: str, end_ts: str) -> Optional[float]:
    """
    Parse two SWTOR-style "[HH:MM:SS.mmm]" timestamps and return the elapsed
    seconds between them. Returns None if either is unparseable.

    SWTOR logs use a time-of-day bracketed prefix without date. A single fight
    is short enough that we don't need to worry about midnight rollover —
    aggregation already splits at long gaps, and no boss takes 12 hours.
    """
    def _parse(ts: str) -> Optional[float]:
        # Strip optional surrounding brackets and whitespace.
        s = ts.strip()
        if s.startswith("[") and s.endswith("]"):
            s = s[1:-1]
        # Expect HH:MM:SS or HH:MM:SS.mmm
        try:
            parts = s.split(":")
            if len(parts) != 3:
                return None
            h = int(parts[0])
            m = int(parts[1])
            sec = float(parts[2])
            return h * 3600 + m * 60 + sec
        except (ValueError, IndexError):
            return None

    a = _parse(start_ts)
    b = _parse(end_ts)
    if a is None or b is None:
        return None
    return b - a


# ─── Convenience: list the things the UI can offer in dropdowns ─────────────


def list_known_encounter_names(limit: int = 200) -> list[str]:
    """Distinct encounter names in the DB, most-frequent first."""
    sql = (
        "SELECT encounter_name, COUNT(*) AS cnt "
        "FROM encounters "
        "WHERE encounter_name <> '' AND encounter_name <> 'Unknown Encounter' "
        "GROUP BY encounter_name "
        "ORDER BY cnt DESC, encounter_name ASC "
        "LIMIT ?"
    )
    with _connect_db() as conn:
        return [row[0] for row in conn.execute(sql, (int(limit),))]


def list_known_class_names() -> list[str]:
    """Distinct non-empty class names seen in player_characters."""
    sql = (
        "SELECT DISTINCT class_name FROM player_characters "
        "WHERE class_name <> '' "
        "ORDER BY class_name ASC"
    )
    with _connect_db() as conn:
        return [row[0] for row in conn.execute(sql)]


def list_known_disciplines(class_name: Optional[str] = None) -> list[str]:
    """
    Distinct non-empty discipline names seen in player_character_encounters
    (Phase C+ data). When `class_name` is provided, only disciplines for
    that class are returned — used by the Find-a-Fight tab to narrow the
    discipline dropdown when a class is selected.

    Returns alphabetically sorted. Empty list if no Phase C data exists yet
    (very fresh DB or pre-Phase-C).
    """
    if class_name:
        sql = (
            "SELECT DISTINCT discipline_name FROM player_character_encounters "
            "WHERE discipline_name <> '' "
            "  AND class_name = ? COLLATE NOCASE "
            "ORDER BY discipline_name ASC"
        )
        params = (class_name,)
    else:
        sql = (
            "SELECT DISTINCT discipline_name FROM player_character_encounters "
            "WHERE discipline_name <> '' "
            "ORDER BY discipline_name ASC"
        )
        params = ()
    with _connect_db() as conn:
        return [row[0] for row in conn.execute(sql, params)]


def list_known_players(
    *, name_contains: str = "", limit: int = 500
) -> list[PlayerSummary]:
    """
    Every player in the DB, with their most-played class, fight count, and
    last-seen date. Powers the Roster app's player list.

    Class resolution: counts each (character, pce.class_name) combo and
    picks the most frequent non-empty value. When the player has no Phase C
    per-fight class data at all, falls back to per-character pc.class_name.
    Empty string when neither has data.

    name_contains  — optional case-insensitive substring filter on character
                     name. The Roster's search box is in-memory anyway, so
                     this is only here for tests and edge cases.
    limit          — hard cap; even on a heavy DB this should be enough for
                     a complete list.

    Sorted by fight_count descending, then character_name ascending. Most
    active players surface first.
    """
    # Two-step query because SQLite doesn't have a clean "mode" aggregate.
    # Step 1: each player's basic stats.
    # Step 2: each player's most-frequent per-fight class (in Python — small).
    sql_basic = (
        "SELECT pc.character_name, pc.class_name AS pc_class, "
        "       COUNT(pce.encounter_key) AS fight_count, "
        "       MAX(pce.encounter_date) AS last_seen "
        "FROM player_characters pc "
        "LEFT JOIN player_character_encounters pce "
        "       ON pce.character_id = pc.character_id "
    )
    where: list[str] = []
    params: list[object] = []
    if name_contains:
        where.append("pc.character_name LIKE ? COLLATE NOCASE")
        params.append(f"%{name_contains}%")
    if where:
        sql_basic += "WHERE " + " AND ".join(where) + " "
    sql_basic += (
        "GROUP BY pc.character_id, pc.character_name, pc.class_name "
        "ORDER BY fight_count DESC, pc.character_name ASC "
        "LIMIT ?"
    )
    params.append(int(limit))

    # Step 2 query: most-frequent pce.class_name per character.
    sql_class_counts = (
        "SELECT pc.character_name, pce.class_name, COUNT(*) AS uses "
        "FROM player_character_encounters pce "
        "JOIN player_characters pc ON pc.character_id = pce.character_id "
        "WHERE pce.class_name <> '' "
        "GROUP BY pc.character_name, pce.class_name"
    )

    summaries: list[PlayerSummary] = []
    with _connect_db() as conn:
        # Build the per-fight class frequency map.
        per_fight_class: dict[str, dict[str, int]] = {}
        for char_name, klass, uses in conn.execute(sql_class_counts):
            per_fight_class.setdefault(char_name, {})[klass] = int(uses)

        for row in conn.execute(sql_basic, params):
            char_name, pc_class, fight_count, last_seen = row
            counts = per_fight_class.get(char_name, {})
            if counts:
                # Most frequent per-fight class wins. Ties broken by class
                # name alphabetically (ascending) so output is deterministic.
                most_played = sorted(
                    counts.items(),
                    key=lambda kv: (-kv[1], kv[0]),
                )[0][0]
            else:
                most_played = pc_class or ""
            summaries.append(PlayerSummary(
                character_name=char_name,
                most_played_class=most_played,
                fight_count=int(fight_count or 0),
                last_seen_date=last_seen or "",
            ))
    return summaries
