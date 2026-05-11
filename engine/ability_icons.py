"""
ability_icons.py - Reusable SWTOR ability icon lookup and noid rename helpers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_ICON_DIR = PROJECT_ROOT / "icons"
DEFAULT_ABILITIES_PATH = PROJECT_ROOT / "data" / "abilities.json"
DEFAULT_ENCOUNTER_DB_PATH = PROJECT_ROOT / "data" / "encounter_history.sqlite3"

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".ico"}
NOID_RE = re.compile(r"^noid_(?:\d+_)+(?P<name>.+)$", re.IGNORECASE)
ID_FILE_RE = re.compile(r"^(?P<id>\d+)_(?P<name>.+)$")


@dataclass
class AbilityIconRenameResult:
    renamed: list[tuple[Path, Path]] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)
    errors: list[tuple[Path, str]] = field(default_factory=list)


def normalize_ability_name(name: str) -> str:
    """Normalize ability names and filename stems for tolerant matching."""
    text = str(name or "").strip()
    noid_match = NOID_RE.match(text)
    if noid_match:
        text = noid_match.group("name")
    id_match = ID_FILE_RE.match(text)
    if id_match:
        text = id_match.group("name")
    text = text.replace("_", " ").replace("-", " ")
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def ability_filename_name(name: str) -> str:
    """Build the ability-name portion used by icon filenames."""
    text = str(name or "").strip().replace(" ", "_")
    text = re.sub(r'[<>:"/\\|?*]+', "", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("._") or "Unknown"


class AbilityIconLibrary:
    """Indexes local ability icons and resolves them by ability ID or name."""

    def __init__(self, icon_dir: str | Path = DEFAULT_ICON_DIR, refresh_interval_seconds: float = 2.0):
        self.icon_dir = Path(icon_dir)
        self._mtime_ns: int | None = None
        self._last_refresh_check = 0.0
        self._refresh_interval_seconds = refresh_interval_seconds
        self._by_id: dict[str, Path] = {}
        self._by_name: dict[str, Path] = {}

    def refresh(self) -> None:
        self._mtime_ns = None
        self._by_id.clear()
        self._by_name.clear()

    def icon_path(self, ability_name: str = "", ability_id: str = "") -> Path | None:
        self._ensure_index()
        ability_id = str(ability_id or "").strip()
        if ability_id and ability_id in self._by_id:
            return self._by_id[ability_id]
        key = normalize_ability_name(ability_name)
        if key:
            return self._by_name.get(key)
        return None

    def rename_noid_icons_from_known_ids(
        self,
        abilities_path: str | Path = DEFAULT_ABILITIES_PATH,
        encounter_db_path: str | Path = DEFAULT_ENCOUNTER_DB_PATH,
    ) -> AbilityIconRenameResult:
        mappings = known_ability_ids_by_name(abilities_path, encounter_db_path)
        result = rename_noid_icons(self.icon_dir, mappings)
        if result.renamed:
            self.refresh()
        return result

    def rename_noid_icons_for_abilities(
        self,
        abilities: Iterable[tuple[str, str]],
    ) -> AbilityIconRenameResult:
        """Rename noid icons using only ability names/IDs from the current encounter."""
        result = rename_noid_icons(self.icon_dir, abilities)
        if result.renamed:
            self.refresh()
        return result

    def _ensure_index(self) -> None:
        now = time.monotonic()
        if self._mtime_ns is not None and now - self._last_refresh_check < self._refresh_interval_seconds:
            return
        self._last_refresh_check = now
        current_mtime = self._directory_mtime_ns()
        if current_mtime == self._mtime_ns:
            return
        self._mtime_ns = current_mtime
        self._by_id.clear()
        self._by_name.clear()
        if not self.icon_dir.is_dir():
            return
        for path in sorted(self.icon_dir.iterdir()):
            if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            stem = path.stem
            id_match = ID_FILE_RE.match(stem)
            noid_match = NOID_RE.match(stem)
            if id_match:
                ability_id = id_match.group("id")
                self._by_id.setdefault(ability_id, path)
                self._by_name.setdefault(normalize_ability_name(id_match.group("name")), path)
            elif noid_match:
                self._by_name.setdefault(normalize_ability_name(noid_match.group("name")), path)
            else:
                self._by_name.setdefault(normalize_ability_name(stem), path)

    def _directory_mtime_ns(self) -> int:
        if not self.icon_dir.is_dir():
            return -1
        try:
            return max((p.stat().st_mtime_ns for p in self.icon_dir.iterdir()), default=0)
        except OSError:
            return 0


_DEFAULT_LIBRARY = AbilityIconLibrary()


def get_ability_icon_library() -> AbilityIconLibrary:
    return _DEFAULT_LIBRARY


def encounter_ability_pairs(fight) -> list[tuple[str, str]]:
    pairs: dict[str, tuple[str, str]] = {}
    for event in getattr(fight, "events", []) or []:
        ability = getattr(event, "ability", None)
        if ability is None:
            continue
        ability_name = str(getattr(ability, "name", "") or "").strip()
        ability_id = str(getattr(ability, "id", "") or "").strip()
        if not ability_name or not ability_id:
            continue
        pairs.setdefault(normalize_ability_name(ability_name), (ability_name, ability_id))
    return list(pairs.values())


def known_ability_ids_by_name(
    abilities_path: str | Path = DEFAULT_ABILITIES_PATH,
    encounter_db_path: str | Path = DEFAULT_ENCOUNTER_DB_PATH,
) -> dict[str, tuple[str, str]]:
    """Return normalized ability name -> (ability_id, display_name)."""
    mappings: dict[str, tuple[str, str]] = {}
    _load_abilities_json_mappings(Path(abilities_path), mappings)
    _load_encounter_db_mappings(Path(encounter_db_path), mappings)
    return mappings


def rename_noid_icons(
    icon_dir: str | Path,
    known_ids_by_name: dict[str, tuple[str, str]] | Iterable[tuple[str, str]],
) -> AbilityIconRenameResult:
    icon_dir = Path(icon_dir)
    result = AbilityIconRenameResult()
    mappings = _coerce_mapping(known_ids_by_name)
    if not icon_dir.is_dir():
        return result

    for path in sorted(icon_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        match = NOID_RE.match(path.stem)
        if not match:
            continue
        key = normalize_ability_name(match.group("name"))
        known = mappings.get(key)
        if not known:
            result.skipped.append((path, "no known ability ID"))
            continue
        ability_id, ability_name = known
        if not ability_id:
            result.skipped.append((path, "known match has no ability ID"))
            continue
        destination = path.with_name(f"{ability_id}_{ability_filename_name(ability_name)}{path.suffix.lower()}")
        if destination.exists():
            result.skipped.append((path, f"{destination.name} already exists"))
            continue
        try:
            path.rename(destination)
            result.renamed.append((path, destination))
        except OSError as exc:
            result.errors.append((path, str(exc)))
    return result


def _coerce_mapping(
    known_ids_by_name: dict[str, tuple[str, str]] | Iterable[tuple[str, str]],
) -> dict[str, tuple[str, str]]:
    if isinstance(known_ids_by_name, dict):
        return known_ids_by_name
    mappings: dict[str, tuple[str, str]] = {}
    for ability_name, ability_id in known_ids_by_name:
        ability_name = str(ability_name or "").strip()
        ability_id = str(ability_id or "").strip()
        if ability_name and ability_id:
            mappings.setdefault(normalize_ability_name(ability_name), (ability_id, ability_name))
    return mappings


def _load_abilities_json_mappings(path: Path, mappings: dict[str, tuple[str, str]]) -> None:
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    abilities = data.get("abilities", {}) if isinstance(data, dict) else {}
    if not isinstance(abilities, dict):
        return
    for ability_name, payload in abilities.items():
        if not isinstance(payload, dict):
            continue
        ability_id = str(payload.get("id") or "").strip()
        ability_name = str(ability_name or "").strip()
        if ability_name and ability_id:
            mappings.setdefault(normalize_ability_name(ability_name), (ability_id, ability_name))


def _load_encounter_db_mappings(path: Path, mappings: dict[str, tuple[str, str]]) -> None:
    if not path.is_file():
        return
    conn = None
    try:
        conn = sqlite3.connect(str(path))
        rows = conn.execute(
            """
            SELECT ability_name, ability_id, COUNT(*) AS seen_count
            FROM combat_log_events
            WHERE ability_name != ''
              AND ability_id != ''
            GROUP BY ability_name, ability_id
            ORDER BY seen_count DESC
            """
        ).fetchall()
    except sqlite3.Error:
        return
    finally:
        if conn is not None:
            conn.close()
    for ability_name, ability_id, _seen_count in rows:
        ability_name = str(ability_name or "").strip()
        ability_id = str(ability_id or "").strip()
        if ability_name and ability_id:
            mappings.setdefault(normalize_ability_name(ability_name), (ability_id, ability_name))
