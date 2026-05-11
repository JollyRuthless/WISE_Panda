"""
ui_roster.comparison_panel
==========================

Bottom region of the Roster window. Given a player + boss + the player's spec
on that boss, this panel shows every *other* player who has fought the same
boss in the same role, with their per-fight totals and overall summary.

This is the "how do I stack up" surface. The signature feature of Roster.

Reads from:
  cohort.find_fights()              — every fight on this boss
  cohort.list_participants_in_fight() — who was in each one
  ui_roster.roles.role_for()        — to filter participants by role

Why a worker thread:
  Building the cohort means walking every fight on this boss and reading its
  participant list. For a popular boss with hundreds of pulls in the DB,
  that's hundreds of small queries. Cheap individually, slow in aggregate
  on the GUI thread, where it would block input. We push the work to a
  QThread and stream a progress label back to the user.

What "comparison" means here:
  Roster v1 deliberately stays simple. We show:
    - the user's own fights on this boss (the baseline)
    - other players' fights on this boss in the same role
    - the median totals across that role-matched cohort
  Per-ability deltas, burst alignment, uptime — all the deeper coaching
  signals — live in the existing Phase H Cohort tab in the main app. Roster
  is for the bird's-eye view: "am I average, top, or below?" Everything
  deeper happens in the existing app via right-click → Open in Yoda (which
  the next-next session will wire up; v1 just shows numbers).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from statistics import median
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont
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
from ui_roster.roles import ROLE_DPS, ROLE_HEALER, ROLE_TANK, ROLE_UNKNOWN, role_for


# ── Column layout ───────────────────────────────────────────────────────────

COL_PLAYER     = 0
COL_SPEC       = 1
COL_FIGHTS     = 2
COL_BEST       = 3   # role-aware: best DPS / HPS / mitigation proxy
COL_MEDIAN     = 4   # role-aware median
COL_LAST_SEEN  = 5
N_COLS         = 6

COLUMN_HEADERS_BY_ROLE = {
    ROLE_TANK:    ["Player", "Spec", "Fights", "Top dmg", "Median dmg", "Last seen"],
    ROLE_HEALER:  ["Player", "Spec", "Fights", "Top heal", "Median heal", "Last seen"],
    ROLE_DPS:     ["Player", "Spec", "Fights", "Top dmg", "Median dmg", "Last seen"],
    ROLE_UNKNOWN: ["Player", "Spec", "Fights", "Top dmg", "Median dmg", "Last seen"],
}

# Highlight colour for the "you" row. Subtle blue tint that works on both
# light and dark Qt themes — neither pure red/green (success/failure-coded)
# nor a hard contrast that fights the alternating row stripes.
YOU_ROW_TINT = QColor(64, 132, 200, 50)

# Cap how many fights we walk for cohort building. A boss with thousands of
# pulls in the DB shouldn't make the panel hang. Most-recent fights are
# walked first, so this caps the cohort to the most relevant slice.
COHORT_FIGHT_CAP = 500


# ── Worker thread ───────────────────────────────────────────────────────────


@dataclass
class _CohortRow:
    """Aggregated stats for one player's appearances on this boss in this role."""
    character_name: str
    spec_text: str            # already-formatted "Class · Discipline"
    fight_count: int
    damages: list[int] = field(default_factory=list)
    healings: list[int] = field(default_factory=list)
    last_seen: str = ""

    @property
    def best_damage(self) -> int:
        return max(self.damages) if self.damages else 0

    @property
    def best_healing(self) -> int:
        return max(self.healings) if self.healings else 0

    @property
    def median_damage(self) -> int:
        return int(median(self.damages)) if self.damages else 0

    @property
    def median_healing(self) -> int:
        return int(median(self.healings)) if self.healings else 0


class _CohortBuildResult:
    """What the worker hands back."""
    def __init__(
        self,
        rows: list[_CohortRow],
        scanned: int,
        skipped_role_mismatch: int,
        skipped_no_class: int,
    ) -> None:
        self.rows = rows
        self.scanned = scanned
        self.skipped_role_mismatch = skipped_role_mismatch
        self.skipped_no_class = skipped_no_class


class _CohortWorker(QThread):
    """
    Builds the role-matched cohort off the GUI thread.

    Signals:
        progress(int, int) — (fights_processed, total_fights)
        finished_ok(_CohortBuildResult)
        failed(str) — error message
    """

    progress = Signal(int, int)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        boss_name: str,
        target_role: str,
        own_player_name: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._boss_name = boss_name
        self._target_role = target_role
        self._own_player_name = own_player_name.lower()
        self._cancelled = False

    def cancel(self) -> None:
        """Request graceful cancellation. Worker checks between fights."""
        self._cancelled = True

    def run(self) -> None:  # noqa: D401 — Qt convention
        try:
            self._run_impl()
        except Exception as exc:  # noqa: BLE001 — translate to UI signal
            self.failed.emit(f"{type(exc).__name__}: {exc}")

    def _run_impl(self) -> None:
        # Step 1: enumerate every fight on this boss. find_fights with
        # encounter_name_contains gives us a forgiving match (handles the
        # "boss" vs "Boss" capitalisation drift that sometimes appears).
        filters = cohort.FightFilters(
            encounter_name_contains=self._boss_name,
            limit=COHORT_FIGHT_CAP,
        )
        all_fights = cohort.find_fights(filters)

        # Defensive narrow: encounter_name_contains is substring-based, so a
        # boss named "Apex" would match "Apex Vanguard". We want exact equality.
        # find_fights doesn't have an exact-match flag (Phase F design choice),
        # so we filter in Python.
        target_lower = self._boss_name.lower()
        fights = [f for f in all_fights if f.encounter_name.lower() == target_lower]

        total = len(fights)
        if total == 0:
            self.finished_ok.emit(_CohortBuildResult([], 0, 0, 0))
            return

        # Step 2: walk participants per fight, role-filter, accumulate.
        # Aggregation key is character_name (case-insensitive) — a player
        # who switched specs on this boss appears as one row with a spec
        # set to "most recent" (see _CohortRow.spec_text usage below).
        by_player: dict[str, _CohortRow] = {}
        scanned = 0
        skipped_role = 0
        skipped_no_class = 0

        for i, fight_ref in enumerate(fights):
            if self._cancelled:
                return
            self.progress.emit(i, total)

            try:
                participants = cohort.list_participants_in_fight(fight_ref.encounter_key)
            except Exception:  # noqa: BLE001
                continue

            for p in participants:
                scanned += 1
                if not p.class_name or not p.discipline_name:
                    skipped_no_class += 1
                    continue
                role = role_for(p.class_name, p.discipline_name)
                if role != self._target_role:
                    skipped_role += 1
                    continue

                key = p.character_name.lower()
                spec_text = _format_spec(p.class_name, p.discipline_name)
                row = by_player.get(key)
                if row is None:
                    row = _CohortRow(
                        character_name=p.character_name,
                        spec_text=spec_text,
                        fight_count=0,
                    )
                    by_player[key] = row
                row.fight_count += 1
                row.damages.append(p.damage_done)
                row.healings.append(p.healing_done)
                # Track the most-recent encounter_date as last_seen, and the
                # spec on that fight as the displayed spec. find_fights
                # returns newest-first so the first time we see a player IS
                # their most recent appearance.
                if not row.last_seen:
                    row.last_seen = p.encounter_date or ""
                    row.spec_text = spec_text

        self.progress.emit(total, total)
        rows = list(by_player.values())

        # Sort rules: own player to the top (the panel highlights them, but
        # also pin them so the user doesn't have to scroll). Then by fight
        # count desc. Then alphabetical for stability.
        rows.sort(key=lambda r: (
            0 if r.character_name.lower() == self._own_player_name else 1,
            -r.fight_count,
            r.character_name.lower(),
        ))

        self.finished_ok.emit(_CohortBuildResult(
            rows, scanned, skipped_role, skipped_no_class
        ))


# ── The panel ───────────────────────────────────────────────────────────────


class ComparisonPanel(QWidget):
    """
    Bottom region: role-matched cohort table for the selected (player, boss).

    Public methods:
        set_context(player, boss, class, discipline)
            Set the analysis context. Triggers a worker rebuild. All four
            args may be empty — the panel will render an appropriate
            placeholder.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._current_player: str = ""
        self._current_boss: str = ""
        self._current_role: str = ROLE_UNKNOWN
        self._worker: Optional[_CohortWorker] = None
        self._build_ui()
        self._render_placeholder("Pick a player and boss to compare.")

    # ── UI construction ─────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 4, 8, 8)
        root.setSpacing(6)

        # Header strip with the analytical context — what we're comparing.
        header_row = QHBoxLayout()
        header_row.setSpacing(8)

        title = QLabel("Comparison")
        title_font = QFont()
        title_font.setBold(True)
        title_font.setPointSize(11)
        title.setFont(title_font)
        header_row.addWidget(title)

        self._context_label = QLabel("")
        self._context_label.setStyleSheet("color: #888;")
        header_row.addWidget(self._context_label, stretch=1)

        root.addLayout(header_row)

        # Status / placeholder line. Used for:
        #   - "select a player and boss"
        #   - "loading cohort..."
        #   - "no other players found"
        #   - error messages
        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color: #888; padding: 12px;")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setWordWrap(True)
        root.addWidget(self._status_label)

        # Summary footer line — counts of skipped rows. Helps the user
        # understand why a cohort might look smaller than expected.
        self._footer_label = QLabel("")
        self._footer_label.setStyleSheet("color: #888; font-size: 11px;")
        self._footer_label.setWordWrap(True)

        # The table.
        self._table = QTableWidget(0, N_COLS, self)
        self._table.setHorizontalHeaderLabels(COLUMN_HEADERS_BY_ROLE[ROLE_UNKNOWN])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(False)

        header = self._table.horizontalHeader()
        header.setSectionResizeMode(COL_PLAYER, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_SPEC, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_FIGHTS, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_BEST, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_MEDIAN, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_LAST_SEEN, QHeaderView.ResizeMode.ResizeToContents)

        root.addWidget(self._table, stretch=1)
        root.addWidget(self._footer_label)

    # ── Public API ──────────────────────────────────────────────────────────

    def set_context(
        self,
        player_name: str,
        boss_name: str,
        class_name: str,
        discipline_name: str,
    ) -> None:
        """
        Set the analysis context. Cancels any in-flight worker, then either
        renders a placeholder (incomplete context) or kicks off a new
        cohort build (complete context).
        """
        self._cancel_in_flight_worker()

        self._current_player = player_name
        self._current_boss = boss_name
        self._current_role = role_for(class_name, discipline_name)

        # Update header context text. We show whatever we have so the user
        # can see exactly which inputs the panel is reasoning about.
        ctx_bits: list[str] = []
        if player_name:
            ctx_bits.append(f"· {player_name}")
        if boss_name:
            ctx_bits.append(f"vs {boss_name}")
        if class_name or discipline_name:
            ctx_bits.append(f"({_format_spec(class_name, discipline_name)})")
        self._context_label.setText(" ".join(ctx_bits))

        # Validate context completeness. We need a boss and a known role to
        # do anything useful. The player can be anyone — we use them only
        # to highlight their row.
        if not boss_name:
            self._render_placeholder("Pick a boss in the middle panel.")
            self._footer_label.setText("")
            return

        if self._current_role == ROLE_UNKNOWN:
            self._render_placeholder(
                "Can't determine the role for this player on this boss "
                "(no class+discipline in their per-fight data). The "
                "comparison panel needs a known role to filter peers."
            )
            self._footer_label.setText("")
            return

        # Update column headers to match the role's preferred metric.
        self._set_role_headers(self._current_role)

        # Kick off the worker.
        self._render_loading(f"Building cohort for {self._current_role}s on {boss_name}…")
        self._footer_label.setText("")
        self._start_worker(boss_name, self._current_role, player_name)

    # ── Worker management ───────────────────────────────────────────────────

    def _start_worker(self, boss_name: str, role: str, player_name: str) -> None:
        worker = _CohortWorker(boss_name, role, player_name, parent=self)
        worker.progress.connect(self._on_progress)
        worker.finished_ok.connect(self._on_cohort_ready)
        worker.failed.connect(self._on_cohort_failed)
        # Auto-clean when done. finished is a built-in QThread signal.
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        worker.start()

    def _cancel_in_flight_worker(self) -> None:
        """Politely ask any active worker to stop and forget it."""
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            # We don't wait on the thread here — that would block the GUI on
            # context switches. The worker checks self._cancelled between
            # fights and exits its loop quickly. Disconnect signals so any
            # late results from the cancelled worker are ignored.
            try:
                self._worker.progress.disconnect(self._on_progress)
                self._worker.finished_ok.disconnect(self._on_cohort_ready)
                self._worker.failed.disconnect(self._on_cohort_failed)
            except (TypeError, RuntimeError):
                # Signal might already be disconnected — fine.
                pass
        self._worker = None

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            pct = int((done / total) * 100)
            self._status_label.setText(
                f"Building cohort for {self._current_role}s on "
                f"{self._current_boss}…  {done}/{total}  ({pct}%)"
            )

    def _on_cohort_ready(self, result: _CohortBuildResult) -> None:
        self._worker = None
        if not result.rows:
            self._render_placeholder(
                f"No {self._current_role.lower()}s found in your DB for "
                f"{self._current_boss}.\n\n"
                f"Scanned {result.scanned} participant rows. "
                f"{result.skipped_no_class} had no class data; "
                f"{result.skipped_role_mismatch} were a different role."
            )
            self._footer_label.setText("")
            return

        self._populate_table(result.rows)
        self._status_label.setVisible(False)
        self._table.setVisible(True)

        # Footer: be honest about what was filtered out.
        bits = [f"{len(result.rows)} player{'s' if len(result.rows) != 1 else ''} matched"]
        if result.skipped_role_mismatch > 0:
            bits.append(f"{result.skipped_role_mismatch} skipped (different role)")
        if result.skipped_no_class > 0:
            bits.append(f"{result.skipped_no_class} skipped (no class data)")
        self._footer_label.setText("  ·  ".join(bits))

    def _on_cohort_failed(self, message: str) -> None:
        self._worker = None
        self._render_placeholder(f"⚠ Cohort build failed: {message}")
        self._footer_label.setText("")

    # ── Rendering ───────────────────────────────────────────────────────────

    def _set_role_headers(self, role: str) -> None:
        headers = COLUMN_HEADERS_BY_ROLE.get(role, COLUMN_HEADERS_BY_ROLE[ROLE_UNKNOWN])
        self._table.setHorizontalHeaderLabels(headers)

    def _render_placeholder(self, message: str) -> None:
        self._status_label.setText(message)
        self._status_label.setVisible(True)
        self._table.setVisible(False)
        self._table.setRowCount(0)

    def _render_loading(self, message: str) -> None:
        self._render_placeholder(message)

    def _populate_table(self, rows: list[_CohortRow]) -> None:
        # Determine which metric to show in the BEST and MEDIAN columns
        # based on role. Healers want HPS-like; everyone else wants damage.
        use_healing = self._current_role == ROLE_HEALER

        own_lower = self._current_player.lower()

        self._table.blockSignals(True)
        try:
            self._table.setRowCount(len(rows))
            for row_idx, row in enumerate(rows):
                self._set_row(row_idx, row, use_healing, own_lower)
        finally:
            self._table.blockSignals(False)

    def _set_row(
        self,
        row_idx: int,
        row: _CohortRow,
        use_healing: bool,
        own_player_lower: str,
    ) -> None:
        is_you = row.character_name.lower() == own_player_lower

        # Player. Decorate with "(you)" suffix when it's the user's row.
        name_text = row.character_name + ("  (you)" if is_you else "")
        name_item = QTableWidgetItem(name_text)
        if is_you:
            f = name_item.font()
            f.setBold(True)
            name_item.setFont(f)
        self._table.setItem(row_idx, COL_PLAYER, name_item)

        # Spec. As-is from the row.
        spec_item = QTableWidgetItem(row.spec_text)
        self._table.setItem(row_idx, COL_SPEC, spec_item)

        # Fights. Right-aligned.
        fights_item = QTableWidgetItem(str(row.fight_count))
        fights_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._table.setItem(row_idx, COL_FIGHTS, fights_item)

        # Best & median. Role-aware metric pick.
        if use_healing:
            best_val = row.best_healing
            median_val = row.median_healing
        else:
            best_val = row.best_damage
            median_val = row.median_damage

        best_item = QTableWidgetItem(_format_int(best_val))
        best_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._table.setItem(row_idx, COL_BEST, best_item)

        median_item = QTableWidgetItem(_format_int(median_val))
        median_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._table.setItem(row_idx, COL_MEDIAN, median_item)

        # Last seen.
        last_item = QTableWidgetItem(row.last_seen or "—")
        self._table.setItem(row_idx, COL_LAST_SEEN, last_item)

        # Highlight the user's own row.
        if is_you:
            for col in range(N_COLS):
                cell = self._table.item(row_idx, col)
                if cell is not None:
                    cell.setBackground(YOU_ROW_TINT)


# ── Module-level helpers ────────────────────────────────────────────────────


def _format_spec(class_name: str, discipline_name: str) -> str:
    if class_name and discipline_name:
        return f"{class_name} · {discipline_name}"
    if class_name:
        return class_name
    if discipline_name:
        return discipline_name
    return "—"


def _format_int(n: int) -> str:
    """
    Group-by-thousands formatting. Damage and healing numbers run into the
    millions so unformatted ints are unreadable. Uses comma — same convention
    as the existing Yoda app's overview tab so the two apps feel consistent.
    """
    return f"{n:,}"
