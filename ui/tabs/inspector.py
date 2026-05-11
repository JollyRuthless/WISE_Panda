"""
ui/tabs/inspector.py — DB Inspector tab.

The missing read layer of the application. Lets the user browse what's
actually stored in encounter_history.sqlite3 — encounters and the players
recorded in each one.

This tab is also the seed of the future Find-a-Fight tab. Same query
patterns, just with more filter controls added on top later.

Mostly read-only. The "Rebuild" button is the one exception: it re-runs
fight aggregation against the log files we've already imported, so existing
encounters get refreshed when the structured-data schema evolves (e.g.
when we added Phase E ability count columns). It does NOT re-import raw
events — those are already in the DB.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import storage.cohort as cohort
from ui.theme import ACCENT, ACCENT4, BG_PANEL, BORDER, TEXT_PRI, TEXT_SEC


# Maximum encounters to show in the list at once. The Inspector should be
# fast even on databases with thousands of encounters; truncating the list
# avoids any UI hitch. The user can use the filter to narrow further.
MAX_ENCOUNTERS_DISPLAYED = 500


class InspectorTab(QWidget):
    """
    Two-pane inspector:

      Left pane:  list of encounters from the DB (filterable, refreshable).
      Right pane: when an encounter is selected, shows its participants and
                  per-player ability counts.
    """

    # Emitted when the user double-clicks an encounter — for future hookups
    # like "open this fight in the analysis tabs." Not wired today.
    encounter_activated = pyqtSignal(str)  # passes encounter_key

    def __init__(self):
        super().__init__()
        self._current_encounter_key: Optional[str] = None
        self._build_ui()
        # Initial population happens lazily — the first time refresh is called
        # by main_window after wiring is done. Avoids hitting the DB before
        # the rest of the app is ready.

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(12)

        # Header
        title = QLabel("Database Inspector")
        title.setObjectName("title")
        root.addWidget(title)

        subtitle = QLabel(
            "Browse what's stored in the encounter database. Read-only."
        )
        subtitle.setObjectName("subtitle")
        root.addWidget(subtitle)

        # Filter row
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)

        filter_label = QLabel("Encounter contains:")
        filter_label.setStyleSheet(f"color: {TEXT_SEC};")
        filter_row.addWidget(filter_label)

        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("e.g. Apex, Dxun, Kanoth...")
        self.filter_input.setMaximumWidth(280)
        # Apply filter on Enter, not on every keystroke. Repeated DB queries
        # while typing would feel laggy on a big database.
        self.filter_input.returnPressed.connect(self.refresh)
        filter_row.addWidget(self.filter_input)

        filter_row.addStretch()

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        filter_row.addWidget(self.refresh_btn)

        # The Rebuild button re-runs fight aggregation against existing imports.
        # Useful after the structured-data schema changes (e.g. new ability
        # count columns get added). Walks combat_log_imports, calls
        # _upsert_fights_from_log on each, refreshes everything in place.
        # Existing per-fight data is replaced, not duplicated.
        self.rebuild_btn = QPushButton("Rebuild Structured Data")
        self.rebuild_btn.setToolTip(
            "Re-run fight aggregation against logs already imported. "
            "Use after a code update changes how fights are recorded."
        )
        self.rebuild_btn.clicked.connect(self._on_rebuild_clicked)
        filter_row.addWidget(self.rebuild_btn)

        root.addLayout(filter_row)

        # Status line — small text under the filter that says how many
        # encounters are showing and whether the result was truncated.
        self.status_label = QLabel("")
        self.status_label.setStyleSheet(f"color: {TEXT_SEC}; font-size: 11px;")
        root.addWidget(self.status_label)

        # Splitter: encounter list (left) + detail panel (right)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ── Left: encounter list ─────────────────────────────────────────────
        self.encounter_table = QTableWidget()
        self.encounter_table.setColumnCount(4)
        self.encounter_table.setHorizontalHeaderLabels(["Date", "Encounter", "Recorder", "Players"])
        # Tweak the column widths so the encounter name gets the most space.
        self.encounter_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.encounter_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.encounter_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.encounter_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.encounter_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.encounter_table.verticalHeader().setVisible(False)
        self.encounter_table.itemSelectionChanged.connect(self._on_encounter_selected)
        self.encounter_table.itemDoubleClicked.connect(self._on_encounter_double_clicked)
        splitter.addWidget(self.encounter_table)

        # ── Right: detail panel ──────────────────────────────────────────────
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        self.detail_header = QLabel("Select an encounter on the left.")
        self.detail_header.setStyleSheet(f"color: {ACCENT}; font-size: 14px; font-weight: 600;")
        right_layout.addWidget(self.detail_header)

        self.detail_meta = QLabel("")
        self.detail_meta.setStyleSheet(f"color: {TEXT_SEC}; font-size: 11px;")
        self.detail_meta.setWordWrap(True)
        right_layout.addWidget(self.detail_meta)

        # Players table
        players_label = QLabel("Players in this encounter")
        players_label.setStyleSheet(f"color: {TEXT_SEC}; font-size: 11px; font-weight: 600;")
        right_layout.addWidget(players_label)

        self.players_table = QTableWidget()
        self.players_table.setColumnCount(7)
        self.players_table.setHorizontalHeaderLabels(
            ["Player", "Class", "Discipline", "Damage", "Healing", "Taunts", "Interrupts"]
        )
        self.players_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.players_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.players_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.players_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.players_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.players_table.verticalHeader().setVisible(False)
        self.players_table.itemSelectionChanged.connect(self._on_player_selected)
        right_layout.addWidget(self.players_table)

        # Abilities table — populated when a player row is selected
        abilities_label = QLabel("Abilities (select a player above)")
        abilities_label.setStyleSheet(f"color: {TEXT_SEC}; font-size: 11px; font-weight: 600;")
        right_layout.addWidget(abilities_label)
        self.abilities_label = abilities_label  # we update its text on selection

        self.abilities_table = QTableWidget()
        self.abilities_table.setColumnCount(4)
        self.abilities_table.setHorizontalHeaderLabels(["Ability", "Pressed", "Prebuff", "Dmg Src"])
        self.abilities_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.abilities_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.abilities_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.abilities_table.verticalHeader().setVisible(False)
        right_layout.addWidget(self.abilities_table)

        splitter.addWidget(right)
        splitter.setSizes([520, 520])  # roughly 50/50 to start
        root.addWidget(splitter, stretch=1)

    # ── Public API ───────────────────────────────────────────────────────────

    def refresh(self) -> None:
        """
        Reload the encounter list from the database, applying any active
        filter. Safe to call repeatedly. Triggered by the Refresh button,
        the filter input's Enter key, and externally when the main window
        knows the DB has just changed.
        """
        text = self.filter_input.text().strip()
        filters = cohort.FightFilters(
            encounter_name_contains=text or None,
            limit=MAX_ENCOUNTERS_DISPLAYED,
        )
        try:
            results = cohort.find_fights(filters)
        except Exception as exc:
            # If the DB doesn't exist yet (very fresh install) or any other
            # transient error, surface it in the status line rather than
            # crashing the tab.
            self.status_label.setText(f"Could not query database: {exc}")
            self.encounter_table.setRowCount(0)
            return

        self._populate_encounter_table(results)

        # Keep selection on the same encounter if it still exists in the
        # filtered list. Otherwise clear the right panel.
        if self._current_encounter_key:
            for row in range(self.encounter_table.rowCount()):
                item = self.encounter_table.item(row, 0)
                if item and item.data(Qt.ItemDataRole.UserRole) == self._current_encounter_key:
                    self.encounter_table.selectRow(row)
                    return
        self._clear_detail_panel()

    # ── Encounter list population ────────────────────────────────────────────

    def _populate_encounter_table(self, results: list[cohort.FightRef]) -> None:
        self.encounter_table.setRowCount(len(results))

        for row, ref in enumerate(results):
            # Date column. The encounter_date is stored as ISO YYYY-MM-DD, so
            # it sorts and displays cleanly without parsing.
            date_item = QTableWidgetItem(ref.encounter_date or "—")
            date_item.setData(Qt.ItemDataRole.UserRole, ref.encounter_key)
            self.encounter_table.setItem(row, 0, date_item)

            # Encounter name
            name_item = QTableWidgetItem(ref.encounter_name)
            self.encounter_table.setItem(row, 1, name_item)

            # Recorder
            recorder_item = QTableWidgetItem(ref.recorded_by or "—")
            self.encounter_table.setItem(row, 2, recorder_item)

            # Player count — this is one query per row, but find_fights only
            # returned a bounded number of rows so worst case is bounded.
            # If this becomes a hotspot we'll batch it; for now correctness
            # over cleverness.
            try:
                participants = cohort.list_participants_in_fight(ref.encounter_key)
                count = len(participants)
            except Exception:
                count = 0
            count_item = QTableWidgetItem(str(count))
            count_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            # Bold the count when it's >1 — makes multi-player fights pop
            # out visually so you can see at a glance which fights have
            # the data you care about.
            if count > 1:
                font = count_item.font()
                font.setBold(True)
                count_item.setFont(font)
                count_item.setForeground(self.palette().color(self.foregroundRole()))
            self.encounter_table.setItem(row, 3, count_item)

        # Status line. If we hit the cap, tell the user there are more.
        if len(results) >= MAX_ENCOUNTERS_DISPLAYED:
            self.status_label.setText(
                f"Showing {len(results)} encounters (capped at {MAX_ENCOUNTERS_DISPLAYED}). "
                f"Use the filter to narrow."
            )
        elif len(results) == 0:
            self.status_label.setText("No encounters match the current filter.")
        else:
            self.status_label.setText(f"Showing {len(results)} encounter(s).")

    # ── Rebuild action ───────────────────────────────────────────────────────

    def _on_rebuild_clicked(self) -> None:
        """
        Confirm with the user, then run rebuild_fights_from_existing_imports
        against every log in combat_log_imports.

        Shows a progress dialog during the rebuild because long logs and
        many imports can take a while. Refreshes the encounter list when
        done so the user can see the updated data immediately.

        Catches any errors and surfaces them in a message box rather than
        letting them crash the app.
        """
        # Confirmation. Rebuild is non-destructive but it does take time and
        # touches every encounter row in the DB. Worth a confirm prompt so
        # the user doesn't kick it off by accident.
        reply = QMessageBox.question(
            self,
            "Rebuild structured data?",
            (
                "This will re-run fight aggregation against every log already "
                "imported. Existing per-fight rows will be replaced with fresh "
                "data using the current code.\n\n"
                "Raw event data is NOT touched — this only rebuilds the "
                "structured tables (encounters, per-player rows, ability counts).\n\n"
                "This can take a while if you have many logs imported."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Lazy import — encounter_db is already loaded but keeping the import
        # here matches the pattern used elsewhere in this file for DB calls.
        from storage.encounter_db import rebuild_fights_from_existing_imports

        # Progress dialog. Set up before the call so the user sees it
        # immediately. We don't know the total log count up front; the
        # callback gets it on the first call.
        progress = QProgressDialog(
            "Preparing to rebuild...", "Cancel", 0, 0, self
        )
        progress.setWindowTitle("Rebuilding structured data")
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.show()
        QApplication.processEvents()

        cancelled = {"value": False}

        def progress_cb(done: int, total: int) -> None:
            if progress.wasCanceled():
                cancelled["value"] = True
                # Note: this doesn't actually halt _upsert_fights_from_log
                # mid-stream (would require deeper plumbing). It just stops
                # showing further updates. Not a major issue — users who hit
                # cancel are probably OK with the rebuild finishing.
                return
            progress.setMaximum(total)
            progress.setValue(done)
            progress.setLabelText(f"Rebuilt {done} of {total} log(s)...")
            QApplication.processEvents()

        try:
            summary = rebuild_fights_from_existing_imports(progress_callback=progress_cb)
        except Exception as exc:
            progress.close()
            QMessageBox.critical(
                self, "Rebuild failed", f"Could not rebuild structured data:\n\n{exc}"
            )
            return

        progress.close()

        # Show a summary dialog with what happened.
        message_lines = [
            f"Logs processed: {summary.logs_processed}",
            f"Logs skipped (file missing): {summary.logs_skipped}",
            f"Fights succeeded: {summary.fights_succeeded}",
            f"Fights failed: {summary.fights_failed}",
        ]
        if cancelled["value"]:
            message_lines.append("")
            message_lines.append("(Cancel was requested but rebuild ran to completion.)")
        QMessageBox.information(
            self, "Rebuild complete", "\n".join(message_lines)
        )

        # Refresh the encounter list so the user sees the updated data.
        self.refresh()

    # ── Selection handlers ───────────────────────────────────────────────────

    def _on_encounter_selected(self) -> None:
        items = self.encounter_table.selectedItems()
        if not items:
            self._clear_detail_panel()
            return
        encounter_key = items[0].data(Qt.ItemDataRole.UserRole)
        if not encounter_key:
            self._clear_detail_panel()
            return
        self._current_encounter_key = encounter_key
        self._populate_detail_panel(encounter_key)

    def _on_encounter_double_clicked(self, _item) -> None:
        if self._current_encounter_key:
            self.encounter_activated.emit(self._current_encounter_key)

    def _on_player_selected(self) -> None:
        if not self._current_encounter_key:
            self.abilities_table.setRowCount(0)
            return
        items = self.players_table.selectedItems()
        if not items:
            self.abilities_table.setRowCount(0)
            self.abilities_label.setText("Abilities (select a player above)")
            return
        player_name = self.players_table.item(items[0].row(), 0).text()
        self._populate_abilities(self._current_encounter_key, player_name)

    # ── Detail panel population ──────────────────────────────────────────────

    def _populate_detail_panel(self, encounter_key: str) -> None:
        # Pull the encounter row out of the table to get the display info.
        row = self.encounter_table.currentRow()
        date = self.encounter_table.item(row, 0).text() if row >= 0 else ""
        name = self.encounter_table.item(row, 1).text() if row >= 0 else ""
        recorder = self.encounter_table.item(row, 2).text() if row >= 0 else ""

        self.detail_header.setText(name or "Encounter")
        self.detail_meta.setText(f"Date: {date} · Recorded by: {recorder}")

        # Participants
        try:
            participants = cohort.list_participants_in_fight(encounter_key)
        except Exception as exc:
            participants = []
            self.detail_meta.setText(f"{self.detail_meta.text()} · Error loading participants: {exc}")

        self.players_table.setRowCount(len(participants))
        for row_idx, p in enumerate(participants):
            self.players_table.setItem(row_idx, 0, QTableWidgetItem(p.character_name))

            class_item = QTableWidgetItem(p.class_name or "—")
            self.players_table.setItem(row_idx, 1, class_item)

            # Phase C: Discipline column. The evidence string lives on the
            # tooltip — keeps the cell uncluttered for the common case
            # while making "why does this say Lethality?" answerable with
            # a hover.
            #
            # Phase C+: cross-fight inference. When a discipline was
            # filled in by inference (rather than declared in this fight
            # or voted from this fight's abilities), we color it purple so
            # it's visually distinct. The evidence string starts with
            # "inferred:" in that case.
            discipline_text = p.discipline_name or "—"
            discipline_item = QTableWidgetItem(discipline_text)
            if p.class_evidence:
                discipline_item.setToolTip(p.class_evidence)
                if p.class_evidence.startswith("inferred:"):
                    from PyQt6.QtGui import QColor
                    discipline_item.setForeground(QColor(ACCENT4))
            self.players_table.setItem(row_idx, 2, discipline_item)

            dmg_item = QTableWidgetItem(f"{p.damage_done:,}")
            dmg_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.players_table.setItem(row_idx, 3, dmg_item)

            heal_item = QTableWidgetItem(f"{p.healing_done:,}")
            heal_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.players_table.setItem(row_idx, 4, heal_item)

            taunt_item = QTableWidgetItem(str(p.taunts))
            taunt_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.players_table.setItem(row_idx, 5, taunt_item)

            int_item = QTableWidgetItem(str(p.interrupts))
            int_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.players_table.setItem(row_idx, 6, int_item)

        # Clear ability table; user must select a player to populate it.
        self.abilities_table.setRowCount(0)
        self.abilities_label.setText("Abilities (select a player above)")

    def _populate_abilities(self, encounter_key: str, player_name: str) -> None:
        """
        Pull this player's ability counts for this encounter from the DB.

        Each ability row gets three independent counts:
          - pressed (use_count): button presses inside the fight
          - prebuff (prebuff_count): pre-cast in the 15s before the fight
          - dmg_src (damage_source_count): damage events attributed to this ability

        Sort: prefer abilities with high pressed counts (the player's main
        rotation). Then by damage_source for DoT-heavy abilities. Then
        prebuff-only abilities at the bottom.
        """
        from storage.encounter_db import _connect_db  # safe internal helper

        try:
            with _connect_db() as conn:
                rows = conn.execute(
                    "SELECT pcea.ability_name, pcea.use_count, pcea.prebuff_count, pcea.damage_source_count "
                    "FROM player_character_encounter_abilities pcea "
                    "JOIN player_characters pc ON pc.character_id = pcea.character_id "
                    "WHERE pcea.encounter_key = ? AND pc.character_name = ? COLLATE NOCASE "
                    "ORDER BY pcea.use_count DESC, pcea.damage_source_count DESC, pcea.ability_name ASC",
                    (encounter_key, player_name),
                ).fetchall()
        except Exception:
            rows = []

        self.abilities_table.setRowCount(len(rows))
        for row_idx, (ability_name, pressed, prebuff, damage_source) in enumerate(rows):
            self.abilities_table.setItem(row_idx, 0, QTableWidgetItem(str(ability_name)))

            pressed_item = QTableWidgetItem(str(pressed))
            pressed_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.abilities_table.setItem(row_idx, 1, pressed_item)

            prebuff_item = QTableWidgetItem(str(prebuff) if prebuff else "—")
            prebuff_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.abilities_table.setItem(row_idx, 2, prebuff_item)

            dmg_src_item = QTableWidgetItem(str(damage_source) if damage_source else "—")
            dmg_src_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.abilities_table.setItem(row_idx, 3, dmg_src_item)

        if rows:
            self.abilities_label.setText(f"Abilities used by {player_name} ({len(rows)})")
        else:
            self.abilities_label.setText(f"No abilities recorded for {player_name}")

    def _clear_detail_panel(self) -> None:
        self._current_encounter_key = None
        self.detail_header.setText("Select an encounter on the left.")
        self.detail_meta.setText("")
        self.players_table.setRowCount(0)
        self.abilities_table.setRowCount(0)
        self.abilities_label.setText("Abilities (select a player above)")
