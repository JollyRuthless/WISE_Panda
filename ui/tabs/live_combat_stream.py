"""
ui/tabs/live_combat_stream.py - LiveCombatStreamTab.

A dedicated tab for "what's happening right now". Designed for a second
monitor while the game is on the first. Reads live data from the live
tracker; never reads from post-fight aggregator state.

Build order from the SOMEDAY spec:
  1. Tab scaffold + stat cards
  2. Combatant table below + healing aggregation  <- THIS COMMIT
  3. Visual state machine (red border, purple/gold card borders)
  4. Threat panel (predictive math)
  5. Fight picker (last 5 live fights)
  6. Persistence to the live_fight_snapshots table
  7. Copy-to-chat button

Public API used by MainWindow:
  - update_from_tracker(tracker): called on every live event tick to
    refresh the cards and table. Safe to call with no tracker / no
    active fight; shows empty placeholders.
  - clear(): reset to empty state, used when Live Tracker turns off.
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QGroupBox,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QBrush

from engine.aggregator import EntityKind
from engine.threat_status import threat_row_status
from ui.theme import (
    ACCENT, ACCENT2, ACCENT3, ACCENT4,
    TEXT_PRI, TEXT_SEC, BORDER,
    THREAT_STATUS_GREEN, THREAT_STATUS_YELLOW, THREAT_STATUS_RED,
    KIND_BADGE, KIND_ROW_BG,
)
from ui.widgets import StatCard

if TYPE_CHECKING:
    from ui.live.tracker import LiveFightTracker


EMPTY_VALUE = "-"

# Columns: Name, Class badge, DPS, Total DMG, HPS, %
TABLE_COLUMNS = ("Name", "Class", "DPS", "Total DMG", "HPS", "%")
COL_NAME, COL_KIND, COL_DPS, COL_DMG, COL_HPS, COL_PCT = range(6)

# Threat panel columns (phase 4b). Both perspectives on the same row:
#   - "Your" and "Tank" threat totals on this NPC
#   - DPS perspective: gap (tank - you), seconds-until-overtake
#   - Tank perspective: gap (tank - second), seconds-until-lose
# HP% gives quick context for whether the fight is almost over.
#
# Phase 4c added a leading status column showing a green/yellow/red
# block — the worst signal across both perspectives. See
# _threat_row_status() for the thresholds.
THREAT_COLUMNS = (
    "",  # status column — header is blank, cell shows a colored block
    "NPC", "HP%",
    "You", "Tank",
    "DPS Gap", "DPS Time",
    "Tank Gap", "Tank Time",
)
(THC_STATUS,
 THC_NAME, THC_HP,
 THC_YOU, THC_TANK,
 THC_DPS_GAP, THC_DPS_TIME,
 THC_TANK_GAP, THC_TANK_TIME) = range(9)

# Threshold constants for row-status grading live in
# engine.threat_status. The tab only needs the function and the colors.


def _format_duration(seconds: float) -> str:
    """Render seconds as M:SS or H:MM:SS for very long fights."""
    if seconds <= 0:
        return EMPTY_VALUE
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _format_dps(value: float) -> str:
    """Render a damage-per-second number compactly (1.2k, 14.5k, 1.2M)."""
    if value <= 0:
        return EMPTY_VALUE
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return f"{int(value)}"


def _format_total(value: float) -> str:
    """Render a total damage/healing number."""
    if value <= 0:
        return EMPTY_VALUE
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return f"{int(value)}"


def _format_pct(value: float) -> str:
    """Render a 0..1 fraction as a percentage."""
    if value <= 0:
        return EMPTY_VALUE
    return f"{value * 100:.0f}%"


def _format_threat(value: float) -> str:
    """Render a threat number — same shape as damage totals (k/M shorthand)."""
    return _format_total(value)


def _format_gap(value: float) -> str:
    """Render a gap value with a sign. Positive = safe; zero or negative
    is unusual (already overtaken) but possible during gap-closing moments."""
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:+.2f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:+.1f}k"
    return f"{int(value):+d}"


def _format_time_left(seconds) -> str:
    """Render a seconds-until-overtake value.
       None → 'safe' (no overtake predicted within the cap window).
       Numbers → 1 decimal place for sub-10s, integer otherwise."""
    if seconds is None:
        return "safe"
    if seconds < 10:
        return f"{seconds:.1f}s"
    return f"{int(seconds)}s"


def _format_hp_pct(current: float, maximum: float) -> str:
    """Render HP%. We don't have live HP for NPCs in the snapshot today,
    so this just shows '-' unless we get max_hp and the panel computes
    it later (phase 4c follow-up). Stub for now."""
    if maximum <= 0:
        return EMPTY_VALUE
    return f"{int(current / maximum * 100)}%"


# Color lookup for the threat status returned by engine.threat_status.
_STATUS_COLORS = {
    "green":  THREAT_STATUS_GREEN,
    "yellow": THREAT_STATUS_YELLOW,
    "red":    THREAT_STATUS_RED,
}


class LiveCombatStreamTab(QWidget):
    """Dedicated tab for live-fight state. See module docstring."""

    def __init__(self):
        super().__init__()
        # Filter toggles. Default mirrors Overview: only players + group
        # members visible; NPCs and companions hidden until toggled on.
        self._show_npcs = False
        self._show_companions = False
        self._build_ui()
        self.clear()

    # ----- Build -----
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(8, 8, 8, 8)

        # Stat cards row. Six headline numbers. Boss DPS and Crit Rate from
        # Overview are deliberately omitted - Boss DPS depends on per-NPC
        # context that the live tracker doesn't expose; Crit Rate isn't
        # actionable in real time. Add later if they earn a slot.
        cards_layout = QHBoxLayout()
        cards_layout.setSpacing(10)
        self.card_duration   = StatCard("Duration",   color=ACCENT)
        self.card_dps        = StatCard("DPS",        color=ACCENT2)
        self.card_active_dps = StatCard("Active DPS", color="#7ee787")
        self.card_hps        = StatCard("HPS",        color="#79c0ff")
        self.card_dmg        = StatCard("Total DMG",  color=ACCENT3)
        self.card_heal       = StatCard("Total Heal", color=ACCENT4)
        self._cards = [
            self.card_duration, self.card_dps, self.card_active_dps,
            self.card_hps, self.card_dmg, self.card_heal,
        ]
        for c in self._cards:
            cards_layout.addWidget(c)
        root.addLayout(cards_layout)

        # Filter row: toggle NPCs / Companions on or off. Mirrors Overview's
        # pattern so the user has a consistent control. Other kinds (PLAYER,
        # GROUP_MEMBER, HAZARD) are always visible when present.
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        legend_lbl = QLabel("Legend:")
        legend_lbl.setStyleSheet(f"color: {TEXT_SEC}; font-size: 11px;")
        filter_row.addWidget(legend_lbl)

        self._legend_badges = {}
        for kind, (badge, bg_color, fg_color) in KIND_BADGE.items():
            if kind == EntityKind.NPC:
                btn = QPushButton(badge)
                btn.setCheckable(True)
                btn.setChecked(self._show_npcs)
                btn.clicked.connect(self._toggle_npcs)
                self._legend_badges[kind] = btn
                self._btn_npcs = btn
                filter_row.addWidget(btn)
            elif kind == EntityKind.COMPANION:
                btn = QPushButton(badge)
                btn.setCheckable(True)
                btn.setChecked(self._show_companions)
                btn.clicked.connect(self._toggle_companions)
                self._legend_badges[kind] = btn
                self._btn_companions = btn
                filter_row.addWidget(btn)
            else:
                lbl = QLabel(f"  {badge}  ")
                lbl.setStyleSheet(
                    f"background:{bg_color}; color:{fg_color}; "
                    "font-size:10px; font-weight:700; "
                    "border-radius:3px; padding:2px 6px;"
                )
                self._legend_badges[kind] = lbl
                filter_row.addWidget(lbl)
        filter_row.addStretch()
        root.addLayout(filter_row)

        # Threat panel (phase 4b). Per-NPC ranked list showing both
        # DPS-perspective and tank-perspective gap and time-to-overtake.
        # Lives above the combatant table because threat is more
        # time-critical. See SOMEDAY.md's Threat panel sub-spec for the
        # math and design decisions; this is the UI half of phase 4.
        threat_grp = QGroupBox("Threat (modeled — approximate)")
        threat_lay = QVBoxLayout(threat_grp)
        threat_lay.setSpacing(4)
        threat_lay.setContentsMargins(8, 6, 8, 6)

        self._threat_table = QTableWidget()
        self._threat_table.setColumnCount(len(THREAT_COLUMNS))
        self._threat_table.setHorizontalHeaderLabels(list(THREAT_COLUMNS))
        self._threat_table.verticalHeader().setVisible(False)
        self._threat_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._threat_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._threat_table.setShowGrid(False)
        self._threat_table.setAlternatingRowColors(False)
        # Cap the panel's height so the combatant table below keeps its
        # generous proportion. Six rows of NPCs is plenty for most fights
        # and the table scrolls if more are engaged.
        self._threat_table.setMaximumHeight(180)

        thh = self._threat_table.horizontalHeader()
        thh.setSectionResizeMode(THC_NAME, QHeaderView.ResizeMode.Stretch)
        for col in (THC_STATUS, THC_HP, THC_YOU, THC_TANK,
                    THC_DPS_GAP, THC_DPS_TIME,
                    THC_TANK_GAP, THC_TANK_TIME):
            thh.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        threat_lay.addWidget(self._threat_table)

        # Empty-state label for the threat panel. Shown when no NPCs are
        # currently engaged (early in a fight, or no threat data yet).
        # Sits below the table inside the same group box.
        self._threat_empty_label = QLabel("No NPCs engaged.")
        self._threat_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._threat_empty_label.setStyleSheet(
            f"color: {TEXT_SEC}; font-size: 11px; padding: 6px;"
        )
        threat_lay.addWidget(self._threat_empty_label)

        # Tank-identity line: surfaces the heuristic's pick so the user
        # can see who's being treated as the tank. Phase 4a's
        # documentation flags this as approximate.
        self._threat_tank_label = QLabel("")
        self._threat_tank_label.setStyleSheet(
            f"color: {TEXT_SEC}; font-size: 10px; padding: 2px 4px;"
        )
        threat_lay.addWidget(self._threat_tank_label)

        root.addWidget(threat_grp)

        # Combatant table.
        self._table = QTableWidget()
        self._table.setColumnCount(len(TABLE_COLUMNS))
        self._table.setHorizontalHeaderLabels(list(TABLE_COLUMNS))
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(False)

        hh = self._table.horizontalHeader()
        # Name column stretches; the rest size to content for compactness.
        hh.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        for col in (COL_KIND, COL_DPS, COL_DMG, COL_HPS, COL_PCT):
            hh.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)

        root.addWidget(self._table, 1)

        # Status label below the table. Used to communicate empty/active
        # state and (eventually) which future pieces are still missing.
        self._status_label = QLabel("Live Combat Stream - waiting for a fight...")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setStyleSheet(
            f"color: {TEXT_SEC}; font-size: 12px; padding: 6px;"
        )
        root.addWidget(self._status_label)

    # ----- Filter toggle handlers -----
    def _toggle_npcs(self):
        self._show_npcs = not self._show_npcs
        self._btn_npcs.setChecked(self._show_npcs)
        # Re-emit the last update so the table re-filters immediately. We
        # cache the last snapshot to avoid having to ask the tracker again.
        self._refresh_table()

    def _toggle_companions(self):
        self._show_companions = not self._show_companions
        self._btn_companions.setChecked(self._show_companions)
        self._refresh_table()

    # ----- Public API -----
    def update_from_tracker(self, tracker):
        """Refresh cards and table from the live tracker.

        Safe to call with tracker=None or with a tracker that has no
        active fight - shows the empty state.
        """
        if tracker is None or tracker.fight_start is None:
            self.clear()
            return

        # Visual state: purple while a fight is in progress, gold once it
        # has ended. The transition is driven by tracker.in_combat — when
        # an exit-combat event lands, in_combat flips to False but
        # fight_start is preserved so the totals stay visible.
        state = "in_progress" if tracker.in_combat else "settled"
        self._apply_card_state(state)

        elapsed = tracker.elapsed
        self.card_duration.set_value(_format_duration(elapsed))

        rows = tracker.snapshot(metric="encounter")
        self._last_rows = rows  # cache for filter-toggle redraws

        # Aggregate player rows for the headline cards. Filter by
        # EntityKind directly rather than string-matching - more robust
        # to future enum representation changes.
        player_rows = [r for r in rows if r.get("kind") == EntityKind.PLAYER]
        if player_rows:
            total_damage  = sum(r.get("total_damage", 0)  for r in player_rows)
            encounter_dps = sum(r.get("encounter_dps", 0) for r in player_rows)
            active_dps    = sum(r.get("active_dps", 0)    for r in player_rows)
            total_heal    = sum(r.get("total_heal", 0)    for r in player_rows)
            encounter_hps = sum(r.get("encounter_hps", 0) for r in player_rows)
            self.card_dps.set_value(_format_dps(encounter_dps))
            self.card_active_dps.set_value(_format_dps(active_dps))
            self.card_dmg.set_value(_format_total(total_damage))
            self.card_hps.set_value(_format_dps(encounter_hps))
            self.card_heal.set_value(_format_total(total_heal))
        else:
            for c in (self.card_dps, self.card_active_dps,
                      self.card_dmg, self.card_hps, self.card_heal):
                c.set_value(EMPTY_VALUE)

        self._refresh_table()
        self._refresh_threat_panel(tracker)
        status_word = "in progress" if state == "in_progress" else "settled"
        self._status_label.setText(
            f"Live fight {status_word} - {_format_duration(elapsed)}"
        )

    def clear(self):
        """Reset to empty state (no active fight)."""
        for c in self._cards:
            c.set_value(EMPTY_VALUE)
        self._apply_card_state("neutral")
        self._last_rows = []
        self._table.setRowCount(0)
        # Reset the threat panel too. Empty table, empty tank line,
        # show the "no NPCs engaged" label.
        self._threat_table.setRowCount(0)
        self._threat_tank_label.setText("")
        self._threat_empty_label.setText("No NPCs engaged.")
        self._threat_empty_label.setVisible(True)
        self._threat_table.setVisible(False)
        self._status_label.setText(
            "Live Combat Stream - waiting for a fight..."
        )

    def _apply_card_state(self, state: str):
        """Push the same visual state to every stat card. Idempotent and
        cheap to call on every event tick — the stylesheet only changes
        the border color, Qt only repaints if it actually changed."""
        for c in self._cards:
            c.set_state(state)

    # ----- Internal: threat panel refresh -----
    def _refresh_threat_panel(self, tracker):
        """Populate the threat panel from tracker.threat_panel_snapshot().

        Empty when no NPCs have threat data yet (very early in fight, or
        if the local player isn't engaged with any NPC). When that happens
        we show the empty-state label and hide the table. Otherwise the
        table is visible and the empty-state hidden.
        """
        rows = tracker.threat_panel_snapshot()

        if not rows:
            self._threat_table.setRowCount(0)
            self._threat_table.setVisible(False)
            self._threat_empty_label.setVisible(True)
            self._threat_empty_label.setText("No NPCs engaged.")
            self._threat_tank_label.setText("")
            return

        # We have at least one engaged NPC. Show the table, hide the
        # empty-state label, surface the tank-identity heuristic.
        self._threat_table.setVisible(True)
        self._threat_empty_label.setVisible(False)
        tank_name = rows[0]["tank_name"]  # consistent across all rows
        self._threat_tank_label.setText(
            f"Tank (heuristic): {tank_name}"
        )

        self._threat_table.setRowCount(len(rows))
        for idx, row in enumerate(rows):
            self._set_threat_row(idx, row)

    def _set_threat_row(self, row_idx: int, r: dict):
        """Populate one threat table row from a snapshot dict.

        Phase 4c: each row carries a leading status cell whose color
        reflects the worst signal across both perspectives (DPS-vs-tank
        and tank-vs-second). Green = safe, yellow = caution, red =
        overtake imminent.
        """
        # Status cell — colored block. The character is a Unicode full
        # block (■). Coloring just the cell background and using the
        # same color for the foreground makes a solid pill that reads
        # at a glance without needing emoji or external icons.
        status = threat_row_status(r)
        color  = _STATUS_COLORS[status]
        status_item = QTableWidgetItem("■")
        status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        status_item.setForeground(QBrush(QColor(color)))
        # Tooltip surfaces the threshold logic so users can learn what
        # triggers each color. Keeps the visible UI uncluttered.
        status_item.setToolTip({
            "green":  "Safe — gap is healthy, not closing",
            "yellow": "Caution — gap is small or closing",
            "red":    "Imminent — overtake predicted within seconds",
        }[status])
        self._threat_table.setItem(row_idx, THC_STATUS, status_item)

        # NPC name
        name_item = QTableWidgetItem(r.get("name", ""))
        self._threat_table.setItem(row_idx, THC_NAME, name_item)

        # HP% — we don't have current HP in the snapshot today; we have
        # max_hp but not current. Leave as "-" until the snapshot can
        # expose current HP. Documented as a phase 4c follow-up.
        hp_item = QTableWidgetItem(EMPTY_VALUE)
        hp_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._threat_table.setItem(row_idx, THC_HP, hp_item)

        # Threat totals
        for col, key in (
            (THC_YOU,  "your_threat"),
            (THC_TANK, "tank_threat"),
        ):
            cell = QTableWidgetItem(_format_threat(r.get(key, 0.0)))
            cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._threat_table.setItem(row_idx, col, cell)

        # Gap + time-left, per perspective
        for col_gap, col_time, key_gap, key_time in (
            (THC_DPS_GAP,  THC_DPS_TIME,  "dps_gap",  "dps_time_left"),
            (THC_TANK_GAP, THC_TANK_TIME, "tank_gap", "tank_time_left"),
        ):
            gap_cell = QTableWidgetItem(_format_gap(r.get(key_gap, 0.0)))
            gap_cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._threat_table.setItem(row_idx, col_gap, gap_cell)

            time_cell = QTableWidgetItem(_format_time_left(r.get(key_time)))
            time_cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._threat_table.setItem(row_idx, col_time, time_cell)

    # ----- Internal: paint the table from cached rows + current filters -----
    def _refresh_table(self):
        """Populate the table from self._last_rows, applying filters."""
        rows = getattr(self, "_last_rows", []) or []

        # Apply kind filters. Players and group members are always shown.
        # NPCs and companions only when their toggle is on. Hazards are
        # always shown if present (they're rare but informative when they
        # do damage).
        visible_rows = []
        for r in rows:
            kind = r.get("kind")
            if kind == EntityKind.NPC and not self._show_npcs:
                continue
            if kind == EntityKind.COMPANION and not self._show_companions:
                continue
            visible_rows.append(r)

        self._table.setRowCount(len(visible_rows))
        for row_idx, r in enumerate(visible_rows):
            self._set_row(row_idx, r)

    def _set_row(self, row_idx: int, r: dict):
        """Populate one table row from a snapshot dict."""
        name = r.get("name", "")
        kind = r.get("kind")
        dps  = r.get("dps", 0)
        dmg  = r.get("total_damage", 0)
        hps  = r.get("hps", 0)
        pct  = r.get("pct", 0)

        # Name
        item = QTableWidgetItem(name)
        bg = KIND_ROW_BG.get(kind)
        if bg:
            item.setBackground(QBrush(QColor(bg)))
        self._table.setItem(row_idx, COL_NAME, item)

        # Class badge - short label from KIND_BADGE, coloured
        badge_tuple = KIND_BADGE.get(kind)
        badge_text = badge_tuple[0] if badge_tuple else ""
        badge_fg   = badge_tuple[2] if badge_tuple else TEXT_PRI
        kind_item = QTableWidgetItem(badge_text)
        kind_item.setForeground(QBrush(QColor(badge_fg)))
        kind_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        if bg:
            kind_item.setBackground(QBrush(QColor(bg)))
        self._table.setItem(row_idx, COL_KIND, kind_item)

        # Numeric columns
        for col, value, formatter in (
            (COL_DPS, dps, _format_dps),
            (COL_DMG, dmg, _format_total),
            (COL_HPS, hps, _format_dps),
            (COL_PCT, pct, _format_pct),
        ):
            cell = QTableWidgetItem(formatter(value))
            cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if bg:
                cell.setBackground(QBrush(QColor(bg)))
            self._table.setItem(row_idx, col, cell)
