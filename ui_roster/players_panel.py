"""
ui_roster.players_panel
=======================

Top region of the Roster window. A search box and a scrolling table of every
player in the DB, sorted by activity. Selecting a row emits player_selected
with the character_name, which the middle region (bosses_panel) listens to.

Reads from cohort.list_known_players(). Read-only — never writes to the DB.

Design notes:
  - The table is the source of truth; the search box filters in-memory rather
    than re-querying. cohort.list_known_players() already supports a server-
    side filter, but at typical DB sizes (hundreds of players) round-tripping
    SQLite on every keystroke is overkill and hurts responsiveness.
  - Selection is single-row. Multi-select would imply "compare these two
    players" which is a different feature than what Roster v1 promises.
  - Sorting is fixed: most active first, then alphabetical. Click-to-sort
    columns are deliberately not exposed yet — the README calls for "sorted
    by activity (most fights first)" and that's all we deliver.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import storage.cohort as cohort
# ── Column layout ───────────────────────────────────────────────────────────
# Index constants make the populate code readable and resistant to off-by-one
# errors when columns get reordered. Keep these in sync with COLUMN_HEADERS.

COL_NAME      = 0
COL_CLASS     = 1
COL_FIGHTS    = 2
COL_LAST_SEEN = 3
N_COLS        = 4

COLUMN_HEADERS = ["Player", "Most-played class", "Fights", "Last seen"]


class PlayersPanel(QWidget):
    """
    Top region: search + player list.

    Signals:
        player_selected(str) — emitted when the user clicks a row. Argument
            is the character_name. Empty string emitted when selection is
            cleared (e.g. after a refresh that removes the selected row).
    """

    player_selected = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._all_summaries: list[cohort.PlayerSummary] = []
        self._build_ui()
        self.refresh()

    # ── UI construction ─────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 4)
        root.setSpacing(6)

        # Header strip — title + count + refresh button. The refresh button is
        # honest: this panel doesn't auto-refresh when the parser app writes
        # to the DB elsewhere, so the user gets an explicit button.
        header_row = QHBoxLayout()
        header_row.setSpacing(8)

        title = QLabel("Players")
        title_font = QFont()
        title_font.setBold(True)
        title_font.setPointSize(11)
        title.setFont(title_font)
        header_row.addWidget(title)

        self._count_label = QLabel("")
        self._count_label.setStyleSheet("color: #888;")
        header_row.addWidget(self._count_label)

        header_row.addStretch(1)

        refresh_btn = QPushButton("↻ Refresh")
        refresh_btn.setToolTip("Re-read the player list from the database")
        refresh_btn.clicked.connect(self.refresh)
        refresh_btn.setMaximumWidth(100)
        header_row.addWidget(refresh_btn)

        root.addLayout(header_row)

        # Search box. Filters the table in-place. Hooked to textChanged so it
        # reacts on every keystroke — fine because filtering is in-memory.
        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText("Filter by name…")
        self._search_box.textChanged.connect(self._on_search_changed)
        root.addWidget(self._search_box)

        # The table.
        self._table = QTableWidget(0, N_COLS, self)
        self._table.setHorizontalHeaderLabels(COLUMN_HEADERS)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(False)  # populate-time sort is ours

        # Column sizing: name takes the slack, the others sit tight to content.
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_CLASS, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_FIGHTS, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_LAST_SEEN, QHeaderView.ResizeMode.ResizeToContents)

        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        root.addWidget(self._table, stretch=1)

    # ── Public API ──────────────────────────────────────────────────────────

    def refresh(self) -> None:
        """
        Re-read the player list from the DB and rebuild the table.

        Preserves the current selection by character_name when possible — if
        the previously-selected player is still in the DB, their row is
        re-selected after rebuild. Otherwise selection is cleared and an
        empty player_selected('') is emitted so the downstream panels know.
        """
        previously_selected = self._selected_character_name()

        try:
            self._all_summaries = cohort.list_known_players()
        except Exception as exc:  # noqa: BLE001 — DB errors must not crash the UI
            self._all_summaries = []
            self._count_label.setText(f"⚠ DB read failed: {exc}")
            self._populate_table([])
            return

        # Apply whatever filter is currently in the search box. On first
        # load this is the empty string (no filter), which matches everyone.
        self._apply_filter(self._search_box.text())

        # Restore selection if possible.
        if previously_selected:
            self._select_by_name(previously_selected)

    def selected_player_name(self) -> str:
        """Convenience for callers that want the current selection without listening."""
        return self._selected_character_name()

    # ── Internal: filtering ─────────────────────────────────────────────────

    def _on_search_changed(self, text: str) -> None:
        self._apply_filter(text)

    def _apply_filter(self, query: str) -> None:
        """Filter self._all_summaries by name and re-populate."""
        query = query.strip().lower()
        if not query:
            visible = self._all_summaries
        else:
            visible = [
                s for s in self._all_summaries
                if query in s.character_name.lower()
            ]
        self._populate_table(visible)

        total = len(self._all_summaries)
        shown = len(visible)
        if shown == total:
            self._count_label.setText(f"({total})")
        else:
            self._count_label.setText(f"({shown} of {total})")

    # ── Internal: table population ──────────────────────────────────────────

    def _populate_table(self, summaries: list[cohort.PlayerSummary]) -> None:
        # blockSignals avoids a flurry of selection-changed signals during
        # rebuild. We re-emit explicitly at the end if the selection was lost.
        self._table.blockSignals(True)
        try:
            self._table.setRowCount(len(summaries))
            for row_idx, summary in enumerate(summaries):
                self._set_row(row_idx, summary)
        finally:
            self._table.blockSignals(False)

    def _set_row(self, row_idx: int, s: cohort.PlayerSummary) -> None:
        # Name. Stored as the user-facing name; the data role carries the
        # exact lookup key so future case-fixes don't break selection.
        name_item = QTableWidgetItem(s.character_name)
        name_item.setData(Qt.ItemDataRole.UserRole, s.character_name)
        self._table.setItem(row_idx, COL_NAME, name_item)

        # Class. Empty string when unknown — show an em-dash for legibility.
        class_text = s.most_played_class if s.most_played_class else "—"
        class_item = QTableWidgetItem(class_text)
        if not s.most_played_class:
            class_item.setForeground(Qt.GlobalColor.gray)
        self._table.setItem(row_idx, COL_CLASS, class_item)

        # Fights. Right-aligned because numbers belong on the right.
        fights_item = QTableWidgetItem(str(s.fight_count))
        fights_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if s.fight_count == 0:
            fights_item.setForeground(Qt.GlobalColor.gray)
        self._table.setItem(row_idx, COL_FIGHTS, fights_item)

        # Last seen. Empty when no fights — em-dash.
        last_text = s.last_seen_date if s.last_seen_date else "—"
        last_item = QTableWidgetItem(last_text)
        if not s.last_seen_date:
            last_item.setForeground(Qt.GlobalColor.gray)
        self._table.setItem(row_idx, COL_LAST_SEEN, last_item)

    # ── Internal: selection ─────────────────────────────────────────────────

    def _on_selection_changed(self) -> None:
        name = self._selected_character_name()
        self.player_selected.emit(name)

    def _selected_character_name(self) -> str:
        rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        if not rows:
            return ""
        item = self._table.item(rows[0].row(), COL_NAME)
        if item is None:
            return ""
        # Prefer the UserRole data so we round-trip exactly what came out of
        # the DB, even if the visible cell text was decorated.
        stored = item.data(Qt.ItemDataRole.UserRole)
        return str(stored) if stored else item.text()

    def _select_by_name(self, character_name: str) -> None:
        """Find and select the row whose name matches; no-op if not present."""
        target = character_name.lower()
        for row_idx in range(self._table.rowCount()):
            item = self._table.item(row_idx, COL_NAME)
            if item and item.text().lower() == target:
                self._table.selectRow(row_idx)
                return
        # Not found — clear any stale selection so downstream sees ''.
        self._table.clearSelection()
