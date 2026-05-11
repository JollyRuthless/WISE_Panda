"""
W.I.S.E. Panda — Combat Log Aggregator
Groups events into fights and computes per-entity stats.
Supports lazy loading: fights are scanned quickly on file open,
but events are only parsed and aggregated when a fight is selected.
"""

from dataclasses import dataclass, field
from datetime import time
from typing import List, Dict, Optional
from enum import Enum
from engine.parser_core import LogEvent, parse_file, parse_line, _open_log


class EntityKind(Enum):
    PLAYER       = "player"
    GROUP_MEMBER = "group_member"
    COMPANION    = "companion"
    NPC          = "npc"
    HAZARD       = "hazard"


# If SWTOR misses an ExitCombat line, treat a long quiet period as a fight boundary.
INACTIVITY_SPLIT_SECONDS = 15.0
CURRENT_FIGHT_LABEL = "Current Fight"
BOSS_FIGHT_HP_THRESHOLD = 100_000
BOSS_DOMINANT_DAMAGE_SHARE = 0.60
BOSS_HP_RATIO_THRESHOLD = 1.5
BOSS_MIN_DURATION_SECONDS = 60.0
DAY_SECONDS = 86400.0
HALF_DAY_SECONDS = DAY_SECONDS / 2
MIN_DISPLAY_DURATION_SECONDS = 1.0
HAZARD_KEYWORDS = (
    "lava", "fire", "flame", "burn", "burning", "poison", "acid", "toxic",
    "gas", "radiation", "electric", "lightning", "storm", "floor", "ground",
    "void", "beam", "hazard", "environment",
)


def time_to_seconds(t: time) -> float:
    return t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1_000_000


def seconds_between(a: time, b: time) -> float:
    delta = time_to_seconds(b) - time_to_seconds(a)
    if delta < -HALF_DAY_SECONDS:
        delta += DAY_SECONDS
    elif delta > HALF_DAY_SECONDS:
        delta -= DAY_SECONDS
    return delta


def elapsed_seconds(start: time, point: time) -> float:
    return max(seconds_between(start, point), 0.0)


def display_duration_for_metric(duration_seconds: float) -> float:
    if duration_seconds <= 0:
        return 0.0
    return max(duration_seconds, MIN_DISPLAY_DURATION_SECONDS)


def active_window_seconds(start: Optional[time], end: Optional[time]) -> float:
    if start is None or end is None:
        return 0.0
    return max(seconds_between(start, end), MIN_DISPLAY_DURATION_SECONDS)


def per_second(total_amount: int, duration_seconds: float) -> float:
    window = display_duration_for_metric(duration_seconds)
    if total_amount <= 0 or window == 0:
        return 0.0
    return total_amount / window


def _has_long_gap(prev_ts: Optional[time], next_ts: Optional[time]) -> bool:
    if prev_ts is None or next_ts is None:
        return False
    return seconds_between(prev_ts, next_ts) > INACTIVITY_SPLIT_SECONDS


def _finalize_scanned_fight(
    fights: List["Fight"],
    current: Optional["Fight"],
    end_time: Optional[time],
    end_line: int,
    npc_max_hp: Dict[str, int],
) -> Optional["Fight"]:
    if current is None:
        return None
    if end_time:
        current.end_time = end_time
    current._line_end = end_line
    if npc_max_hp:
        current.boss_max_hp = max(npc_max_hp.values())
        if not current.boss_name:
            current.boss_name = max(npc_max_hp, key=npc_max_hp.get)
    fights.append(current)
    return None


@dataclass
class AbilityStats:
    name: str
    hits: int = 0
    crits: int = 0
    misses: int = 0
    total_amount: int = 0
    max_hit: int = 0
    total_absorbed: int = 0

    @property
    def crit_rate(self) -> float:
        return self.crits / self.hits if self.hits else 0.0

    @property
    def avg_hit(self) -> float:
        return self.total_amount / self.hits if self.hits else 0.0


@dataclass
class EntityStats:
    name: str
    kind: "EntityKind" = EntityKind.NPC
    damage_dealt: int = 0
    damage_taken: int = 0
    healing_done: int = 0
    healing_received: int = 0
    damage_absorbed: int = 0
    hits: int = 0
    crits: int = 0
    misses: int = 0
    deaths: int = 0
    abilities_damage: Dict[str, AbilityStats] = field(default_factory=dict)
    abilities_heal: Dict[str, AbilityStats] = field(default_factory=dict)
    damage_timeline: List[tuple] = field(default_factory=list)
    heal_timeline: List[tuple] = field(default_factory=list)

    @property
    def crit_rate(self) -> float:
        return self.crits / self.hits if self.hits else 0.0


@dataclass
class Fight:
    index: int
    start_time: time
    end_time: Optional[time] = None
    events: List[LogEvent] = field(default_factory=list)
    entity_stats: Dict[str, EntityStats] = field(default_factory=dict)
    player_name: Optional[str] = None
    boss_name: Optional[str] = None
    boss_max_hp: int = 0
    boss_damage_share: float = 0.0
    boss_hp_ratio: float = 0.0
    custom_name: Optional[str] = None

    # Lazy loading support
    _log_path: Optional[str] = field(default=None, repr=False)
    _line_start: int = 0          # first line of this fight in the log file
    _line_end: int = 0            # last line (inclusive)
    _loaded: bool = False         # True once events are parsed and aggregated

    @property
    def duration_seconds(self) -> float:
        if self.end_time is None: return 0.0
        return max(seconds_between(self.start_time, self.end_time), 0.001)

    @property
    def display_duration_seconds(self) -> float:
        if self.end_time is None:
            return 0.0
        return display_duration_for_metric(self.duration_seconds)

    @property
    def duration_str(self) -> str:
        s = self.duration_seconds
        return f"{int(s // 60)}:{int(s % 60):02d}"

    @property
    def label(self) -> str:
        name = self.custom_name or self.boss_name or "Unknown Encounter"
        return f"#{self.index} - {name}  ({self.duration_str})"

    @property
    def is_boss_like(self) -> bool:
        return (
            self.duration_seconds >= BOSS_MIN_DURATION_SECONDS
            and
            self.boss_damage_share >= BOSS_DOMINANT_DAMAGE_SHARE
            and self.boss_hp_ratio >= BOSS_HP_RATIO_THRESHOLD
            and self.boss_max_hp >= BOSS_FIGHT_HP_THRESHOLD
        )

    def get_or_create(self, name: str, kind: "EntityKind" = EntityKind.NPC) -> EntityStats:
        if name not in self.entity_stats:
            self.entity_stats[name] = EntityStats(name=name, kind=kind)
        return self.entity_stats[name]

    def dps(self, entity_name: str) -> float:
        s = self.entity_stats.get(entity_name)
        if not s:
            return 0.0
        return per_second(s.damage_dealt, self.duration_seconds)

    def active_dps(self, entity_name: str) -> float:
        damage_events = [
            ev for ev in self.events
            if (
                ev.is_damage
                and ev.result
                and not ev.result.is_miss
                and ev.source.display_name == entity_name
            )
        ]
        if not damage_events:
            return 0.0
        start = damage_events[0].timestamp
        end = damage_events[-1].timestamp
        total = sum(ev.result.amount for ev in damage_events if ev.result)
        return per_second(total, active_window_seconds(start, end))

    def boss_dps(self, entity_name: str) -> float:
        if self.display_duration_seconds == 0 or not self.boss_name or self.boss_name == CURRENT_FIGHT_LABEL:
            return 0.0
        total = sum(
            ev.result.amount
            for ev in self.events
            if (
                ev.is_damage
                and ev.result
                and not ev.result.is_miss
                and ev.source.display_name == entity_name
                and ev.target.npc
                and ev.target.display_name == self.boss_name
            )
        )
        return per_second(total, self.duration_seconds)

    def hps(self, entity_name: str) -> float:
        s = self.entity_stats.get(entity_name)
        if not s:
            return 0.0
        return per_second(s.healing_done, self.duration_seconds)

    def ensure_loaded(self):
        """Parse events and aggregate stats on demand. No-op if already loaded."""
        if self._loaded:
            return
        if self._log_path and self._line_end > self._line_start:
            events = _load_lines(self._log_path, self._line_start, self._line_end)
            self.events = events
        aggregate_fight(self)
        self._loaded = True


def _load_lines(path: str, line_start: int, line_end: int) -> List[LogEvent]:
    """Read and parse only the lines in [line_start, line_end] from a log file."""
    events = []
    with _open_log(path) as f:
        for i, raw in enumerate(f):
            if i < line_start:
                continue
            if i > line_end:
                break
            ev = parse_line(raw)
            if ev and not ev.is_enter_combat and not ev.is_exit_combat:
                events.append(ev)
    return events


def load_raw_lines(path: str, line_start: int, line_end: int) -> List[str]:
    """Read raw log lines in [line_start, line_end] exactly as stored in the file."""
    lines: List[str] = []
    with _open_log(path) as f:
        for i, raw in enumerate(f):
            if i < line_start:
                continue
            if i > line_end:
                break
            lines.append(raw.rstrip("\r\n"))
    return lines


# ── Fast fight boundary scanner ──────────────────────────────────────────────

def _parse_timestamp(line: str) -> Optional[time]:
    """Extract timestamp from a raw log line without full parsing."""
    if len(line) < 13 or line[0] != '[':
        return None
    try:
        ts_str = line[1:13]  # "HH:MM:SS.mmm"
        parts = ts_str.split(".")
        hms = parts[0].split(":")
        micro = int(parts[1].ljust(6, "0")[:6]) if len(parts) > 1 else 0
        return time(int(hms[0]), int(hms[1]), int(hms[2]), micro)
    except (ValueError, IndexError):
        return None


def _detect_boss_from_line(line: str) -> Optional[str]:
    """Try to extract NPC name and max HP from a raw line for boss detection."""
    # Look for NPC entity pattern with HP: name {id}:instance|(x,y,z,r)|(hp/maxhp)
    import re
    # Quick check — skip lines without the HP pattern
    if "/(" not in line:
        return None
    m = re.search(r'([^{|\]]+?)\s*\{(\d+)\}:\d+\|[^|]+\|\((\d+)/(\d+)\)', line)
    if m:
        name = m.group(1).strip()
        maxhp = int(m.group(4))
        # Skip player entities (they start after @)
        if '@' not in line[:line.find(name)]:
            return f"{name}|{maxhp}"
    return None


def _score_encounter_candidates(events: List[LogEvent], player_name: Optional[str]) -> dict[str, dict]:
    candidates: Dict[str, dict] = {}

    def ensure(ent) -> dict:
        name = ent.display_name.strip()
        row = candidates.setdefault(name, {
            "name": name,
            "max_hp": 0,
            "damage_taken_from_players": 0,
            "damage_done_to_players": 0,
            "combat_events": 0,
        })
        row["max_hp"] = max(row["max_hp"], ent.maxhp or 0)
        return row

    for ev in events:
        if ev.is_damage and ev.result:
            if ev.target.npc:
                if _kind_from_entity(ev.target, player_name, role="target") == EntityKind.HAZARD:
                    pass
                else:
                    row = ensure(ev.target)
                    row["combat_events"] += 1
                    if _kind_from_entity(ev.source, player_name, ev.ability.name if ev.ability else "", ev.effect_name, "source") not in (EntityKind.NPC, EntityKind.HAZARD) and not ev.result.is_miss:
                        row["damage_taken_from_players"] += ev.result.amount
            if ev.source.npc:
                if _kind_from_entity(ev.source, player_name, role="source") == EntityKind.HAZARD:
                    pass
                else:
                    row = ensure(ev.source)
                    row["combat_events"] += 1
                    if _kind_from_entity(ev.target, player_name, ev.ability.name if ev.ability else "", ev.effect_name, "target") not in (EntityKind.NPC, EntityKind.HAZARD) and not ev.result.is_miss:
                        row["damage_done_to_players"] += ev.result.amount
            continue

        if ev.target.npc and ev.effect_type not in ("Event",):
            if _kind_from_entity(ev.target, player_name, ev.ability.name if ev.ability else "", ev.effect_name, "target") != EntityKind.HAZARD:
                ensure(ev.target)["combat_events"] += 1
        if ev.source.npc and ev.effect_type not in ("Event",):
            if _kind_from_entity(ev.source, player_name, ev.ability.name if ev.ability else "", ev.effect_name, "source") != EntityKind.HAZARD:
                ensure(ev.source)["combat_events"] += 1

    return candidates


def choose_encounter_name(events: List[LogEvent], player_name: Optional[str]) -> Optional[str]:
    summary = summarize_encounter(events, player_name)
    return summary["name"] if summary else None


def summarize_encounter(events: List[LogEvent], player_name: Optional[str]) -> Optional[dict]:
    candidates = _score_encounter_candidates(events, player_name)
    if not candidates:
        return None

    scored = sorted(
        candidates.values(),
        key=lambda row: (
            1 if (row["damage_taken_from_players"] > 0 or row["damage_done_to_players"] > 0) else 0,
            row["damage_taken_from_players"],
            row["damage_done_to_players"],
            row["max_hp"],
            row["combat_events"],
            row["name"].lower(),
        ),
        reverse=True,
    )
    best = scored[0]
    if (
        best["damage_taken_from_players"] <= 0
        and best["damage_done_to_players"] <= 0
        and best["combat_events"] <= 0
    ):
        return None
    total_player_damage = sum(max(row["damage_taken_from_players"], 0) for row in candidates.values())
    sorted_hp = sorted((row["max_hp"] for row in candidates.values()), reverse=True)
    next_highest_hp = sorted_hp[1] if len(sorted_hp) > 1 else 0
    damage_share = (best["damage_taken_from_players"] / total_player_damage) if total_player_damage else 0.0
    hp_ratio = (best["max_hp"] / next_highest_hp) if next_highest_hp > 0 else float("inf")
    return {
        **best,
        "damage_share": damage_share,
        "next_highest_hp": next_highest_hp,
        "hp_ratio": hp_ratio,
    }


def scan_fights(path: str) -> List[Fight]:
    """
    Fast scan of a combat log file to find fight boundaries.
    Returns Fight objects with line ranges but NO parsed events or stats.
    Much faster than full parsing — only reads lines looking for combat markers.
    """
    fights: List[Fight] = []
    current: Optional[Fight] = None
    fight_idx = 0
    npc_max_hp: Dict[str, int] = {}
    last_ts: Optional[time] = None
    last_line_num = 0
    raw_line = ""

    with _open_log(path) as f:
        for line_num, raw_line in enumerate(f):
            raw_line = raw_line.rstrip("\r\n")
            if not raw_line:
                continue

            ts = _parse_timestamp(raw_line)

            # Missing ExitCombat lines can glue wipes and re-pulls together.
            # Split the current fight if the log goes quiet for a while.
            if current is not None and _has_long_gap(last_ts, ts):
                current = _finalize_scanned_fight(
                    fights, current, last_ts, last_line_num, npc_max_hp
                )
                npc_max_hp = {}

            if "EnterCombat" in raw_line:
                if current is None:
                    fight_idx += 1
                    if not ts:
                        continue
                    # Try to detect player name from the source entity
                    player = None
                    if "[@" in raw_line:
                        import re
                        pm = re.search(r'\[@([^#/|\]]+)', raw_line)
                        if pm:
                            player = pm.group(1)
                    current = Fight(
                        index=fight_idx,
                        start_time=ts,
                        player_name=player,
                        _log_path=path,
                        _line_start=line_num,
                    )
                    npc_max_hp = {}

            elif "ExitCombat" in raw_line:
                if current is not None:
                    current = _finalize_scanned_fight(
                        fights, current, ts, line_num, npc_max_hp
                    )
                    npc_max_hp = {}

            elif current is not None:
                # Track NPC max HP for boss detection without full parsing
                boss_info = _detect_boss_from_line(raw_line)
                if boss_info:
                    name, hp_str = boss_info.rsplit("|", 1)
                    hp = int(hp_str)
                    npc_max_hp[name] = max(npc_max_hp.get(name, 0), hp)

            if ts:
                last_ts = ts
                last_line_num = line_num

    # Handle unclosed fight
    if current is not None:
        current = _finalize_scanned_fight(
            fights, current, last_ts, last_line_num, npc_max_hp
        )

    return fights


def resolve_fight_names(path: str, fights: List[Fight]) -> None:
    """
    Populate boss_name for scanned fights in one pass through the file.
    This is more robust than the lightweight regex scan but much cheaper
    than fully loading and aggregating every fight up front.
    """
    if not fights:
        return

    fight_idx = 0
    fight_events: List[LogEvent] = []

    with _open_log(path) as f:
        for line_num, raw_line in enumerate(f):
            while fight_idx < len(fights) and line_num > fights[fight_idx]._line_end:
                fight = fights[fight_idx]
                summary = summarize_encounter(fight_events, fight.player_name)
                if summary:
                    fight.boss_name = summary["name"]
                    fight.boss_max_hp = max(fight.boss_max_hp, summary["max_hp"])
                    fight.boss_damage_share = summary["damage_share"]
                    fight.boss_hp_ratio = summary["hp_ratio"]
                fight_events = []
                fight_idx += 1

            if fight_idx >= len(fights):
                break

            fight = fights[fight_idx]
            if line_num < fight._line_start:
                continue
            if line_num > fight._line_end:
                continue

            ev = parse_line(raw_line)
            if not ev:
                continue
            fight_events.append(ev)

    while fight_idx < len(fights):
        fight = fights[fight_idx]
        summary = summarize_encounter(fight_events, fight.player_name)
        if summary:
            fight.boss_name = summary["name"]
            fight.boss_max_hp = max(fight.boss_max_hp, summary["max_hp"])
            fight.boss_damage_share = summary["damage_share"]
            fight.boss_hp_ratio = summary["hp_ratio"]
        fight_events = []
        fight_idx += 1


def attach_log_ranges(fights: List[Fight], path: str) -> None:
    """
    Add file-backed line ranges to eagerly-built fights.

    Live mode builds fights from parsed in-memory events, but review tools need
    the original log path and line window to load raw lines and infer metadata.
    """
    if not fights or not path:
        return

    scanned_fights = scan_fights(path)
    unmatched = list(scanned_fights)

    for fight in fights:
        match = next((item for item in unmatched if item.start_time == fight.start_time), None)
        if match is None:
            continue
        fight._log_path = match._log_path or path
        fight._line_start = match._line_start
        fight._line_end = match._line_end
        unmatched.remove(match)


# ── Full event-based flow (used by live mode) ────────────────────────────────

def detect_fights(events: List[LogEvent]) -> List[Fight]:
    fights, current, fight_idx = [], None, 0
    last_ts: Optional[time] = None
    for ev in events:
        if current is not None and _has_long_gap(last_ts, ev.timestamp):
            current.end_time = last_ts
            fights.append(current)
            current = None

        if ev.is_enter_combat:
            if current is None:
                fight_idx += 1
                player = ev.source.player if (ev.source.player and not ev.source.companion) else None
                current = Fight(
                    index=fight_idx,
                    start_time=ev.timestamp,
                    player_name=player,
                    boss_name=CURRENT_FIGHT_LABEL,
                )
        elif ev.is_exit_combat:
            if current is not None:
                current.end_time = ev.timestamp
                fights.append(current)
                current = None
        else:
            if current is not None:
                current.events.append(ev)
        last_ts = ev.timestamp
    if current is not None and current.events:
        current.end_time = current.events[-1].timestamp
        fights.append(current)
    return fights


def _text_looks_hazardous(*values: str) -> bool:
    text = " ".join((value or "").strip().lower() for value in values)
    return any(keyword in text for keyword in HAZARD_KEYWORDS)


def _kind_from_entity(
    entity,
    player_name: Optional[str] = None,
    ability_name: str = "",
    effect_name: str = "",
    role: str = "",
) -> "EntityKind":
    if entity.companion:
        return EntityKind.COMPANION
    if entity.player:
        if player_name and entity.player != player_name:
            return EntityKind.GROUP_MEMBER
        return EntityKind.PLAYER
    if entity.is_empty and role == "source":
        return EntityKind.HAZARD
    if entity.npc:
        name = entity.display_name.strip()
        if _text_looks_hazardous(name, ability_name, effect_name):
            if entity.maxhp in (None, 0, 1) or role == "source":
                return EntityKind.HAZARD
    return EntityKind.NPC


def aggregate_fight(fight: Fight):
    for ev in fight.events:
        t_offset = elapsed_seconds(fight.start_time, ev.timestamp)

        if ev.is_damage and ev.result:
            src = ev.source.display_name
            tgt = ev.target.display_name
            r   = ev.result
            ab  = ev.ability.name if ev.ability else "Unknown"
            src_kind = _kind_from_entity(ev.source, fight.player_name, ab, ev.effect_name, "source")
            tgt_kind = _kind_from_entity(ev.target, fight.player_name, ab, ev.effect_name, "target")
            src_name = src if src else ("Environment" if src_kind == EntityKind.HAZARD else "unknown")
            tgt_name = tgt if tgt else ("Environment" if tgt_kind == EntityKind.HAZARD else "unknown")
            src_stats = fight.get_or_create(src_name, src_kind)
            tgt_stats = fight.get_or_create(tgt_name, tgt_kind)
            if r.is_miss:
                src_stats.misses += 1
                src_stats.abilities_damage.setdefault(ab, AbilityStats(name=ab)).misses += 1
            else:
                src_stats.damage_dealt  += r.amount
                src_stats.hits          += 1
                src_stats.crits         += int(r.is_crit)
                src_stats.damage_absorbed += (r.absorbed or 0)
                src_stats.damage_timeline.append((t_offset, r.amount))
                tgt_stats.damage_taken  += r.amount
                ab_s = src_stats.abilities_damage.setdefault(ab, AbilityStats(name=ab))
                ab_s.hits   += 1
                ab_s.crits  += int(r.is_crit)
                ab_s.total_amount += r.amount
                ab_s.max_hit = max(ab_s.max_hit, r.amount)
                ab_s.total_absorbed += (r.absorbed or 0)

        elif ev.is_heal and ev.result:
            src = ev.source.display_name
            tgt = ev.target.display_name
            if tgt == "self": tgt = src
            r   = ev.result
            ab  = ev.ability.name if ev.ability else "Unknown"
            eff = r.amount - (r.overheal or 0)
            src_kind = _kind_from_entity(ev.source, fight.player_name, ab, ev.effect_name, "source")
            tgt_kind = _kind_from_entity(ev.target, fight.player_name, ab, ev.effect_name, "target")
            src_name = src if src else ("Environment" if src_kind == EntityKind.HAZARD else "unknown")
            tgt_name = tgt if tgt else ("Environment" if tgt_kind == EntityKind.HAZARD else "unknown")
            src_stats = fight.get_or_create(src_name, src_kind)
            tgt_stats = fight.get_or_create(tgt_name, tgt_kind)
            src_stats.healing_done     += eff
            src_stats.hits             += 1
            src_stats.crits            += int(r.is_crit)
            src_stats.heal_timeline.append((t_offset, eff))
            tgt_stats.healing_received += eff
            ab_s = src_stats.abilities_heal.setdefault(ab, AbilityStats(name=ab))
            ab_s.hits        += 1
            ab_s.crits       += int(r.is_crit)
            ab_s.total_amount += eff
            ab_s.max_hit = max(ab_s.max_hit, eff)

    summary = summarize_encounter(fight.events, fight.player_name)
    if summary:
        fight.boss_name = summary["name"]
        fight.boss_max_hp = summary["max_hp"]
        fight.boss_damage_share = summary["damage_share"]
        fight.boss_hp_ratio = summary["hp_ratio"]
    elif not fight.boss_name:
        fight.boss_name = CURRENT_FIGHT_LABEL


def build_mob_damage_breakdown(fight: Fight, hide_companions: bool = False) -> List[dict]:
    """
    Group NPC targets in a fight and summarize who contributed damage to each one.
    Mobs are merged by display name + NPC entity id across multiple instances.
    """
    mobs: Dict[str, dict] = {}
    hp_by_instance: Dict[str, int] = {}

    for ev in fight.events:
        tgt = ev.target
        if not tgt.npc:
            continue

        mob_name = tgt.display_name.strip()
        npc_entity_id = tgt.npc_entity_id or ""
        mob_key = f"{mob_name}|{npc_entity_id}"
        mob_row = mobs.setdefault(mob_key, {
            "mob_key": mob_key,
            "mob_name": mob_name,
            "npc_entity_id": npc_entity_id,
            "max_hp_seen": 0,
            "instances": set(),
            "defeats": 0,
            "total_damage_taken": 0,
            "contributors": {},
        })
        mob_row["max_hp_seen"] = max(mob_row["max_hp_seen"], tgt.maxhp or 0)
        if tgt.npc_instance:
            mob_row["instances"].add(tgt.npc_instance)

        # Track observed deaths by watching HP cross to 0 on individual instances.
        instance_key = f"{mob_key}|{tgt.npc_instance or ''}"
        if tgt.hp is not None:
            previous_hp = hp_by_instance.get(instance_key)
            if tgt.hp <= 0 and (previous_hp is None or previous_hp > 0):
                mob_row["defeats"] += 1
            hp_by_instance[instance_key] = tgt.hp

        if not (ev.is_damage and ev.result and not ev.result.is_miss):
            continue

        src = ev.source
        src_kind = _kind_from_entity(src, fight.player_name, ev.ability.name if ev.ability else "", ev.effect_name, "source")
        if src_kind not in (EntityKind.PLAYER, EntityKind.GROUP_MEMBER, EntityKind.COMPANION):
            continue
        if hide_companions and src.companion:
            continue

        contributor_name = src.display_name.strip()
        contributor = mob_row["contributors"].setdefault(contributor_name, {
            "name": contributor_name,
            "kind": src_kind,
            "damage": 0,
            "hits": 0,
            "crits": 0,
            "max_hit": 0,
            "absorbed": 0,
        })
        contributor["damage"] += ev.result.amount
        contributor["hits"] += 1
        contributor["crits"] += int(ev.result.is_crit)
        contributor["max_hit"] = max(contributor["max_hit"], ev.result.amount)
        contributor["absorbed"] += ev.result.absorbed or 0
        mob_row["total_damage_taken"] += ev.result.amount

    output: List[dict] = []
    for mob_row in mobs.values():
        contributors = list(mob_row["contributors"].values())
        contributors.sort(key=lambda item: (-item["damage"], item["name"].lower()))
        total_damage = mob_row["total_damage_taken"]
        for contributor in contributors:
            contributor["share"] = (contributor["damage"] / total_damage) if total_damage else 0.0
            contributor["crit_rate"] = (contributor["crits"] / contributor["hits"]) if contributor["hits"] else 0.0
            contributor["avg_hit"] = (contributor["damage"] / contributor["hits"]) if contributor["hits"] else 0.0

        top = contributors[0] if contributors else None
        output.append({
            "mob_key": mob_row["mob_key"],
            "mob_name": mob_row["mob_name"],
            "npc_entity_id": mob_row["npc_entity_id"],
            "max_hp_seen": mob_row["max_hp_seen"],
            "instances_seen": max(len(mob_row["instances"]), 1),
            "defeats": mob_row["defeats"],
            "total_damage_taken": total_damage,
            "top_contributor": top["name"] if top else "—",
            "top_share": top["share"] if top else 0.0,
            "contributors": contributors,
        })

    output.sort(key=lambda item: (-item["total_damage_taken"], -item["max_hp_seen"], item["mob_name"].lower()))
    return output


def build_fights(events: List[LogEvent]) -> List[Fight]:
    """Full eager parse+aggregate. Used by live mode."""
    fights = detect_fights(events)
    for f in fights:
        f._loaded = True
        aggregate_fight(f)
    return fights
