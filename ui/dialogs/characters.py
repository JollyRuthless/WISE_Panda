"""
ui/dialogs/characters.py — CharacterListDialog + CharacterAbilitiesDialog.
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QDialogButtonBox,
    QLabel, QTableWidget, QTableWidgetItem, QHeaderView, QPushButton,
)
from PyQt6.QtCore import Qt

from storage.encounter_db import list_player_characters, list_character_abilities_with_import_fallback
from ui.theme import TEXT_SEC


class CharacterListDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Characters")
        self.resize(980, 520)
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self._rows = []
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)
        title = QLabel("Characters")
        title.setObjectName("title")
        root.addWidget(title)

        subtitle = QLabel("Characters discovered from log headers and updated by processed encounters.")
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        root.addWidget(subtitle)

        self.summary = QLabel("")
        self.summary.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        root.addWidget(self.summary)

        self.table = QTableWidget()
        self.table.setColumnCount(9)
        self.table.setHorizontalHeaderLabels([
            "ID", "Name", "Class", "First Seen", "Last Seen",
            "Damage", "Healing", "Taunts", "Interrupts",
        ])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.doubleClicked.connect(lambda _: self._view_selected_abilities())
        root.addWidget(self.table, 1)

        action_row = QHBoxLayout()
        self.view_abilities_btn = QPushButton("View Abilities")
        self.view_abilities_btn.clicked.connect(self._view_selected_abilities)
        action_row.addWidget(self.view_abilities_btn)
        self.view_database_btn = QPushButton("Database Profiles")
        self.view_database_btn.clicked.connect(self._view_database_profiles)
        action_row.addWidget(self.view_database_btn)
        self.view_roster_btn = QPushButton("Seen Players")
        self.view_roster_btn.clicked.connect(self._view_player_roster)
        action_row.addWidget(self.view_roster_btn)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        action_row.addWidget(self.refresh_btn)
        action_row.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        action_row.addWidget(buttons)
        root.addLayout(action_row)

    def refresh(self):
        rows = list_player_characters()
        self._rows = rows
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))

        def cell(text: str, align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item.setTextAlignment(align)
            return item

        center = Qt.AlignmentFlag.AlignCenter
        right  = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        for row_idx, row in enumerate(rows):
            self.table.setItem(row_idx, 0, cell(str(row.character_id), center))
            self.table.setItem(row_idx, 1, cell(row.character_name))
            self.table.setItem(row_idx, 2, cell(row.class_name))
            self.table.setItem(row_idx, 3, cell(row.first_seen_date, center))
            self.table.setItem(row_idx, 4, cell(row.last_seen_date,  center))
            self.table.setItem(row_idx, 5, cell(f"{row.total_damage_done:,}",  right))
            self.table.setItem(row_idx, 6, cell(f"{row.total_healing_done:,}", right))
            self.table.setItem(row_idx, 7, cell(str(row.total_taunts),     center))
            self.table.setItem(row_idx, 8, cell(str(row.total_interrupts), center))

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for col in range(2, 9):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSortingEnabled(True)
        self.table.sortItems(1, Qt.SortOrder.AscendingOrder)
        self.summary.setText(f"{len(rows):,} character(s) found.")
        self.view_abilities_btn.setEnabled(bool(rows))
        if rows and self.table.currentRow() < 0:
            self.table.selectRow(0)

    def _selected_character_name(self) -> str:
        row = self.table.currentRow()
        if row < 0:
            return ""
        item = self.table.item(row, 1)
        return item.text().strip() if item is not None else ""

    def _view_selected_abilities(self):
        character_name = self._selected_character_name()
        if not character_name:
            return
        window = self.parent()
        if window is not None and hasattr(window, "_view_character_abilities"):
            window._view_character_abilities(character_name)

    def _view_database_profiles(self):
        window = self.parent()
        if window is not None and hasattr(window, "_view_database_characters"):
            window._view_database_characters()

    def _view_player_roster(self):
        window = self.parent()
        if window is not None and hasattr(window, "_view_player_roster"):
            window._view_player_roster()

    def closeEvent(self, event):
        window = self.parent()
        if window is not None and hasattr(window, "_character_list_dialog"):
            window._character_list_dialog = None
        super().closeEvent(event)


class CharacterAbilitiesDialog(QDialog):
    def __init__(self, parent, character_name: str):
        super().__init__(parent)
        self._character_name = character_name
        self.setWindowTitle(f"Abilities - {character_name}")
        self.resize(760, 520)
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)
        title = QLabel("Character Abilities")
        title.setObjectName("title")
        root.addWidget(title)

        self.subtitle = QLabel("")
        self.subtitle.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        root.addWidget(self.subtitle)

        self.summary = QLabel("")
        self.summary.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        root.addWidget(self.summary)

        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Ability Name", "Ability ID", "Uses"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        root.addWidget(self.table, 1)

        action_row = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        action_row.addWidget(self.refresh_btn)
        action_row.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        action_row.addWidget(buttons)
        root.addLayout(action_row)

    def refresh(self):
        rows = list_character_abilities_with_import_fallback(self._character_name)
        self.setWindowTitle(f"Abilities - {self._character_name}")
        self.subtitle.setText(
            f"Ability uses for {self._character_name} from processed encounters, "
            "with imported-log fallback when encounter rollups are empty."
        )
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))

        def cell(text: str, align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item.setTextAlignment(align)
            return item

        center = Qt.AlignmentFlag.AlignCenter
        right  = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        for row_idx, row in enumerate(rows):
            self.table.setItem(row_idx, 0, cell(row.ability_name))
            self.table.setItem(row_idx, 1, cell(row.ability_id, center))
            self.table.setItem(row_idx, 2, cell(str(row.total_uses), right))

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSortingEnabled(True)
        self.table.sortItems(2, Qt.SortOrder.DescendingOrder)
        total_uses = sum(row.total_uses for row in rows)
        self.summary.setText(f"{len(rows):,} abilities tracked · {total_uses:,} total uses")

    def closeEvent(self, event):
        window = self.parent()
        if (window is not None
                and hasattr(window, "_character_abilities_dialogs")
                and isinstance(window._character_abilities_dialogs, dict)):
            window._character_abilities_dialogs.pop(self._character_name, None)
        super().closeEvent(event)
