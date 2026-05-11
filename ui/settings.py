"""
ui/settings.py — Lightweight JSON-backed key-value settings store.

Usage:
    from ui.settings import settings   # shared singleton
    settings.get("last_path", "")
    settings.set("last_path", "/some/file.txt")

User-facing preferences (paths the user configured, retention prefs,
server choice, etc.) live under a nested "user_settings" object so
they're cleanly separated from app-state (window positions, tab order,
last-opened file paths, etc.). Use user_get/user_set for those:

    settings.user_get("live_log_dir", "")
    settings.user_set("live_log_dir", "/path/to/logs")
"""

import json
from pathlib import Path

# Settings file lives next to the project root (same location as before)
_SETTINGS_FILE = Path(__file__).parent.parent / "settings.json"

_USER_SETTINGS_KEY = "user_settings"


class Settings:
    """Lightweight JSON-backed key-value store saved next to the app."""

    def __init__(self):
        self._data: dict = {}
        self._load()

    def _load(self):
        try:
            if _SETTINGS_FILE.exists():
                self._data = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            self._data = {}

    def _save(self):
        try:
            _SETTINGS_FILE.write_text(
                json.dumps(self._data, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value):
        self._data[key] = value
        self._save()

    def user_get(self, key: str, default=None):
        """Read a key from the nested user_settings object."""
        bucket = self._data.get(_USER_SETTINGS_KEY) or {}
        if not isinstance(bucket, dict):
            return default
        return bucket.get(key, default)

    def user_set(self, key: str, value):
        """Write a key to the nested user_settings object and persist."""
        bucket = self._data.get(_USER_SETTINGS_KEY)
        if not isinstance(bucket, dict):
            bucket = {}
        bucket[key] = value
        self._data[_USER_SETTINGS_KEY] = bucket
        self._save()


# Shared singleton — import this, don't construct your own
settings = Settings()
