"""
W.I.S.E. Panda — SWTOR Combat Log Parser
Parses raw log lines into structured Python objects.
"""

import re
import json
from dataclasses import dataclass, field
from datetime import time
from typing import Optional
from functools import lru_cache
from pathlib import Path

# ── Entity regex ──────────────────────────────────────────────────────────────
ENTITY_RE = re.compile(
    r"""
    (?:
        @(?P<player>[^#/|\]]+)
        (?:\#(?P<player_id>\d+))?
        (?:
            /(?P<companion>[^{]+)
            \s*\{(?P<companion_entity_id>\d+)\}
            :(?P<companion_instance>\d+)
        )?
    |
        (?P<npc>[^{|\]]+?)
        \s*\{(?P<npc_entity_id>\d+)\}
        :(?P<npc_instance>\d+)
    )
    \|
    \((?P<x>-?\d+\.\d+),(?P<y>-?\d+\.\d+),(?P<z>-?\d+\.\d+),(?P<r>-?\d+\.\d+)\)
    \|
    \((?P<hp>\d+)/(?P<maxhp>\d+)\)
    """,
    re.VERBOSE,
)

NAMED_THING_RE = re.compile(r"(?P<n>[^{]+?)\s*\{(?P<id>\d+)\}")
ID_ONLY_RE = re.compile(r"^\{(?P<id>\d+)\}$")

DAMAGE_RE = re.compile(
    r"""
    \(
      (?P<amount>\d+)(?P<crit>\*)?
      (?:\s*~(?P<overheal>\d+))?
      (?:\s+(?P<dmg_type>[^{()\-<]+?)\s*\{(?P<dmg_type_id>\d+)\})?
      (?:\s+-(?P<result>[a-z]*)\s*(?:\{(?P<result_id>\d+)\})?)?
      (?:\s+\((?P<absorbed>\d+)\s+absorbed\s*\{(?P<absorbed_id>\d+)\}\))?
      (?:\s*\((?P<reflected>reflected)\s*\{(?P<reflected_id>\d+)\}\))?
    \)
    (?:\s*<(?P<threat>[\d.]+)>)?
    """,
    re.VERBOSE,
)

RESTORE_SPEND_RE = re.compile(r"\((?P<amount>[\d.]+)\)")
CHARGES_RE = re.compile(r"\((?P<charges>\d+)\s+charges")


@dataclass
class Entity:
    is_self: bool = False
    is_empty: bool = False
    player: Optional[str] = None
    player_id: Optional[str] = None
    companion: Optional[str] = None
    companion_entity_id: Optional[str] = None
    companion_instance: Optional[str] = None
    npc: Optional[str] = None
    npc_entity_id: Optional[str] = None
    npc_instance: Optional[str] = None
    x: Optional[float] = None
    y: Optional[float] = None
    z: Optional[float] = None
    hp: Optional[int] = None
    maxhp: Optional[int] = None

    @property
    def display_name(self) -> str:
        if self.is_self:   return "self"
        if self.is_empty:  return ""
        if self.companion: return f"{self.player}/{self.companion}"
        if self.player:    return self.player
        return self.npc or "unknown"

    @property
    def is_player_controlled(self) -> bool:
        return self.player is not None

    @property
    def unique_id(self) -> str:
        if self.companion:       return f"{self.player}/{self.companion}"
        if self.player:          return self.player
        if self.npc_entity_id:   return self.npc_entity_id
        return "unknown"


@dataclass
class NamedThing:
    name: str
    id: str


@dataclass
class DamageResult:
    amount: int
    is_crit: bool
    overheal: Optional[int]
    dmg_type: Optional[str]
    result: Optional[str]
    absorbed: Optional[int]
    threat: Optional[float]

    @property
    def is_miss(self) -> bool:
        return self.result in ("miss", "dodge", "parry", "deflect", "immune", "resist") or (
            self.amount == 0 and self.result in ("shield", "glance", "")
        )

    @property
    def effective_amount(self) -> int:
        return 0 if self.is_miss else self.amount


@dataclass
class LogEvent:
    timestamp: time
    source: Entity
    target: Entity
    ability: Optional[NamedThing]
    effect_type: str
    effect_name: str
    effect_id: str
    effect_detail: Optional[NamedThing]
    result: Optional[DamageResult] = None
    restore_amount: Optional[float] = None
    spend_amount: Optional[float] = None
    charges: Optional[int] = None
    raw_result_text: str = ""
    # Phase C: full unparsed text after the colon in the effect block.
    # parse_named_thing() truncates at the first '{', which silently drops
    # the discipline half of "Operative {id}/Lethality {id}". Keeping the
    # raw string here lets specialized parsers (like DisciplineChanged) get
    # at the full payload without altering the existing parse pipeline.
    effect_detail_raw: str = ""

    @property
    def is_damage(self) -> bool:
        return bool(self.effect_detail and self.effect_detail.name.strip() == "Damage")

    @property
    def is_heal(self) -> bool:
        return bool(self.effect_detail and self.effect_detail.name.strip() == "Heal")

    @property
    def is_enter_combat(self) -> bool:
        return "EnterCombat" in self.effect_name

    @property
    def is_exit_combat(self) -> bool:
        return "ExitCombat" in self.effect_name

    @property
    def is_ability_activate(self) -> bool:
        return "AbilityActivate" in self.effect_name


# ── Parsing helpers ───────────────────────────────────────────────────────────

def parse_entity(raw: str) -> Entity:
    raw = raw.strip()
    if raw == "=":   return Entity(is_self=True)
    if raw == "":    return Entity(is_empty=True)
    m = ENTITY_RE.match(raw)
    if not m:        return Entity(is_empty=True)
    g = m.groupdict()
    return Entity(
        player=g.get("player"),
        player_id=g.get("player_id"),
        companion=g.get("companion"),
        companion_entity_id=g.get("companion_entity_id"),
        companion_instance=g.get("companion_instance"),
        npc=g.get("npc"),
        npc_entity_id=g.get("npc_entity_id"),
        npc_instance=g.get("npc_instance"),
        x=float(g["x"]) if g.get("x") else None,
        y=float(g["y"]) if g.get("y") else None,
        z=float(g["z"]) if g.get("z") else None,
        hp=int(g["hp"]) if g.get("hp") else None,
        maxhp=int(g["maxhp"]) if g.get("maxhp") else None,
    )


def parse_named_thing(raw: str) -> Optional[NamedThing]:
    raw = raw.strip()
    if not raw: return None
    id_only = ID_ONLY_RE.match(raw)
    if id_only:
        thing_id = id_only.group("id")
        resolved = _lookup_ability_name_by_id(thing_id)
        if resolved:
            return NamedThing(name=resolved, id=thing_id)
        return NamedThing(name=f"Unknown Ability [{thing_id}]", id=thing_id)
    m = NAMED_THING_RE.match(raw)
    if m:       return NamedThing(name=m.group("n").strip(), id=m.group("id"))
    return NamedThing(name=raw, id="")


@lru_cache(maxsize=1)
def _ability_name_map_by_id() -> dict[str, str]:
    db_path = Path(__file__).parent.parent / "data" / "abilities.json"
    if not db_path.exists():
        return {}
    try:
        raw = json.loads(db_path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    mapping: dict[str, str] = {}
    for name, info in raw.get("abilities", {}).items():
        thing_id = str(info.get("id", "")).strip()
        if thing_id:
            mapping[thing_id] = name
    return mapping


def _lookup_ability_name_by_id(thing_id: str) -> Optional[str]:
    return _ability_name_map_by_id().get(thing_id)


def parse_effect_block(raw: str):
    raw = raw.strip()
    m = re.match(r"(\w+)\s*\{(\d+)\}(?::\s*(.*))?$", raw, re.DOTALL)
    if not m:
        return "Unknown", raw, "", None, ""
    effect_type = m.group(1)
    effect_id   = m.group(2)
    detail_raw  = (m.group(3) or "").strip()
    detail      = parse_named_thing(detail_raw)
    return effect_type, detail.name if detail else "", effect_id, detail, detail_raw


def parse_result(raw: str, effect_type: str, effect_detail):
    raw = raw.strip()
    if not raw: return None, None, None, None
    cm = CHARGES_RE.match(raw)
    if cm: return None, None, None, int(cm.group("charges"))
    if effect_type in ("Spend", "Restore"):
        rsm = RESTORE_SPEND_RE.match(raw)
        if rsm:
            amount = float(rsm.group("amount"))
            if effect_type == "Spend":
                return None, None, amount, None
            return None, amount, None, None
    dm = DAMAGE_RE.match(raw)
    if dm:
        g = dm.groupdict()
        dr = DamageResult(
            amount=int(g["amount"]),
            is_crit=g["crit"] == "*",
            overheal=int(g["overheal"]) if g.get("overheal") else None,
            dmg_type=(g.get("dmg_type") or "").strip() or None,
            result=g.get("result"),
            absorbed=int(g["absorbed"]) if g.get("absorbed") else None,
            threat=float(g["threat"]) if g.get("threat") else None,
        )
        return dr, None, None, None
    return None, None, None, None


LINE_RE = re.compile(
    r"^\[(?P<ts>\d{2}:\d{2}:\d{2}\.\d+)\]\s+"
    r"\[(?P<source>[^\]]*)\]\s+"
    r"\[(?P<target>[^\]]*)\]\s+"
    r"\[(?P<ability>[^\]]*)\]\s+"
    r"\[(?P<effect>[^\]]*)\]"
    r"(?:\s+(?P<result_raw>.*))?$",
    re.DOTALL,
)


def parse_line(line: str) -> Optional[LogEvent]:
    line = line.rstrip("\r\n")
    m = LINE_RE.match(line)
    if not m: return None
    g = m.groupdict()
    ts_str = g["ts"]
    parts = ts_str.split(".")
    hms = parts[0].split(":")
    ts = time(int(hms[0]), int(hms[1]), int(hms[2]),
              int(parts[1].ljust(6, "0")[:6]) if len(parts) > 1 else 0)
    source = parse_entity(g["source"])
    target = parse_entity(g["target"])
    ability = parse_named_thing(g["ability"])
    effect_type, effect_name, effect_id, effect_detail, effect_detail_raw = parse_effect_block(g["effect"])
    result_raw = (g.get("result_raw") or "").strip()
    dmg_result, restore_amt, spend_amt, charges = parse_result(result_raw, effect_type, effect_detail)
    return LogEvent(
        timestamp=ts, source=source, target=target, ability=ability,
        effect_type=effect_type, effect_name=effect_name, effect_id=effect_id,
        effect_detail=effect_detail, result=dmg_result, restore_amount=restore_amt,
        spend_amount=spend_amt, charges=charges, raw_result_text=result_raw,
        effect_detail_raw=effect_detail_raw,
    )


def _open_log(path: str):
    """Open a combat log with smart encoding detection.
    SWTOR logs may be UTF-8 or Windows-1252 depending on system locale.
    Try UTF-8 first (strict), fall back to cp1252, then latin-1 as last resort.
    """
    # A small sample is enough for the UTF-8/cp1252 fallback choice here.
    try:
        with open(path, "rb") as handle:
            raw = handle.read(16384)
    except OSError:
        return open(path, "r", encoding="utf-8", errors="replace")

    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            raw.decode(enc)
            # Success — open with this encoding
            return open(path, "r", encoding=enc, errors="replace")
        except (UnicodeDecodeError, UnicodeError):
            continue

    # Final fallback
    return open(path, "r", encoding="utf-8", errors="replace")


def parse_file(path: str):
    events, errors = [], 0
    with _open_log(path) as f:
        for line in f:
            line = line.strip()
            if not line: continue
            ev = parse_line(line)
            if ev: events.append(ev)
            else:  errors += 1
    return events, errors
