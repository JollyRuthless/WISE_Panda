"""
ui/tabs/raw_log.py — RawFightLogTab: raw combat log viewer with search, snapshot save,
                      fight validation, and Great Hunt shortcut.
"""

from pathlib import Path
from typing import Optional, List

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPlainTextEdit,
    QPushButton, QLabel, QLineEdit, QFileDialog,
    QDialog, QDialogButtonBox, QApplication,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QTextCursor, QTextDocument

from engine.aggregator import Fight, load_raw_lines, build_fights
from engine.parser_core import parse_line
from engine.validate_parser_upgraded import validate_fight as run_fight_validation, format_report_text
from ui.theme import BG_PANEL, BORDER, TEXT_PRI, TEXT_SEC
from ui.settings import settings


class RawFightLogTab(QWidget):
    def __init__(self):
        super().__init__()
        self._fight: Optional[Fight] = None
        self._raw_lines: List[str] = []
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        # ── Top action row ────────────────────────────────────────────────
        top = QHBoxLayout()
        self.info_label = QLabel("Select a fight to view the raw combat log.")
        self.info_label.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        top.addWidget(self.info_label)
        top.addStretch()

        self.save_btn = QPushButton("💾  Save Fight Snapshot…")
        self.save_btn.clicked.connect(self._save_snapshot)
        self.save_btn.setEnabled(False)
        top.addWidget(self.save_btn)

        self.validate_btn = QPushButton("Validate Fight")
        self.validate_btn.clicked.connect(self._validate_fight)
        self.validate_btn.setEnabled(False)
        top.addWidget(self.validate_btn)

        self.hunt_btn = QPushButton("Open Great Hunt")
        self.hunt_btn.clicked.connect(self._open_great_hunt)
        self.hunt_btn.setEnabled(False)
        top.addWidget(self.hunt_btn)
        root.addLayout(top)

        # ── Search row ────────────────────────────────────────────────────
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Search:"))
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Find text in this fight log…")
        self.search_input.returnPressed.connect(self._find_next)
        self.search_input.textChanged.connect(self._on_search_text_changed)
        search_row.addWidget(self.search_input)
        self.find_prev_btn = QPushButton("Previous")
        self.find_prev_btn.clicked.connect(self._find_previous)
        search_row.addWidget(self.find_prev_btn)
        self.find_next_btn = QPushButton("Next")
        self.find_next_btn.clicked.connect(self._find_next)
        search_row.addWidget(self.find_next_btn)
        self.search_status = QLabel("")
        self.search_status.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        search_row.addWidget(self.search_status)
        search_row.addStretch()
        root.addLayout(search_row)

        # ── Log viewer ────────────────────────────────────────────────────
        self.viewer = QPlainTextEdit()
        self.viewer.setReadOnly(True)
        self.viewer.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.viewer.setStyleSheet(f"""
            QPlainTextEdit {{
                background: {BG_PANEL};
                border: 1px solid {BORDER};
                border-radius: 6px;
                color: {TEXT_PRI};
                selection-background-color: #1f3d5c;
                font-family: Consolas, "Cascadia Mono", "Courier New", monospace;
                font-size: 12px;
            }}
        """)
        root.addWidget(self.viewer)
        self._update_search_buttons()

    # ── Public API ────────────────────────────────────────────────────────────

    def load_fight(self, fight: Fight, hide_companions: bool = False, hide_npcs: bool = False):
        del hide_companions, hide_npcs  # raw log always shows exact file contents
        self._fight = fight
        self._raw_lines = []

        if fight._log_path and fight._line_end >= fight._line_start:
            cache = getattr(fight, "_raw_log_lines_cache", None)
            if cache is None:
                cache = load_raw_lines(fight._log_path, fight._line_start, fight._line_end)
                fight._raw_log_lines_cache = cache
            self._raw_lines = list(cache)
            self.viewer.setPlainText("\n".join(self._raw_lines))
            self.info_label.setText(
                f"{len(self._raw_lines):,} raw lines from this fight. "
                "You can save this slice as a standalone snapshot."
            )
            self.save_btn.setEnabled(bool(self._raw_lines))
            self.validate_btn.setEnabled(bool(self._raw_lines))
            self.hunt_btn.setEnabled(bool(self._raw_lines))
            self.viewer.moveCursor(QTextCursor.MoveOperation.Start)
            self._reset_search_state()
            return

        self.viewer.setPlainText(
            "Raw log is unavailable for this fight.\n\n"
            "This usually means the fight came from a live/in-memory session rather than "
            "a file-backed combat log."
        )
        self.info_label.setText("Raw fight log unavailable for this selection.")
        self.save_btn.setEnabled(False)
        self.validate_btn.setEnabled(False)
        self.hunt_btn.setEnabled(False)
        self._reset_search_state()

    # ── Save snapshot ─────────────────────────────────────────────────────────

    def _save_snapshot(self):
        if not self._fight or not self._raw_lines:
            return
        suggested_base = self._suggested_filename()
        last_save_dir = settings.get("last_save_dir", "")
        if not last_save_dir or not Path(last_save_dir).is_dir():
            last_save_dir = str(Path.home() / "Documents")
        dest, _ = QFileDialog.getSaveFileName(
            self, "Save Fight Snapshot",
            str(Path(last_save_dir) / f"{suggested_base}.txt"),
            "Text Files (*.txt);;All Files (*)",
        )
        if not dest:
            return
        try:
            Path(dest).write_text("\n".join(self._raw_lines) + "\n", encoding="utf-8")
            settings.set("last_save_dir", str(Path(dest).parent))
            self._status(f"Saved fight snapshot: {dest}")
        except Exception as e:
            self._status(f"Save failed: {e}")

    def _suggested_filename(self) -> str:
        fight = self._fight
        if not fight:
            return "fight_snapshot"
        label = fight.boss_name or f"fight_{fight.index}"
        safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in label.strip())
        safe = safe.strip("_") or "fight_snapshot"
        return f"{safe}_{fight.start_time.strftime('%H%M%S')}"

    # ── Validation ────────────────────────────────────────────────────────────

    def _validate_fight(self):
        if not self._fight or not self._raw_lines:
            return
        fight = self._fight
        try:
            raw_events = [ev for line in self._raw_lines if (ev := parse_line(line)) is not None]
            eager_fights = build_fights(raw_events)
            eager_fight  = eager_fights[0] if eager_fights else None
            results = run_fight_validation(
                fight=fight, eager_fight=eager_fight,
                log_path=fight._log_path or "", global_parse_errors=None,
            )
            report = format_report_text(fight, results)
            self._show_validation_dialog(report, results)
            fails = sum(1 for r in results if r.status == "FAIL")
            warns = sum(1 for r in results if r.status == "WARN")
            self._status(f"Fight validation complete: {fails} failures, {warns} warnings.")
        except Exception as e:
            self._status(f"Fight validation failed: {e}")

    def _show_validation_dialog(self, report: str, results):
        dlg = QDialog(self)
        dlg.setWindowTitle("Validate Fight")
        dlg.resize(980, 720)
        layout = QVBoxLayout(dlg)

        fails  = sum(1 for r in results if r.status == "FAIL")
        warns  = sum(1 for r in results if r.status == "WARN")
        passes = sum(1 for r in results if r.status == "PASS")
        summary = QLabel(
            f"Validation summary: {passes} pass, {warns} warning, {fails} failure. "
            "Compare this against the raw fight log and StarParse for the same encounter."
        )
        summary.setWordWrap(True)
        summary.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        layout.addWidget(summary)

        viewer = QPlainTextEdit()
        viewer.setReadOnly(True)
        viewer.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        viewer.setPlainText(report)
        viewer.setStyleSheet(f"""
            QPlainTextEdit {{
                background: {BG_PANEL};
                border: 1px solid {BORDER};
                border-radius: 6px;
                color: {TEXT_PRI};
                selection-background-color: #1f3d5c;
                font-family: Consolas, "Cascadia Mono", "Courier New", monospace;
                font-size: 12px;
            }}
        """)
        viewer.moveCursor(QTextCursor.MoveOperation.Start)
        layout.addWidget(viewer, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        dlg.exec()

    # ── Great Hunt bridge ─────────────────────────────────────────────────────

    def _open_great_hunt(self):
        if not self._fight:
            return
        window = self.window()
        if hasattr(window, "_open_great_hunt_dialog"):
            window._open_great_hunt_dialog(self._fight)

    # ── Search ────────────────────────────────────────────────────────────────

    def _on_search_text_changed(self, _text: str):
        self.search_status.setText("")
        self._update_search_buttons()

    def _update_search_buttons(self):
        enabled = bool(self.search_input.text().strip()) and bool(self.viewer.toPlainText())
        self.find_prev_btn.setEnabled(enabled)
        self.find_next_btn.setEnabled(enabled)

    def _reset_search_state(self):
        self.search_status.setText("")
        cursor = self.viewer.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        self.viewer.setTextCursor(cursor)
        self._update_search_buttons()

    def _find_next(self):
        self._find_in_viewer(backward=False)

    def _find_previous(self):
        self._find_in_viewer(backward=True)

    def _find_in_viewer(self, backward: bool):
        needle = self.search_input.text().strip()
        if not needle:
            self.search_status.setText("Enter text to search.")
            return
        flags = QTextDocument.FindFlag.FindBackward if backward else QTextDocument.FindFlag(0)
        if self.viewer.find(needle, flags):
            self.search_status.setText("")
            return
        cursor = self.viewer.textCursor()
        cursor.movePosition(
            QTextCursor.MoveOperation.End if backward else QTextCursor.MoveOperation.Start
        )
        self.viewer.setTextCursor(cursor)
        if self.viewer.find(needle, flags):
            self.search_status.setText("Wrapped search.")
        else:
            self.search_status.setText("No matches found.")

    # ── Status bar helper ─────────────────────────────────────────────────────

    def _status(self, msg: str):
        window = self.window()
        if hasattr(window, "status"):
            window.status.showMessage(msg)
