"""
W.I.S.E. Panda — Analysis Engine
Handles: ability database, rotation building, role detection, role-specific metrics, comparison engine.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from datetime import time

from engine.parser_core import LogEvent
from engine.aggregator import Fight, EntityKind, elapsed_seconds

# ── Ability database ──────────────────────────────────────────────────────────

DB_PATH = Path(__file__).parent.parent / "data" / "abilities.json"

@dataclass
class AbilityInfo:
    name: str
    id: str = ""
    role: str = "unknown"       # dps | healer | tank | any | unknown
    type: str = "unknown"       # damage | heal | buff | debuff | taunt | interrupt | cooldown
    gcd: bool = True
    cooldown_sec: float = 0.0
    base_damage_min: int = 0
    base_damage_max: int = 0
    base_heal_min: int = 0
    base_heal_max: int = 0
    dot_ticks: int = 0
    aoe: bool = False
    notes: str = ""

    @property
    def base_damage_mid(self) -> float:
        if self.base_damage_min and self.base_damage_max:
            return (self.base_damage_min + self.base_damage_max) / 2
        return 0.0

    @property
    def is_offensive(self) -> bool:
        return self.type in ("damage",)

    @property
    def is_healing(self) -> bool:
        return self.type == "heal"

    @property
    def is_tank_utility(self) -> bool:
        return self.type in ("taunt", "interrupt", "cooldown")


class AbilityDB:
    def __init__(self):
        self._db: Dict[str, AbilityInfo] = {}
        self._load()

    def _load(self):
        if not DB_PATH.exists():
            return
        try:
            raw = json.loads(DB_PATH.read_text(encoding="utf-8"))
            for name, d in raw.get("abilities", {}).items():
                self._db[name] = AbilityInfo(
                    name=name,
                    id=d.get("id", ""),
                    role=d.get("role", "unknown"),
                    type=d.get("type", "unknown"),
                    gcd=d.get("gcd", True),
                    cooldown_sec=float(d.get("cooldown_sec", 0)),
                    base_damage_min=int(d.get("base_damage_min", 0)),
                    base_damage_max=int(d.get("base_damage_max", 0)),
                    base_heal_min=int(d.get("base_heal_min", 0)),
                    base_heal_max=int(d.get("base_heal_max", 0)),
                    dot_ticks=int(d.get("dot_ticks", 0)),
                    aoe=bool(d.get("aoe", False)),
                    notes=d.get("notes", ""),
                )
        except Exception as e:
            print(f"[AbilityDB] Failed to load: {e}")

    def get(self, name: str) -> AbilityInfo:
        return self._db.get(name, AbilityInfo(name=name))

    def reload(self):
        self._db.clear()
        self._load()

    @property
    def loaded(self) -> bool:
        return bool(self._db)


# Shared singleton
_ability_db = AbilityDB()


def get_db() -> AbilityDB:
    return _ability_db


def _get_fight_cache(fight: Fight, cache_name: str) -> dict:
    cache = getattr(fight, cache_name, None)
    if cache is None:
        cache = {}
        setattr(fight, cache_name, cache)
    return cache


# ── Rotation building ─────────────────────────────────────────────────────────

GCD_THRESHOLD  = 0.3   # ignore gaps shorter than this (animation/server lag)
DEAD_TIME_WARN = 1.8   # SWTOR GCD = 1.5s, flag anything over 1.8s as dead time


@dataclass
class RotationEntry:
    """One ability press by one entity."""
    t_offset: float          # seconds into the fight
    ability_name: str
    ability_info: AbilityInfo
    damage: int = 0
    heal: int = 0
    is_crit: bool = False
    absorbed: int = 0
    is_miss: bool = False
    hits: int = 1            # for multi-hit / AoE
    gap_before: float = 0.0  # seconds since previous ability
    gap_after: float = 0.0   # filled in post-process

    @property
    def is_dead_time(self) -> bool:
        return self.gap_before > DEAD_TIME_WARN

    @property
    def efficiency(self) -> Optional[float]:
        """Actual damage vs expected base damage (if DB has data)."""
        base = self.ability_info.base_damage_mid
        if base and self.damage:
            return self.damage / base
        return None

    @property
    def result_str(self) -> str:
        if self.is_miss:   return "Miss"
        if self.damage:
            s = f"{self.damage:,}"
            if self.is_crit: s += "★"
            if self.absorbed: s += f" ({self.absorbed:,} abs)"
            return s
        if self.heal:
            s = f"+{self.heal:,}"
            if self.is_crit: s += "★"
            return s
        return "—"


def build_rotation(fight: Fight, entity_name: str) -> List[RotationEntry]:
    """
    Build the ordered ability sequence for one entity in one fight.
    Only includes AbilityActivate events (actual presses), then
    annotates with the damage/heal result that followed.
    Results are cached on the fight object to avoid redundant rebuilds.
    """
    # ── Cache check ──────────────────────────────────────────────────────────
    cache = getattr(fight, '_rotation_cache', None)
    if cache is None:
        cache = {}
        fight._rotation_cache = cache
    if entity_name in cache:
        return cache[entity_name]

    entries: List[RotationEntry] = []

    # Pass 1: collect ability activations in order
    for ev in fight.events:
        if ev.source.display_name != entity_name:
            continue
        if not ev.is_ability_activate:
            continue
        if not ev.ability or not ev.ability.name.strip():
            continue

        ab_name = ev.ability.name.strip()
        info    = _ability_db.get(ab_name)
        t       = elapsed_seconds(fight.start_time, ev.timestamp)

        entries.append(RotationEntry(
            t_offset=t,
            ability_name=ab_name,
            ability_info=info,
        ))

    # Pass 2: annotate with damage/heal results using O(n) indexed lookup
    # Group damage/heal events by ability name, sorted by time
    dmg_by_ability: Dict[str, List[tuple]] = {}
    for ev in fight.events:
        if ev.source.display_name != entity_name:
            continue
        if not ev.ability or not (ev.is_damage or ev.is_heal) or not ev.result:
            continue
        ab_name = ev.ability.name.strip()
        t = elapsed_seconds(fight.start_time, ev.timestamp)
        dmg_by_ability.setdefault(ab_name, []).append((t, ev))

    # For each activation, find the closest result using the pre-grouped list
    # Use a cursor per ability to avoid rescanning from the start
    cursors: Dict[str, int] = {}
    for entry in entries:
        results = dmg_by_ability.get(entry.ability_name)
        if not results:
            continue
        cursor = cursors.get(entry.ability_name, 0)
        best = None
        best_dt = 999.0
        # Scan forward from cursor (events are time-ordered)
        for j in range(cursor, len(results)):
            t_r, ev = results[j]
            dt = abs(t_r - entry.t_offset)
            if dt < best_dt and dt < 3.0:
                best_dt = dt
                best = ev
                cursors[entry.ability_name] = j + 1  # advance cursor past this match
            elif t_r > entry.t_offset + 3.0:
                break  # past the window, stop scanning

        if best and best.result:
            r = best.result
            if best.is_damage:
                entry.damage   = r.amount if not r.is_miss else 0
                entry.is_miss  = r.is_miss
                entry.is_crit  = r.is_crit
                entry.absorbed = r.absorbed or 0
            elif best.is_heal:
                entry.heal    = r.amount - (r.overheal or 0)
                entry.is_crit = r.is_crit

    # Pass 3: compute gaps
    for i, entry in enumerate(entries):
        if i > 0:
            gap = entry.t_offset - entries[i - 1].t_offset
            entry.gap_before = max(0.0, gap)
        if i < len(entries) - 1:
            entry.gap_after = max(0.0, entries[i + 1].t_offset - entry.t_offset)

    cache[entity_name] = entries
    return entries


# ── Role detection ────────────────────────────────────────────────────────────

def detect_role(fight: Fight, entity_name: str) -> str:
    """
    Infer role from abilities used in the fight.
    Returns: "tank" | "healer" | "dps"
    """
    cache = _get_fight_cache(fight, "_role_cache")
    if entity_name in cache:
        return cache[entity_name]

    stats = fight.entity_stats.get(entity_name)
    if not stats:
        cache[entity_name] = "dps"
        return cache[entity_name]

    rotation = build_rotation(fight, entity_name)
    used_types = {_ability_db.get(e.ability_name).type for e in rotation}

    # Definitive signals
    if "taunt" in used_types:
        cache[entity_name] = "tank"
        return cache[entity_name]
    if stats.healing_done > stats.damage_dealt:
        cache[entity_name] = "healer"
        return cache[entity_name]
    if "heal" in used_types and stats.healing_done > 5000:
        cache[entity_name] = "healer"
        return cache[entity_name]

    cache[entity_name] = "dps"
    return cache[entity_name]


# ── DPS metrics ───────────────────────────────────────────────────────────────

@dataclass
class DpsMetrics:
    entity_name: str
    fight_duration: float
    total_damage: int
    dps: float
    damage_by_type: Dict[str, int]        # energy / kinetic / elemental / internal
    dead_time_total: float                # seconds with no ability pressed
    dead_time_pct: float                  # fraction of fight
    ability_usage: Dict[str, int]         # name → count
    rotation: List[RotationEntry]
    crit_rate: float
    longest_gap: float                    # single longest dead time gap


def analyse_dps(fight: Fight, entity_name: str) -> DpsMetrics:
    cache = _get_fight_cache(fight, "_dps_metrics_cache")
    if entity_name in cache:
        return cache[entity_name]

    stats    = fight.entity_stats.get(entity_name)
    rotation = build_rotation(fight, entity_name)
    dur      = fight.duration_seconds

    # Damage breakdown by type
    dmg_by_type: Dict[str, int] = {}
    for ev in fight.events:
        if (ev.source.display_name == entity_name and ev.is_damage
                and ev.result and not ev.result.is_miss):
            t = (ev.result.dmg_type or "unknown").strip().lower()
            dmg_by_type[t] = dmg_by_type.get(t, 0) + ev.result.amount

    # Dead time
    dead_gaps = [e.gap_before for e in rotation if e.gap_before > DEAD_TIME_WARN]
    dead_total = sum(dead_gaps)
    longest    = max(dead_gaps, default=0.0)

    # Ability usage counts
    ab_counts: Dict[str, int] = {}
    for e in rotation:
        ab_counts[e.ability_name] = ab_counts.get(e.ability_name, 0) + 1

    # Crit rate
    hits  = stats.hits  if stats else 0
    crits = stats.crits if stats else 0

    metrics = DpsMetrics(
        entity_name=entity_name,
        fight_duration=dur,
        total_damage=stats.damage_dealt if stats else 0,
        dps=fight.dps(entity_name),
        damage_by_type=dmg_by_type,
        dead_time_total=dead_total,
        dead_time_pct=dead_total / dur if dur else 0,
        ability_usage=ab_counts,
        rotation=rotation,
        crit_rate=crits / hits if hits else 0,
        longest_gap=longest,
    )
    cache[entity_name] = metrics
    return metrics


# ── Tank metrics ──────────────────────────────────────────────────────────────

@dataclass
class TankMetrics:
    entity_name: str
    fight_duration: float
    damage_taken: int
    damage_absorbed: int
    taunt_count: int
    interrupt_count: int
    defensive_cooldowns: List[Tuple[float, str]]   # (t_offset, ability_name)
    missed_interrupts: int                          # placeholder — needs cast data
    rotation: List[RotationEntry]
    threat_events: int                              # taunt + threat abilities used


def analyse_tank(fight: Fight, entity_name: str) -> TankMetrics:
    cache = _get_fight_cache(fight, "_tank_metrics_cache")
    if entity_name in cache:
        return cache[entity_name]

    stats    = fight.entity_stats.get(entity_name)
    rotation = build_rotation(fight, entity_name)
    taunts     = 0
    interrupts = 0
    cooldowns: List[Tuple[float, str]] = []

    for entry in rotation:
        info = entry.ability_info
        if info.type == "taunt":
            taunts += 1
        elif info.type == "interrupt":
            interrupts += 1
        elif info.type == "cooldown":
            cooldowns.append((entry.t_offset, entry.ability_name))

    metrics = TankMetrics(
        entity_name=entity_name,
        fight_duration=fight.duration_seconds,
        damage_taken=stats.damage_taken if stats else 0,
        damage_absorbed=stats.damage_absorbed if stats else 0,
        taunt_count=taunts,
        interrupt_count=interrupts,
        defensive_cooldowns=cooldowns,
        missed_interrupts=0,
        rotation=rotation,
        threat_events=taunts,
    )
    cache[entity_name] = metrics
    return metrics


# ── Healer metrics ────────────────────────────────────────────────────────────

@dataclass
class HealerMetrics:
    entity_name: str
    fight_duration: float
    healing_done: int
    hps: float
    overheal_total: int
    overheal_pct: float
    ability_usage: Dict[str, int]
    ability_healing: Dict[str, int]
    targets_healed: Dict[str, int]   # entity → total healing received
    crit_rate: float
    rotation: List[RotationEntry]


def analyse_healer(fight: Fight, entity_name: str) -> HealerMetrics:
    cache = _get_fight_cache(fight, "_healer_metrics_cache")
    if entity_name in cache:
        return cache[entity_name]

    stats    = fight.entity_stats.get(entity_name)
    rotation = build_rotation(fight, entity_name)

    overheal = 0
    raw_heal  = 0
    ab_counts: Dict[str, int] = {}
    ab_heal:   Dict[str, int] = {}
    targets:   Dict[str, int] = {}

    for ev in fight.events:
        if ev.source.display_name != entity_name or not ev.is_heal or not ev.result:
            continue
        r = ev.result
        raw_heal += r.amount
        overheal += (r.overheal or 0)

        ab_name = ev.ability.name.strip() if ev.ability else "Unknown"
        ab_counts[ab_name] = ab_counts.get(ab_name, 0) + 1
        eff = r.amount - (r.overheal or 0)
        ab_heal[ab_name]   = ab_heal.get(ab_name, 0) + eff

        tgt = ev.target.display_name
        if tgt == "self": tgt = entity_name
        targets[tgt] = targets.get(tgt, 0) + eff

    hits  = stats.hits  if stats else 0
    crits = stats.crits if stats else 0

    metrics = HealerMetrics(
        entity_name=entity_name,
        fight_duration=fight.duration_seconds,
        healing_done=stats.healing_done if stats else 0,
        hps=fight.hps(entity_name),
        overheal_total=overheal,
        overheal_pct=overheal / raw_heal if raw_heal else 0,
        ability_usage=ab_counts,
        ability_healing=ab_heal,
        targets_healed=targets,
        crit_rate=crits / hits if hits else 0,
        rotation=rotation,
    )
    cache[entity_name] = metrics
    return metrics


# ── Comparison Engine ────────────────────────────────────────────────────────

@dataclass
class AbilityComparison:
    """Side-by-side comparison for a single ability."""
    ability_name: str
    ability_type: str           # from AbilityDB
    user_count: int
    ref_count: int
    delta_count: int            # user - ref (negative = underused)
    user_damage: int
    ref_damage: int
    delta_damage: int
    user_healing: int
    ref_healing: int
    delta_healing: int
    user_avg_interval: float    # average seconds between uses
    ref_avg_interval: float
    user_first_use: float       # seconds into fight
    ref_first_use: float
    comment: str                # auto-generated observation


@dataclass
class MetricComparison:
    """One scalar metric compared between two entities."""
    metric_name: str
    user_value: float
    ref_value: float
    delta: float                # user - ref
    pct_delta: float            # percentage difference
    unit: str                   # "dps", "%", "s", "count"
    better_when: str            # "higher" or "lower"
    comment: str


@dataclass
class ComparisonInsight:
    """A single coaching insight derived from the comparison."""
    category: str               # rotation | cooldown | uptime | dead_time | output
    severity: str               # high | medium | low
    impact_estimate: float      # rough DPS/HPS loss estimate (0 if unknown)
    message: str


@dataclass
class ComparisonResult:
    """Full comparison between user_entity and reference_entity in one fight."""
    fight_label: str
    fight_duration: float
    user_name: str
    ref_name: str
    user_role: str
    ref_role: str

    # Scalar metric comparisons
    metrics: List[MetricComparison]

    # Per-ability breakdown
    abilities: List[AbilityComparison]

    # Ranked coaching insights
    insights: List[ComparisonInsight]


def _avg_interval(rotation: List[RotationEntry], ability_name: str) -> float:
    """Average seconds between consecutive uses of one ability."""
    times = [e.t_offset for e in rotation if e.ability_name == ability_name]
    if len(times) < 2:
        return 0.0
    gaps = [times[i+1] - times[i] for i in range(len(times) - 1)]
    return sum(gaps) / len(gaps)


def _first_use(rotation: List[RotationEntry], ability_name: str) -> float:
    """Time of first use of an ability (seconds into fight). -1 if never used."""
    for e in rotation:
        if e.ability_name == ability_name:
            return e.t_offset
    return -1.0


def _ability_damage(fight: Fight, entity_name: str, ability_name: str) -> int:
    stats = fight.entity_stats.get(entity_name)
    if not stats:
        return 0
    ab = stats.abilities_damage.get(ability_name)
    return ab.total_amount if ab else 0


def _ability_healing(fight: Fight, entity_name: str, ability_name: str) -> int:
    stats = fight.entity_stats.get(entity_name)
    if not stats:
        return 0
    ab = stats.abilities_heal.get(ability_name)
    return ab.total_amount if ab else 0


def compare_entities(
    fight: Fight,
    user_name: str,
    ref_name: str,
) -> ComparisonResult:
    """
    Build a full side-by-side comparison of two entities in the same fight.
    user_name = the player seeking coaching.
    ref_name  = the reference / stronger player being compared against.
    """
    cache = _get_fight_cache(fight, "_comparison_cache")
    cache_key = (user_name, ref_name)
    if cache_key in cache:
        return cache[cache_key]

    db = get_db()
    dur = fight.duration_seconds

    # Build rotations
    user_rot = build_rotation(fight, user_name)
    ref_rot  = build_rotation(fight, ref_name)

    # Detect roles
    user_role = detect_role(fight, user_name)
    ref_role  = detect_role(fight, ref_name)

    # Get entity stats
    user_stats = fight.entity_stats.get(user_name)
    ref_stats  = fight.entity_stats.get(ref_name)

    # Get role-aware metrics
    user_dps_m = analyse_dps(fight, user_name)
    ref_dps_m  = analyse_dps(fight, ref_name)

    # ── Scalar metric comparisons ────────────────────────────────────────────
    metrics: List[MetricComparison] = []

    def _add_metric(name, u_val, r_val, unit, better):
        delta = u_val - r_val
        pct = delta / r_val if r_val else 0.0
        if better == "higher":
            good = delta >= 0
        else:
            good = delta <= 0
        if abs(pct) < 0.02:
            comment = "Roughly equal"
        elif good:
            comment = "You're ahead" if better == "higher" else "You're better here"
        else:
            comment = "Behind reference" if better == "higher" else "Needs improvement"
        metrics.append(MetricComparison(
            metric_name=name, user_value=u_val, ref_value=r_val,
            delta=delta, pct_delta=pct, unit=unit,
            better_when=better, comment=comment,
        ))

    u_dps = fight.dps(user_name)
    r_dps = fight.dps(ref_name)
    _add_metric("DPS", u_dps, r_dps, "dps", "higher")

    u_hps = fight.hps(user_name)
    r_hps = fight.hps(ref_name)
    _add_metric("HPS", u_hps, r_hps, "hps", "higher")

    _add_metric("Total Damage",
                user_stats.damage_dealt if user_stats else 0,
                ref_stats.damage_dealt if ref_stats else 0,
                "dmg", "higher")

    _add_metric("Total Healing",
                user_stats.healing_done if user_stats else 0,
                ref_stats.healing_done if ref_stats else 0,
                "heal", "higher")

    _add_metric("Crit Rate", user_dps_m.crit_rate, ref_dps_m.crit_rate, "%", "higher")

    _add_metric("Dead Time %", user_dps_m.dead_time_pct, ref_dps_m.dead_time_pct, "%", "lower")

    _add_metric("Dead Time (s)", user_dps_m.dead_time_total, ref_dps_m.dead_time_total, "s", "lower")

    _add_metric("Ability Presses", len(user_rot), len(ref_rot), "count", "higher")

    # APM (actions per minute)
    u_apm = len(user_rot) / (dur / 60) if dur else 0
    r_apm = len(ref_rot) / (dur / 60) if dur else 0
    _add_metric("APM", u_apm, r_apm, "apm", "higher")

    _add_metric("Longest Gap", user_dps_m.longest_gap, ref_dps_m.longest_gap, "s", "lower")

    # ── Per-ability comparison ───────────────────────────────────────────────
    # Collect all abilities used by either player
    all_abilities: set = set()
    for e in user_rot:
        all_abilities.add(e.ability_name)
    for e in ref_rot:
        all_abilities.add(e.ability_name)

    ability_comps: List[AbilityComparison] = []
    for ab_name in sorted(all_abilities):
        info = db.get(ab_name)
        u_count = sum(1 for e in user_rot if e.ability_name == ab_name)
        r_count = sum(1 for e in ref_rot if e.ability_name == ab_name)
        u_dmg = _ability_damage(fight, user_name, ab_name)
        r_dmg = _ability_damage(fight, ref_name, ab_name)
        u_heal = _ability_healing(fight, user_name, ab_name)
        r_heal = _ability_healing(fight, ref_name, ab_name)
        u_interval = _avg_interval(user_rot, ab_name)
        r_interval = _avg_interval(ref_rot, ab_name)
        u_first = _first_use(user_rot, ab_name)
        r_first = _first_use(ref_rot, ab_name)

        delta_count = u_count - r_count

        # Generate comment
        comment = ""
        if u_count == 0 and r_count > 0:
            comment = f"Reference used this {r_count}x — you never pressed it"
        elif r_count == 0 and u_count > 0:
            comment = f"You used this {u_count}x — reference never did"
        elif delta_count < -2:
            comment = "Likely delayed or underused"
        elif delta_count > 3:
            comment = "Possible filler overuse"
        elif abs(delta_count) <= 1:
            comment = "Similar usage"
        elif delta_count < 0:
            comment = "Slightly underused"
        else:
            comment = "Slightly more than reference"

        # Refine comment with cooldown knowledge
        if info.cooldown_sec > 15 and delta_count < -1:
            expected = dur / max(info.cooldown_sec, 1)
            comment = (f"High-CD ability underused by {abs(delta_count)} "
                       f"(~{expected:.0f} possible in {dur:.0f}s)")

        ability_comps.append(AbilityComparison(
            ability_name=ab_name,
            ability_type=info.type,
            user_count=u_count,
            ref_count=r_count,
            delta_count=delta_count,
            user_damage=u_dmg,
            ref_damage=r_dmg,
            delta_damage=u_dmg - r_dmg,
            user_healing=u_heal,
            ref_healing=r_heal,
            delta_healing=u_heal - r_heal,
            user_avg_interval=u_interval,
            ref_avg_interval=r_interval,
            user_first_use=u_first,
            ref_first_use=r_first,
            comment=comment,
        ))

    # Sort abilities: biggest |delta_damage + delta_healing| first (most impactful)
    ability_comps.sort(
        key=lambda a: -(abs(a.delta_damage) + abs(a.delta_healing) + abs(a.delta_count) * 100)
    )

    # ── Generate coaching insights ───────────────────────────────────────────
    insights: List[ComparisonInsight] = []

    # DPS gap
    dps_gap = r_dps - u_dps
    if dps_gap > 50:
        insights.append(ComparisonInsight(
            category="output",
            severity="high" if dps_gap > 500 else "medium",
            impact_estimate=dps_gap,
            message=(f"Your DPS is {u_dps:,.0f} vs reference's {r_dps:,.0f} "
                     f"— a gap of {dps_gap:,.0f} DPS."),
        ))

    # Dead time gap
    dt_gap = user_dps_m.dead_time_pct - ref_dps_m.dead_time_pct
    if dt_gap > 0.05:
        lost_dps = u_dps * dt_gap  # rough estimate
        insights.append(ComparisonInsight(
            category="dead_time",
            severity="high" if dt_gap > 0.15 else "medium",
            impact_estimate=lost_dps,
            message=(f"You had {user_dps_m.dead_time_pct:.0%} dead time vs "
                     f"reference's {ref_dps_m.dead_time_pct:.0%}. "
                     f"That's ~{user_dps_m.dead_time_total - ref_dps_m.dead_time_total:.1f}s "
                     f"more inactivity. Queue your next ability before the current "
                     f"one finishes."),
        ))

    # Underused high-value abilities
    for ac in ability_comps:
        if ac.delta_count < -2 and ac.ref_damage > 0:
            lost = ac.ref_damage - ac.user_damage
            if lost > 0:
                insights.append(ComparisonInsight(
                    category="rotation",
                    severity="high" if lost > 5000 else "medium",
                    impact_estimate=lost / dur if dur else 0,
                    message=(f"'{ac.ability_name}' — you used it {ac.user_count}x vs "
                             f"{ac.ref_count}x. That's {abs(ac.delta_count)} fewer uses "
                             f"and ~{lost:,} less damage from this ability alone."),
                ))

    # Filler overuse
    for ac in ability_comps:
        info = db.get(ac.ability_name)
        if (ac.delta_count > 3 and info.cooldown_sec == 0
                and info.type == "damage"
                and ac.user_damage < ac.ref_damage):
            insights.append(ComparisonInsight(
                category="rotation",
                severity="medium",
                impact_estimate=0,
                message=(f"'{ac.ability_name}' was used {ac.delta_count} more times "
                         f"than reference — possible filler overuse. Replace some "
                         f"with higher-priority abilities."),
            ))

    # Cooldown misuse (high-CD abilities with late first use)
    for ac in ability_comps:
        info = db.get(ac.ability_name)
        if info.cooldown_sec >= 30 and ac.ref_first_use >= 0 and ac.user_first_use >= 0:
            delay = ac.user_first_use - ac.ref_first_use
            if delay > 5.0:
                insights.append(ComparisonInsight(
                    category="cooldown",
                    severity="medium",
                    impact_estimate=0,
                    message=(f"'{ac.ability_name}' — you first used it at "
                             f"{ac.user_first_use:.1f}s, reference at "
                             f"{ac.ref_first_use:.1f}s. "
                             f"That's a {delay:.1f}s delay on a key cooldown."),
                ))

    # Abilities reference used that user never pressed
    for ac in ability_comps:
        if ac.user_count == 0 and ac.ref_count >= 2 and ac.ref_damage > 0:
            insights.append(ComparisonInsight(
                category="rotation",
                severity="high" if ac.ref_damage > 3000 else "low",
                impact_estimate=ac.ref_damage / dur if dur else 0,
                message=(f"Reference used '{ac.ability_name}' {ac.ref_count}x "
                         f"for {ac.ref_damage:,} damage. You never pressed it."),
            ))

    # Crit rate gap
    crit_gap = ref_dps_m.crit_rate - user_dps_m.crit_rate
    if crit_gap > 0.08:
        insights.append(ComparisonInsight(
            category="output",
            severity="medium",
            impact_estimate=0,
            message=(f"Crit rate: {user_dps_m.crit_rate:.0%} vs "
                     f"reference's {ref_dps_m.crit_rate:.0%}. "
                     f"Check gear stats and crit buffs/adrenals."),
        ))

    # Sort insights by impact estimate (highest first), then severity
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    insights.sort(key=lambda i: (severity_rank.get(i.severity, 3), -i.impact_estimate))

    # Cap at top 7 insights to avoid noise
    insights = insights[:7]

    result = ComparisonResult(
        fight_label=fight.label,
        fight_duration=dur,
        user_name=user_name,
        ref_name=ref_name,
        user_role=user_role,
        ref_role=ref_role,
        metrics=metrics,
        abilities=ability_comps,
        insights=insights,
    )
    cache[cache_key] = result
    return result
