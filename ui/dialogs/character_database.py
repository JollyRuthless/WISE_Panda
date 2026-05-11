"""
ui/dialogs/character_database.py — imported-log character profiles and abilities.
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QDialogButtonBox,
    QLabel, QTableWidget, QTableWidgetItem, QHeaderView, QPushButton,
)
from PyQt6.QtCore import Qt

from storage.encounter_db import list_imported_characters, list_imported_character_abilities
from ui.theme import TEXT_SEC


class ImportedCharacterListDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Database Characters")
        self.resize(980, 560)
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self._rows = []
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)
        title = QLabel("Database Characters")
        title.setObjectName("title")
        root.addWidget(title)

        subtitle = QLabel(
            "Your character profiles built from imported combat logs. "
            "Reads the cached profile tables so the app does not have to rebuild this summary every time."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        root.addWidget(subtitle)

        self.summary = QLabel("")
        self.summary.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        root.addWidget(self.summary)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels([
            "Character",
            "Latest Class",
            "Classes Seen",
            "Imports",
            "Abilities Seen",
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
        self.view_abilities_btn = QPushButton("View Seen Abilities")
        self.view_abilities_btn.clicked.connect(self._view_selected_abilities)
        action_row.addWidget(self.view_abilities_btn)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        action_row.addWidget(self.refresh_btn)
        action_row.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        action_row.addWidget(buttons)
        root.addLayout(action_row)

    def refresh(self):
        rows = list_imported_characters()
        self._rows = rows
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))

        def cell(text: str, align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item.setTextAlignment(align)
            return item

        center = Qt.AlignmentFlag.AlignCenter
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        for row_idx, row in enumerate(rows):
            self.table.setItem(row_idx, 0, cell(row.character_name))
            self.table.setItem(row_idx, 1, cell(row.latest_class_name, center))
            self.table.setItem(row_idx, 2, cell(", ".join(row.classes_seen)))
            self.table.setItem(row_idx, 3, cell(str(row.import_count), right))
            self.table.setItem(row_idx, 4, cell(str(row.ability_count), right))

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSortingEnabled(True)
        self.table.sortItems(0, Qt.SortOrder.AscendingOrder)
        self.summary.setText(f"{len(rows):,} character(s) found in imported logs.")
        self.view_abilities_btn.setEnabled(bool(rows))
        if rows and self.table.currentRow() < 0:
            self.table.selectRow(0)

    def _selected_character_name(self) -> str:
        row = self.table.currentRow()
        if row < 0:
            return ""
        item = self.table.item(row, 0)
        return item.text().strip() if item is not None else ""

    def _view_selected_abilities(self):
        character_name = self._selected_character_name()
        if not character_name:
            return
        window = self.parent()
        if window is not None and hasattr(window, "_view_imported_character_abilities"):
            window._view_imported_character_abilities(character_name)

    def closeEvent(self, event):
        window = self.parent()
        if window is not None and hasattr(window, "_database_character_list_dialog"):
            window._database_character_list_dialog = None
        super().closeEvent(event)


class ImportedCharacterAbilitiesDialog(QDialog):
    def __init__(self, parent, character_name: str):
        super().__init__(parent)
        self._character_name = character_name
        self.setWindowTitle(f"Seen Abilities - {character_name}")
        self.resize(760, 560)
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)
        title = QLabel("Seen Character Abilities")
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
        self.table.setHorizontalHeaderLabels(["Ability Name", "Ability ID", "Times Seen"])
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
        rows = list_imported_character_abilities(self._character_name)
        self.subtitle.setText(
            "All abilities this character has been seen using across imported logs, read from the cached ability table."
        )
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))

        def cell(text: str, align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item.setTextAlignment(align)
            return item

        center = Qt.AlignmentFlag.AlignCenter
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        for row_idx, row in enumerate(rows):
            self.table.setItem(row_idx, 0, cell(row.ability_name))
            self.table.setItem(row_idx, 1, cell(row.ability_id, center))
            self.table.setItem(row_idx, 2, cell(str(row.use_count), right))

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSortingEnabled(True)
        self.table.sortItems(2, Qt.SortOrder.DescendingOrder)
        total_seen = sum(row.use_count for row in rows)
        self.summary.setText(f"{len(rows):,} distinct abilities · {total_seen:,} total observed uses")

    def closeEvent(self, event):
        window = self.parent()
        if (window is not None
                and hasattr(window, "_database_character_abilities_dialogs")
                and isinstance(window._database_character_abilities_dialogs, dict)):
            window._database_character_abilities_dialogs.pop(self._character_name, None)
        super().closeEvent(event)
