"""
ui/dialogs/settings.py — SettingsDialog: user-facing preferences.

Opens from the ⚙ Settings button on the main toolbar. Tabbed layout so we
can grow into more pages later without a redesign.

Current pages:
  • Log Files — where to find live combat logs, where to archive old ones,
                retention preference, and an on-demand cleanup launcher.
  • Server   — which SWTOR server the user plays on. Stored for future use
               by Find-a-Fight / Cohort filters.

Settings live in settings.json under a nested "user_settings" object;
read/written via settings.user_get / settings.user_set.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QTabWidget, QWidget,
    QLabel, QLineEdit, QPushButton, QComboBox, QFileDialog,
    QDialogButtonBox, QMessageBox,
)

from ui.settings import settings as _settings
from ui.dialogs.log_cleanup import LogCleanupDialog
from engine.server_list import load_servers, format_display_name


RETENTION_CHOICES = [
    ("1 month",  1),
    ("2 months", 2),
    ("3 months", 3),
]
DEFAULT_RETENTION_MONTHS = 2


class SettingsDialog(QDialog):
    """User-facing settings, opened from the toolbar ⚙ button."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumSize(560, 380)
        self.setModal(True)
        # Load the SWTOR server list from data/swtor_servers.json. The
        # loader creates the file on first launch from a built-in default,
        # so this should always return a non-empty list.
        self._servers = load_servers()
        self._build_ui()
        self._load_from_settings()

    # ── Build ─────────────────────────────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 12)
        root.setSpacing(10)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_log_files_page(), "Log Files")
        self._tabs.addTab(self._build_server_page(), "Server")
        root.addWidget(self._tabs, 1)

        # Standard OK / Cancel / Apply. Apply saves without closing so the
        # user can save, then immediately click "Clean up now…" using the
        # newly-saved values without re-typing.
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply
        )
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        btns.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._save)
        root.addWidget(btns)

    def _build_log_files_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 12, 8, 8)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(8)

        # Live combat logs folder
        self._live_edit = QLineEdit()
        self._live_edit.setPlaceholderText("e.g. C:/Users/You/Documents/Star Wars - The Old Republic/CombatLogs")
        live_browse = QPushButton("Browse…")
        live_browse.clicked.connect(lambda: self._pick_folder(self._live_edit, "Choose live logs folder"))
        live_row = QHBoxLayout()
        live_row.setSpacing(6)
        live_row.addWidget(self._live_edit, 1)
        live_row.addWidget(live_browse)
        live_w = QWidget(); live_w.setLayout(live_row)
        form.addRow("Live combat logs:", live_w)

        # History folder (optional)
        self._history_edit = QLineEdit()
        self._history_edit.setPlaceholderText("(optional — leave blank if you don't archive logs)")
        hist_browse = QPushButton("Browse…")
        hist_browse.clicked.connect(lambda: self._pick_folder(self._history_edit, "Choose history folder"))
        hist_row = QHBoxLayout()
        hist_row.setSpacing(6)
        hist_row.addWidget(self._history_edit, 1)
        hist_row.addWidget(hist_browse)
        hist_w = QWidget(); hist_w.setLayout(hist_row)
        form.addRow("History folder:", hist_w)

        # Retention dropdown
        self._retention_combo = QComboBox()
        for label, _months in RETENTION_CHOICES:
            self._retention_combo.addItem(label)
        form.addRow("Keep live logs for:", self._retention_combo)

        layout.addLayout(form)

        # Explanatory note about the History-blank rule
        note = QLabel(
            "<i>If History is blank, the Live folder is treated as the home "
            "for both. In that case, the Clean up dialog can only delete — "
            "there's nowhere separate to move files to.</i>"
        )
        note.setTextFormat(Qt.TextFormat.RichText)
        note.setWordWrap(True)
        note.setStyleSheet("color: #8b949e; font-size: 11px; padding: 4px 0;")
        layout.addWidget(note)

        # Clean up button
        cleanup_row = QHBoxLayout()
        cleanup_row.addStretch()
        self._btn_cleanup = QPushButton("Clean up now…")
        self._btn_cleanup.setToolTip("Scan the live folder for old logs and offer to delete or move them.")
        self._btn_cleanup.clicked.connect(self._on_cleanup)
        cleanup_row.addWidget(self._btn_cleanup)
        layout.addLayout(cleanup_row)

        layout.addStretch()
        return page

    def _build_server_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 12, 8, 8)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(8)

        self._server_combo = QComboBox()
        self._server_combo.addItem("(not set)")
        for info in self._servers:
            self._server_combo.addItem(format_display_name(info))
        form.addRow("SWTOR server:", self._server_combo)

        layout.addLayout(form)

        note = QLabel(
            "<i>Used for future Find-a-Fight and Cohort filters so you can "
            "find fights from players on your server.<br><br>"
            "The server list is loaded from "
            "<code>data/swtor_servers.json</code> — you can edit that file "
            "to add or rename servers if Bioware updates them.</i>"
        )
        note.setTextFormat(Qt.TextFormat.RichText)
        note.setWordWrap(True)
        note.setStyleSheet("color: #8b949e; font-size: 11px; padding: 4px 0;")
        layout.addWidget(note)

        layout.addStretch()
        return page

    # ── Helpers ────────────────────────────────────────────────────────────
    def _pick_folder(self, edit: QLineEdit, title: str):
        start = edit.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, title, start)
        if chosen:
            edit.setText(chosen)

    def _retention_months_from_combo(self) -> int:
        idx = self._retention_combo.currentIndex()
        if 0 <= idx < len(RETENTION_CHOICES):
            return RETENTION_CHOICES[idx][1]
        return DEFAULT_RETENTION_MONTHS

    # ── Load / Save ────────────────────────────────────────────────────────
    def _load_from_settings(self):
        self._live_edit.setText(_settings.user_get("live_log_dir", "") or "")
        self._history_edit.setText(_settings.user_get("history_log_dir", "") or "")

        retention = _settings.user_get("log_retention_months", DEFAULT_RETENTION_MONTHS)
        match_idx = 0
        for i, (_label, months) in enumerate(RETENTION_CHOICES):
            if months == retention:
                match_idx = i
                break
        self._retention_combo.setCurrentIndex(match_idx)

        server = _settings.user_get("server", "") or ""
        server_names = [info.name for info in self._servers]
        if server in server_names:
            # +1 because index 0 is "(not set)"
            self._server_combo.setCurrentIndex(server_names.index(server) + 1)
        else:
            self._server_combo.setCurrentIndex(0)

    def _save(self):
        _settings.user_set("live_log_dir", self._live_edit.text().strip())
        _settings.user_set("history_log_dir", self._history_edit.text().strip())
        _settings.user_set("log_retention_months", self._retention_months_from_combo())

        idx = self._server_combo.currentIndex()
        if idx == 0:
            _settings.user_set("server", "")
        else:
            # idx 1..len(_servers) maps to self._servers[idx - 1]
            chosen = self._servers[idx - 1] if 0 <= idx - 1 < len(self._servers) else None
            _settings.user_set("server", chosen.name if chosen else "")

    def _on_accept(self):
        self._save()
        self.accept()

    # ── Cleanup launcher ──────────────────────────────────────────────────
    def _on_cleanup(self):
        live_text = self._live_edit.text().strip()
        if not live_text:
            QMessageBox.information(
                self, "No live folder set",
                "Set a Live combat logs folder above before running Clean up."
            )
            return
        live_path = Path(live_text)
        if not live_path.exists() or not live_path.is_dir():
            QMessageBox.warning(
                self, "Folder not found",
                f"This folder doesn't exist:\n{live_path}"
            )
            return

        history_text = self._history_edit.text().strip()
        history_path = Path(history_text) if history_text else None

        months = self._retention_months_from_combo()

        # Open the cleanup dialog modally. It handles its own confirm flow.
        dlg = LogCleanupDialog(
            live_folder=live_path,
            history_folder=history_path,
            retention_months=months,
            parent=self,
        )
        dlg.exec()
