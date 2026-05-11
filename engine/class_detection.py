"""
W.I.S.E. Panda — Class & Discipline Detection (Phase C)
========================================================

Determine a player's (class, discipline) for a given fight.

How SWTOR works
---------------
Each character belongs to a *family* — Tech or Force — and within that family
can hold up to two advanced classes, each with three disciplines. So one
character can be associated with up to 6 disciplines across its life. Class
and discipline can be switched outside of combat, never during. A *fight* is
therefore locked to exactly one (class, discipline) per participant, even
though a character's history might span many.

Detection strategy
------------------
Three sources, in order of authority:

1. **DisciplineChanged events** (confidence 1.0) — the game itself emits
   these for every player when they zone in or change spec.

2. **Stance buffs** (confidence 1.0) — Dark Charge, Soresu Form, etc.
   When a player has a stance buff applied during the fight, that's
   essentially a self-declaration of discipline.

3. **Ability fingerprint vote** — fallback for players who never emit
   DisciplineChanged AND have no stance buff. We tally signature abilities
   the player USED during the fight (presses, prebuffs, AND damage
   sources, weighted differently). Each ability votes for one or more
   (class, discipline) pairs.

Why we vote on damage_source AND presses
-----------------------------------------
A bystander player may walk through the fight and only formally "press" 1-2
abilities (auto-attacks aside) but have many DoT ticks recorded as damage
sources. Those DoT ticks ARE diagnostic. Pure-press voting misses bystanders.

Weights:
- Pressed: 1.0× (intentional choice)
- Prebuff: 1.0× (also intentional)
- Damage source: 0.3× (attenuated; DoT ticks shouldn't drown out presses)

Public API
----------
- detect_class(events, character_name, ability_counts=None) -> ClassDetection
- ClassDetection: class_name, discipline_name, confidence, evidence
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional


# ─── Result type ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ClassDetection:
    """
    Result of detecting a player's class and discipline for one fight.

    evidence examples:
      "declared:DisciplineChanged"
      "stance:Dark Charge"
      "voted:Tracer Missile=18,Heatseeker Missiles=9"
      ""
    """
    class_name: str = ""
    discipline_name: str = ""
    confidence: float = 0.0
    evidence: str = ""

    @property
    def is_known(self) -> bool:
        return bool(self.class_name)


# ─── DisciplineChanged regex ────────────────────────────────────────────────

DISCIPLINE_CHANGED_RE = re.compile(
    r"""
    (?P<class_name>[^/{]+?)   \s* \{ \d+ \}
    \s* / \s*
    (?P<discipline_name>[^{]+?) \s* \{ \d+ \}
    """,
    re.VERBOSE,
)


# ─── Stance table ───────────────────────────────────────────────────────────
#
# Stances are persistent buffs that map to a specific (class, discipline).
# Detected by walking events for ApplyEffect/ReapplyEffect with a matching
# effect_detail name. Confidence ~1.0.
#
# Note: "Plasma Cell" name collides between Commando/Assault Specialist and
# Vanguard/Plasmatech. Stance hits for collisions go through the ambiguous
# table — class only, discipline blank — and let fingerprint refine.

_STANCE_TABLE: dict[str, tuple[str, Optional[str]]] = {
    # Force / Empire
    "Dark Charge":              ("Assassin",   "Darkness"),
    "Surging Charge":           ("Assassin",   "Deception"),
    "Lightning Charge":         ("Assassin",   "Hatred"),
    "Soresu Form":              ("Juggernaut", "Immortal"),
    "Shien Form":               ("Juggernaut", "Vengeance"),
    "Juyo Form":                ("Marauder",   "Annihilation"),
    "Ataru Form":               ("Marauder",   "Carnage"),
    # Force / Republic
    "Combat Technique":         ("Shadow",     "Kinetic Combat"),
    "Force Technique":          ("Shadow",     "Infiltration"),
    # Tech
    "Acid Blade":               ("Operative",  "Concealment"),
    "Combustible Gas Cylinder": ("Mercenary",  "Innovative Ordnance"),
    "Combat Support Cylinder":  ("Mercenary",  "Bodyguard"),
    "Ion Cell":                 ("Vanguard",   "Shield Specialist"),
    "High Energy Cell":         ("Vanguard",   "Tactics"),
}

_AMBIGUOUS_STANCES: dict[str, list[tuple[str, str]]] = {
    "Plasma Cell": [
        ("Commando", "Assault Specialist"),
        ("Vanguard", "Plasmatech"),
    ],
}


# ─── Ability fingerprint table ──────────────────────────────────────────────
#
# Entry: ability_name -> (class, discipline_or_None, weight)
# Weights:
#   10 = iconic
#   5-8 = strongly indicative
#   2-3 = class-confirming only (discipline=None)

_FINGERPRINT_TABLE_BY_CLASS: dict[str, list[tuple[str, Optional[str], int]]] = {
    # ─── Tech / Empire ─────────────────────────────────────────────────────
    "Mercenary": [
        ("Tracer Missile",         "Arsenal",              10),
        ("Heatseeker Missiles",    "Arsenal",               8),
        ("Blazing Bolts",          "Arsenal",               6),
        ("Mag Shot",               "Innovative Ordnance",  10),
        ("Serrated Shot",          "Innovative Ordnance",   8),
        ("Incendiary Missile",     "Innovative Ordnance",   6),
        ("Kolto Shell",            "Bodyguard",            10),
        ("Kolto Missile",          "Bodyguard",             6),
        ("Healing Scan",           "Bodyguard",             5),
        ("Progressive Scan",       "Bodyguard",             5),
        ("Progressive Kolto Scan", "Bodyguard",             5),
        ("Power Shot",             None,                    3),
        ("Rapid Shots",            None,                    2),
        ("Unload",                 None,                    2),
        ("Death from Above",       None,                    3),
    ],
    "Powertech": [
        ("Magnetic Blast",         "Advanced Prototype",   10),
        ("Energy Burst",           "Advanced Prototype",    8),
        ("Immolate",               "Pyrotech",             10),
        ("Searing Wave",           "Pyrotech",              8),
        ("Incendiary Missile",     "Pyrotech",              5),
        ("Heat Blast",             "Shield Tech",          10),
        ("Oil Slick",              "Shield Tech",           7),
        ("Heat Screen",            "Shield Tech",           5),
        ("Rapid Shots",            None,                    2),
        ("Flame Burst",            None,                    3),
        ("Rocket Punch",           None,                    2),
        ("Flame Sweep",            None,                    2),
    ],
    "Operative": [
        # Concealment — added Laceration, Collateral Strike, Acid Blade
        # from real-log evidence (Really Easy fight, Apr 24)
        ("Backstab",               "Concealment",          10),
        ("Veiled Strike",          "Concealment",           8),
        ("Laceration",             "Concealment",          10),  # NEW
        ("Collateral Strike",      "Concealment",          10),  # NEW
        ("Acid Blade",             "Concealment",          10),  # NEW
        ("Volatile Substance",     "Concealment",           7),
        ("Hidden Strike",          "Concealment",           8),
        # Lethality
        ("Toxic Blast",            "Lethality",            10),
        ("Corrosive Dart",         "Lethality",             7),
        ("Corrosive Assault",      "Lethality",             6),
        ("Corrosive Grenade",      "Lethality",             5),
        ("Cull",                   "Lethality",             8),
        # Medicine
        ("Kolto Probe",            "Medicine",             10),
        ("Kolto Infusion",         "Medicine",              7),
        ("Recuperative Nanotech",  "Medicine",              7),
        ("Surgical Probe",         "Medicine",              6),
        ("Diagnostic Scan",        "Medicine",              4),
        # Class-confirming
        ("Rifle Shot",             None,                    2),
        ("Stim Boost",             None,                    3),
        ("Overload Shot",          None,                    2),
        ("Fragmentation Grenade",  None,                    2),
        ("Stealth",                None,                    2),
        ("Shiv",                   None,                    3),
    ],
    "Sniper": [
        ("Ambush",                 "Marksmanship",         10),
        ("Followthrough",          "Marksmanship",          8),
        ("Penetrating Blasts",     "Marksmanship",          6),
        ("Plasma Probe",           "Engineering",          10),
        ("Interrogation Probe",    "Engineering",           8),
        ("Series of Shots",        "Engineering",           5),
        ("Cull",                   "Virulence",            10),
        ("Weakening Blast",        "Virulence",             8),
        ("Rifle Shot",             None,                    2),
        ("Snipe",                  None,                    3),
        ("Cover Pulse",            None,                    2),
    ],

    # ─── Tech / Republic (mirrors) ─────────────────────────────────────────
    "Commando": [
        ("Grav Round",             "Gunnery",              10),
        ("Demolition Round",       "Gunnery",               8),
        ("Boltstorm",              "Gunnery",               6),
        ("High Impact Bolt",       "Assault Specialist",   10),
        ("Serrated Bolt",          "Assault Specialist",    8),
        ("Incendiary Round",       "Assault Specialist",    6),
        ("Trauma Probe",           "Combat Medic",         10),
        ("Kolto Bomb",             "Combat Medic",          6),
        ("Medical Probe",          "Combat Medic",          5),
        ("Advanced Medical Probe", "Combat Medic",          5),
        ("Charged Bolts",          None,                    3),
        ("Hammer Shot",            None,                    2),
        ("Full Auto",              None,                    2),
    ],
    "Vanguard": [
        ("Ion Wave",               "Plasmatech",           10),
        ("Plasmatize",             "Plasmatech",            8),
        ("Tactical Surge",         "Tactics",              10),
        ("Cell Burst",             "Tactics",               8),
        ("Energy Blast",           "Shield Specialist",    10),
        ("Riot Gas",               "Shield Specialist",     7),
        ("Energy Shield",          "Shield Specialist",     5),
        ("Hammer Shot",            None,                    2),
        ("Ion Pulse",              None,                    3),
        ("Stockstrike",            None,                    2),
    ],
    "Scoundrel": [
        ("Back Blast",             "Scrapper",             10),
        ("Blaster Volley",         "Scrapper",              8),
        ("Sucker Punch",           "Scrapper",              7),
        ("Blood Boiler",           "Ruffian",              10),
        ("Vital Shot",             "Ruffian",               5),
        ("Brutal Shots",           "Ruffian",               6),
        ("Slow-release Medpac",    "Sawbones",             10),
        ("Kolto Cloud",            "Sawbones",              7),
        ("Kolto Infusion",         "Sawbones",              6),
        ("Emergency Medpac",       "Sawbones",              6),
        ("Flurry of Bolts",        None,                    2),
        ("Smuggler's Luck",        None,                    3),
    ],
    "Gunslinger": [
        ("Aimed Shot",             "Sharpshooter",         10),
        ("Trickshot",              "Sharpshooter",          8),
        ("Penetrating Rounds",     "Sharpshooter",          6),
        ("Incendiary Grenade",     "Saboteur",             10),
        ("Sabotage Charge",        "Saboteur",              8),
        ("Speed Shot",             "Saboteur",              5),
        ("Wounding Shots",         "Dirty Fighting",       10),
        ("Hemorrhaging Blast",     "Dirty Fighting",        8),
        ("Flurry of Bolts",        None,                    2),
        ("Charged Burst",          None,                    3),
    ],

    # ─── Force / Empire ────────────────────────────────────────────────────
    "Juggernaut": [
        ("Ravage",                 "Vengeance",            10),
        ("Shatter",                "Vengeance",             8),
        ("Impale",                 "Vengeance",             6),
        ("Force Crush",            "Rage",                 10),
        ("Furious Strike",         "Rage",                  8),
        ("Vengeful Slam",          "Rage",                  7),
        ("Crushing Blow",          "Immortal",             10),
        ("Aegis Assault",          "Immortal",              8),
        ("Backhand",               "Immortal",              6),
        ("Vicious Slash",          None,                    3),
        ("Sundering Assault",      None,                    3),
        ("Force Charge",           None,                    2),
    ],
    "Marauder": [
        ("Annihilate",             "Annihilation",         10),
        ("Rupture",                "Annihilation",          7),
        ("Deadly Saber",           "Annihilation",          6),
        ("Massacre",               "Carnage",              10),
        ("Gore",                   "Carnage",               8),
        ("Devastating Blast",      "Carnage",               7),
        ("Ferocious Strike",       "Carnage",               6),
        ("Furious Strike",         "Fury",                 10),
        ("Raging Burst",           "Fury",                  8),
        ("Vicious Slash",          None,                    3),
        ("Battering Assault",      None,                    3),
        ("Force Charge",           None,                    2),
    ],
    "Assassin": [
        # Deception — Maul moved out (it's class-shared, not Deception-only)
        ("Voltaic Slash",          "Deception",            10),
        ("Ball Lightning",         "Deception",             7),
        ("Phantom Stride",         "Deception",             7),
        ("Static Charge",          "Deception",             5),
        # Hatred
        ("Demolish",               "Hatred",               10),
        ("Leeching Strike",        "Hatred",                8),
        ("Death Field",            "Hatred",                7),
        ("Creeping Terror",        "Hatred",                6),
        # Darkness — added Discharge from real-log evidence
        ("Wither",                 "Darkness",             10),
        ("Depredating Volts",      "Darkness",              8),
        ("Dark Ward",              "Darkness",              5),
        ("Discharge",              "Darkness",              5),  # NEW
        # Class-confirming (Maul, Thrash, Saber Strike, Shock, Force Pull
        # all used by all 3 disciplines)
        ("Maul",                   None,                    3),  # FIXED
        ("Thrash",                 None,                    3),
        ("Saber Strike",           None,                    2),
        ("Shock",                  None,                    2),
        ("Force Pull",             None,                    2),
        ("Lacerate",               None,                    2),
    ],
    "Sorcerer": [
        ("Thundering Blast",       "Lightning",            10),
        ("Chain Lightning",        "Lightning",             7),
        ("Lightning Bolt",         "Lightning",             5),
        ("Lightning Flash",        "Lightning",             6),
        ("Affliction",             "Madness",               5),
        ("Force Leech",            "Madness",               7),
        ("Demolish",               "Madness",               7),  # downgraded
        ("Death Field",            "Madness",               5),
        ("Innervate",              "Corruption",           10),
        ("Resurgence",             "Corruption",            7),
        ("Roaming Mend",           "Corruption",            7),
        ("Revivification",         "Corruption",            6),
        ("Force Lightning",        None,                    3),
        ("Shock",                  None,                    2),
    ],

    # ─── Force / Republic (mirrors) ────────────────────────────────────────
    "Guardian": [
        ("Master Strike",          "Vigilance",            10),
        ("Plasma Brand",           "Vigilance",             8),
        ("Overhead Slash",         "Vigilance",             6),
        ("Force Exhaustion",       "Focus",                10),
        ("Concentrated Slice",     "Focus",                 8),
        ("Zealous Leap",           "Focus",                 6),
        ("Guardian Slash",         "Defense",              10),
        ("Blade Storm",            "Defense",               7),
        ("Riposte",                "Defense",               5),
        ("Slash",                  None,                    3),
        ("Sundering Strike",       None,                    3),
        ("Force Leap",             None,                    2),
    ],
    "Sentinel": [
        ("Merciless Slash",        "Watchman",             10),
        ("Cauterize",              "Watchman",              7),
        ("Overload Saber",         "Watchman",              6),
        ("Blade Rush",             "Combat",               10),
        ("Precision",              "Combat",                8),
        ("Defensive Forms",        "Combat",                6),
        ("Concentrated Slice",     "Concentration",        10),
        ("Zealous Leap",           "Concentration",         8),
        ("Force Exhaustion",       "Concentration",         6),
        ("Strike",                 None,                    2),
        ("Zealous Strike",         None,                    3),
        ("Force Leap",             None,                    2),
    ],
    "Shadow": [
        ("Shadow Strike",          "Infiltration",         10),
        ("Clairvoyant Strike",     "Infiltration",          7),
        ("Psychokinetic Blast",    "Infiltration",          7),
        ("Vanquish",               "Serenity",             10),
        ("Force Breach",           "Serenity",              6),
        ("Sever Force",            "Serenity",              5),
        ("Slow Time",              "Kinetic Combat",       10),
        ("Cascading Debris",       "Kinetic Combat",        8),
        ("Kinetic Ward",           "Kinetic Combat",        5),
        ("Double Strike",          None,                    3),
        ("Saber Strike",           None,                    2),
        ("Project",                None,                    2),
    ],
    "Sage": [
        ("Turbulence",             "Telekinetics",         10),
        ("Telekinetic Wave",       "Telekinetics",          7),
        ("Telekinetic Burst",      "Telekinetics",          5),
        ("Vanquish",               "Balance",              10),
        ("Weaken Mind",            "Balance",               5),
        ("Force Serenity",         "Balance",               7),
        ("Healing Trance",         "Seer",                 10),
        ("Rejuvenate",             "Seer",                  7),
        ("Wandering Mend",         "Seer",                  7),
        ("Salvation",              "Seer",                  6),
        ("Telekinetic Throw",      None,                    3),
        ("Project",                None,                    2),
    ],
}


def _flatten_fingerprint_table() -> dict[str, list[tuple[str, Optional[str], int]]]:
    table: dict[str, list[tuple[str, Optional[str], int]]] = {}
    for class_name, entries in _FINGERPRINT_TABLE_BY_CLASS.items():
        for ability_name, discipline, weight in entries:
            table.setdefault(ability_name, []).append((class_name, discipline, weight))
    return table


_FINGERPRINT_TABLE: dict[str, list[tuple[str, Optional[str], int]]] = (
    _flatten_fingerprint_table()
)


# ─── Public detection entry point ──────────────────────────────────────────


# Scoring weights for the three count types.
_WEIGHT_PRESSED = 1.0
_WEIGHT_PREBUFF = 1.0
_WEIGHT_DAMAGE_SOURCE = 0.3


def detect_class(
    events: Iterable,
    character_name: str,
    ability_counts: Optional[dict] = None,
) -> ClassDetection:
    """
    Determine (class, discipline) for `character_name`.

    Strategy:
      1. DisciplineChanged in events → declared, conf 1.0
      2. Stance buff in events → stance, conf 1.0
      3. Fingerprint vote on ability_counts (or events if no counts)
      4. Empty result if nothing fires

    `ability_counts` shape (when provided):
      {(ability_id, ability_name): {"pressed": int, "prebuff": int,
                                     "damage_source": int}}
    Produced by encounter_db's _player_character_ability_counts_full().
    When None, falls back to walking events for AbilityActivate.
    """
    name = (character_name or "").strip()
    if not name:
        return ClassDetection()

    events_list = list(events)

    declared = _detect_via_discipline_changed(events_list, name)
    if declared.is_known:
        return declared

    stance = _detect_via_stance(events_list, name)
    if stance.is_known:
        return stance

    return _detect_via_fingerprint(events_list, name, ability_counts)


# ─── Path 1: DisciplineChanged ──────────────────────────────────────────────


def _detect_via_discipline_changed(events_list, character_name: str) -> ClassDetection:
    last_class = ""
    last_discipline = ""

    for ev in events_list:
        if ev.effect_type != "DisciplineChanged":
            continue
        if not ev.source or not ev.source.player:
            continue
        if ev.source.player.strip() != character_name:
            continue
        raw = getattr(ev, "effect_detail_raw", "") or ""
        m = DISCIPLINE_CHANGED_RE.search(raw)
        if not m:
            continue
        last_class = (m.group("class_name") or "").strip()
        last_discipline = (m.group("discipline_name") or "").strip()

    if last_class:
        return ClassDetection(
            class_name=last_class,
            discipline_name=last_discipline,
            confidence=1.0,
            evidence="declared:DisciplineChanged",
        )
    return ClassDetection()


# ─── Path 2: Stance buff ────────────────────────────────────────────────────


def _detect_via_stance(events_list, character_name: str) -> ClassDetection:
    """
    Walk events for ApplyEffect / ReapplyEffect events that match a known
    stance. Stance buffs are persistent and tightly coupled to a discipline.

    Returns confidence 1.0 — as authoritative as DisciplineChanged.
    """
    found_stances: dict[str, int] = {}

    for ev in events_list:
        if ev.effect_type not in ("ApplyEffect", "ReapplyEffect"):
            continue
        if not ev.source or not ev.source.player:
            continue
        if ev.source.companion:
            continue
        if ev.source.player.strip() != character_name:
            continue
        if ev.effect_detail is None:
            continue
        applied_name = (ev.effect_detail.name or "").strip()
        if not applied_name:
            continue
        if applied_name in _STANCE_TABLE:
            found_stances[applied_name] = found_stances.get(applied_name, 0) + 1

    if not found_stances:
        return ClassDetection()

    best_stance = max(found_stances.items(), key=lambda x: x[1])
    stance_name = best_stance[0]
    class_name, discipline = _STANCE_TABLE[stance_name]
    return ClassDetection(
        class_name=class_name,
        discipline_name=discipline or "",
        confidence=1.0,
        evidence=f"stance:{stance_name}",
    )


# ─── Path 3: Ability fingerprint vote ──────────────────────────────────────


_MIN_DISCIPLINE_SCORE = 6   # was 10 — lowered, now that damage_source helps
_MIN_CLASS_SCORE = 3


def _detect_via_fingerprint(
    events_list,
    character_name: str,
    ability_counts: Optional[dict] = None,
) -> ClassDetection:
    """
    Tally signature ability uses across the fight and vote.

    With ability_counts, voting weighs pressed + prebuff + damage_source
    (with attenuation on damage_source so DoT ticks don't drown presses).
    Without ability_counts, walks events for AbilityActivate (compat path).
    """
    signal: dict[str, float] = {}

    if ability_counts is not None:
        # Aggregated counts path. Weight by the three counter types.
        for (ability_id, ability_name), counts in ability_counts.items():
            name = (ability_name or "").strip()
            if not name:
                continue
            score = (
                _WEIGHT_PRESSED        * float(counts.get("pressed", 0))
                + _WEIGHT_PREBUFF        * float(counts.get("prebuff", 0))
                + _WEIGHT_DAMAGE_SOURCE  * float(counts.get("damage_source", 0))
            )
            if score > 0:
                signal[name] = signal.get(name, 0.0) + score
    else:
        # Compat path: presses-only.
        for ev in events_list:
            if not ev.is_ability_activate:
                continue
            if not ev.source or not ev.source.player:
                continue
            if ev.source.companion:
                continue
            if ev.source.player.strip() != character_name:
                continue
            if ev.ability is None:
                continue
            name = (ev.ability.name or "").strip()
            if not name:
                continue
            signal[name] = signal.get(name, 0.0) + 1.0

    if not signal:
        return ClassDetection()

    class_scores: dict[str, float] = {}
    discipline_scores: dict[tuple[str, str], float] = {}
    contributing: dict[str, float] = {}

    for ability_name, count in signal.items():
        votes = _FINGERPRINT_TABLE.get(ability_name)
        if not votes:
            continue
        for class_name, discipline_or_none, weight in votes:
            score = weight * count
            class_scores[class_name] = class_scores.get(class_name, 0.0) + score
            if discipline_or_none is not None:
                key = (class_name, discipline_or_none)
                discipline_scores[key] = discipline_scores.get(key, 0.0) + score
            contributing[ability_name] = count

    if not class_scores:
        return ClassDetection()

    best_class = max(class_scores.items(), key=lambda x: x[1])
    class_name, class_score = best_class

    if class_score < _MIN_CLASS_SCORE:
        return ClassDetection()

    best_discipline = ""
    discipline_score = 0.0
    for (cls, disc), score in discipline_scores.items():
        if cls != class_name:
            continue
        if score > discipline_score:
            best_discipline = disc
            discipline_score = score
    if discipline_score < _MIN_DISCIPLINE_SCORE:
        best_discipline = ""

    confidence = min(0.9, class_score / 50.0)

    top = sorted(contributing.items(), key=lambda x: -x[1])[:4]
    bits = ",".join(
        f"{a}={int(c)}" if c == int(c) else f"{a}={c:.1f}"
        for a, c in top
    )
    evidence = f"voted:{bits}" if bits else ""

    return ClassDetection(
        class_name=class_name,
        discipline_name=best_discipline,
        confidence=confidence,
        evidence=evidence,
    )
