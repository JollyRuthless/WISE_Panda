"""
ui/dialogs/encounter.py — EncounterDataDialog: review unresolved encounters.
"""

from typing import List

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QDialogButtonBox,
    QLabel, QTableWidget, QTableWidgetItem, QHeaderView, QPushButton,
)
from PyQt6.QtCore import Qt

from ui.theme import TEXT_SEC


class EncounterDataDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Encounter Data")
        self.resize(960, 620)
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self._rows: List[dict] = []
        self._build_ui()
        self._refresh_rows()

    def _build_ui(self):
        root = QVBoxLayout(self)
        title = QLabel("Encounter Data")
        title.setObjectName("title")
        root.addWidget(title)

        subtitle = QLabel(
            "Review unresolved encounters one at a time. "
            "Shared fight data is entered once, and mob type is entered per mob."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        root.addWidget(subtitle)

        self.summary = QLabel("")
        self.summary.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        root.addWidget(self.summary)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["Encounter", "Location", "Zone", "Mobs", "Needs"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.doubleClicked.connect(lambda _: self._review_selected())
        root.addWidget(self.table, 1)

        action_row = QHBoxLayout()
        self.review_btn = QPushButton("Review Selected")
        self.review_btn.clicked.connect(self._review_selected)
        action_row.addWidget(self.review_btn)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self._refresh_rows)
        action_row.addWidget(self.refresh_btn)
        action_row.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        action_row.addWidget(buttons)
        root.addLayout(action_row)

    def _refresh_rows(self):
        window = self.parent()
        self._rows = window._encounter_data_rows() if window is not None else []
        self.summary.setText(f"{len(self._rows):,} encounter(s) still need data.")
        self.table.setRowCount(len(self._rows))

        def cell(text: str, align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter):
            item = QTableWidgetItem(text)
            item.setTextAlignment(align)
            return item

        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        for row_idx, row in enumerate(self._rows):
            item = cell(row["label"])
            item.setData(Qt.ItemDataRole.UserRole, row["fight_key"])
            self.table.setItem(row_idx, 0, item)
            self.table.setItem(row_idx, 1, cell(row["location"]))
            self.table.setItem(row_idx, 2, cell(row["zone"]))
            self.table.setItem(row_idx, 3, cell(str(row["mob_count"]), right))
            self.table.setItem(row_idx, 4, cell(row["needs"]))

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.review_btn.setEnabled(bool(self._rows))
        if self._rows and self.table.currentRow() < 0:
            self.table.selectRow(0)

    def _review_selected(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self._rows):
            return
        fight_key = self._rows[row]["fight_key"]
        window = self.parent()
        if window is not None and window._review_encounter_data_fight(fight_key):
            self._refresh_rows()

    def closeEvent(self, event):
        window = self.parent()
        if window is not None and hasattr(window, "_encounter_data_dialog"):
            window._encounter_data_dialog = None
        super().closeEvent(event)
