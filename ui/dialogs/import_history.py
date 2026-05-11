"""
ui/dialogs/import_history.py — ImportHistoryDialog: review imported combat logs.
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QDialogButtonBox,
    QLabel, QTableWidget, QTableWidgetItem, QHeaderView, QPushButton, QFileDialog,
)
from PyQt6.QtCore import Qt

from storage.encounter_db import export_combat_log_events_csv, list_combat_log_imports
from ui.dialogs.import_events import ImportEventsDialog
from ui.theme import TEXT_SEC


class ImportHistoryDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Import History")
        self.resize(1080, 560)
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self._rows = []
        self._event_dialogs: dict[int, ImportEventsDialog] = {}
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)
        title = QLabel("Import History")
        title.setObjectName("title")
        root.addWidget(title)

        subtitle = QLabel(
            "Imported combat logs tracked by the database. "
            "Each row shows the source file and how many lines were captured."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        root.addWidget(subtitle)

        self.summary = QLabel("")
        self.summary.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        root.addWidget(self.summary)

        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels([
            "File",
            "Character",
            "Class",
            "Rows",
            "Parsed",
            "Errors",
            "Path",
        ])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        root.addWidget(self.table, 1)

        action_row = QHBoxLayout()
        self.view_btn = QPushButton("View Selected")
        self.view_btn.clicked.connect(self._view_selected)
        action_row.addWidget(self.view_btn)
        self.export_btn = QPushButton("Export CSV")
        self.export_btn.clicked.connect(self._export_selected_csv)
        action_row.addWidget(self.export_btn)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        action_row.addWidget(self.refresh_btn)
        action_row.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        action_row.addWidget(buttons)
        root.addLayout(action_row)

    def refresh(self):
        rows = list_combat_log_imports()
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
            file_item = cell(row.file_name)
            file_item.setData(Qt.ItemDataRole.UserRole, row.import_id)
            self.table.setItem(row_idx, 0, file_item)
            self.table.setItem(row_idx, 1, cell(row.source_character_name))
            self.table.setItem(row_idx, 2, cell(row.source_class_name, center))
            self.table.setItem(row_idx, 3, cell(f"{row.line_count:,}", right))
            self.table.setItem(row_idx, 4, cell(f"{row.parsed_line_count:,}", right))
            self.table.setItem(row_idx, 5, cell(f"{row.parse_error_count:,}", right))
            self.table.setItem(row_idx, 6, cell(row.log_path))

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        total_rows = sum(row.line_count for row in rows)
        total_errors = sum(row.parse_error_count for row in rows)
        self.summary.setText(
            f"{len(rows):,} imported log(s) · {total_rows:,} captured rows · {total_errors:,} parse errors"
        )
        self.table.setSortingEnabled(True)
        self.table.sortItems(0, Qt.SortOrder.AscendingOrder)
        self.view_btn.setEnabled(bool(rows))
        self.export_btn.setEnabled(bool(rows))
        if rows and self.table.currentRow() < 0:
            self.table.selectRow(0)

    def _selected_import(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        if item is None:
            return None
        import_id = int(item.data(Qt.ItemDataRole.UserRole) or 0)
        for summary in self._rows:
            if summary.import_id == import_id:
                return summary
        return None

    def _view_selected(self):
        selected = self._selected_import()
        if selected is None:
            return
        dialog = self._event_dialogs.get(selected.import_id)
        if dialog is None:
            dialog = ImportEventsDialog(self, selected)
            self._event_dialogs[selected.import_id] = dialog
        else:
            dialog.refresh()
        dialog.show()
        dialog.raise_()

    def _export_selected_csv(self):
        selected = self._selected_import()
        if selected is None:
            return
        suggested = f"{selected.file_name}.csv"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Imported Event Rows",
            suggested,
            "CSV Files (*.csv);;All Files (*)",
        )
        if not path:
            return
        row_count = export_combat_log_events_csv(selected.import_id, path)
        self.summary.setText(
            f"{len(self._rows):,} imported log(s) · exported {row_count:,} row(s) to {path}"
        )

    def closeEvent(self, event):
        window = self.parent()
        if window is not None and hasattr(window, "_import_history_dialog"):
            window._import_history_dialog = None
        super().closeEvent(event)
