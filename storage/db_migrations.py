"""
W.I.S.E. Panda — Schema Migrations
====================================

Idempotent schema migrations that run at app startup.

This module exists so init_db() in encounter_db.py stays readable. Each
migration is a standalone function that:

  1. Detects whether it has already been applied
  2. If not, takes a timestamped backup of the database file
  3. Applies the migration inside a transaction
  4. Logs what happened to stdout

If a migration is already applied, it does nothing — including no backup.
This means launching the app a second time after migration is free.

Migration v2: per-player-per-fight schema fix
---------------------------------------------

Two tables had primary keys that prevented storing more than one player per
fight:

  player_character_encounters:
      OLD: PRIMARY KEY (encounter_key)
      NEW: PRIMARY KEY (encounter_key, character_id)

  player_character_encounter_abilities:
      OLD: PRIMARY KEY (encounter_key, ability_id, ability_name)
      NEW: PRIMARY KEY (encounter_key, character_id, ability_id, ability_name)

This is the foundation for cohort-based coaching, multi-player history, and
the right-click Deeper View. See A10_PLAN.md for the full picture.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional


# ─── Public entry point ──────────────────────────────────────────────────────


def run_pending_migrations(db_path: Path) -> list[str]:
    """
    Run every migration in order. Each migration is a no-op if already applied.

    Returns a list of human-readable messages about what happened — useful for
    showing the user a "Database upgraded" notice on first launch after an
    update, or just for logging.

    Never raises on "already applied." Raises on actual problems (corrupt
    database, permission errors, etc).
    """
    messages: list[str] = []
    if not db_path.exists():
        # Brand-new install — no migrations needed because init_db will create
        # the new shape from scratch. Don't take a backup of a file that
        # doesn't exist yet.
        return messages

    # Each migration takes the path and returns a message (or None if no-op).
    msg_v2 = _migration_v2_per_player_per_fight(db_path)
    if msg_v2:
        messages.append(msg_v2)

    msg_v3 = _migration_v3_ability_count_columns(db_path)
    if msg_v3:
        messages.append(msg_v3)

    msg_v4 = _migration_v4_class_per_fight(db_path)
    if msg_v4:
        messages.append(msg_v4)

    return messages


# ─── Migration v2 ────────────────────────────────────────────────────────────


def _migration_v2_per_player_per_fight(db_path: Path) -> Optional[str]:
    """
    Upgrade player_character_encounters and player_character_encounter_abilities
    to support multiple players per fight.

    Returns a message describing what was done, or None if already applied.
    """
    if _migration_v2_already_applied(db_path):
        return None

    # Take a backup before doing anything destructive. The filename includes
    # the migration name and a timestamp so it's obvious what it's for and
    # when it was made.
    backup_path = _take_backup(db_path, suffix="before_v2_per_player_migration")

    with sqlite3.connect(str(db_path), timeout=30) as conn:
        # WAL mode is the project default; preserve it.
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass

        # Foreign keys are off by default in SQLite — explicitly disable
        # during migration so we can rename tables without cascade trouble.
        conn.execute("PRAGMA foreign_keys = OFF")

        try:
            conn.execute("BEGIN")
            _migrate_player_character_encounters(conn)
            _migrate_player_character_encounter_abilities(conn)
            conn.execute("COMMIT")
        except Exception:
            # On any error, roll back and let the user keep the backup.
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

    return (
        f"Database upgraded to v2 (per-player-per-fight). "
        f"Backup saved to: {backup_path.name}"
    )


# ─── Migration v2 — detection ────────────────────────────────────────────────


def _migration_v2_already_applied(db_path: Path) -> bool:
    """
    True if the new schema is already in place. We check by inspecting the
    primary key columns of both tables.

    Safe to call on a fresh DB (returns True because the new shape is what
    init_db creates from scratch).
    """
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        # If neither table exists yet (brand new DB about to be initialized),
        # we say "already applied" — init_db will create them in the new
        # shape, no migration needed.
        if not _table_exists(conn, "player_character_encounters"):
            return True

        pce_cols = _primary_key_columns(conn, "player_character_encounters")
        pcea_cols = _primary_key_columns(conn, "player_character_encounter_abilities")

    pce_new = set(pce_cols) == {"encounter_key", "character_id"}
    pcea_new = set(pcea_cols) == {"encounter_key", "character_id", "ability_id", "ability_name"}
    return pce_new and pcea_new


# ─── Migration v2 — table-by-table work ──────────────────────────────────────


def _migrate_player_character_encounters(conn: sqlite3.Connection) -> None:
    """
    Recreate player_character_encounters with PRIMARY KEY (encounter_key, character_id).

    Standard SQLite "rename, recreate, copy, drop" pattern. Done inside the
    caller's transaction.
    """
    # Skip if this specific table is already correct (in case a partial earlier
    # migration only got one of the two tables right).
    if set(_primary_key_columns(conn, "player_character_encounters")) == {"encounter_key", "character_id"}:
        return

    conn.execute("ALTER TABLE player_character_encounters RENAME TO _old_player_character_encounters_v1")
    # The old index follows the renamed table and now has a stale name.
    # SQLite will silently no-op a CREATE INDEX IF NOT EXISTS with a matching
    # name, so we must drop it before recreating against the new table.
    conn.execute("DROP INDEX IF EXISTS idx_player_character_encounters_character_id")
    conn.execute(
        """
        CREATE TABLE player_character_encounters (
            encounter_key TEXT NOT NULL,
            character_id INTEGER NOT NULL,
            encounter_date TEXT NOT NULL,
            damage_done INTEGER NOT NULL DEFAULT 0,
            healing_done INTEGER NOT NULL DEFAULT 0,
            taunts INTEGER NOT NULL DEFAULT 0,
            interrupts INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (encounter_key, character_id),
            FOREIGN KEY(character_id) REFERENCES player_characters(character_id) ON DELETE CASCADE
        )
        """
    )
    # Copy every row from the old table into the new. Because the old PK was
    # (encounter_key) alone, there's at most one row per encounter_key — so
    # the (encounter_key, character_id) pairs in the destination are unique
    # by construction. No INSERT OR IGNORE needed.
    conn.execute(
        """
        INSERT INTO player_character_encounters
            (encounter_key, character_id, encounter_date,
             damage_done, healing_done, taunts, interrupts)
        SELECT encounter_key, character_id, encounter_date,
               damage_done, healing_done, taunts, interrupts
        FROM _old_player_character_encounters_v1
        """
    )
    # Recreate the existing index on character_id (needed for joins).
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_player_character_encounters_character_id
        ON player_character_encounters(character_id)
        """
    )
    conn.execute("DROP TABLE _old_player_character_encounters_v1")


def _migrate_player_character_encounter_abilities(conn: sqlite3.Connection) -> None:
    """
    Recreate player_character_encounter_abilities with
    PRIMARY KEY (encounter_key, character_id, ability_id, ability_name).

    Same rename/recreate/copy/drop pattern.
    """
    new_pk = {"encounter_key", "character_id", "ability_id", "ability_name"}
    if set(_primary_key_columns(conn, "player_character_encounter_abilities")) == new_pk:
        return

    conn.execute(
        "ALTER TABLE player_character_encounter_abilities "
        "RENAME TO _old_player_character_encounter_abilities_v1"
    )
    # Same stale-index issue as the encounters table — drop the old name first.
    conn.execute("DROP INDEX IF EXISTS idx_player_character_encounter_abilities_character_id")
    conn.execute(
        """
        CREATE TABLE player_character_encounter_abilities (
            encounter_key TEXT NOT NULL,
            character_id INTEGER NOT NULL,
            ability_name TEXT NOT NULL,
            ability_id TEXT NOT NULL DEFAULT '',
            use_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (encounter_key, character_id, ability_id, ability_name),
            FOREIGN KEY(character_id) REFERENCES player_characters(character_id) ON DELETE CASCADE
        )
        """
    )
    # Old PK was (encounter_key, ability_id, ability_name) without character_id,
    # so collisions were impossible across (the single recorded) characters.
    # In the new table, character_id is part of the key — no dedup needed
    # because the old data already had at most one character per encounter.
    conn.execute(
        """
        INSERT INTO player_character_encounter_abilities
            (encounter_key, character_id, ability_name, ability_id, use_count)
        SELECT encounter_key, character_id, ability_name, ability_id, use_count
        FROM _old_player_character_encounter_abilities_v1
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_player_character_encounter_abilities_character_id
        ON player_character_encounter_abilities(character_id)
        """
    )
    conn.execute("DROP TABLE _old_player_character_encounter_abilities_v1")


# ─── Migration v3 — pressed / damage_source / prebuff ability counts ─────────


def _migration_v3_ability_count_columns(db_path: Path) -> Optional[str]:
    """
    Add prebuff_count and damage_source_count columns to
    player_character_encounter_abilities.

    The existing use_count column keeps its meaning: in-fight AbilityActivate
    counts (button presses during the fight). The new columns let us record
    two additional kinds of "ability appeared in this fight":

      - prebuff_count: AbilityActivates the player did in the 10-15 seconds
        BEFORE EnterCombat (DoT seeding, ground-target setup, stealth setup)
      - damage_source_count: how many damage events in the fight named this
        ability — captures DoT ticks, procs, reflected damage that don't have
        a corresponding AbilityActivate inside the fight

    These three counts are independent. The Inspector shows all three. The
    user interprets ("zero pressed but lots of damage_source = pre-cast DoT
    carryover").

    Idempotent: if the columns already exist, this does nothing. Adding
    columns with DEFAULT 0 is non-destructive — every existing row gets 0
    for the new columns automatically.
    """
    if _migration_v3_already_applied(db_path):
        return None

    backup_path = _take_backup(db_path, suffix="before_v3_ability_counts")

    with sqlite3.connect(str(db_path), timeout=30) as conn:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass

        try:
            conn.execute("BEGIN")
            # ALTER TABLE ADD COLUMN with a DEFAULT is safe and fast in SQLite —
            # it's a metadata-only change, no row rewriting required.
            conn.execute(
                "ALTER TABLE player_character_encounter_abilities "
                "ADD COLUMN prebuff_count INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute(
                "ALTER TABLE player_character_encounter_abilities "
                "ADD COLUMN damage_source_count INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise

    return (
        f"Database upgraded to v3 (ability count breakdown). "
        f"Backup saved to: {backup_path.name}"
    )


def _migration_v3_already_applied(db_path: Path) -> bool:
    """
    True if the new columns already exist on the abilities table. We use
    PRAGMA table_info to check rather than trying to add and catching the
    error — cleaner and faster.
    """
    with sqlite3.connect(str(db_path), timeout=30) as conn:
        if not _table_exists(conn, "player_character_encounter_abilities"):
            # No abilities table at all — fresh DB. The CREATE TABLE in
            # init_db has the new columns built in, so v3 is "implicitly"
            # already applied. Return True so we don't try to ALTER a
            # nonexistent table.
            return True
        rows = conn.execute(
            "PRAGMA table_info(player_character_encounter_abilities)"
        ).fetchall()
        column_names = {row[1] for row in rows}
        return (
            "prebuff_count" in column_names
            and "damage_source_count" in column_names
        )


# ─── Migration v4 — per-fight class / discipline / evidence ─────────────────


def _migration_v4_class_per_fight(db_path: Path) -> Optional[str]:
    """
    Add class_name, discipline_name, and class_evidence columns to
    player_character_encounters.

    A SWTOR character can hold up to two advanced classes within their family
    (Tech vs. Force) and three disciplines per class — meaning up to 6
    disciplines per character. They can switch class/discipline outside
    combat. So class+discipline is fundamentally per-FIGHT, not per-character.

    The legacy player_characters.class_name column stays put (used by the
    Inspector character list and historical UI), but the source of truth for
    a given fight lives in player_character_encounters.

    Three new columns:
      - class_name: e.g. "Operative" (faction-correct, as the game emits)
      - discipline_name: e.g. "Lethality"
      - class_evidence: human-readable provenance string, useful for the
        Inspector and for debugging when a detection seems wrong.
        Examples: "declared:DisciplineChanged"
                  "voted:Tracer Missile=18,Heatseeker Missiles=9"
                  "" (no detection)

    Idempotent: adding columns with DEFAULT '' is non-destructive.
    """
    if _migration_v4_already_applied(db_path):
        return None

    backup_path = _take_backup(db_path, suffix="before_v4_class_per_fight")

    with sqlite3.connect(str(db_path), timeout=30) as conn:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass

        try:
            conn.execute("BEGIN")
            conn.execute(
                "ALTER TABLE player_character_encounters "
                "ADD COLUMN class_name TEXT NOT NULL DEFAULT ''"
            )
            conn.execute(
                "ALTER TABLE player_character_encounters "
                "ADD COLUMN discipline_name TEXT NOT NULL DEFAULT ''"
            )
            conn.execute(
                "ALTER TABLE player_character_encounters "
                "ADD COLUMN class_evidence TEXT NOT NULL DEFAULT ''"
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise

    return (
        f"Database upgraded to v4 (per-fight class / discipline). "
        f"Backup saved to: {backup_path.name}"
    )


def _migration_v4_already_applied(db_path: Path) -> bool:
    """
    True if the per-fight class columns already exist. Uses PRAGMA
    table_info, same approach as v3.
    """
    with sqlite3.connect(str(db_path), timeout=30) as conn:
        if not _table_exists(conn, "player_character_encounters"):
            # Fresh DB; init_db will create the table with the new columns
            # baked in, so v4 is "implicitly" already applied.
            return True
        rows = conn.execute(
            "PRAGMA table_info(player_character_encounters)"
        ).fetchall()
        column_names = {row[1] for row in rows}
        return (
            "class_name" in column_names
            and "discipline_name" in column_names
            and "class_evidence" in column_names
        )


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _primary_key_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    """
    Return the columns participating in the primary key of `table_name`,
    in their PK ordinal position. Works for both single-column and composite
    primary keys.

    Returns [] if the table doesn't exist or has no primary key.
    """
    if not _table_exists(conn, table_name):
        return []
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk)
    # pk == 0 means not part of PK; pk > 0 means position in PK (1-indexed).
    pk_cols = [(row[5], row[1]) for row in rows if row[5] > 0]
    pk_cols.sort()  # sort by pk ordinal
    return [name for _, name in pk_cols]


def _take_backup(db_path: Path, *, suffix: str) -> Path:
    """
    Copy the database file next to itself with a timestamped name. Returns
    the backup path. We use shutil.copy2 to preserve metadata.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{db_path.stem}.{suffix}.{timestamp}{db_path.suffix}"
    backup_path = db_path.parent / backup_name
    shutil.copy2(db_path, backup_path)
    return backup_path
