"""
ui_roster/roles.py — class+discipline → role lookup.

SWTOR has 16 advanced classes (8 Empire + 8 mirrored Republic) × 3 disciplines
each = 48 specs total. Each spec has exactly one role: Tank, Healer, or DPS.

This is pure data — no logic, no DB. Imported by the Roster comparison panel
to filter "other players' fights on this boss" by role match.

The class+discipline strings here MUST match what class_detection.py produces.
Source of truth for those strings is _FINGERPRINT_TABLE_BY_CLASS in
class_detection.py (top-level keys are class names; the second tuple element
in the entries is the discipline name).
"""

from __future__ import annotations


# Role constants. Strings, not an enum — the rest of the project uses strings
# for class/discipline so role stays consistent with that.
ROLE_TANK = "Tank"
ROLE_HEALER = "Healer"
ROLE_DPS = "DPS"
ROLE_UNKNOWN = "Unknown"

ROLES = (ROLE_TANK, ROLE_HEALER, ROLE_DPS)


# Source of truth: every (class, discipline) → role for the 48 specs.
#
# Layout note: Empire/Republic mirrors are listed together so it's obvious
# nothing got missed when the table was hand-built. Don't reformat to one
# line per pair — the visual block-per-mirror is part of the audit trail.

ROLE_BY_SPEC: dict[tuple[str, str], str] = {
    # ─── Juggernaut / Guardian ──────────────────────────────────────────────
    ("Juggernaut", "Immortal"):   ROLE_TANK,
    ("Guardian",   "Defense"):    ROLE_TANK,
    ("Juggernaut", "Vengeance"):  ROLE_DPS,
    ("Guardian",   "Vigilance"):  ROLE_DPS,
    ("Juggernaut", "Rage"):       ROLE_DPS,
    ("Guardian",   "Focus"):      ROLE_DPS,

    # ─── Powertech / Vanguard ───────────────────────────────────────────────
    ("Powertech", "Shield Tech"):        ROLE_TANK,
    ("Vanguard",  "Shield Specialist"):  ROLE_TANK,
    ("Powertech", "Pyrotech"):           ROLE_DPS,
    ("Vanguard",  "Plasmatech"):         ROLE_DPS,
    ("Powertech", "Advanced Prototype"): ROLE_DPS,
    ("Vanguard",  "Tactics"):            ROLE_DPS,

    # ─── Assassin / Shadow ──────────────────────────────────────────────────
    ("Assassin", "Darkness"):       ROLE_TANK,
    ("Shadow",   "Kinetic Combat"): ROLE_TANK,
    ("Assassin", "Deception"):      ROLE_DPS,
    ("Shadow",   "Infiltration"):   ROLE_DPS,
    ("Assassin", "Hatred"):         ROLE_DPS,
    ("Shadow",   "Serenity"):       ROLE_DPS,

    # ─── Mercenary / Commando ───────────────────────────────────────────────
    ("Mercenary", "Bodyguard"):           ROLE_HEALER,
    ("Commando",  "Combat Medic"):        ROLE_HEALER,
    ("Mercenary", "Arsenal"):             ROLE_DPS,
    ("Commando",  "Gunnery"):             ROLE_DPS,
    ("Mercenary", "Innovative Ordnance"): ROLE_DPS,
    ("Commando",  "Assault Specialist"):  ROLE_DPS,

    # ─── Operative / Scoundrel ──────────────────────────────────────────────
    ("Operative", "Medicine"):    ROLE_HEALER,
    ("Scoundrel", "Sawbones"):    ROLE_HEALER,
    ("Operative", "Concealment"): ROLE_DPS,
    ("Scoundrel", "Scrapper"):    ROLE_DPS,
    ("Operative", "Lethality"):   ROLE_DPS,
    ("Scoundrel", "Ruffian"):     ROLE_DPS,

    # ─── Sorcerer / Sage ────────────────────────────────────────────────────
    ("Sorcerer", "Corruption"):   ROLE_HEALER,
    ("Sage",     "Seer"):         ROLE_HEALER,
    ("Sorcerer", "Lightning"):    ROLE_DPS,
    ("Sage",     "Telekinetics"): ROLE_DPS,
    ("Sorcerer", "Madness"):      ROLE_DPS,
    ("Sage",     "Balance"):      ROLE_DPS,

    # ─── Marauder / Sentinel ────────────────────────────────────────────────
    # Pure-DPS class. All three disciplines deal damage.
    ("Marauder", "Annihilation"):  ROLE_DPS,
    ("Sentinel", "Watchman"):      ROLE_DPS,
    ("Marauder", "Carnage"):       ROLE_DPS,
    ("Sentinel", "Combat"):        ROLE_DPS,
    ("Marauder", "Fury"):          ROLE_DPS,
    ("Sentinel", "Concentration"): ROLE_DPS,

    # ─── Sniper / Gunslinger ────────────────────────────────────────────────
    # Pure-DPS class. All three disciplines deal damage.
    ("Sniper",     "Marksmanship"):    ROLE_DPS,
    ("Gunslinger", "Sharpshooter"):    ROLE_DPS,
    ("Sniper",     "Engineering"):     ROLE_DPS,
    ("Gunslinger", "Saboteur"):        ROLE_DPS,
    ("Sniper",     "Virulence"):       ROLE_DPS,
    ("Gunslinger", "Dirty Fighting"):  ROLE_DPS,
}


def role_for(class_name: str, discipline_name: str) -> str:
    """
    Return the role for a given (class, discipline) pair.

    Returns ROLE_UNKNOWN when:
      - class or discipline is empty (e.g. Phase C couldn't detect)
      - the pair isn't in the lookup table (typo, new spec, mirror typo)

    Treat ROLE_UNKNOWN as "don't filter this row out, but warn the UI."
    The Roster app shows Unknown rows when the role filter is off and
    hides them when filtering — same as any other role.
    """
    if not class_name or not discipline_name:
        return ROLE_UNKNOWN
    return ROLE_BY_SPEC.get((class_name, discipline_name), ROLE_UNKNOWN)


def all_classes_for_role(role: str) -> list[tuple[str, str]]:
    """
    Every (class, discipline) pair that fills a given role. Useful for
    debugging or for future "show me all healers" queries.
    """
    return sorted(
        spec for spec, r in ROLE_BY_SPEC.items() if r == role
    )
