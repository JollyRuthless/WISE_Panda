"""
ui/dialogs/player_notes.py — rich-text note editor for seen players.
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QDialogButtonBox,
    QLabel, QPushButton, QTextEdit,
)
from PyQt6.QtCore import Qt

from storage.encounter_db import get_seen_player_note_html, update_seen_player_note_html
from ui.theme import TEXT_SEC


class SeenPlayerNoteDialog(QDialog):
    def __init__(self, parent, player_name: str):
        super().__init__(parent)
        self._player_name = player_name
        self.setWindowTitle(f"Player Note - {player_name}")
        self.resize(760, 620)
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)

        title = QLabel("Player Note")
        title.setObjectName("title")
        root.addWidget(title)

        subtitle = QLabel(
            "Rich-text notes for this player profile. "
            "Use this for roster history, callouts, raid notes, guild context, or anything else you want to keep."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        root.addWidget(subtitle)

        self.summary = QLabel("")
        self.summary.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        root.addWidget(self.summary)

        self.editor = QTextEdit()
        self.editor.setAcceptRichText(True)
        root.addWidget(self.editor, 1)

        action_row = QHBoxLayout()
        self.save_btn = QPushButton("Save Note")
        self.save_btn.clicked.connect(self.save_note)
        action_row.addWidget(self.save_btn)

        self.reload_btn = QPushButton("Reload")
        self.reload_btn.clicked.connect(self.refresh)
        action_row.addWidget(self.reload_btn)

        action_row.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        action_row.addWidget(buttons)
        root.addLayout(action_row)

    def refresh(self):
        note_html = get_seen_player_note_html(self._player_name)
        if note_html.strip():
            self.editor.setHtml(note_html)
            self.summary.setText("Saved rich-text note loaded from the player profile table.")
        else:
            self.editor.clear()
            self.summary.setText("No saved note yet. This editor stores rich text on the player's cached profile row.")

    def save_note(self):
        update_seen_player_note_html(self._player_name, self.editor.toHtml())
        self.summary.setText("Player note saved.")

    def closeEvent(self, event):
        window = self.parent()
        if (
            window is not None
            and hasattr(window, "_player_roster_note_dialogs")
            and isinstance(window._player_roster_note_dialogs, dict)
        ):
            window._player_roster_note_dialogs.pop(self._player_name, None)
        super().closeEvent(event)
