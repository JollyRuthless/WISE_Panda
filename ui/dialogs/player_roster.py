"""
ui/dialogs/player_roster.py — all players seen across imported logs.
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QDialogButtonBox,
    QLabel, QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView, QPushButton,
)
from PyQt6.QtCore import Qt

from storage.encounter_db import list_seen_players, update_seen_player_profile
from ui.theme import TEXT_SEC


class NumericTableWidgetItem(QTableWidgetItem):
    def __init__(self, value: int):
        super().__init__(f"{value:,}")
        self.setData(Qt.ItemDataRole.UserRole, int(value))
        self.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def __lt__(self, other):
        if isinstance(other, QTableWidgetItem):
            left = self.data(Qt.ItemDataRole.UserRole)
            right = other.data(Qt.ItemDataRole.UserRole)
            if left is not None and right is not None:
                return int(left) < int(right)
        return super().__lt__(other)


class PlayerRosterDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Seen Players")
        self.resize(1100, 600)
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self._rows = []
        self._filtered_rows = []
        self._updating_table = False
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)
        title = QLabel("Seen Players")
        title.setObjectName("title")
        root.addWidget(title)

        subtitle = QLabel(
            "Every player name seen anywhere in imported logs. "
            "Use this as a roster for friends, guildmates, and regular groups."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        root.addWidget(subtitle)

        self.summary = QLabel("")
        self.summary.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        root.addWidget(self.summary)

        search_row = QHBoxLayout()
        search_label = QLabel("Search")
        search_label.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        search_row.addWidget(search_label)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Type part of a player name...")
        self.search_box.textChanged.connect(self._apply_filter)
        search_row.addWidget(self.search_box, 1)
        root.addLayout(search_row)

        self.table = QTableWidget()
        self.table.setColumnCount(9)
        self.table.setHorizontalHeaderLabels([
            "Player",
            "Mentions",
            "Logs Seen",
            "As Source",
            "As Target",
            "Abilities Seen",
            "Legacy",
            "Guild",
            "Friend",
        ])
        self.table.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked
            | QTableWidget.EditTrigger.EditKeyPressed
            | QTableWidget.EditTrigger.SelectedClicked
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.doubleClicked.connect(self._handle_double_click)
        self.table.itemChanged.connect(self._handle_item_changed)
        root.addWidget(self.table, 1)

        action_row = QHBoxLayout()
        self.view_btn = QPushButton("View Seen Abilities")
        self.view_btn.clicked.connect(self._view_selected_abilities)
        action_row.addWidget(self.view_btn)
        self.note_btn = QPushButton("Player Note")
        self.note_btn.clicked.connect(self._view_selected_note)
        action_row.addWidget(self.note_btn)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self._refresh_requested)
        action_row.addWidget(self.refresh_btn)
        action_row.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        action_row.addWidget(buttons)
        root.addLayout(action_row)

    def refresh(self):
        rows = list_seen_players()
        self._rows = rows
        self._apply_filter()

    def _apply_filter(self):
        query = self.search_box.text().strip().casefold() if hasattr(self, "search_box") else ""
        if query:
            rows = [row for row in self._rows if query in row.player_name.casefold()]
        else:
            rows = list(self._rows)
        self._filtered_rows = rows
        self._updating_table = True
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))

        def cell(
            text: str,
            align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            editable: bool = False,
        ):
            item = QTableWidgetItem(text)
            flags = item.flags()
            if editable:
                item.setFlags(flags | Qt.ItemFlag.ItemIsEditable)
            else:
                item.setFlags(flags & ~Qt.ItemFlag.ItemIsEditable)
            item.setTextAlignment(align)
            return item

        for row_idx, row in enumerate(rows):
            self.table.setItem(row_idx, 0, cell(row.player_name))
            self.table.setItem(row_idx, 1, NumericTableWidgetItem(row.mention_count))
            self.table.setItem(row_idx, 2, NumericTableWidgetItem(row.import_count))
            self.table.setItem(row_idx, 3, NumericTableWidgetItem(row.source_event_count))
            self.table.setItem(row_idx, 4, NumericTableWidgetItem(row.target_event_count))
            self.table.setItem(row_idx, 5, NumericTableWidgetItem(row.ability_count))
            self.table.setItem(row_idx, 6, cell(row.legacy_name, editable=True))
            self.table.setItem(row_idx, 7, cell(row.guild_name, editable=True))
            self.table.setItem(row_idx, 8, cell(row.friend_name, editable=True))

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, 6):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        for col in range(6, 9):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSortingEnabled(True)
        self.table.sortItems(1, Qt.SortOrder.DescendingOrder)
        total_mentions = sum(row.mention_count for row in rows)
        total_all = len(self._rows)
        if query:
            self.summary.setText(
                f"{len(rows):,} matching player(s) · {total_mentions:,} mentions in filtered view · "
                f"{total_all:,} total cached players."
            )
        else:
            self.summary.setText(
                f"{len(rows):,} player(s) seen · {total_mentions:,} total player mentions · "
                "Legacy/Guild/Friend are editable profile notes."
            )
        self.view_btn.setEnabled(bool(rows))
        self.note_btn.setEnabled(bool(rows))
        if rows and self.table.currentRow() < 0:
            self.table.selectRow(0)
        self._updating_table = False

    def _refresh_requested(self):
        window = self.parent()
        if window is not None and hasattr(window, "_refresh_player_roster_dialog"):
            window._refresh_player_roster_dialog()
            return
        self.refresh()

    def _handle_item_changed(self, item: QTableWidgetItem):
        if self._updating_table or item.column() not in {6, 7, 8}:
            return
        row = item.row()
        player_item = self.table.item(row, 0)
        if player_item is None:
            return
        player_name = player_item.text().strip()
        legacy_name = self.table.item(row, 6).text().strip() if self.table.item(row, 6) is not None else ""
        guild_name = self.table.item(row, 7).text().strip() if self.table.item(row, 7) is not None else ""
        friend_name = self.table.item(row, 8).text().strip() if self.table.item(row, 8) is not None else ""
        update_seen_player_profile(player_name, legacy_name, guild_name, friend_name)

    def _handle_double_click(self, index):
        if index.column() in {6, 7, 8}:
            return
        self._view_selected_abilities()

    def _selected_player_name(self) -> str:
        row = self.table.currentRow()
        if row < 0:
            return ""
        item = self.table.item(row, 0)
        return item.text().strip() if item is not None else ""

    def _view_selected_abilities(self):
        player_name = self._selected_player_name()
        if not player_name:
            return
        window = self.parent()
        if window is not None and hasattr(window, "_view_seen_player_abilities"):
            window._view_seen_player_abilities(player_name)

    def _view_selected_note(self):
        player_name = self._selected_player_name()
        if not player_name:
            return
        window = self.parent()
        if window is not None and hasattr(window, "_view_seen_player_note"):
            window._view_seen_player_note(player_name)

    def closeEvent(self, event):
        window = self.parent()
        if window is not None and hasattr(window, "_player_roster_dialog"):
            window._player_roster_dialog = None
        super().closeEvent(event)


class SeenPlayerAbilitiesDialog(QDialog):
    def __init__(self, parent, player_name: str):
        super().__init__(parent)
        self._player_name = player_name
        self.setWindowTitle(f"Seen Player Abilities - {player_name}")
        self.resize(760, 560)
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)
        title = QLabel("Seen Player Abilities")
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
        rows = list_seen_player_abilities(self._player_name)
        self.subtitle.setText(
            "All abilities seen for this player after name cleanup/alias merging."
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
                and hasattr(window, "_player_roster_abilities_dialogs")
                and isinstance(window._player_roster_abilities_dialogs, dict)):
            window._player_roster_abilities_dialogs.pop(self._player_name, None)
        super().closeEvent(event)
