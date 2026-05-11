"""
engine/server_list.py — SWTOR server list loader.

The list of live servers lives in data/swtor_servers.json so it can be
edited without recompiling. If the file is missing, this module creates
it from the built-in defaults on first call. If the file exists but is
corrupt, the built-in list is used for this session and the file is left
alone (so user edits aren't clobbered).

Used by:
  • ui/dialogs/settings.py — populates the Server dropdown
  • (future) Find-a-Fight / Cohort filters

Public API:
  load_servers() -> list[ServerInfo]
  format_display_name(info) -> str  e.g. "Star Forge (NA)"
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

DATA_FILE = Path(__file__).parent.parent / "data" / "swtor_servers.json"

# Built-in defaults. If data/swtor_servers.json is missing, this list is
# written to disk and then read back. If the file exists but is malformed,
# this list is used for the current session (the broken file is not
# overwritten — user edits stay intact).
#
# Source: official SWTOR server status page at swtor.com/server-status,
# verified May 2026.
_BUILT_IN_SERVERS: list[dict] = [
    {"name": "Star Forge",    "region": "North America",     "region_short": "NA"},
    {"name": "Satele Shan",   "region": "North America",     "region_short": "NA"},
    {"name": "Darth Malgus",  "region": "Europe (English)",  "region_short": "EU"},
    {"name": "Tulak Hord",    "region": "Europe (German)",   "region_short": "DE"},
    {"name": "The Leviathan", "region": "Europe (French)",   "region_short": "FR"},
    {"name": "Shae Vizla",    "region": "Asia-Pacific",      "region_short": "APAC"},
]


@dataclass(frozen=True)
class ServerInfo:
    name: str
    region: str
    region_short: str


def _coerce(raw: dict) -> Optional[ServerInfo]:
    """Validate one entry from the JSON file. Skip rows missing a name."""
    name = str(raw.get("name", "")).strip()
    if not name:
        return None
    region = str(raw.get("region", "")).strip()
    region_short = str(raw.get("region_short", "")).strip()
    return ServerInfo(name=name, region=region, region_short=region_short)


def _write_built_in_defaults(path: Path) -> None:
    """Create data/swtor_servers.json from the built-in list."""
    payload = {
        "_comment": (
            "Live SWTOR servers. Edit this file to add or remove servers "
            "without recompiling. If you delete this file, the app will "
            "recreate it from its built-in list on next launch. "
            "region_short shows next to the name in dropdowns; region is "
            "the descriptive long form."
        ),
        "servers": _BUILT_IN_SERVERS,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        _log.warning("Could not write %s: %s", path, exc)


def load_servers() -> list[ServerInfo]:
    """Return the configured server list.

    Behaviour:
      • Missing file → write defaults to disk, return defaults
      • Valid file → return parsed list
      • Corrupt file → log a warning and return defaults for THIS SESSION;
        do not overwrite the file (would clobber a partially-edited file)
      • Empty servers list → treated like corrupt; return defaults
    """
    if not DATA_FILE.exists():
        _write_built_in_defaults(DATA_FILE)
        return [ServerInfo(**row) for row in _BUILT_IN_SERVERS]

    try:
        payload = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning(
            "Could not read %s (%s) — using built-in server list for this session.",
            DATA_FILE.name, exc,
        )
        return [ServerInfo(**row) for row in _BUILT_IN_SERVERS]

    raw_list = payload.get("servers") if isinstance(payload, dict) else None
    if not isinstance(raw_list, list) or not raw_list:
        _log.warning(
            "%s has no 'servers' list — using built-in server list for this session.",
            DATA_FILE.name,
        )
        return [ServerInfo(**row) for row in _BUILT_IN_SERVERS]

    parsed = [info for info in (_coerce(row) for row in raw_list if isinstance(row, dict)) if info]
    if not parsed:
        _log.warning(
            "%s contained no usable entries — using built-in server list for this session.",
            DATA_FILE.name,
        )
        return [ServerInfo(**row) for row in _BUILT_IN_SERVERS]

    return parsed


def format_display_name(info: ServerInfo) -> str:
    """Render a server for display in dropdowns: 'Star Forge (NA)'.

    If a server has no short region code, just the name is returned.
    """
    if info.region_short:
        return f"{info.name} ({info.region_short})"
    return info.name
