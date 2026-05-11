import csv
import json
import re
import sqlite3
import time as time_mod
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from engine.aggregator import Fight, resolve_fight_names, scan_fights
from engine.analysis import analyse_tank
from storage.db_migrations import run_pending_migrations
from engine.parser_core import _open_log, parse_line


DB_PATH = Path(__file__).parent.parent / "data" / "encounter_history.sqlite3"
IMPORT_LEDGER_PATH = Path(__file__).parent.parent / "data" / "combat_log_imports.json"
DISCIPLINE_CLASS_RE = re.compile(r"DisciplineChanged\s*\{\d+\}:\s*(?P<class_name>[^/{]+?)\s*\{\d+\}\s*/")
DB_TIMEOUT_SECONDS = 10
DB_LOCK_RETRY_DELAYS = (0.25, 0.5, 1.0)


@dataclass
class EncounterExtreme:
    amount: int = 0
    actor: str = ""
    ability: str = ""


@dataclass
class EncounterSummary:
    encounter_key: str
    encounter_name: str
    encounter_date: str
    log_path: str
    recorded_by: str
    biggest_hit: EncounterExtreme
    biggest_heal: EncounterExtreme
    deaths: dict[str, int]


@dataclass
class PlayerCharacterSummary:
    character_id: int
    character_name: str
    class_name: str
    first_seen_date: str
    last_seen_date: str
    total_damage_done: int
    total_healing_done: int
    total_taunts: int
    total_interrupts: int


@dataclass
class PlayerCharacterAbilitySummary:
    character_ability_id: int
    character_id: int
    character_name: str
    ability_name: str
    ability_id: str
    total_uses: int


@dataclass
class CombatLogImportSummary:
    import_id: int
    log_path: str
    file_name: str
    line_count: int
    parsed_line_count: int
    parse_error_count: int
    source_character_name: str
    source_class_name: str
    # Phase G: fight-level idempotency reporting. These start at 0 and get
    # populated by import_combat_log after the per-fight upsert pass.
    # Defaults make this safe to add without breaking existing callers.
    fights_total: int = 0       # how many fights scan_fights found
    fights_new: int = 0         # didn't have an encounters row before
    fights_refreshed: int = 0   # encounters row already existed; was updated
    fights_failed: int = 0      # could not be aggregated/upserted


@dataclass
class DatabaseDashboardSnapshot:
    encounter_count: int
    imported_log_count: int
    imported_event_count: int
    imported_character_count: int
    seen_player_count: int


class DuplicateCombatLogImportError(RuntimeError):
    pass


@dataclass
class ImportedCharacterSummary:
    character_name: str
    latest_class_name: str
    classes_seen: list[str]
    import_count: int
    ability_count: int


@dataclass
class ImportedCharacterAbilitySummary:
    character_name: str
    ability_name: str
    ability_id: str
    use_count: int


@dataclass
class SeenPlayerSummary:
    player_name: str
    mention_count: int
    import_count: int
    source_event_count: int
    target_event_count: int
    ability_count: int
    legacy_name: str
    guild_name: str
    friend_name: str
    note_html: str


def _connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=DB_TIMEOUT_SECONDS)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    return conn


def _run_db_operation(operation, context: str):
    last_error: Optional[sqlite3.OperationalError] = None
    for attempt, delay in enumerate((0.0, *DB_LOCK_RETRY_DELAYS), start=1):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            last_error = exc
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                raise RuntimeError(f"{context} failed: {exc}") from exc
            if attempt > len(DB_LOCK_RETRY_DELAYS):
                break
            time_mod.sleep(delay)
    raise RuntimeError(
        f"{context} failed because the encounter database remained locked."
    ) from last_error


def init_db() -> None:
    # Run pending schema migrations on the existing database file before
    # touching any tables. Each migration is a no-op if already applied.
    # See db_migrations.py for what's in flight.
    migration_messages = run_pending_migrations(DB_PATH)
    for msg in migration_messages:
        print(f"[encounter_db] {msg}")

    with _connect_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS encounters (
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
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS player_characters (
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
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS player_character_encounters (
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
                PRIMARY KEY (encounter_key, character_id),
                FOREIGN KEY(character_id) REFERENCES player_characters(character_id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_player_character_encounters_character_id
            ON player_character_encounters(character_id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS player_character_abilities (
                character_ability_id INTEGER PRIMARY KEY AUTOINCREMENT,
                character_id INTEGER NOT NULL,
                ability_name TEXT NOT NULL,
                ability_id TEXT NOT NULL DEFAULT '',
                total_uses INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                UNIQUE(character_id, ability_id, ability_name),
                FOREIGN KEY(character_id) REFERENCES player_characters(character_id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS player_character_encounter_abilities (
                encounter_key TEXT NOT NULL,
                character_id INTEGER NOT NULL,
                ability_name TEXT NOT NULL,
                ability_id TEXT NOT NULL DEFAULT '',
                use_count INTEGER NOT NULL DEFAULT 0,
                prebuff_count INTEGER NOT NULL DEFAULT 0,
                damage_source_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(encounter_key, character_id, ability_id, ability_name),
                FOREIGN KEY(character_id) REFERENCES player_characters(character_id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_player_character_abilities_character_id
            ON player_character_abilities(character_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_player_character_encounter_abilities_character_id
            ON player_character_encounter_abilities(character_id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS combat_log_imports (
                import_id INTEGER PRIMARY KEY AUTOINCREMENT,
                log_path TEXT NOT NULL UNIQUE,
                file_name TEXT NOT NULL,
                file_size INTEGER NOT NULL DEFAULT 0,
                file_modified_at TEXT NOT NULL DEFAULT '',
                imported_at TEXT NOT NULL,
                line_count INTEGER NOT NULL DEFAULT 0,
                parsed_line_count INTEGER NOT NULL DEFAULT 0,
                parse_error_count INTEGER NOT NULL DEFAULT 0,
                source_character_name TEXT NOT NULL DEFAULT '',
                source_class_name TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS combat_log_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                import_id INTEGER NOT NULL,
                line_number INTEGER NOT NULL,
                parse_status TEXT NOT NULL DEFAULT 'parsed',
                raw_line TEXT NOT NULL DEFAULT '',
                timestamp_text TEXT NOT NULL DEFAULT '',
                source_name TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT '',
                source_player_id TEXT NOT NULL DEFAULT '',
                source_companion_name TEXT NOT NULL DEFAULT '',
                source_entity_id TEXT NOT NULL DEFAULT '',
                source_instance_id TEXT NOT NULL DEFAULT '',
                source_hp INTEGER,
                source_max_hp INTEGER,
                source_x REAL,
                source_y REAL,
                source_z REAL,
                source_json TEXT NOT NULL DEFAULT '{}',
                target_name TEXT NOT NULL DEFAULT '',
                target_type TEXT NOT NULL DEFAULT '',
                target_player_id TEXT NOT NULL DEFAULT '',
                target_companion_name TEXT NOT NULL DEFAULT '',
                target_entity_id TEXT NOT NULL DEFAULT '',
                target_instance_id TEXT NOT NULL DEFAULT '',
                target_hp INTEGER,
                target_max_hp INTEGER,
                target_x REAL,
                target_y REAL,
                target_z REAL,
                target_json TEXT NOT NULL DEFAULT '{}',
                ability_name TEXT NOT NULL DEFAULT '',
                ability_id TEXT NOT NULL DEFAULT '',
                effect_type TEXT NOT NULL DEFAULT '',
                effect_name TEXT NOT NULL DEFAULT '',
                effect_id TEXT NOT NULL DEFAULT '',
                effect_detail_name TEXT NOT NULL DEFAULT '',
                effect_detail_id TEXT NOT NULL DEFAULT '',
                raw_result_text TEXT NOT NULL DEFAULT '',
                result_amount INTEGER,
                result_is_crit INTEGER NOT NULL DEFAULT 0,
                result_overheal INTEGER,
                result_type TEXT NOT NULL DEFAULT '',
                result_dmg_type TEXT NOT NULL DEFAULT '',
                result_absorbed INTEGER,
                result_threat REAL,
                restore_amount REAL,
                spend_amount REAL,
                charges INTEGER,
                FOREIGN KEY(import_id) REFERENCES combat_log_imports(import_id) ON DELETE CASCADE,
                UNIQUE(import_id, line_number)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_combat_log_events_import_line
            ON combat_log_events(import_id, line_number)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_combat_log_events_ability
            ON combat_log_events(ability_name, ability_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_combat_log_events_source_name
            ON combat_log_events(source_name)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_combat_log_events_target_name
            ON combat_log_events(target_name)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS imported_player_cache_state (
                cache_key TEXT PRIMARY KEY,
                last_import_id INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS imported_player_characters (
                character_name TEXT PRIMARY KEY,
                latest_class_name TEXT NOT NULL DEFAULT '',
                first_import_id INTEGER NOT NULL DEFAULT 0,
                last_import_id INTEGER NOT NULL DEFAULT 0,
                import_count INTEGER NOT NULL DEFAULT 0,
                ability_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS imported_player_character_classes (
                character_name TEXT NOT NULL,
                class_name TEXT NOT NULL,
                seen_count INTEGER NOT NULL DEFAULT 0,
                last_import_id INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(character_name, class_name)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS imported_player_character_abilities (
                character_name TEXT NOT NULL,
                ability_name TEXT NOT NULL,
                ability_id TEXT NOT NULL DEFAULT '',
                use_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(character_name, ability_name, ability_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_imported_player_character_classes_name
            ON imported_player_character_classes(character_name)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_imported_player_character_abilities_name
            ON imported_player_character_abilities(character_name)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_player_cache_state (
                cache_key TEXT PRIMARY KEY,
                last_import_id INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_players (
                canonical_name TEXT PRIMARY KEY,
                mention_count INTEGER NOT NULL DEFAULT 0,
                import_count INTEGER NOT NULL DEFAULT 0,
                source_event_count INTEGER NOT NULL DEFAULT 0,
                target_event_count INTEGER NOT NULL DEFAULT 0,
                ability_count INTEGER NOT NULL DEFAULT 0,
                legacy_name TEXT NOT NULL DEFAULT '',
                guild_name TEXT NOT NULL DEFAULT '',
                friend_name TEXT NOT NULL DEFAULT '',
                note_html TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_player_aliases (
                raw_name TEXT PRIMARY KEY,
                canonical_name TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_player_abilities (
                canonical_name TEXT NOT NULL,
                ability_name TEXT NOT NULL,
                ability_id TEXT NOT NULL DEFAULT '',
                use_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(canonical_name, ability_name, ability_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_seen_player_aliases_canonical_name
            ON seen_player_aliases(canonical_name)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_seen_player_abilities_canonical_name
            ON seen_player_abilities(canonical_name)
            """
        )
        seen_player_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(seen_players)").fetchall()
        }
        if "legacy_name" not in seen_player_columns:
            conn.execute(
                "ALTER TABLE seen_players ADD COLUMN legacy_name TEXT NOT NULL DEFAULT ''"
            )
        if "guild_name" not in seen_player_columns:
            conn.execute(
                "ALTER TABLE seen_players ADD COLUMN guild_name TEXT NOT NULL DEFAULT ''"
            )
        if "friend_name" not in seen_player_columns:
            conn.execute(
                "ALTER TABLE seen_players ADD COLUMN friend_name TEXT NOT NULL DEFAULT ''"
            )
        if "note_html" not in seen_player_columns:
            conn.execute(
                "ALTER TABLE seen_players ADD COLUMN note_html TEXT NOT NULL DEFAULT ''"
            )
        conn.execute(
            """
            UPDATE seen_players
            SET legacy_name = CASE
                    WHEN import_count = 1 AND TRIM(legacy_name) = '' THEN 'Rando'
                    ELSE legacy_name
                END,
                guild_name = CASE
                    WHEN import_count = 1 AND TRIM(guild_name) = '' THEN 'Rando'
                    ELSE guild_name
                END,
                friend_name = CASE
                    WHEN import_count = 1 AND TRIM(friend_name) = '' THEN 'Rando'
                    ELSE friend_name
                END
            WHERE import_count = 1
            """
        )
        conn.commit()


def encounter_key_for(fight: Fight) -> str:
    log_path = str(Path(fight._log_path or "memory").resolve()).lower()
    return f"{log_path}|{fight._line_start}|{fight._line_end}|{fight.start_time.isoformat()}"


def summarize_fight(fight: Fight) -> EncounterSummary:
    biggest_hit = EncounterExtreme()
    biggest_heal = EncounterExtreme()
    deaths: dict[str, int] = {}
    hp_by_target: dict[str, int] = {}

    if fight._log_path and fight._line_end >= fight._line_start:
        with _open_log(fight._log_path) as f:
            for line_num, raw in enumerate(f):
                if line_num < fight._line_start:
                    continue
                if line_num > fight._line_end:
                    break
                ev = parse_line(raw)
                if not ev or not ev.result:
                    continue

                if ev.is_damage and not ev.result.is_miss and ev.result.amount > biggest_hit.amount:
                    biggest_hit = EncounterExtreme(
                        amount=ev.result.amount,
                        actor=ev.source.display_name,
                        ability=ev.ability.name if ev.ability else "Unknown",
                    )

                if ev.is_heal:
                    effective_heal = ev.result.amount - (ev.result.overheal or 0)
                    if effective_heal > biggest_heal.amount:
                        biggest_heal = EncounterExtreme(
                            amount=effective_heal,
                            actor=ev.source.display_name,
                            ability=ev.ability.name if ev.ability else "Unknown",
                        )

                tgt = ev.target
                if tgt and tgt.hp is not None:
                    key = tgt.unique_id or tgt.display_name
                    previous_hp = hp_by_target.get(key)
                    if tgt.hp <= 0 and (previous_hp is None or previous_hp > 0):
                        name = tgt.display_name or key
                        deaths[name] = deaths.get(name, 0) + 1
                    hp_by_target[key] = tgt.hp

    return EncounterSummary(
        encounter_key=encounter_key_for(fight),
        encounter_name=fight.custom_name or fight.boss_name or "Unknown Encounter",
        encounter_date=_encounter_date_for(fight),
        log_path=str(Path(fight._log_path or "").resolve()) if fight._log_path else "",
        recorded_by=fight.player_name or "",
        biggest_hit=biggest_hit,
        biggest_heal=biggest_heal,
        deaths=deaths,
    )


def upsert_fight(fight: Fight) -> None:
    summary = summarize_fight(fight)
    updated_at = datetime.now().isoformat(timespec="seconds")

    def _write() -> None:
        with _connect_db() as conn:
            conn.execute(
                """
                INSERT INTO encounters (
                    encounter_key,
                    encounter_name,
                    encounter_date,
                    log_path,
                    recorded_by,
                    biggest_hit_amount,
                    biggest_hit_by,
                    biggest_hit_ability,
                    biggest_heal_amount,
                    biggest_heal_by,
                    biggest_heal_ability,
                    deaths_json,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(encounter_key) DO UPDATE SET
                    encounter_name=excluded.encounter_name,
                    encounter_date=excluded.encounter_date,
                    log_path=excluded.log_path,
                    recorded_by=excluded.recorded_by,
                    biggest_hit_amount=excluded.biggest_hit_amount,
                    biggest_hit_by=excluded.biggest_hit_by,
                    biggest_hit_ability=excluded.biggest_hit_ability,
                    biggest_heal_amount=excluded.biggest_heal_amount,
                    biggest_heal_by=excluded.biggest_heal_by,
                    biggest_heal_ability=excluded.biggest_heal_ability,
                    deaths_json=excluded.deaths_json,
                    updated_at=excluded.updated_at
                """,
                (
                    summary.encounter_key,
                    summary.encounter_name,
                    summary.encounter_date,
                    summary.log_path,
                    summary.recorded_by,
                    summary.biggest_hit.amount,
                    summary.biggest_hit.actor,
                    summary.biggest_hit.ability,
                    summary.biggest_heal.amount,
                    summary.biggest_heal.actor,
                    summary.biggest_heal.ability,
                    json.dumps(summary.deaths, sort_keys=True),
                    updated_at,
                ),
            )
            character_ids = _upsert_player_character_for_fight(
                conn, fight, summary.encounter_key, summary.encounter_date, updated_at
            )
            if character_ids:
                _upsert_player_character_abilities_for_fight(
                    conn, fight, character_ids, summary.encounter_key, updated_at
                )
            conn.commit()

    _run_db_operation(_write, "Saving encounter history")


def list_player_characters() -> list[PlayerCharacterSummary]:
    init_db()
    with _connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                character_id,
                character_name,
                class_name,
                first_seen_date,
                last_seen_date,
                total_damage_done,
                total_healing_done,
                total_taunts,
                total_interrupts
            FROM player_characters
            ORDER BY lower(character_name)
            """
        ).fetchall()
    return [
        PlayerCharacterSummary(
            character_id=int(row["character_id"]),
            character_name=str(row["character_name"] or ""),
            class_name=str(row["class_name"] or ""),
            first_seen_date=str(row["first_seen_date"] or ""),
            last_seen_date=str(row["last_seen_date"] or ""),
            total_damage_done=int(row["total_damage_done"] or 0),
            total_healing_done=int(row["total_healing_done"] or 0),
            total_taunts=int(row["total_taunts"] or 0),
            total_interrupts=int(row["total_interrupts"] or 0),
        )
        for row in rows
    ]


def update_player_character_class(character_name: str, class_name: str) -> None:
    init_db()
    name = str(character_name or "").strip()
    if not name:
        return
    def _write() -> None:
        with _connect_db() as conn:
            conn.execute(
                """
                UPDATE player_characters
                SET class_name = ?, updated_at = ?
                WHERE character_name = ?
                """,
                (
                    str(class_name or "").strip(),
                    datetime.now().isoformat(timespec="seconds"),
                    name,
                ),
            )
            conn.commit()

    _run_db_operation(_write, "Updating player class")


def list_player_character_abilities(character_name: Optional[str] = None) -> list[PlayerCharacterAbilitySummary]:
    init_db()
    params: list[str] = []
    query = """
        SELECT
            a.character_ability_id,
            a.character_id,
            c.character_name,
            a.ability_name,
            a.ability_id,
            a.total_uses
        FROM player_character_abilities a
        JOIN player_characters c ON c.character_id = a.character_id
    """
    if character_name:
        query += " WHERE c.character_name = ?"
        params.append(str(character_name).strip())
    query += " ORDER BY lower(c.character_name), lower(a.ability_name), a.ability_id"

    with _connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
    return [
        PlayerCharacterAbilitySummary(
            character_ability_id=int(row["character_ability_id"]),
            character_id=int(row["character_id"]),
            character_name=str(row["character_name"] or ""),
            ability_name=str(row["ability_name"] or ""),
            ability_id=str(row["ability_id"] or ""),
            total_uses=int(row["total_uses"] or 0),
        )
        for row in rows
    ]


def list_character_abilities_with_import_fallback(character_name: Optional[str] = None) -> list[PlayerCharacterAbilitySummary]:
    rows = list_player_character_abilities(character_name)
    if rows:
        return rows
    if not character_name:
        return rows

    imported_rows = list_imported_character_abilities(character_name)
    return [
        PlayerCharacterAbilitySummary(
            character_ability_id=0,
            character_id=0,
            character_name=row.character_name,
            ability_name=row.ability_name,
            ability_id=row.ability_id,
            total_uses=row.use_count,
        )
        for row in imported_rows
    ]


def seed_player_characters_from_logs(log_dir: str | Path, max_lines: int = 40) -> int:
    init_db()
    folder = Path(log_dir)
    if not folder.is_dir():
        return 0

    seeded = 0
    for path in sorted(folder.glob("combat_*.txt")):
        if seed_player_character_from_log(path, max_lines=max_lines):
            seeded += 1
    return seeded


def seed_player_character_from_log(log_path: str | Path, max_lines: int = 40) -> bool:
    init_db()
    info = _scan_character_info_from_log(Path(log_path), max_lines=max_lines)
    if not info:
        return False

    updated_at = datetime.now().isoformat(timespec="seconds")
    def _write() -> None:
        with _connect_db() as conn:
            conn.execute(
                """
                INSERT INTO player_characters (
                    character_name,
                    class_name,
                    first_seen_date,
                    last_seen_date,
                    total_damage_done,
                    total_healing_done,
                    total_taunts,
                    total_interrupts,
                    updated_at
                ) VALUES (?, ?, ?, ?, 0, 0, 0, 0, ?)
                ON CONFLICT(character_name) DO UPDATE SET
                    class_name = CASE
                        WHEN excluded.class_name != '' THEN excluded.class_name
                        ELSE player_characters.class_name
                    END,
                    first_seen_date = CASE
                        WHEN player_characters.first_seen_date = '' OR excluded.first_seen_date < player_characters.first_seen_date
                        THEN excluded.first_seen_date
                        ELSE player_characters.first_seen_date
                    END,
                    last_seen_date = CASE
                        WHEN player_characters.last_seen_date = '' OR excluded.last_seen_date > player_characters.last_seen_date
                        THEN excluded.last_seen_date
                        ELSE player_characters.last_seen_date
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    info["character_name"],
                    info["class_name"],
                    info["seen_date"],
                    info["seen_date"],
                    updated_at,
                ),
            )
            conn.commit()

    _run_db_operation(_write, "Seeding player character data")
    return True


@dataclass
class _FightUpsertSummary:
    """Internal result of running fight aggregation against a log."""
    total: int          # how many fights scan_fights found in the log
    new: int            # didn't have an encounters row before
    refreshed: int      # encounters row already existed; was updated
    failed: int         # could not be aggregated/upserted


def _upsert_fights_from_log(
    log_path: Path,
    progress_callback=None,
) -> _FightUpsertSummary:
    """
    Phase D-1 (extended for Phase G): Build Fight objects from a log file
    and run each through upsert_fight() so per-player encounter rows get
    written.

    This is what makes the bulk-import path actually populate the per-player
    tables. Without it, importing only writes raw events — useful for
    debugging the log but useless for cohort coaching.

    Each fight is processed independently:
      - scan_fights(path) finds fight boundaries (cheap — no event parsing)
      - resolve_fight_names(path, fights) walks once to set boss names
      - per fight, check whether the encounter_key already exists in the DB
      - per fight, ensure_loaded() parses events and aggregates entity_stats
      - per fight, upsert_fight() writes the per-player rows
      - the fight's events are dropped after upsert to release memory

    Each fight gets its own transaction (upsert_fight commits per call).
    A failure on one fight is logged but does NOT abort the whole import —
    we'd rather have N-1 fights ingested than zero.

    progress_callback, if provided, is called with (fights_processed, total_fights)
    after each fight. Useful for progress UI.

    Returns _FightUpsertSummary with new/refreshed/failed counts. "new" means
    we created the encounters row; "refreshed" means it already existed and
    we updated it. Either way the row's data is current after this call.
    """
    log_path_str = str(log_path)
    fights = scan_fights(log_path_str)
    if not fights:
        return _FightUpsertSummary(total=0, new=0, refreshed=0, failed=0)

    # Populate boss names so encounters get sensible labels in the DB.
    # Without this, every encounter would be "Unknown Encounter".
    try:
        resolve_fight_names(log_path_str, fights)
    except Exception:
        # If name resolution fails, fights still ingest with empty boss names.
        # That's worse than ideal but better than aborting the whole thing.
        pass

    # Pre-compute encounter keys for all fights and check which ones the DB
    # already has. One query is way cheaper than N separate per-fight queries.
    # We use summarize_fight() to build the same key upsert_fight will use.
    fight_keys: list[str] = []
    for fight in fights:
        try:
            summary = summarize_fight(fight)
            fight_keys.append(summary.encounter_key)
        except Exception:
            fight_keys.append("")  # placeholder; this fight will fail the upsert

    existing_keys: set[str] = set()
    valid_keys = [k for k in fight_keys if k]
    if valid_keys:
        try:
            with _connect_db() as conn:
                placeholders = ",".join("?" * len(valid_keys))
                rows = conn.execute(
                    f"SELECT encounter_key FROM encounters WHERE encounter_key IN ({placeholders})",
                    valid_keys,
                ).fetchall()
                existing_keys = {str(row[0]) for row in rows}
        except Exception:
            # If we can't query (DB not initialized yet, etc.), every fight
            # will be classified as "new" — not strictly accurate but the
            # ON CONFLICT clauses still keep the data correct.
            existing_keys = set()

    total = len(fights)
    new_count = 0
    refreshed_count = 0
    failed_count = 0

    for index, (fight, key) in enumerate(zip(fights, fight_keys), start=1):
        was_existing = key in existing_keys
        try:
            fight.ensure_loaded()
            upsert_fight(fight)
            if was_existing:
                refreshed_count += 1
            else:
                new_count += 1
        except Exception:
            failed_count += 1
        finally:
            # Drop the fight's events to release memory between iterations.
            fight.events = []

        if progress_callback is not None:
            try:
                progress_callback(index, total)
            except Exception:
                pass

    return _FightUpsertSummary(
        total=total,
        new=new_count,
        refreshed=refreshed_count,
        failed=failed_count,
    )


def import_combat_log(log_path: str | Path) -> CombatLogImportSummary:
    """
    Import a combat log into the database.

    Phase G: this function is now idempotent at the FIGHT level. Re-importing
    a log that's already been imported is no longer an error — the events
    table is rewritten (cheap because the log file is the source of truth)
    and each fight is upserted in place. Fights that already exist as
    encounters rows are *refreshed* with current code; fights that don't
    yet exist are created. The summary tells the caller new vs. refreshed.

    Why we removed the duplicate-import block: the old behavior assumed the
    only way fights got into the DB was through this function. With the
    "Save Fight to DB" workflow added in Phase G, fights can be saved live,
    one at a time. Later running a bulk import on the full log file would
    have errored under the old behavior. Now it runs and just refreshes
    those fights instead.
    """
    init_db()
    path = Path(log_path).resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Combat log not found: {path}")

    info = _scan_character_info_from_log(path, max_lines=80) or {}
    stat = path.stat()
    imported_at = datetime.now().isoformat(timespec="seconds")
    file_modified_at = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
    line_count = 0
    parsed_line_count = 0
    parse_error_count = 0
    event_rows: list[tuple] = []

    with _open_log(str(path)) as handle:
        for line_number, raw in enumerate(handle, start=1):
            raw_line = raw.rstrip("\r\n")
            line_count += 1
            event = parse_line(raw_line)
            if event is None:
                parse_error_count += 1
                event_rows.append(_combat_log_event_row(None, raw_line, line_number))
                continue
            parsed_line_count += 1
            event_rows.append(_combat_log_event_row(event, raw_line, line_number))

    def _write() -> CombatLogImportSummary:
        with _connect_db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO combat_log_imports (
                    log_path,
                    file_name,
                    file_size,
                    file_modified_at,
                    imported_at,
                    line_count,
                    parsed_line_count,
                    parse_error_count,
                    source_character_name,
                    source_class_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(log_path) DO UPDATE SET
                    file_name = excluded.file_name,
                    file_size = excluded.file_size,
                    file_modified_at = excluded.file_modified_at,
                    imported_at = excluded.imported_at,
                    line_count = excluded.line_count,
                    parsed_line_count = excluded.parsed_line_count,
                    parse_error_count = excluded.parse_error_count,
                    source_character_name = excluded.source_character_name,
                    source_class_name = excluded.source_class_name
                """,
                (
                    str(path),
                    path.name,
                    int(stat.st_size),
                    file_modified_at,
                    imported_at,
                    line_count,
                    parsed_line_count,
                    parse_error_count,
                    str(info.get("character_name", "") or ""),
                    str(info.get("class_name", "") or ""),
                ),
            )
            import_id = int(cursor.lastrowid or 0)
            if import_id == 0:
                row = conn.execute(
                    "SELECT import_id FROM combat_log_imports WHERE log_path = ?",
                    (str(path),),
                ).fetchone()
                if row is None:
                    raise RuntimeError(f"Failed to resolve import row for {path}")
                import_id = int(row[0])

            conn.execute("DELETE FROM combat_log_events WHERE import_id = ?", (import_id,))
            conn.executemany(
                """
                INSERT INTO combat_log_events (
                    import_id,
                    line_number,
                    parse_status,
                    raw_line,
                    timestamp_text,
                    source_name,
                    source_type,
                    source_player_id,
                    source_companion_name,
                    source_entity_id,
                    source_instance_id,
                    source_hp,
                    source_max_hp,
                    source_x,
                    source_y,
                    source_z,
                    source_json,
                    target_name,
                    target_type,
                    target_player_id,
                    target_companion_name,
                    target_entity_id,
                    target_instance_id,
                    target_hp,
                    target_max_hp,
                    target_x,
                    target_y,
                    target_z,
                    target_json,
                    ability_name,
                    ability_id,
                    effect_type,
                    effect_name,
                    effect_id,
                    effect_detail_name,
                    effect_detail_id,
                    raw_result_text,
                    result_amount,
                    result_is_crit,
                    result_overheal,
                    result_type,
                    result_dmg_type,
                    result_absorbed,
                    result_threat,
                    restore_amount,
                    spend_amount,
                    charges
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [(import_id, *row) for row in event_rows],
            )
            conn.commit()
            return CombatLogImportSummary(
                import_id=import_id,
                log_path=str(path),
                file_name=path.name,
                line_count=line_count,
                parsed_line_count=parsed_line_count,
                parse_error_count=parse_error_count,
                source_character_name=str(info.get("character_name", "") or ""),
                source_class_name=str(info.get("class_name", "") or ""),
            )

    result = _run_db_operation(_write, "Importing combat log")
    if info:
        try:
            seed_player_character_from_log(path)
        except Exception:
            pass

    # Phase D-1 (extended for Phase G): now that events are in the DB, also
    # build fights from the log file and write per-player encounter rows.
    # The fight summary tells us how many fights were new vs already in the
    # DB (e.g. saved live earlier with "Save Fight to DB").
    fights_total = 0
    fights_new = 0
    fights_refreshed = 0
    fights_failed = 0
    try:
        fight_summary = _upsert_fights_from_log(path)
        fights_total = fight_summary.total
        fights_new = fight_summary.new
        fights_refreshed = fight_summary.refreshed
        fights_failed = fight_summary.failed
    except Exception:
        # Events were already committed; the import itself is "successful"
        # even if fight ingestion fails. The user can rebuild later via
        # the Inspector tab.
        pass

    sync_imported_character_cache(import_id=result.import_id)
    sync_seen_player_cache(import_id=result.import_id)
    sync_import_ledger()

    # Stitch the fight counts into the summary. The dataclass defaults are
    # 0 so this is just filling in real values.
    result.fights_total = fights_total
    result.fights_new = fights_new
    result.fights_refreshed = fights_refreshed
    result.fights_failed = fights_failed
    return result


@dataclass
class FightRebuildSummary:
    """Result of rebuilding fights from existing imports."""
    logs_processed: int
    logs_skipped: int       # log file no longer exists on disk
    fights_succeeded: int
    fights_failed: int


def rebuild_fights_from_existing_imports(
    progress_callback=None,
) -> FightRebuildSummary:
    """
    Re-run fight ingestion against every log already imported into the DB.

    This is the missing complement to import_combat_log. After we change
    the structured-data schema (e.g. adding Phase E ability count columns),
    existing imports still have the OLD shape — they were ingested before
    the new code existed. This function rebuilds them from the log files
    on disk without re-importing the raw events.

    What it does, per imported log:
      1. Look up the log path from combat_log_imports
      2. Verify the log file still exists on disk (skip if not)
      3. Run _upsert_fights_from_log against it — same fight-aggregation
         and upsert path as a fresh import, but events are already in the DB
      4. Existing per-fight rows are replaced (upsert_fight is idempotent)

    Raw event rows in combat_log_events are NOT touched. They were already
    correct from the original import. We only re-run the *structured-data*
    aggregation that produces encounters, player_character_encounters, and
    player_character_encounter_abilities.

    progress_callback, if provided, is called with (logs_done, logs_total)
    after each log finishes — useful for a Qt progress dialog.

    Returns a FightRebuildSummary with counts. Errors on individual logs
    or fights are swallowed; the summary's failed counts surface them.

    This function is safe to run repeatedly. It's also safe if the log
    file is missing — those logs are counted as skipped, not failed.
    """
    init_db()

    with _connect_db() as conn:
        rows = conn.execute(
            "SELECT log_path FROM combat_log_imports ORDER BY imported_at"
        ).fetchall()

    log_paths = [Path(str(row[0])) for row in rows]
    total = len(log_paths)
    logs_processed = 0
    logs_skipped = 0
    fights_succeeded = 0
    fights_failed = 0

    for index, log_path in enumerate(log_paths, start=1):
        if not log_path.exists() or not log_path.is_file():
            # Log file was deleted, moved, or is on a drive that's not mounted.
            # Skip rather than fail — the import row still exists for ledger
            # purposes but we can't re-derive structured data without the file.
            logs_skipped += 1
        else:
            try:
                fight_summary = _upsert_fights_from_log(log_path)
                # In the rebuild context, we treat "succeeded" as new + refreshed
                # because both produce up-to-date data. The distinction matters
                # for the import workflow but not for an explicit rebuild.
                fights_succeeded += fight_summary.new + fight_summary.refreshed
                fights_failed += fight_summary.failed
                logs_processed += 1
            except Exception:
                # Catastrophic failure on a log — count as skipped so the
                # caller can tell something went wrong. Don't abort the
                # whole rebuild over one bad log.
                logs_skipped += 1

        if progress_callback is not None:
            try:
                progress_callback(index, total)
            except Exception:
                pass

    return FightRebuildSummary(
        logs_processed=logs_processed,
        logs_skipped=logs_skipped,
        fights_succeeded=fights_succeeded,
        fights_failed=fights_failed,
    )


def sync_import_ledger() -> None:
    init_db()
    with _connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                import_id,
                log_path,
                file_name,
                file_size,
                file_modified_at,
                imported_at,
                line_count,
                parsed_line_count,
                parse_error_count,
                source_character_name,
                source_class_name
            FROM combat_log_imports
            ORDER BY import_id
            """
        ).fetchall()

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "database_path": str(DB_PATH.resolve()),
        "import_count": len(rows),
        "imports": [
            {
                "import_id": int(row["import_id"]),
                "log_path": str(row["log_path"] or ""),
                "file_name": str(row["file_name"] or ""),
                "file_size": int(row["file_size"] or 0),
                "file_modified_at": str(row["file_modified_at"] or ""),
                "imported_at": str(row["imported_at"] or ""),
                "line_count": int(row["line_count"] or 0),
                "parsed_line_count": int(row["parsed_line_count"] or 0),
                "parse_error_count": int(row["parse_error_count"] or 0),
                "source_character_name": str(row["source_character_name"] or ""),
                "source_class_name": str(row["source_class_name"] or ""),
            }
            for row in rows
        ],
    }
    IMPORT_LEDGER_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def list_combat_log_imports() -> list[CombatLogImportSummary]:
    init_db()
    with _connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                import_id,
                log_path,
                file_name,
                line_count,
                parsed_line_count,
                parse_error_count,
                source_character_name,
                source_class_name
            FROM combat_log_imports
            ORDER BY import_id DESC
            """
        ).fetchall()
    return [
        CombatLogImportSummary(
            import_id=int(row["import_id"]),
            log_path=str(row["log_path"] or ""),
            file_name=str(row["file_name"] or ""),
            line_count=int(row["line_count"] or 0),
            parsed_line_count=int(row["parsed_line_count"] or 0),
            parse_error_count=int(row["parse_error_count"] or 0),
            source_character_name=str(row["source_character_name"] or ""),
            source_class_name=str(row["source_class_name"] or ""),
        )
        for row in rows
    ]


def is_combat_log_imported(log_path: str | Path) -> bool:
    init_db()
    path = str(Path(log_path).resolve())
    with _connect_db() as conn:
        row = conn.execute(
            "SELECT import_id FROM combat_log_imports WHERE log_path = ?",
            (path,),
        ).fetchone()
    return row is not None


def list_combat_log_events(import_id: int, limit: int = 2000) -> list[dict[str, object]]:
    init_db()
    with _connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                line_number,
                parse_status,
                timestamp_text,
                source_name,
                target_name,
                ability_name,
                effect_type,
                effect_name,
                result_amount,
                result_type,
                result_dmg_type,
                raw_result_text,
                raw_line
            FROM combat_log_events
            WHERE import_id = ?
            ORDER BY line_number
            LIMIT ?
            """,
            (int(import_id), int(limit)),
        ).fetchall()
    return [dict(row) for row in rows]


def export_combat_log_events_csv(import_id: int, destination: str | Path) -> int:
    init_db()
    dest = Path(destination)
    with _connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT *
            FROM combat_log_events
            WHERE import_id = ?
            ORDER BY line_number
            """,
            (int(import_id),),
        ).fetchall()
    fieldnames = list(rows[0].keys()) if rows else [
        "event_id", "import_id", "line_number", "parse_status", "raw_line",
        "timestamp_text", "source_name", "source_type", "source_player_id",
        "source_companion_name", "source_entity_id", "source_instance_id",
        "source_hp", "source_max_hp", "source_x", "source_y", "source_z",
        "source_json", "target_name", "target_type", "target_player_id",
        "target_companion_name", "target_entity_id", "target_instance_id",
        "target_hp", "target_max_hp", "target_x", "target_y", "target_z",
        "target_json", "ability_name", "ability_id", "effect_type",
        "effect_name", "effect_id", "effect_detail_name", "effect_detail_id",
        "raw_result_text", "result_amount", "result_is_crit", "result_overheal",
        "result_type", "result_dmg_type", "result_absorbed", "result_threat",
        "restore_amount", "spend_amount", "charges",
    ]
    with dest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))
    return len(rows)


def get_database_dashboard_snapshot() -> DatabaseDashboardSnapshot:
    init_db()
    with _connect_db() as conn:
        encounter_count = int(
            conn.execute("SELECT COUNT(*) FROM encounters").fetchone()[0] or 0
        )
        imported_log_count = int(
            conn.execute("SELECT COUNT(*) FROM combat_log_imports").fetchone()[0] or 0
        )
        imported_event_count = int(
            conn.execute("SELECT COALESCE(SUM(line_count), 0) FROM combat_log_imports").fetchone()[0] or 0
        )
        imported_character_count = int(
            conn.execute("SELECT COUNT(*) FROM imported_player_characters").fetchone()[0] or 0
        )
        seen_player_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM seen_players WHERE instr(canonical_name, char(65533)) = 0"
            ).fetchone()[0] or 0
        )
    return DatabaseDashboardSnapshot(
        encounter_count=encounter_count,
        imported_log_count=imported_log_count,
        imported_event_count=imported_event_count,
        imported_character_count=imported_character_count,
        seen_player_count=seen_player_count,
    )


def list_imported_characters() -> list[ImportedCharacterSummary]:
    init_db()
    sync_imported_character_cache()
    with _connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                pc.character_name,
                pc.latest_class_name,
                GROUP_CONCAT(
                    CASE WHEN pcc.class_name != '' THEN pcc.class_name END,
                    ','
                ) AS classes_seen_csv,
                pc.import_count,
                pc.ability_count
            FROM imported_player_characters pc
            LEFT JOIN imported_player_character_classes pcc
              ON pcc.character_name = pc.character_name
            GROUP BY
                pc.character_name,
                pc.latest_class_name,
                pc.import_count,
                pc.ability_count
            ORDER BY lower(pc.character_name)
            """
        ).fetchall()
    output: list[ImportedCharacterSummary] = []
    for row in rows:
        classes_seen = sorted(
            {part.strip() for part in str(row["classes_seen_csv"] or "").split(",") if part.strip()}
        )
        output.append(
            ImportedCharacterSummary(
                character_name=str(row["character_name"] or ""),
                latest_class_name=str(row["latest_class_name"] or ""),
                classes_seen=classes_seen,
                import_count=int(row["import_count"] or 0),
                ability_count=int(row["ability_count"] or 0),
            )
        )
    return output


def list_imported_character_abilities(character_name: str) -> list[ImportedCharacterAbilitySummary]:
    init_db()
    sync_imported_character_cache()
    name = str(character_name or "").strip()
    if not name:
        return []
    with _connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                character_name,
                ability_name,
                ability_id,
                use_count
            FROM imported_player_character_abilities
            WHERE character_name = ?
            ORDER BY lower(ability_name), ability_id
            """,
            (name,),
        ).fetchall()
    return [
        ImportedCharacterAbilitySummary(
            character_name=str(row["character_name"] or ""),
            ability_name=str(row["ability_name"] or ""),
            ability_id=str(row["ability_id"] or ""),
            use_count=int(row["use_count"] or 0),
        )
        for row in rows
    ]


def sync_imported_character_cache(progress_callback=None, import_id: Optional[int] = None) -> None:
    init_db()
    with _connect_db() as conn:
        conn.row_factory = sqlite3.Row
        state_row = conn.execute(
            "SELECT last_import_id FROM imported_player_cache_state WHERE cache_key = 'default'"
        ).fetchone()
        last_import_id = int(state_row["last_import_id"]) if state_row is not None else 0
        if import_id is not None:
            import_ids = [int(import_id)] if int(import_id) > last_import_id else []
        else:
            import_ids = [
                int(row["import_id"])
                for row in conn.execute(
                    """
                    SELECT import_id
                    FROM combat_log_imports
                    WHERE import_id > ?
                    ORDER BY import_id
                    """,
                    (last_import_id,),
                ).fetchall()
            ]

        total = len(import_ids)
        for idx, current_import_id in enumerate(import_ids, start=1):
            _process_imported_character_import(conn, current_import_id)
            conn.execute(
                """
                INSERT INTO imported_player_cache_state (cache_key, last_import_id, updated_at)
                VALUES ('default', ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    last_import_id = excluded.last_import_id,
                    updated_at = excluded.updated_at
                """,
                (current_import_id, datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()
            if progress_callback is not None:
                progress_callback(idx, total)


def _process_imported_character_import(conn: sqlite3.Connection, import_id: int) -> None:
    conn.row_factory = sqlite3.Row
    import_row = conn.execute(
        """
        SELECT source_character_name, source_class_name
        FROM combat_log_imports
        WHERE import_id = ?
        """,
        (int(import_id),),
    ).fetchone()
    if import_row is None:
        return

    character_name = str(import_row["source_character_name"] or "").strip()
    if not character_name:
        return

    class_name = str(import_row["source_class_name"] or "").strip()
    now = datetime.now().isoformat(timespec="seconds")

    conn.execute(
        """
        INSERT INTO imported_player_characters (
            character_name,
            latest_class_name,
            first_import_id,
            last_import_id,
            import_count,
            ability_count,
            updated_at
        ) VALUES (?, ?, ?, ?, 1, 0, ?)
        ON CONFLICT(character_name) DO UPDATE SET
            latest_class_name = CASE
                WHEN excluded.last_import_id >= imported_player_characters.last_import_id
                THEN excluded.latest_class_name
                ELSE imported_player_characters.latest_class_name
            END,
            first_import_id = CASE
                WHEN imported_player_characters.first_import_id = 0
                THEN excluded.first_import_id
                ELSE MIN(imported_player_characters.first_import_id, excluded.first_import_id)
            END,
            last_import_id = MAX(imported_player_characters.last_import_id, excluded.last_import_id),
            import_count = imported_player_characters.import_count + 1,
            updated_at = excluded.updated_at
        """,
        (character_name, class_name, int(import_id), int(import_id), now),
    )

    if class_name:
        conn.execute(
            """
            INSERT INTO imported_player_character_classes (
                character_name,
                class_name,
                seen_count,
                last_import_id,
                updated_at
            ) VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(character_name, class_name) DO UPDATE SET
                seen_count = imported_player_character_classes.seen_count + 1,
                last_import_id = MAX(imported_player_character_classes.last_import_id, excluded.last_import_id),
                updated_at = excluded.updated_at
            """,
            (character_name, class_name, int(import_id), now),
        )

    ability_rows = conn.execute(
        """
        SELECT ability_name, ability_id, COUNT(*) AS use_count
        FROM combat_log_events
        WHERE import_id = ?
          AND source_type = 'player'
          AND source_name = ?
          AND ability_name != ''
        GROUP BY ability_name, ability_id
        """,
        (int(import_id), character_name),
    ).fetchall()

    for row in ability_rows:
        conn.execute(
            """
            INSERT INTO imported_player_character_abilities (
                character_name,
                ability_name,
                ability_id,
                use_count,
                updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(character_name, ability_name, ability_id) DO UPDATE SET
                use_count = imported_player_character_abilities.use_count + excluded.use_count,
                updated_at = excluded.updated_at
            """,
            (
                character_name,
                str(row["ability_name"] or ""),
                str(row["ability_id"] or ""),
                int(row["use_count"] or 0),
                now,
            ),
        )

    ability_count_row = conn.execute(
        """
        SELECT COUNT(*) AS ability_count
        FROM imported_player_character_abilities
        WHERE character_name = ?
        """,
        (character_name,),
    ).fetchone()
    conn.execute(
        """
        UPDATE imported_player_characters
        SET ability_count = ?,
            updated_at = ?
        WHERE character_name = ?
        """,
        (
            int(ability_count_row["ability_count"] if ability_count_row is not None else 0),
            now,
            character_name,
        ),
    )


def rebuild_imported_character_cache(progress_callback=None) -> None:
    init_db()
    with _connect_db() as conn:
        conn.execute("DELETE FROM imported_player_character_abilities")
        conn.execute("DELETE FROM imported_player_character_classes")
        conn.execute("DELETE FROM imported_player_characters")
        conn.execute(
            """
            INSERT INTO imported_player_cache_state (cache_key, last_import_id, updated_at)
            VALUES ('default', 0, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                last_import_id = 0,
                updated_at = excluded.updated_at
            """,
            (datetime.now().isoformat(timespec="seconds"),),
        )
        conn.commit()
    sync_imported_character_cache(progress_callback=progress_callback)


def list_seen_players() -> list[SeenPlayerSummary]:
    init_db()
    sync_seen_player_cache()
    with _connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                canonical_name,
                mention_count,
                import_count,
                source_event_count,
                target_event_count,
                ability_count,
                legacy_name,
                guild_name,
                friend_name,
                note_html
            FROM seen_players
            WHERE instr(canonical_name, char(65533)) = 0
            ORDER BY mention_count DESC, lower(canonical_name)
            """
        ).fetchall()
    return [
        SeenPlayerSummary(
            player_name=str(row["canonical_name"] or ""),
            mention_count=int(row["mention_count"] or 0),
            import_count=int(row["import_count"] or 0),
            source_event_count=int(row["source_event_count"] or 0),
            target_event_count=int(row["target_event_count"] or 0),
            ability_count=int(row["ability_count"] or 0),
            legacy_name=str(row["legacy_name"] or ""),
            guild_name=str(row["guild_name"] or ""),
            friend_name=str(row["friend_name"] or ""),
            note_html=str(row["note_html"] or ""),
        )
        for row in rows
    ]


def list_seen_player_abilities(player_name: str) -> list[ImportedCharacterAbilitySummary]:
    sync_seen_player_cache()
    name = str(player_name or "").strip()
    if not name:
        return []
    canonical_name = _seen_player_canonical_name(name)
    with _connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT ability_name, ability_id, use_count
            FROM seen_player_abilities
            WHERE canonical_name = ?
            ORDER BY lower(ability_name), ability_id
            """,
            (canonical_name,),
        ).fetchall()
    return [
        ImportedCharacterAbilitySummary(
            character_name=canonical_name,
            ability_name=str(row["ability_name"] or ""),
            ability_id=str(row["ability_id"] or ""),
            use_count=int(row["use_count"] or 0),
        )
        for row in rows
    ]


def update_seen_player_profile(
    player_name: str,
    legacy_name: str,
    guild_name: str,
    friend_name: str,
) -> None:
    init_db()
    canonical_name = _seen_player_canonical_name(str(player_name or "").strip())
    if not canonical_name:
        return
    with _connect_db() as conn:
        conn.execute(
            """
            UPDATE seen_players
            SET legacy_name = ?,
                guild_name = ?,
                friend_name = ?,
                updated_at = ?
            WHERE canonical_name = ?
            """,
            (
                str(legacy_name or "").strip(),
                str(guild_name or "").strip(),
                str(friend_name or "").strip(),
                datetime.now().isoformat(timespec="seconds"),
                canonical_name,
            ),
        )
        conn.commit()


def get_seen_player_note_html(player_name: str) -> str:
    init_db()
    canonical_name = _seen_player_canonical_name(str(player_name or "").strip())
    if not canonical_name:
        return ""
    with _connect_db() as conn:
        row = conn.execute(
            "SELECT note_html FROM seen_players WHERE canonical_name = ?",
            (canonical_name,),
        ).fetchone()
    return str(row[0] or "") if row is not None else ""


def update_seen_player_note_html(player_name: str, note_html: str) -> None:
    init_db()
    canonical_name = _seen_player_canonical_name(str(player_name or "").strip())
    if not canonical_name:
        return
    with _connect_db() as conn:
        conn.execute(
            """
            UPDATE seen_players
            SET note_html = ?,
                updated_at = ?
            WHERE canonical_name = ?
            """,
            (
                str(note_html or ""),
                datetime.now().isoformat(timespec="seconds"),
                canonical_name,
            ),
        )
        conn.commit()


def sync_seen_player_cache(progress_callback=None, import_id: Optional[int] = None) -> None:
    init_db()
    sync_imported_character_cache(import_id=import_id)
    with _connect_db() as conn:
        conn.row_factory = sqlite3.Row
        state_row = conn.execute(
            "SELECT last_import_id FROM seen_player_cache_state WHERE cache_key = 'default'"
        ).fetchone()
        last_import_id = int(state_row["last_import_id"]) if state_row is not None else 0
        if import_id is not None:
            import_ids = [int(import_id)] if int(import_id) > last_import_id else []
        else:
            import_ids = [
                int(row["import_id"])
                for row in conn.execute(
                    """
                    SELECT import_id
                    FROM combat_log_imports
                    WHERE import_id > ?
                    ORDER BY import_id
                    """,
                    (last_import_id,),
                ).fetchall()
            ]
        total = len(import_ids)
        clean_names = {
            str(row["canonical_name"] or "")
            for row in conn.execute(
                "SELECT canonical_name FROM seen_players WHERE instr(canonical_name, char(65533)) = 0"
            ).fetchall()
        }
        own_names = {
            str(row["character_name"] or "")
            for row in conn.execute(
                "SELECT character_name FROM imported_player_characters"
            ).fetchall()
        }
        for idx, current_import_id in enumerate(import_ids, start=1):
            _process_seen_player_import(conn, current_import_id, clean_names, own_names)
            conn.execute(
                """
                INSERT INTO seen_player_cache_state (cache_key, last_import_id, updated_at)
                VALUES ('default', ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    last_import_id = excluded.last_import_id,
                    updated_at = excluded.updated_at
                """,
                (current_import_id, datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()
            if progress_callback is not None:
                progress_callback(idx, total)


def _process_seen_player_import(
    conn: sqlite3.Connection,
    import_id: int,
    clean_names: set[str],
    own_names: set[str],
) -> None:
    mention_rows = conn.execute(
        """
        WITH player_mentions AS (
            SELECT
                source_name AS player_name,
                1 AS source_hit,
                0 AS target_hit
            FROM combat_log_events
            WHERE import_id = ?
              AND source_type = 'player'
              AND source_name != ''
            UNION ALL
            SELECT
                target_name AS player_name,
                0 AS source_hit,
                1 AS target_hit
            FROM combat_log_events
            WHERE import_id = ?
              AND target_type = 'player'
              AND target_name != ''
        )
        SELECT
            player_name,
            COUNT(*) AS mention_count,
            COALESCE(SUM(source_hit), 0) AS source_event_count,
            COALESCE(SUM(target_hit), 0) AS target_event_count
        FROM player_mentions
        GROUP BY player_name
        """,
        (int(import_id), int(import_id)),
    ).fetchall()
    ability_rows = conn.execute(
        """
        SELECT
            source_name AS player_name,
            ability_name,
            ability_id,
            COUNT(*) AS use_count
        FROM combat_log_events
        WHERE import_id = ?
          AND source_type = 'player'
          AND source_name != ''
          AND ability_name != ''
        GROUP BY source_name, ability_name, ability_id
        """,
        (int(import_id),),
    ).fetchall()

    names_in_import = [str(row["player_name"] or "") for row in mention_rows]
    names_in_import.extend(str(row["player_name"] or "") for row in ability_rows)
    canonical_map = _seen_player_canonical_map(list(clean_names | set(names_in_import)))

    touched_canonical_names: set[str] = set()
    import_seen_canonical_names: set[str] = set()
    now = datetime.now().isoformat(timespec="seconds")
    for row in mention_rows:
        raw_name = str(row["player_name"] or "")
        canonical_name = canonical_map.get(raw_name, raw_name)
        if canonical_name in own_names:
            continue
        _repoint_seen_player_alias(conn, raw_name, canonical_name)
        conn.execute(
            """
            INSERT INTO seen_players (
                canonical_name,
                mention_count,
                import_count,
                source_event_count,
                target_event_count,
                ability_count,
                legacy_name,
                guild_name,
                friend_name,
                note_html,
                updated_at
            ) VALUES (?, ?, 0, ?, ?, 0, '', '', '', '', ?)
            ON CONFLICT(canonical_name) DO UPDATE SET
                mention_count = seen_players.mention_count + excluded.mention_count,
                source_event_count = seen_players.source_event_count + excluded.source_event_count,
                target_event_count = seen_players.target_event_count + excluded.target_event_count,
                updated_at = excluded.updated_at
            """,
            (
                canonical_name,
                int(row["mention_count"] or 0),
                int(row["source_event_count"] or 0),
                int(row["target_event_count"] or 0),
                now,
            ),
        )
        touched_canonical_names.add(canonical_name)
        import_seen_canonical_names.add(canonical_name)
        if "\ufffd" not in canonical_name:
            clean_names.add(canonical_name)

    for canonical_name in import_seen_canonical_names:
        conn.execute(
            """
            UPDATE seen_players
            SET import_count = import_count + 1,
                updated_at = ?
            WHERE canonical_name = ?
            """,
            (now, canonical_name),
        )

    for row in ability_rows:
        raw_name = str(row["player_name"] or "")
        canonical_name = canonical_map.get(raw_name, raw_name)
        if canonical_name in own_names:
            continue
        _repoint_seen_player_alias(conn, raw_name, canonical_name)
        conn.execute(
            """
            INSERT INTO seen_player_abilities (
                canonical_name,
                ability_name,
                ability_id,
                use_count,
                updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(canonical_name, ability_name, ability_id) DO UPDATE SET
                use_count = seen_player_abilities.use_count + excluded.use_count,
                updated_at = excluded.updated_at
            """,
            (
                canonical_name,
                str(row["ability_name"] or ""),
                str(row["ability_id"] or ""),
                int(row["use_count"] or 0),
                now,
            ),
        )
        touched_canonical_names.add(canonical_name)

    for canonical_name in touched_canonical_names:
        ability_count_row = conn.execute(
            "SELECT COUNT(*) AS ability_count FROM seen_player_abilities WHERE canonical_name = ?",
            (canonical_name,),
        ).fetchone()
        conn.execute(
            """
            UPDATE seen_players
            SET ability_count = ?,
                updated_at = ?
            WHERE canonical_name = ?
            """,
            (
                int(ability_count_row["ability_count"] if ability_count_row is not None else 0),
                now,
                canonical_name,
            ),
        )
        conn.execute(
            """
            UPDATE seen_players
            SET legacy_name = CASE
                    WHEN import_count = 1 AND TRIM(legacy_name) = '' THEN 'Rando'
                    ELSE legacy_name
                END,
                guild_name = CASE
                    WHEN import_count = 1 AND TRIM(guild_name) = '' THEN 'Rando'
                    ELSE guild_name
                END,
                friend_name = CASE
                    WHEN import_count = 1 AND TRIM(friend_name) = '' THEN 'Rando'
                    ELSE friend_name
                END,
                updated_at = ?
            WHERE canonical_name = ?
            """,
            (now, canonical_name),
        )


def _repoint_seen_player_alias(conn: sqlite3.Connection, raw_name: str, canonical_name: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    existing = conn.execute(
        "SELECT canonical_name FROM seen_player_aliases WHERE raw_name = ?",
        (raw_name,),
    ).fetchone()
    if existing is not None:
        old_canonical_name = str(existing["canonical_name"] or "")
        if old_canonical_name == canonical_name:
            conn.execute(
                "UPDATE seen_player_aliases SET updated_at = ? WHERE raw_name = ?",
                (now, raw_name),
            )
            return
        _merge_seen_player_records(conn, old_canonical_name, canonical_name)
    conn.execute(
        """
        INSERT INTO seen_player_aliases (raw_name, canonical_name, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(raw_name) DO UPDATE SET
            canonical_name = excluded.canonical_name,
            updated_at = excluded.updated_at
        """,
        (raw_name, canonical_name, now),
    )


def _merge_seen_player_records(conn: sqlite3.Connection, old_name: str, new_name: str) -> None:
    if not old_name or old_name == new_name:
        return
    now = datetime.now().isoformat(timespec="seconds")
    old_row = conn.execute(
        """
        SELECT
            mention_count,
            import_count,
            source_event_count,
            target_event_count,
            ability_count,
            legacy_name,
            guild_name,
            friend_name,
            note_html
        FROM seen_players
        WHERE canonical_name = ?
        """,
        (old_name,),
    ).fetchone()
    if old_row is not None:
        conn.execute(
            """
            INSERT INTO seen_players (
                canonical_name,
                mention_count,
                import_count,
                source_event_count,
                target_event_count,
                ability_count,
                legacy_name,
                guild_name,
                friend_name,
                note_html,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(canonical_name) DO UPDATE SET
                mention_count = seen_players.mention_count + excluded.mention_count,
                import_count = MAX(seen_players.import_count, excluded.import_count),
                source_event_count = seen_players.source_event_count + excluded.source_event_count,
                target_event_count = seen_players.target_event_count + excluded.target_event_count,
                ability_count = MAX(seen_players.ability_count, excluded.ability_count),
                legacy_name = CASE
                    WHEN TRIM(seen_players.legacy_name) = '' THEN excluded.legacy_name
                    ELSE seen_players.legacy_name
                END,
                guild_name = CASE
                    WHEN TRIM(seen_players.guild_name) = '' THEN excluded.guild_name
                    ELSE seen_players.guild_name
                END,
                friend_name = CASE
                    WHEN TRIM(seen_players.friend_name) = '' THEN excluded.friend_name
                    ELSE seen_players.friend_name
                END,
                note_html = CASE
                    WHEN TRIM(seen_players.note_html) = '' THEN excluded.note_html
                    ELSE seen_players.note_html
                END,
                updated_at = excluded.updated_at
            """,
            (
                new_name,
                int(old_row["mention_count"] or 0),
                int(old_row["import_count"] or 0),
                int(old_row["source_event_count"] or 0),
                int(old_row["target_event_count"] or 0),
                int(old_row["ability_count"] or 0),
                str(old_row["legacy_name"] or ""),
                str(old_row["guild_name"] or ""),
                str(old_row["friend_name"] or ""),
                str(old_row["note_html"] or ""),
                now,
            ),
        )
        conn.execute("DELETE FROM seen_players WHERE canonical_name = ?", (old_name,))

    ability_rows = conn.execute(
        """
        SELECT ability_name, ability_id, use_count
        FROM seen_player_abilities
        WHERE canonical_name = ?
        """,
        (old_name,),
    ).fetchall()
    for ability_row in ability_rows:
        conn.execute(
            """
            INSERT INTO seen_player_abilities (
                canonical_name,
                ability_name,
                ability_id,
                use_count,
                updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(canonical_name, ability_name, ability_id) DO UPDATE SET
                use_count = seen_player_abilities.use_count + excluded.use_count,
                updated_at = excluded.updated_at
            """,
            (
                new_name,
                str(ability_row["ability_name"] or ""),
                str(ability_row["ability_id"] or ""),
                int(ability_row["use_count"] or 0),
                now,
            ),
        )
    conn.execute("DELETE FROM seen_player_abilities WHERE canonical_name = ?", (old_name,))
    conn.execute(
        """
        UPDATE seen_player_aliases
        SET canonical_name = ?, updated_at = ?
        WHERE canonical_name = ?
        """,
        (new_name, now, old_name),
    )


def _seen_player_canonical_name(name: str) -> str:
    with _connect_db() as conn:
        row = conn.execute(
            "SELECT canonical_name FROM seen_player_aliases WHERE raw_name = ?",
            (name,),
        ).fetchone()
    return str(row[0] or name) if row is not None else name


def rebuild_seen_player_cache(progress_callback=None) -> None:
    init_db()
    with _connect_db() as conn:
        conn.execute("DELETE FROM seen_player_abilities")
        conn.execute("DELETE FROM seen_player_aliases")
        conn.execute("DELETE FROM seen_players")
        conn.execute(
            """
            INSERT INTO seen_player_cache_state (cache_key, last_import_id, updated_at)
            VALUES ('default', 0, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                last_import_id = 0,
                updated_at = excluded.updated_at
            """,
            (datetime.now().isoformat(timespec="seconds"),),
        )
        conn.commit()
    sync_seen_player_cache(progress_callback=progress_callback)


def _seen_player_aliases() -> dict[str, set[str]]:
    init_db()
    with _connect_db() as conn:
        rows = conn.execute(
            """
            WITH names AS (
                SELECT source_name AS player_name
                FROM combat_log_events
                WHERE source_type = 'player'
                  AND source_name != ''
                UNION
                SELECT target_name AS player_name
                FROM combat_log_events
                WHERE target_type = 'player'
                  AND target_name != ''
            )
            SELECT DISTINCT player_name
            FROM names
            """
        ).fetchall()
    names = [str(row[0] or "") for row in rows]
    canonical_map = _seen_player_canonical_map(names)
    aliases: dict[str, set[str]] = {}
    for raw_name, canonical_name in canonical_map.items():
        aliases.setdefault(canonical_name, set()).add(raw_name)
    return aliases


def _seen_player_canonical_map(names: list[str]) -> dict[str, str]:
    clean_names = [name for name in names if name and "\ufffd" not in name]
    canonical_map: dict[str, str] = {}
    for name in names:
        if not name:
            continue
        canonical_map[name] = _repair_seen_player_name(name, clean_names)
    return canonical_map


def _repair_seen_player_name(name: str, clean_names: list[str]) -> str:
    if "\ufffd" not in name:
        return name
    candidates = [candidate for candidate in clean_names if _replacement_wildcard_match(name, candidate)]
    if len(candidates) == 1:
        return candidates[0]
    return name


def _replacement_wildcard_match(damaged: str, clean: str) -> bool:
    if "\ufffd" not in damaged or "\ufffd" in clean or len(damaged) != len(clean):
        return False
    damaged_folded = _fold_name_for_match(damaged)
    clean_folded = _fold_name_for_match(clean)
    for left, right in zip(damaged_folded, clean_folded):
        if left == "\ufffd":
            continue
        if left != right:
            return False
    return True


def _fold_name_for_match(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text.lower())
        if not unicodedata.combining(ch)
    )


def _encounter_date_for(fight: Fight) -> str:
    if fight._log_path:
        log_date = _date_from_log_path(Path(fight._log_path))
        if log_date:
            return log_date
        try:
            return datetime.fromtimestamp(Path(fight._log_path).stat().st_mtime).date().isoformat()
        except OSError:
            pass
    return datetime.now().date().isoformat()


def _upsert_player_character_for_fight(
    conn: sqlite3.Connection,
    fight: Fight,
    encounter_key: str,
    encounter_date: str,
    updated_at: str,
) -> list[int]:
    """
    Persist one row per player participant in this fight.

    Phase B (multi-player ingestion): every PLAYER and GROUP_MEMBER entity in
    fight.entity_stats gets a player_characters row and a
    player_character_encounters row. The recorder isn't special anymore —
    they're just one of the participants.

    Returns the list of character_ids that were upserted, so the caller can
    refresh per-character ability rollups.
    """
    participants = _player_character_stats_for_all_participants(fight)
    if not participants:
        return []

    character_ids: list[int] = []

    for player_stats in participants:
        # Step 1: ensure a player_characters row exists. Class merging logic
        # is unchanged from the legacy single-player code: don't overwrite a
        # known class with an unknown one.
        conn.execute(
            """
            INSERT INTO player_characters (
                character_name,
                class_name,
                first_seen_date,
                last_seen_date,
                total_damage_done,
                total_healing_done,
                total_taunts,
                total_interrupts,
                updated_at
            ) VALUES (?, ?, ?, ?, 0, 0, 0, 0, ?)
            ON CONFLICT(character_name) DO UPDATE SET
                class_name = CASE
                    WHEN player_characters.class_name = '' AND excluded.class_name != '' THEN excluded.class_name
                    ELSE player_characters.class_name
                END,
                updated_at = excluded.updated_at
            """,
            (
                player_stats["character_name"],
                player_stats["class_name"],
                encounter_date,
                encounter_date,
                updated_at,
            ),
        )

        # Step 2: resolve the auto-assigned character_id by name.
        character_row = conn.execute(
            "SELECT character_id FROM player_characters WHERE character_name = ?",
            (player_stats["character_name"],),
        ).fetchone()
        if character_row is None:
            # Should be impossible after the upsert above, but be defensive
            # rather than silently lose a player.
            continue
        character_id = int(character_row[0])
        character_ids.append(character_id)

        # Step 3: write the per-fight totals for this player. The composite
        # primary key (encounter_key, character_id) makes this safe.
        conn.execute(
            """
            INSERT INTO player_character_encounters (
                encounter_key,
                character_id,
                encounter_date,
                damage_done,
                healing_done,
                taunts,
                interrupts,
                class_name,
                discipline_name,
                class_evidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(encounter_key, character_id) DO UPDATE SET
                encounter_date = excluded.encounter_date,
                damage_done = excluded.damage_done,
                healing_done = excluded.healing_done,
                taunts = excluded.taunts,
                interrupts = excluded.interrupts,
                class_name = CASE
                    WHEN excluded.class_name != '' THEN excluded.class_name
                    ELSE player_character_encounters.class_name
                END,
                discipline_name = CASE
                    WHEN excluded.discipline_name != '' THEN excluded.discipline_name
                    ELSE player_character_encounters.discipline_name
                END,
                class_evidence = CASE
                    WHEN excluded.class_evidence != '' THEN excluded.class_evidence
                    ELSE player_character_encounters.class_evidence
                END
            """,
            (
                encounter_key,
                character_id,
                encounter_date,
                player_stats["damage_done"],
                player_stats["healing_done"],
                player_stats["taunts"],
                player_stats["interrupts"],
                player_stats.get("class_name", ""),
                player_stats.get("discipline_name", ""),
                player_stats.get("class_evidence", ""),
            ),
        )

    # Step 4: refresh each touched character's lifetime totals from the
    # per-encounter rows. This loop is per-character because each player has
    # their own set of historical encounters.
    for character_id in character_ids:
        totals = conn.execute(
            """
            SELECT
                MIN(encounter_date),
                MAX(encounter_date),
                COALESCE(SUM(damage_done), 0),
                COALESCE(SUM(healing_done), 0),
                COALESCE(SUM(taunts), 0),
                COALESCE(SUM(interrupts), 0)
            FROM player_character_encounters
            WHERE character_id = ?
            """,
            (character_id,),
        ).fetchone()
        if totals is None:
            continue

        conn.execute(
            """
            UPDATE player_characters
            SET
                first_seen_date = ?,
                last_seen_date = ?,
                total_damage_done = ?,
                total_healing_done = ?,
                total_taunts = ?,
                total_interrupts = ?,
                updated_at = ?
            WHERE character_id = ?
            """,
            (
                str(totals[0] or encounter_date),
                str(totals[1] or encounter_date),
                int(totals[2] or 0),
                int(totals[3] or 0),
                int(totals[4] or 0),
                int(totals[5] or 0),
                updated_at,
                character_id,
            ),
        )

    return character_ids


def _upsert_player_character_abilities_for_fight(
    conn: sqlite3.Connection,
    fight: Fight,
    character_ids: list[int],
    encounter_key: str,
    updated_at: str,
) -> None:
    """
    Persist per-fight ability use counts for every player participant.

    Walks fight.events ONCE and groups ability activations by source player.
    Then writes counts for every (encounter, player) pair in this fight,
    and refreshes each touched player's lifetime ability rollup.

    The encounter_key-wide DELETE up front is intentional: it represents
    "re-ingest this fight from scratch." Old rows for everyone in this
    encounter are wiped and replaced. Doing it per-character would risk
    leaving stale rows for players who used an ability last time but not
    this time.

    Records THREE counts per (player, ability):
      - use_count: AbilityActivates inside the fight (button presses)
      - prebuff_count: AbilityActivates in the 15s before EnterCombat
      - damage_source_count: damage events attributed to this ability

    These three counts are independent and tell different stories:
      - high use_count + matching damage_source_count = normal rotation
      - 0 use_count + high damage_source_count = DoT/proc carryover
      - prebuff_count > 0 = pre-cast before pull
    """
    if not character_ids:
        return

    # Build the full per-player breakdown. _log_path is needed for the prebuff
    # scan; if Fight wasn't created via scan_fights, _log_path will be missing
    # and prebuff counts will all be zero (graceful degradation).
    log_path = getattr(fight, "_log_path", None)
    counts_by_player = _player_character_ability_counts_full(fight, log_path)

    # Map character_name -> character_id by querying. This is faster than
    # caching at the helper level and handles case-insensitive name matching.
    name_to_id: dict[str, int] = {}
    for cid in character_ids:
        row = conn.execute(
            "SELECT character_name FROM player_characters WHERE character_id = ?",
            (cid,),
        ).fetchone()
        if row is not None:
            name_to_id[str(row[0])] = cid

    # Wipe any existing per-encounter ability rows for this fight before
    # re-inserting. A fight is upserted as a whole — partial state isn't
    # something we ever want to keep.
    conn.execute(
        "DELETE FROM player_character_encounter_abilities WHERE encounter_key = ?",
        (encounter_key,),
    )

    for character_name, ability_counts in counts_by_player.items():
        # Resolve the character_id for this name. Case-insensitive lookup
        # because player_characters.character_name is COLLATE NOCASE.
        character_id = None
        for known_name, cid in name_to_id.items():
            if known_name.lower() == character_name.lower():
                character_id = cid
                break
        if character_id is None:
            # Player appeared in events but didn't pass the participant filter
            # (e.g. zero damage/healing/taunts/interrupts and so didn't get a
            # player_characters row). Skip their ability counts too — keeping
            # the two tables consistent is more important than completeness.
            continue

        for (ability_id, ability_name), counts in ability_counts.items():
            pressed = int(counts.get("pressed", 0))
            prebuff = int(counts.get("prebuff", 0))
            damage_source = int(counts.get("damage_source", 0))
            # Skip rows where every count is zero — those carry no information
            # and just bloat the table. Realistically this shouldn't happen
            # because each (ability_id, ability_name) key only exists if at
            # least one source contributed, but be defensive.
            if pressed == 0 and prebuff == 0 and damage_source == 0:
                continue

            conn.execute(
                """
                INSERT INTO player_character_encounter_abilities (
                    encounter_key,
                    character_id,
                    ability_name,
                    ability_id,
                    use_count,
                    prebuff_count,
                    damage_source_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    encounter_key,
                    character_id,
                    ability_name,
                    ability_id,
                    pressed,
                    prebuff,
                    damage_source,
                ),
            )

    # Refresh the lifetime ability rollup table for each touched character.
    # We rebuild from per-encounter data so the totals are always derivable
    # from the source-of-truth tables.
    for character_id in character_ids:
        conn.execute(
            "DELETE FROM player_character_abilities WHERE character_id = ?",
            (character_id,),
        )
        aggregated = conn.execute(
            """
            SELECT ability_name, ability_id, COALESCE(SUM(use_count), 0) AS total_uses
            FROM player_character_encounter_abilities
            WHERE character_id = ?
            GROUP BY ability_id, ability_name
            HAVING total_uses > 0
            ORDER BY lower(ability_name), ability_id
            """,
            (character_id,),
        ).fetchall()
        for ability_name, ability_id, total_uses in aggregated:
            conn.execute(
                """
                INSERT INTO player_character_abilities (
                    character_id,
                    ability_name,
                    ability_id,
                    total_uses,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    character_id,
                    str(ability_name or "").strip(),
                    str(ability_id or "").strip(),
                    int(total_uses or 0),
                    updated_at,
                ),
            )


def _player_character_stats_for_all_participants(fight: Fight) -> list[dict[str, int | str]]:
    """
    Return per-player totals for every PLAYER and GROUP_MEMBER entity in
    the fight that did something measurable.

    Only entities with non-zero participation make it into the result.
    "Participation" means: damage dealt, healing done, taunts, or
    interrupts. A player who appeared in the events log but contributed
    nothing isn't worth a database row — they were probably just standing
    next to the boss.

    Companions are excluded — their damage/healing belongs to the
    companion entity, not the owning player. The Overview tab treats them
    that way too, so we stay consistent.

    NPCs and Hazards are excluded — they're not players.

    Returns: list of dicts with keys character_name, class_name,
    damage_done, healing_done, taunts, interrupts.
    """
    from engine.aggregator import EntityKind  # local import — aggregator already imported above

    results: list[dict[str, int | str]] = []

    # Cache by name to avoid redundant computation if somehow the same name
    # appears twice.
    tank_metrics_cache: dict[str, tuple[int, int]] = {}

    # Phase C+: pre-compute the rich ability counts for every player so the
    # class detector can vote across pressed + prebuff + damage_source
    # without re-walking events per player. Single fight walk = much faster
    # than per-player when there are 8 raiders. log_path may be missing on
    # synthetic fights — _player_character_ability_counts_full degrades
    # gracefully (prebuff counts come back as 0).
    log_path = getattr(fight, "_log_path", None)
    try:
        all_ability_counts = _player_character_ability_counts_full(fight, log_path)
    except Exception:
        # If the count helper fails for any reason, detection falls back to
        # presses-only via the events path. We don't want a class-detection
        # bug to block ingestion.
        all_ability_counts = {}

    for entity_name, entity in fight.entity_stats.items():
        if entity.kind not in (EntityKind.PLAYER, EntityKind.GROUP_MEMBER):
            continue
        name = str(entity_name or "").strip()
        if not name:
            continue

        damage_done = int(entity.damage_dealt or 0)
        healing_done = int(entity.healing_done or 0)

        # analyse_tank walks fight.events and computes taunt/interrupt counts
        # for the named player. It can fail on edge cases — wrap defensively.
        if name not in tank_metrics_cache:
            taunts, interrupts = 0, 0
            try:
                m = analyse_tank(fight, name)
                taunts = int(m.taunt_count or 0)
                interrupts = int(m.interrupt_count or 0)
            except Exception:
                pass
            tank_metrics_cache[name] = (taunts, interrupts)
        else:
            taunts, interrupts = tank_metrics_cache[name]

        # Participation gate: drop players who did nothing measurable. This
        # mirrors the spirit of the original `HAVING total_uses > 0` filter
        # — don't store empty rows.
        if damage_done == 0 and healing_done == 0 and taunts == 0 and interrupts == 0:
            continue

        # Phase C: detect class + discipline + evidence. Pass this player's
        # aggregated ability counts so the fingerprint vote can include
        # prebuff + damage_source signal, not just AbilityActivate presses.
        # The per-player slice may be empty for players who appeared in
        # entity_stats but have no abilities tracked (very rare).
        player_ability_counts = all_ability_counts.get(name, {})
        detection = _class_detection_from_fight(
            fight, name, ability_counts=player_ability_counts
        )

        results.append({
            "character_name": name,
            "class_name": detection.class_name,
            "discipline_name": detection.discipline_name,
            "class_evidence": detection.evidence,
            "damage_done": damage_done,
            "healing_done": healing_done,
            "taunts": taunts,
            "interrupts": interrupts,
        })

    return results


def _class_detection_from_fight(fight: Fight, character_name: str, ability_counts=None):
    """
    Phase C: run class/discipline detection for one player in one fight.

    Returns a class_detection.ClassDetection with class_name,
    discipline_name, confidence, and evidence. Returns an empty detection
    if no signal could be extracted — the upsert layer treats that as "no
    change" rather than overwriting known data.

    `ability_counts` (optional): pre-aggregated counts in the shape produced
    by _player_character_ability_counts_full(). Passing them in lets the
    fingerprint vote include prebuff + damage_source signal, dramatically
    improving detection for bystander players who didn't press much.
    """
    from engine.class_detection import detect_class
    return detect_class(fight.events, character_name, ability_counts=ability_counts)


def _class_name_from_fight(fight: Fight, character_name: str) -> str:
    """
    Backward-compatible class-name-only accessor. Kept because external
    code or older tests may still call it. Internally we use
    _class_detection_from_fight which carries discipline + evidence too.
    """
    return _class_detection_from_fight(fight, character_name).class_name


def _player_character_ability_counts_for_all_participants(
    fight: Fight,
) -> dict[str, dict[tuple[str, str], int]]:
    """
    Walk fight.events ONCE and produce ability use counts grouped by source
    player.

    Result shape: {character_name: {(ability_id, ability_name): use_count}}

    The single-pass walk is the performance win over a per-player approach.
    With 8 raiders and a 5-minute boss fight, fight.events has tens of
    thousands of entries. Walking it once is N times faster than walking
    it once per player.
    """
    counts: dict[str, dict[tuple[str, str], int]] = {}

    for ev in fight.events:
        if not ev.is_ability_activate or not ev.ability:
            continue
        # Skip companion casts — they belong to a player but aren't player
        # ability presses, and the player_character_abilities table is for
        # things the player themselves clicked.
        if ev.source.companion:
            continue
        # Source must be a player. ev.source.player is the player name when
        # the source is a player, empty/None otherwise.
        source_player = str(ev.source.player or "").strip()
        if not source_player:
            continue

        ability_name = str(ev.ability.name or "").strip()
        ability_id = str(ev.ability.id or "").strip()
        if not ability_name:
            continue

        bucket = counts.setdefault(source_player, {})
        key = (ability_id, ability_name)
        bucket[key] = bucket.get(key, 0) + 1

    return counts


# ─── Phase E: pressed / damage_source / prebuff ability counts ──────────────


# How far back to look (in seconds) for pre-buff abilities before EnterCombat.
# 15 seconds catches most realistic pre-pulls: DoT seeding, ground-target
# placement, stealth setup, Heroic Moment cooldowns. Long enough to be useful,
# short enough that we don't accidentally pick up casts from the previous
# fight's cleanup.
PREBUFF_WINDOW_SECONDS = 15


def _seconds_between(earlier, later) -> float:
    """
    Return seconds between two datetime.time values. Handles the 'crossing
    midnight' edge case by assuming `later` is on the same day OR the next.
    Combat logs that cross midnight are rare but real — Bioware's logs use
    wall-clock time. If `later` looks numerically smaller than `earlier`,
    we treat it as having crossed midnight.
    """
    earlier_seconds = (
        earlier.hour * 3600 + earlier.minute * 60 + earlier.second
        + earlier.microsecond / 1_000_000
    )
    later_seconds = (
        later.hour * 3600 + later.minute * 60 + later.second
        + later.microsecond / 1_000_000
    )
    diff = later_seconds - earlier_seconds
    if diff < -3600:  # more than an hour "earlier" → crossed midnight
        diff += 86400
    return diff


def _scan_prebuff_ability_activates(
    log_path: str,
    fight: Fight,
) -> dict[str, dict[tuple[str, str], int]]:
    """
    Walk the log file from the beginning up to the fight's start line, keeping
    only AbilityActivate events that happened in the last PREBUFF_WINDOW_SECONDS
    before EnterCombat.

    Returns the same shape as _player_character_ability_counts_for_all_participants:
        {character_name: {(ability_id, ability_name): count}}

    Why walk from the start: random-access to a specific line in a text file
    is awkward. The pre-fight scan only needs to read up to fight._line_start,
    which is bounded. For a 5,000-line log with a fight starting at line 2,000,
    we read 2,000 lines once and discard most of them — fast enough that this
    isn't worth optimizing.

    Pre-buff events that fall *before* the previous fight's ExitCombat are
    excluded — those belong to the previous fight, not this one.
    """
    counts: dict[str, dict[tuple[str, str], int]] = {}
    fight_start_time = fight.start_time
    if fight_start_time is None:
        return counts

    # We collect candidate events in a small buffer keyed by timestamp, then
    # filter at the end. This avoids re-walking the buffer on each line.
    candidates: list = []  # list of (event, timestamp)

    try:
        with _open_log(log_path) as handle:
            for line_number, raw in enumerate(handle):
                # We only care about lines strictly before the fight starts.
                # _line_start is the line index of the EnterCombat itself.
                if line_number >= fight._line_start:
                    break

                ev = parse_line(raw.rstrip("\r\n"))
                if ev is None:
                    continue

                # If this is an ExitCombat, drop everything we've collected
                # so far — that's the previous fight ending. Anything pre-buff
                # for THIS fight has to come after the previous fight's exit.
                if ev.is_exit_combat:
                    candidates = []
                    continue

                # Only AbilityActivate events from players, no companions.
                if not ev.is_ability_activate or not ev.ability:
                    continue
                if ev.source.companion:
                    continue
                if not ev.source.player:
                    continue
                if not (ev.ability.name or "").strip():
                    continue

                candidates.append((ev, ev.timestamp))
    except Exception:
        # Reading the log failed for some reason. Return empty rather than
        # blowing up the whole import.
        return counts

    # Filter candidates to those within PREBUFF_WINDOW_SECONDS of fight start.
    for ev, ts in candidates:
        if ts is None:
            continue
        gap = _seconds_between(ts, fight_start_time)
        if gap < 0:
            # Event is after fight start — shouldn't happen given our break,
            # but guard anyway.
            continue
        if gap > PREBUFF_WINDOW_SECONDS:
            continue

        source_player = str(ev.source.player or "").strip()
        ability_name = str(ev.ability.name or "").strip()
        ability_id = str(ev.ability.id or "").strip()

        bucket = counts.setdefault(source_player, {})
        key = (ability_id, ability_name)
        bucket[key] = bucket.get(key, 0) + 1

    return counts


def _player_character_damage_source_counts(
    fight: Fight,
) -> dict[str, dict[tuple[str, str], int]]:
    """
    Walk fight.events ONCE and count, per player, how many damage events were
    attributed to each of their abilities.

    This is option (C): "the abilities the player did damage with," not "the
    abilities the player pressed." Captures DoT ticks, procs, reflected damage,
    and similar events that don't have a corresponding AbilityActivate inside
    the fight window.

    Only damage events count here. Heals are tracked separately if at all.

    Same return shape as the other two helpers.
    """
    counts: dict[str, dict[tuple[str, str], int]] = {}

    for ev in fight.events:
        if not ev.is_damage:
            continue
        if not ev.ability:
            continue
        # Companion damage stays attributed to the companion, not the player.
        if ev.source.companion:
            continue
        source_player = str(ev.source.player or "").strip()
        if not source_player:
            continue
        ability_name = str(ev.ability.name or "").strip()
        ability_id = str(ev.ability.id or "").strip()
        if not ability_name:
            continue

        bucket = counts.setdefault(source_player, {})
        key = (ability_id, ability_name)
        bucket[key] = bucket.get(key, 0) + 1

    return counts


def _player_character_ability_counts_full(
    fight: Fight,
    log_path: Optional[str] = None,
) -> dict[str, dict[tuple[str, str], dict[str, int]]]:
    """
    Build the full per-player ability count breakdown for a fight.

    Returns:
        {character_name: {(ability_id, ability_name): {
            "pressed":         <int>,  # AbilityActivates inside the fight
            "prebuff":         <int>,  # AbilityActivates in the pre-fight window
            "damage_source":   <int>,  # Damage events attributed to this ability
        }}}

    Every ability the player touched in any of these three ways gets one
    entry. Counts default to 0 for sources where they didn't contribute.

    log_path is needed for the prebuff scan because Fight.events only contains
    in-fight events. If log_path is None or the scan fails, prebuff counts
    will all be 0 — degraded gracefully.
    """
    pressed_counts = _player_character_ability_counts_for_all_participants(fight)
    damage_counts = _player_character_damage_source_counts(fight)

    if log_path:
        prebuff_counts = _scan_prebuff_ability_activates(log_path, fight)
    else:
        prebuff_counts = {}

    # Merge into a single shape. Iterate over the union of all three sources.
    all_players = set(pressed_counts) | set(damage_counts) | set(prebuff_counts)
    result: dict[str, dict[tuple[str, str], dict[str, int]]] = {}

    for player in all_players:
        player_pressed = pressed_counts.get(player, {})
        player_damage = damage_counts.get(player, {})
        player_prebuff = prebuff_counts.get(player, {})

        all_keys = set(player_pressed) | set(player_damage) | set(player_prebuff)
        bucket: dict[tuple[str, str], dict[str, int]] = {}
        for key in all_keys:
            bucket[key] = {
                "pressed": player_pressed.get(key, 0),
                "prebuff": player_prebuff.get(key, 0),
                "damage_source": player_damage.get(key, 0),
            }
        result[player] = bucket

    return result


def _scan_character_info_from_log(path: Path, max_lines: int = 40) -> Optional[dict[str, str]]:
    if not path.exists() or not path.is_file():
        return None

    character_name = ""
    class_name = ""
    try:
        with _open_log(str(path)) as handle:
            for idx, raw in enumerate(handle):
                if idx >= max_lines:
                    break
                raw = raw.strip()
                if not raw:
                    continue
                if not class_name:
                    match = DISCIPLINE_CLASS_RE.search(raw)
                    if match:
                        class_name = str(match.group("class_name") or "").strip()
                ev = parse_line(raw)
                if not ev:
                    continue
                for entity in (ev.source, ev.target):
                    if entity.player and not entity.companion:
                        character_name = entity.player.strip()
                        break
                if character_name and class_name:
                    break
    except Exception:
        return None

    character_name = character_name.strip()
    if not character_name:
        return None
    return {
        "character_name": character_name,
        "class_name": class_name.strip(),
        "seen_date": _date_from_log_path(path) or datetime.fromtimestamp(path.stat().st_mtime).date().isoformat(),
    }


def _combat_log_event_row(event, raw_line: str, line_number: int) -> tuple:
    if event is None:
        return (
            line_number, "parse_error", raw_line, "",
            "", "", "", "", "", "", None, None, None, None, None, json.dumps({}, sort_keys=True),
            "", "", "", "", "", "", None, None, None, None, None, json.dumps({}, sort_keys=True),
            "", "", "", "", "", "", "", "",
            None, 0, None, "", "", None, None, None, None, None,
        )

    source = _entity_db_payload(event.source)
    target = _entity_db_payload(event.target)
    return (
        line_number,
        "parsed",
        raw_line,
        event.timestamp.isoformat(timespec="milliseconds"),
        source["name"],
        source["entity_type"],
        source["player_id"],
        source["companion_name"],
        source["entity_id"],
        source["instance_id"],
        source["hp"],
        source["max_hp"],
        source["x"],
        source["y"],
        source["z"],
        source["json"],
        target["name"],
        target["entity_type"],
        target["player_id"],
        target["companion_name"],
        target["entity_id"],
        target["instance_id"],
        target["hp"],
        target["max_hp"],
        target["x"],
        target["y"],
        target["z"],
        target["json"],
        event.ability.name if event.ability else "",
        event.ability.id if event.ability else "",
        str(event.effect_type or ""),
        str(event.effect_name or ""),
        str(event.effect_id or ""),
        event.effect_detail.name if event.effect_detail else "",
        event.effect_detail.id if event.effect_detail else "",
        str(event.raw_result_text or ""),
        int(event.result.amount) if event.result else None,
        1 if event.result and event.result.is_crit else 0,
        int(event.result.overheal) if event.result and event.result.overheal is not None else None,
        str(event.result.result or "") if event.result else "",
        str(event.result.dmg_type or "") if event.result else "",
        int(event.result.absorbed) if event.result and event.result.absorbed is not None else None,
        float(event.result.threat) if event.result and event.result.threat is not None else None,
        float(event.restore_amount) if event.restore_amount is not None else None,
        float(event.spend_amount) if event.spend_amount is not None else None,
        int(event.charges) if event.charges is not None else None,
    )


def _entity_db_payload(entity) -> dict:
    if entity is None:
        return {
            "name": "",
            "entity_type": "",
            "player_id": "",
            "companion_name": "",
            "entity_id": "",
            "instance_id": "",
            "hp": None,
            "max_hp": None,
            "x": None,
            "y": None,
            "z": None,
            "json": json.dumps({}, sort_keys=True),
        }

    if entity.is_self:
        entity_type = "self"
    elif entity.companion:
        entity_type = "companion"
    elif entity.player:
        entity_type = "player"
    elif entity.npc:
        entity_type = "npc"
    elif entity.is_empty:
        entity_type = "empty"
    else:
        entity_type = "unknown"

    payload = {
        "is_self": bool(entity.is_self),
        "is_empty": bool(entity.is_empty),
        "player": entity.player,
        "player_id": entity.player_id,
        "companion": entity.companion,
        "companion_entity_id": entity.companion_entity_id,
        "companion_instance": entity.companion_instance,
        "npc": entity.npc,
        "npc_entity_id": entity.npc_entity_id,
        "npc_instance": entity.npc_instance,
        "x": entity.x,
        "y": entity.y,
        "z": entity.z,
        "hp": entity.hp,
        "maxhp": entity.maxhp,
    }
    return {
        "name": str(entity.display_name or ""),
        "entity_type": entity_type,
        "player_id": str(entity.player_id or ""),
        "companion_name": str(entity.companion or ""),
        "entity_id": str(entity.companion_entity_id or entity.npc_entity_id or entity.player_id or ""),
        "instance_id": str(entity.companion_instance or entity.npc_instance or ""),
        "hp": int(entity.hp) if entity.hp is not None else None,
        "max_hp": int(entity.maxhp) if entity.maxhp is not None else None,
        "x": float(entity.x) if entity.x is not None else None,
        "y": float(entity.y) if entity.y is not None else None,
        "z": float(entity.z) if entity.z is not None else None,
        "json": json.dumps(payload, sort_keys=True),
    }


def _date_from_log_path(path: Path) -> str:
    match = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
    if match:
        return match.group(1)
    match = re.search(r"(\d{4})[-_](\d{2})[-_](\d{2})", path.name)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return ""
