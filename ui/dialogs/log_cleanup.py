"""
ui/dialogs/log_cleanup.py — LogCleanupDialog: scan a live-log folder for old
combat logs and offer to delete them or move them to a history folder.

Trigger: opened from the Settings dialog via a "Clean up now…" button.

Safety principles:
- Never operate on files outside the configured live folder
- Never delete or move without explicit confirmation
- The retention dropdown is a *default*, not a rule — the dialog always
  shows the user the file list and asks before doing anything
- File moves use shutil.move which falls back to copy+delete across drives
- Errors per-file are caught individually so one bad file doesn't kill the
  whole batch; a summary lists what worked and what didn't
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QMessageBox, QDialogButtonBox,
    QAbstractItemView,
)


def _format_size(size_bytes: float) -> str:
    """Human-readable file size (KB / MB / GB)."""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"


def _format_age(days: float) -> str:
    """Human-readable file age."""
    if days < 1:
        return "today"
    if days < 30:
        n = int(days)
        return f"{n} day{'s' if n != 1 else ''} ago"
    months = int(days / 30)
    if months < 12:
        return f"{months} month{'s' if months != 1 else ''} ago"
    years = days / 365.25
    return f"{years:.1f} years ago"


class LogCleanupDialog(QDialog):
    """Modal cleanup dialog: scan, confirm, delete or move.

    Construct with the live folder path, optional history folder path, and
    retention threshold in months. Shows the user a list of candidate files
    older than the threshold, and offers Delete or Move-to-History.
    """

    def __init__(
        self,
        live_folder: Path,
        history_folder: Optional[Path],
        retention_months: int,
        parent=None,
    ):
        super().__init__(parent)
        self._live_folder = Path(live_folder)
        self._history_folder = Path(history_folder) if history_folder else None
        self._retention_months = retention_months
        self._candidates: list[Path] = []

        self.setWindowTitle("Clean up old combat logs")
        self.setMinimumSize(560, 460)
        self._build_ui()
        self._scan()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        header = QLabel(
            f"<b>Live folder:</b> {self._live_folder}<br>"
            f"<b>Retention threshold:</b> {self._retention_months} month"
            f"{'s' if self._retention_months != 1 else ''}"
        )
        header.setTextFormat(Qt.TextFormat.RichText)
        header.setWordWrap(True)
        root.addWidget(header)

        self._summary_lbl = QLabel("Scanning…")
        self._summary_lbl.setStyleSheet("color: #8b949e;")
        root.addWidget(self._summary_lbl)

        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list.setAlternatingRowColors(True)
        root.addWidget(self._list, 1)

        hint = QLabel(
            "Tip: select specific files to act on, or leave nothing "
            "selected to act on the full list."
        )
        hint.setStyleSheet("color: #6e7681; font-size: 11px;")
        hint.setWordWrap(True)
        root.addWidget(hint)

        action_row = QHBoxLayout()
        action_row.addStretch()

        self._btn_move = QPushButton("Move to History")
        self._btn_move.clicked.connect(self._on_move)
        action_row.addWidget(self._btn_move)

        self._btn_delete = QPushButton("Delete")
        self._btn_delete.setStyleSheet("color: #f85149; font-weight: 500;")
        self._btn_delete.clicked.connect(self._on_delete)
        action_row.addWidget(self._btn_delete)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(self.reject)
        action_row.addWidget(btns)

        root.addLayout(action_row)

    def _scan(self):
        """Find log files older than the threshold and populate the list."""
        self._list.clear()
        self._candidates = []

        if not self._live_folder.exists() or not self._live_folder.is_dir():
            self._summary_lbl.setText(
                f"⚠ Folder not found: {self._live_folder}"
            )
            self._btn_move.setEnabled(False)
            self._btn_delete.setEnabled(False)
            return

        cutoff_ts = time.time() - (self._retention_months * 30 * 24 * 3600)
        now = time.time()

        try:
            log_files = sorted(self._live_folder.glob("combat_*.txt"))
        except OSError as exc:
            self._summary_lbl.setText(f"⚠ Could not scan: {exc}")
            return

        total_size = 0
        for path in log_files:
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime > cutoff_ts:
                continue
            self._candidates.append(path)
            total_size += stat.st_size
            age_days = (now - stat.st_mtime) / (24 * 3600)
            label = (
                f"{path.name}    "
                f"{_format_size(stat.st_size)}    "
                f"{_format_age(age_days)}"
            )
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            self._list.addItem(item)

        if not self._candidates:
            self._summary_lbl.setText(
                f"No logs older than {self._retention_months} month"
                f"{'s' if self._retention_months != 1 else ''}. "
                "Nothing to clean up."
            )
            self._btn_move.setEnabled(False)
            self._btn_delete.setEnabled(False)
            return

        self._summary_lbl.setText(
            f"Found {len(self._candidates)} file"
            f"{'s' if len(self._candidates) != 1 else ''} "
            f"({_format_size(total_size)} total)."
        )
        # Move button is only useful if a history folder is configured AND
        # it's actually different from the live folder.
        history_usable = (
            self._history_folder is not None
            and self._history_folder != self._live_folder
        )
        self._btn_move.setEnabled(history_usable)
        if not history_usable:
            self._btn_move.setToolTip(
                "Set a History folder in Settings → Log Files to enable this. "
                "(If History is unset or equals Live, there's nowhere to move to.)"
            )

    def _selected_or_all(self) -> list[Path]:
        """Return the user's selection, or all candidates if none selected."""
        selected_items = self._list.selectedItems()
        if not selected_items:
            return list(self._candidates)
        paths = []
        for item in selected_items:
            raw = item.data(Qt.ItemDataRole.UserRole)
            if raw:
                paths.append(Path(raw))
        return paths

    def _on_delete(self):
        targets = self._selected_or_all()
        if not targets:
            return
        confirm = QMessageBox(self)
        confirm.setWindowTitle("Delete logs?")
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setText(
            f"Permanently delete {len(targets)} file"
            f"{'s' if len(targets) != 1 else ''}?"
        )
        confirm.setInformativeText("This cannot be undone.")
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes
        )
        confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return

        ok, failed = 0, []
        for path in targets:
            try:
                path.unlink()
                ok += 1
            except OSError as exc:
                failed.append((path.name, str(exc)))
        self._show_result_summary("Deleted", ok, failed)
        self._scan()

    def _on_move(self):
        if self._history_folder is None:
            return
        targets = self._selected_or_all()
        if not targets:
            return
        confirm = QMessageBox(self)
        confirm.setWindowTitle("Move logs?")
        confirm.setIcon(QMessageBox.Icon.Question)
        confirm.setText(
            f"Move {len(targets)} file"
            f"{'s' if len(targets) != 1 else ''} to History?"
        )
        confirm.setInformativeText(
            f"Destination: {self._history_folder}\n\n"
            "Files of the same name in the destination will not be overwritten "
            "— they'll be skipped and counted as failures."
        )
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes
        )
        confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return

        try:
            self._history_folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(
                self, "Move failed",
                f"Could not create destination folder:\n{exc}"
            )
            return

        ok, failed = 0, []
        for path in targets:
            dest = self._history_folder / path.name
            if dest.exists():
                failed.append((path.name, "already exists in history"))
                continue
            try:
                shutil.move(str(path), str(dest))
                ok += 1
            except (OSError, shutil.Error) as exc:
                failed.append((path.name, str(exc)))
        self._show_result_summary("Moved", ok, failed)
        self._scan()

    def _show_result_summary(self, verb: str, ok_count: int, failed: list):
        msg = QMessageBox(self)
        if not failed:
            msg.setIcon(QMessageBox.Icon.Information)
            msg.setWindowTitle("Done")
            msg.setText(
                f"{verb} {ok_count} file"
                f"{'s' if ok_count != 1 else ''}."
            )
        else:
            msg.setIcon(QMessageBox.Icon.Warning)
            msg.setWindowTitle("Partial success")
            msg.setText(
                f"{verb} {ok_count} file{'s' if ok_count != 1 else ''}, "
                f"{len(failed)} failed."
            )
            details = "\n".join(f"• {name}: {reason}" for name, reason in failed[:20])
            if len(failed) > 20:
                details += f"\n…and {len(failed) - 20} more."
            msg.setDetailedText(details)
        msg.setStandardButtons(QMessageBox.StandardButton.Ok)
        msg.exec()
