"""
ui/tabs/find_fight.py — Phase F: Find-a-Fight tab.

The original product vision from the design doc: "find me every fight on
this boss where someone played this class/discipline." The Inspector
already lets you browse the DB; this tab makes it searchable.

Layout:
    ┌─────────────────────────────────────────────────────────────┐
    │ Filters: [Boss ▾] [Class ▾] [Discipline ▾] [From] [To] [Go] │
    ├─────────────────────────────────────────────────────────────┤
    │ Results (newest first):                                     │
    │   Date │ Boss │ Duration │ Recorder │ # Players │ Log       │
    │   ...                                                       │
    ├─────────────────────────────────────────────────────────────┤
    │ Preview: participants in the selected fight                 │
    │ [Open this fight] button                                    │
    └─────────────────────────────────────────────────────────────┘

Search is on-click only — the user fills filters, hits Search. We don't
filter-as-you-type because each query is a real DB hit and we don't want
to flicker. No autosearch on combo changes.

Discipline cascade: when the user picks a class, the discipline dropdown
narrows to disciplines actually seen for that class in the DB. Selecting
a discipline without a class is allowed but rare.

Click a result row → preview panel populates with that fight's
participants (same shape the Inspector shows). "Open this fight" button
emits a signal that main_window picks up to load the log + select the
fight in the existing analysis tabs.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import storage.cohort as cohort
from ui.theme import ACCENT, ACCENT4, BORDER, TEXT_PRI, TEXT_SEC


# Default date-range = "all time", encoded as a date in the past that any
# real log will be after. Cleaner than special-casing nullable dates in the
# UI. The user can still drag the "From" date forward to narrow.
_EPOCH_FROM = date(2010, 1, 1)


class FindFightTab(QWidget):
    """
    Phase F: Find-a-Fight. Filter-driven search over the full encounter DB.

    Emits `fight_open_requested(encounter_key)` when the user clicks
    "Open this fight". Wired up by ui/features.py to main_window's
    fight-loading flow.
    """

    fight_open_requested = pyqtSignal(str)  # encounter_key

    def __init__(self):
        super().__init__()
        self._current_results: list[cohort.FightRef] = []
        self._build_ui()
        # Initial population is lazy — first time the user clicks Search
        # OR the first time refresh() is called by main_window's startup.

    # ── UI construction ─────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # ── Filter bar ──────────────────────────────────────────────────────
        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)

        filter_row.addWidget(self._make_label("Boss:"))
        self.boss_combo = QComboBox()
        self.boss_combo.setEditable(True)
        self.boss_combo.setMinimumWidth(180)
        self.boss_combo.setSizePolicy(QSizePolicy.Policy.MinimumExpanding,
                                      QSizePolicy.Policy.Fixed)
        # Editable combo: user can type to filter or paste a fragment.
        # The empty top entry means "any boss".
        filter_row.addWidget(self.boss_combo)

        filter_row.addWidget(self._make_label("Class:"))
        self.class_combo = QComboBox()
        self.class_combo.setMinimumWidth(140)
        # Class change cascades to the discipline list — narrowing it.
        self.class_combo.currentTextChanged.connect(self._on_class_changed)
        filter_row.addWidget(self.class_combo)

        filter_row.addWidget(self._make_label("Discipline:"))
        self.discipline_combo = QComboBox()
        self.discipline_combo.setMinimumWidth(160)
        filter_row.addWidget(self.discipline_combo)

        filter_row.addWidget(self._make_label("From:"))
        self.date_from = QDateEdit()
        self.date_from.setCalendarPopup(True)
        self.date_from.setDate(_EPOCH_FROM)
        self.date_from.setDisplayFormat("yyyy-MM-dd")
        filter_row.addWidget(self.date_from)

        filter_row.addWidget(self._make_label("To:"))
        self.date_to = QDateEdit()
        self.date_to.setCalendarPopup(True)
        # Default "to" = today, so the range is "all logged so far."
        self.date_to.setDate(date.today())
        self.date_to.setDisplayFormat("yyyy-MM-dd")
        filter_row.addWidget(self.date_to)

        filter_row.addStretch(1)

        self.search_btn = QPushButton("🔎  Search")
        self.search_btn.clicked.connect(self._run_search)
        filter_row.addWidget(self.search_btn)

        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self._clear_filters)
        filter_row.addWidget(self.clear_btn)

        root.addLayout(filter_row)

        # ── Status line ─────────────────────────────────────────────────────
        self.status_label = QLabel("Pick filters and hit Search.")
        self.status_label.setStyleSheet(f"color: {TEXT_SEC}; font-size: 11px;")
        root.addWidget(self.status_label)

        # ── Splitter: results list (top) + preview pane (bottom) ───────────
        splitter = QSplitter(Qt.Orientation.Vertical)

        # Results table.
        self.results_table = QTableWidget()
        self.results_table.setColumnCount(6)
        self.results_table.setHorizontalHeaderLabels(
            ["Date", "Boss", "Duration", "Recorder", "Players", "Log file"]
        )
        self.results_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.results_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.results_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.results_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        # The Boss column is the most useful one to be wide.
        self.results_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.results_table.itemSelectionChanged.connect(self._on_row_selected)
        # Double-click is a power-user shortcut for "open this fight" —
        # saves them clicking the button below.
        self.results_table.itemDoubleClicked.connect(self._open_selected_fight)
        splitter.addWidget(self.results_table)

        # Preview pane — participants of the selected fight + open button.
        preview_container = QWidget()
        preview_layout = QVBoxLayout(preview_container)
        preview_layout.setContentsMargins(0, 6, 0, 0)

        header_row = QHBoxLayout()
        self.preview_header = QLabel("Select a fight to preview its participants.")
        self.preview_header.setStyleSheet(
            f"color: {TEXT_SEC}; font-size: 11px; font-weight: 600;"
        )
        header_row.addWidget(self.preview_header)
        header_row.addStretch(1)

        self.open_fight_btn = QPushButton("Open this fight")
        self.open_fight_btn.setEnabled(False)
        self.open_fight_btn.clicked.connect(self._open_selected_fight)
        header_row.addWidget(self.open_fight_btn)

        preview_layout.addLayout(header_row)

        self.preview_table = QTableWidget()
        self.preview_table.setColumnCount(5)
        self.preview_table.setHorizontalHeaderLabels(
            ["Player", "Class", "Discipline", "Damage", "Healing"]
        )
        self.preview_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.preview_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.preview_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        preview_layout.addWidget(self.preview_table)

        splitter.addWidget(preview_container)
        # Top half = results, bottom half = preview. Slightly favor results.
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        root.addWidget(splitter, 1)

    def _make_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(f"color: {TEXT_SEC}; font-size: 11px;")
        return label

    # ── Refresh dropdowns from DB ───────────────────────────────────────────

    def refresh(self) -> None:
        """
        Repopulate the dropdowns from the current DB state. Called when the
        tab first becomes visible OR after the import flow finishes (so
        newly-imported boss names appear).

        We DON'T auto-run a search here — that would be surprising behavior.
        The user always initiates the actual query.
        """
        self._populate_boss_combo()
        self._populate_class_combo()
        # Discipline combo populates as a side-effect of class combo
        # changing during _populate_class_combo (which fires the signal).
        # If class_combo settled on "" (any), populate the discipline combo
        # explicitly with all disciplines.
        if not self.class_combo.currentText():
            self._populate_discipline_combo(class_filter=None)

    def _populate_boss_combo(self) -> None:
        # Preserve current selection if possible — refresh shouldn't make
        # the user re-pick what they had.
        current = self.boss_combo.currentText()
        self.boss_combo.blockSignals(True)
        self.boss_combo.clear()
        self.boss_combo.addItem("")  # "any boss"
        try:
            names = cohort.list_known_encounter_names(limit=500)
        except Exception:
            names = []
        for name in names:
            self.boss_combo.addItem(name)
        # Restore previous selection if still present.
        if current:
            idx = self.boss_combo.findText(current)
            if idx >= 0:
                self.boss_combo.setCurrentIndex(idx)
            else:
                self.boss_combo.setEditText(current)
        self.boss_combo.blockSignals(False)

    def _populate_class_combo(self) -> None:
        current = self.class_combo.currentText()
        self.class_combo.blockSignals(True)
        self.class_combo.clear()
        self.class_combo.addItem("")  # "any class"
        try:
            classes = cohort.list_known_class_names()
        except Exception:
            classes = []
        for c in classes:
            self.class_combo.addItem(c)
        if current:
            idx = self.class_combo.findText(current)
            if idx >= 0:
                self.class_combo.setCurrentIndex(idx)
        self.class_combo.blockSignals(False)
        # Now populate disciplines based on whatever class is current.
        self._populate_discipline_combo(class_filter=self.class_combo.currentText() or None)

    def _populate_discipline_combo(self, class_filter: Optional[str]) -> None:
        current = self.discipline_combo.currentText()
        self.discipline_combo.blockSignals(True)
        self.discipline_combo.clear()
        self.discipline_combo.addItem("")  # "any discipline"
        try:
            disciplines = cohort.list_known_disciplines(class_name=class_filter)
        except Exception:
            disciplines = []
        for d in disciplines:
            self.discipline_combo.addItem(d)
        # Try to preserve the user's previous discipline if it's still in
        # the narrowed list. Otherwise clear it (rather than silently
        # showing a discipline that no longer exists for the new class).
        if current:
            idx = self.discipline_combo.findText(current)
            if idx >= 0:
                self.discipline_combo.setCurrentIndex(idx)
        self.discipline_combo.blockSignals(False)

    # ── Filter handlers ─────────────────────────────────────────────────────

    def _on_class_changed(self, _new_class: str) -> None:
        # Cascade: narrow the discipline dropdown to the selected class.
        # Empty-string class = "any" → show all disciplines.
        self._populate_discipline_combo(class_filter=_new_class or None)

    def _clear_filters(self) -> None:
        self.boss_combo.setCurrentIndex(0)
        self.boss_combo.setEditText("")
        self.class_combo.setCurrentIndex(0)
        # Discipline combo will be repopulated by the class-change cascade.
        self.discipline_combo.setCurrentIndex(0)
        self.date_from.setDate(_EPOCH_FROM)
        self.date_to.setDate(date.today())
        self.results_table.setRowCount(0)
        self.preview_table.setRowCount(0)
        self.preview_header.setText("Select a fight to preview its participants.")
        self.open_fight_btn.setEnabled(False)
        self.status_label.setText("Filters cleared. Hit Search to query.")

    # ── Run the search ──────────────────────────────────────────────────────

    def _run_search(self) -> None:
        boss = self.boss_combo.currentText().strip()
        class_name = self.class_combo.currentText().strip()
        discipline = self.discipline_combo.currentText().strip()
        date_from = self.date_from.date().toString("yyyy-MM-dd")
        date_to = self.date_to.date().toString("yyyy-MM-dd")

        # Build filters. Empty strings → None (any).
        filters = cohort.FightFilters(
            date_from=date_from if date_from else None,
            date_to=date_to if date_to else None,
            encounter_name_contains=boss if boss else None,
            class_name=class_name if class_name else None,
            discipline_name=discipline if discipline else None,
            limit=500,
        )

        try:
            refs = cohort.find_fights(filters)
        except Exception as exc:
            self.status_label.setText(f"Search failed: {exc}")
            self.results_table.setRowCount(0)
            return

        self._current_results = refs
        self._populate_results_table(refs)

        # Status: report what filters fired and how many results.
        descriptors: list[str] = []
        if boss:        descriptors.append(f'boss="{boss}"')
        if class_name:  descriptors.append(f'class={class_name}')
        if discipline:  descriptors.append(f'discipline={discipline}')
        if date_from > _EPOCH_FROM.isoformat():
            descriptors.append(f'from={date_from}')
        if date_to < date.today().isoformat():
            descriptors.append(f'to={date_to}')
        suffix = (" with filters: " + ", ".join(descriptors)) if descriptors else ""
        self.status_label.setText(
            f"Found {len(refs)} fight(s){suffix}."
            + (" Showing newest first." if refs else "")
        )

    def _populate_results_table(self, refs: list[cohort.FightRef]) -> None:
        self.results_table.setRowCount(0)
        self.preview_table.setRowCount(0)
        self.preview_header.setText("Select a fight to preview its participants.")
        self.open_fight_btn.setEnabled(False)

        # We need participant counts per encounter to render the "Players"
        # column. One DB query per row would be fine but for 500 rows it
        # adds up. We bulk-fetch the counts in a single query.
        encounter_keys = [r.encounter_key for r in refs]
        participant_counts = _bulk_participant_counts(encounter_keys)

        self.results_table.setRowCount(len(refs))
        for row_idx, ref in enumerate(refs):
            date_item = QTableWidgetItem(ref.encounter_date or "")
            # Stash the encounter_key on the row's first cell so we can
            # retrieve it on selection.
            date_item.setData(Qt.ItemDataRole.UserRole, ref.encounter_key)
            self.results_table.setItem(row_idx, 0, date_item)

            self.results_table.setItem(row_idx, 1, QTableWidgetItem(ref.encounter_name or "Unknown"))

            duration_text = _format_duration(ref.duration_estimate)
            duration_item = QTableWidgetItem(duration_text)
            duration_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.results_table.setItem(row_idx, 2, duration_item)

            self.results_table.setItem(row_idx, 3, QTableWidgetItem(ref.recorded_by or ""))

            count = participant_counts.get(ref.encounter_key, 0)
            count_item = QTableWidgetItem(str(count))
            count_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.results_table.setItem(row_idx, 4, count_item)

            self.results_table.setItem(row_idx, 5, QTableWidgetItem(ref.log_filename))

    # ── Row selection → preview ─────────────────────────────────────────────

    def _on_row_selected(self) -> None:
        row = self.results_table.currentRow()
        if row < 0 or row >= len(self._current_results):
            self.preview_table.setRowCount(0)
            self.preview_header.setText("Select a fight to preview its participants.")
            self.open_fight_btn.setEnabled(False)
            return

        ref = self._current_results[row]
        self.preview_header.setText(
            f"{ref.encounter_name} · {ref.encounter_date} · {_format_duration(ref.duration_estimate)}"
            f"  ({ref.log_filename})"
        )

        try:
            participants = cohort.list_participants_in_fight(ref.encounter_key)
        except Exception as exc:
            self.preview_table.setRowCount(0)
            self.preview_header.setText(f"Preview failed: {exc}")
            self.open_fight_btn.setEnabled(False)
            return

        self.preview_table.setRowCount(len(participants))
        for prow, p in enumerate(participants):
            self.preview_table.setItem(prow, 0, QTableWidgetItem(p.character_name))
            self.preview_table.setItem(prow, 1, QTableWidgetItem(p.class_name or "—"))

            disc_text = p.discipline_name or "—"
            disc_item = QTableWidgetItem(disc_text)
            if p.class_evidence:
                disc_item.setToolTip(p.class_evidence)
                # Same color treatment as Inspector: purple for inferred
                # disciplines (i.e., not declared in this fight). Keeps
                # the visual language consistent across tabs.
                if p.class_evidence.startswith("inferred:"):
                    disc_item.setForeground(QColor(ACCENT4))
            self.preview_table.setItem(prow, 2, disc_item)

            dmg_item = QTableWidgetItem(f"{p.damage_done:,}")
            dmg_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.preview_table.setItem(prow, 3, dmg_item)

            heal_item = QTableWidgetItem(f"{p.healing_done:,}")
            heal_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.preview_table.setItem(prow, 4, heal_item)

        self.open_fight_btn.setEnabled(True)

    # ── Open the selected fight in the analysis tabs ────────────────────────

    def _open_selected_fight(self, *_args) -> None:
        row = self.results_table.currentRow()
        if row < 0 or row >= len(self._current_results):
            return
        ref = self._current_results[row]
        # Hand off to main_window via signal. Main window's slot will
        # load the log file (if not already loaded) and select the right
        # fight in the existing analysis tabs.
        self.fight_open_requested.emit(ref.encounter_key)


# ─── Helpers ────────────────────────────────────────────────────────────────


def _format_duration(seconds: float) -> str:
    """Format a fight duration as 'M:SS' for the results table."""
    if seconds <= 0:
        return "—"
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def _bulk_participant_counts(encounter_keys: list[str]) -> dict[str, int]:
    """
    Return {encounter_key: participant_count} for the given keys in one DB
    round-trip. Used to populate the Players column in the results table
    without N+1 queries.

    Empty input → empty result, no DB hit.
    """
    if not encounter_keys:
        return {}
    # SQLite has a default parameter limit (around 999). For safety we
    # batch in chunks of 500 — typical results limit is 500 anyway.
    out: dict[str, int] = {}
    CHUNK = 500
    # Lazy-import the DB connection helper to avoid forcing cohort.py
    # imports during module load time.
    from storage.encounter_db import _connect_db
    for i in range(0, len(encounter_keys), CHUNK):
        chunk = encounter_keys[i:i + CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        sql = (
            f"SELECT encounter_key, COUNT(*) "
            f"FROM player_character_encounters "
            f"WHERE encounter_key IN ({placeholders}) "
            f"GROUP BY encounter_key"
        )
        with _connect_db() as conn:
            for key, count in conn.execute(sql, chunk):
                out[key] = int(count or 0)
    return out
