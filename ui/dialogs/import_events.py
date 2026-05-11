"""
ui/dialogs/import_events.py — view stored event rows for one imported combat log.
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QDialogButtonBox,
    QLabel, QTableWidget, QTableWidgetItem, QHeaderView, QPushButton,
)
from PyQt6.QtCore import Qt

from storage.encounter_db import CombatLogImportSummary, list_combat_log_events
from ui.theme import TEXT_SEC


class ImportEventsDialog(QDialog):
    def __init__(self, parent, import_summary: CombatLogImportSummary):
        super().__init__(parent)
        self._import_summary = import_summary
        self.setWindowTitle(f"Imported Events - {import_summary.file_name}")
        self.resize(1280, 680)
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self._rows: list[dict[str, object]] = []
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)
        title = QLabel("Imported Event Rows")
        title.setObjectName("title")
        root.addWidget(title)

        subtitle = QLabel(
            f"{self._import_summary.file_name} · showing the first stored rows for quick inspection."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        root.addWidget(subtitle)

        self.summary = QLabel("")
        self.summary.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        root.addWidget(self.summary)

        self.table = QTableWidget()
        self.table.setColumnCount(10)
        self.table.setHorizontalHeaderLabels([
            "Line",
            "Status",
            "Time",
            "Source",
            "Target",
            "Ability",
            "Effect",
            "Amount",
            "Result",
            "Raw",
        ])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
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
        rows = list_combat_log_events(self._import_summary.import_id)
        self._rows = rows
        self.table.setRowCount(len(rows))

        def cell(text: str, align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item.setTextAlignment(align)
            return item

        center = Qt.AlignmentFlag.AlignCenter
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        for row_idx, row in enumerate(rows):
            effect_label = " / ".join(part for part in (
                str(row.get("effect_type") or "").strip(),
                str(row.get("effect_name") or "").strip(),
            ) if part)
            result_label = " / ".join(part for part in (
                str(row.get("result_type") or "").strip(),
                str(row.get("result_dmg_type") or "").strip(),
            ) if part)
            self.table.setItem(row_idx, 0, cell(str(row.get("line_number") or ""), right))
            self.table.setItem(row_idx, 1, cell(str(row.get("parse_status") or ""), center))
            self.table.setItem(row_idx, 2, cell(str(row.get("timestamp_text") or ""), center))
            self.table.setItem(row_idx, 3, cell(str(row.get("source_name") or "")))
            self.table.setItem(row_idx, 4, cell(str(row.get("target_name") or "")))
            self.table.setItem(row_idx, 5, cell(str(row.get("ability_name") or "")))
            self.table.setItem(row_idx, 6, cell(effect_label))
            amount = row.get("result_amount")
            self.table.setItem(row_idx, 7, cell("" if amount is None else f"{int(amount):,}", right))
            self.table.setItem(row_idx, 8, cell(result_label))
            self.table.setItem(row_idx, 9, cell(str(row.get("raw_line") or "")))

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(8, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(9, QHeaderView.ResizeMode.Stretch)
        self.summary.setText(
            f"Showing {len(rows):,} stored row(s) from import #{self._import_summary.import_id}. "
            "CSV export includes the full stored event table."
        )
