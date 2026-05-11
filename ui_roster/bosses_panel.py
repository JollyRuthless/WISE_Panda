"""
ui_roster.bosses_panel
======================

Middle region of the Roster window. When a player is selected up top, this
panel shows every boss they've fought, with fight count and last-seen date.

Selecting a boss row drives the bottom region (comparison_panel).

Reads from cohort.find_player_history(). Read-only.

Design notes:
  - We aggregate find_player_history's flat list of FightRefs into per-boss
    rows here in Python. Could be done in SQL, but a player's history is
    bounded (default limit=200 in find_player_history) so the in-memory
    grouping is trivial and keeps the SQL surface small.
  - The selection emits both the boss name and the player's class+discipline
    *for that boss*. Why? Because the comparison panel needs to know "what
    role was this player playing on this boss" to filter the cohort. A player
    might tank one boss and DPS another on the same character, and the panel
    below needs the right answer for the right boss.
  - Class+discipline lookup is done via list_participants_in_fight on the
    most recent matching fight. One extra DB query per selection — negligible.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import storage.cohort as cohort
# ── Column layout ───────────────────────────────────────────────────────────

COL_BOSS_NAME = 0
COL_FIGHTS    = 1
COL_LAST_SEEN = 2
COL_SPEC      = 3   # class / discipline played on this boss (best-known)
N_COLS        = 4

COLUMN_HEADERS = ["Boss", "Fights", "Last seen", "Spec"]


@dataclass(frozen=True)
class _BossRow:
    """Row data for the bosses table. Aggregated from a player's history."""
    boss_name: str
    fight_count: int
    last_seen_date: str
    encounter_keys: tuple[str, ...]   # newest first


class BossesPanel(QWidget):
    """
    Middle region: bosses the selected player has fought.

    Signals:
        boss_selected(str, str, str) — emitted on row selection. Args are
            (boss_name, class_name, discipline_name). Class and discipline
            come from the most recent fight on this boss for this player;
            either or both may be empty when the parser couldn't determine
            them. Empty boss_name means selection cleared.
    """

    boss_selected = Signal(str, str, str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._current_player: str = ""
        self._rows: list[_BossRow] = []
        self._build_ui()
        self._render_empty("Select a player above to see their boss history.")

    # ── UI construction ─────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 4, 8, 4)
        root.setSpacing(6)

        header_row = QHBoxLayout()
        header_row.setSpacing(8)

        title = QLabel("Bosses")
        title_font = QFont()
        title_font.setBold(True)
        title_font.setPointSize(11)
        title.setFont(title_font)
        header_row.addWidget(title)

        self._subtitle = QLabel("")
        self._subtitle.setStyleSheet("color: #888;")
        header_row.addWidget(self._subtitle)

        header_row.addStretch(1)
        root.addLayout(header_row)

        # Status / placeholder line. Used when no player is selected, when the
        # player has no fights, or when a DB read fails. Hidden once the
        # table has rows.
        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color: #888; padding: 12px;")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setWordWrap(True)
        root.addWidget(self._status_label)

        # The table.
        self._table = QTableWidget(0, N_COLS, self)
        self._table.setHorizontalHeaderLabels(COLUMN_HEADERS)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(False)

        header = self._table.horizontalHeader()
        header.setSectionResizeMode(COL_BOSS_NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_FIGHTS, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_LAST_SEEN, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_SPEC, QHeaderView.ResizeMode.ResizeToContents)

        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        root.addWidget(self._table, stretch=1)

    # ── Public API ──────────────────────────────────────────────────────────

    def set_player(self, character_name: str) -> None:
        """
        Switch which player's boss history is shown. Empty string clears.

        Called by the players_panel via its player_selected signal. Re-loading
        the same player is a no-op.
        """
        if character_name == self._current_player:
            return
        self._current_player = character_name

        if not character_name:
            self._rows = []
            self._populate_table([])
            self._render_empty("Select a player above to see their boss history.")
            self._subtitle.setText("")
            # Cascade the clear down to the comparison panel.
            self.boss_selected.emit("", "", "")
            return

        try:
            history = cohort.find_player_history(character_name)
        except Exception as exc:  # noqa: BLE001
            self._rows = []
            self._populate_table([])
            self._render_empty(f"⚠ DB read failed: {exc}")
            self._subtitle.setText("")
            self.boss_selected.emit("", "", "")
            return

        self._rows = self._aggregate_history(history)
        self._populate_table(self._rows)

        if not self._rows:
            self._render_empty(
                f"{character_name} has no fights in the DB. They may have "
                "appeared in a log but not had any encounters recorded."
            )
            self._subtitle.setText("")
        else:
            self._status_label.setVisible(False)
            self._table.setVisible(True)
            self._subtitle.setText(
                f"· {character_name}  ·  "
                f"{len(self._rows)} unique boss"
                + ("es" if len(self._rows) != 1 else "")
            )

    # ── Internal: aggregation ───────────────────────────────────────────────

    @staticmethod
    def _aggregate_history(history: list[cohort.FightRef]) -> list[_BossRow]:
        """
        Group a flat list of FightRefs by encounter_name. Within each group,
        ordered newest-first (find_player_history already returns that order,
        but we preserve it explicitly so the contract is local).

        Skips entries with empty encounter_name — those exist in the DB
        when a fight couldn't be auto-named, and grouping them all under
        "" would falsely conflate unrelated fights.
        """
        groups: dict[str, list[cohort.FightRef]] = defaultdict(list)
        for ref in history:
            if not ref.encounter_name:
                continue
            groups[ref.encounter_name].append(ref)

        rows: list[_BossRow] = []
        for boss_name, refs in groups.items():
            # find_player_history returns newest-first, but we re-sort
            # defensively in case the contract changes upstream.
            refs_sorted = sorted(
                refs, key=lambda r: r.encounter_date, reverse=True
            )
            rows.append(_BossRow(
                boss_name=boss_name,
                fight_count=len(refs_sorted),
                last_seen_date=refs_sorted[0].encounter_date,
                encounter_keys=tuple(r.encounter_key for r in refs_sorted),
            ))

        # Sort the groups by fight count desc, then most-recent first, then
        # boss name. Most-active bosses surface to the top.
        rows.sort(key=lambda r: (-r.fight_count, _negate_date(r.last_seen_date), r.boss_name))
        return rows

    # ── Internal: table population ──────────────────────────────────────────

    def _populate_table(self, rows: list[_BossRow]) -> None:
        self._table.blockSignals(True)
        try:
            self._table.setRowCount(len(rows))
            for row_idx, row in enumerate(rows):
                self._set_row(row_idx, row)
        finally:
            self._table.blockSignals(False)

    def _set_row(self, row_idx: int, row: _BossRow) -> None:
        boss_item = QTableWidgetItem(row.boss_name)
        # Store the encounter_keys tuple so the spec lookup can use the most
        # recent one without re-querying find_player_history.
        boss_item.setData(Qt.ItemDataRole.UserRole, row.encounter_keys)
        self._table.setItem(row_idx, COL_BOSS_NAME, boss_item)

        fights_item = QTableWidgetItem(str(row.fight_count))
        fights_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._table.setItem(row_idx, COL_FIGHTS, fights_item)

        last_item = QTableWidgetItem(row.last_seen_date or "—")
        self._table.setItem(row_idx, COL_LAST_SEEN, last_item)

        # Spec column populated lazily on selection — leaving blank here
        # keeps the panel snappy when switching players. The active row's
        # spec is shown when selected (see _on_selection_changed).
        self._table.setItem(row_idx, COL_SPEC, QTableWidgetItem(""))

    def _render_empty(self, message: str) -> None:
        self._status_label.setText(message)
        self._status_label.setVisible(True)
        self._table.setVisible(False)

    # ── Internal: selection ─────────────────────────────────────────────────

    def _on_selection_changed(self) -> None:
        rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        if not rows:
            self.boss_selected.emit("", "", "")
            return

        row_idx = rows[0].row()
        boss_item = self._table.item(row_idx, COL_BOSS_NAME)
        if boss_item is None:
            self.boss_selected.emit("", "", "")
            return

        boss_name = boss_item.text()
        encounter_keys = boss_item.data(Qt.ItemDataRole.UserRole) or ()

        # Determine which spec the player was on this boss. We look at the
        # most recent fight on this boss and find the player's row in its
        # participant list. Multiple specs across history are summarised by
        # "most recent" — Roster v1 doesn't expose history-of-specs.
        class_name, discipline_name = self._lookup_spec_for_boss(
            self._current_player, encounter_keys
        )

        # Update the spec cell in-place so the user can see what we resolved.
        spec_text = _format_spec(class_name, discipline_name)
        spec_cell = self._table.item(row_idx, COL_SPEC)
        if spec_cell is None:
            spec_cell = QTableWidgetItem(spec_text)
            self._table.setItem(row_idx, COL_SPEC, spec_cell)
        else:
            spec_cell.setText(spec_text)
        if not class_name and not discipline_name:
            spec_cell.setForeground(Qt.GlobalColor.gray)

        self.boss_selected.emit(boss_name, class_name, discipline_name)

    @staticmethod
    def _lookup_spec_for_boss(
        player_name: str, encounter_keys: tuple[str, ...]
    ) -> tuple[str, str]:
        """
        Find the class+discipline this player used on this boss most recently.

        Walks the encounter_keys (newest first) and returns the first
        non-empty (class, discipline) pair found. Returns ("", "") if no
        fight has both — common for older fights ingested before Phase C.
        """
        if not player_name or not encounter_keys:
            return "", ""

        target = player_name.lower()
        # We walk newest-first and return the first useful answer. This is
        # one DB query per fight worst-case, but in practice the first or
        # second is almost always populated.
        for key in encounter_keys:
            try:
                participants = cohort.list_participants_in_fight(key)
            except Exception:  # noqa: BLE001
                continue
            for p in participants:
                if p.character_name.lower() == target:
                    if p.class_name or p.discipline_name:
                        return p.class_name or "", p.discipline_name or ""
                    break  # found the player but no class data — try older
        return "", ""


# ── Module-level helpers ────────────────────────────────────────────────────


def _negate_date(date_str: str) -> str:
    """
    Sort key that makes lexically-larger dates sort earlier.

    SQLite stores ISO dates which sort correctly as strings, but list.sort()
    uses ascending order. The cleanest "newest-first" tiebreaker without
    splitting into multi-key sorts is to invert via subtract-from-fixed —
    use 'Z' padding to push empty/short strings (no date) to the end.
    """
    if not date_str:
        return ""  # Empty dates sort last among same-fight-count groups.
    # We want descending date order in the same sort that uses ascending fight
    # count. Easy trick: invert each character. ord-based inversion works
    # within the printable ASCII range that ISO dates use.
    return "".join(chr(255 - ord(c)) for c in date_str)


def _format_spec(class_name: str, discipline_name: str) -> str:
    """Compact display: 'Juggernaut · Vengeance', or 'Juggernaut', or '—'."""
    if class_name and discipline_name:
        return f"{class_name} · {discipline_name}"
    if class_name:
        return class_name
    if discipline_name:
        return discipline_name
    return "—"
