"""
ui/tabs/cohort_compare.py — Phase H: Cohort Compare tab.

When a fight is loaded, this tab pulls every other fight in the database
matching the same boss + same class + same discipline, computes medians
across that cohort, and shows you-vs-cohort side by side.

This is the "behavioral differences" view from the original design doc:
the part that turns the parser from a scoreboard into a coach.

Honest scope (v1):
  - Per-minute totals: damage, healing. Counts: taunts, interrupts.
  - Top abilities by your usage, with cohort median use counts.
  - "Not enough data" empty state when cohort < 3.

Punted to a future phase (need data we don't store yet):
  - Active GCD %, first-use timing, buff uptime.
  - Coaching language. v1 shows numbers, not interpretations.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor, QFont
from PyQt6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from engine.aggregator import Fight
from engine.analysis import analyse_tank, build_rotation
from storage.cohort import (
    BenchmarkProfile,
    Cohort,
    build_cohort,
    cohort_benchmark,
    cohort_durations,
)
from storage.encounter_db import _connect_db, encounter_key_for
from ui.theme import ACCENT2, ACCENT3, TEXT_PRI, TEXT_SEC


# Minimum cohort size below which we still show the table but display a
# "not enough data" warning. Same threshold cohort.Cohort.is_meaningful uses.
COHORT_MIN_MEANINGFUL = 3

# How many of the user's top abilities to show in the comparison table.
# Picked to fit on screen without scrolling for typical fights. Sorted by
# your usage descending — the abilities you actually leaned on.
TOP_ABILITY_LIMIT = 15


class CohortCompareTab(QWidget):
    """
    Class-aware comparison: you vs others of your spec on this boss.

    Implements the duck-typed load_fight() interface that MainWindow uses
    to push the active fight to whichever tab the user is looking at.
    """

    def __init__(self):
        super().__init__()
        self._fight: Optional[Fight] = None
        self._build_ui()

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(8, 8, 8, 8)

        # Header strip — what we matched on, cohort size, warning.
        header = QGroupBox("Cohort Match")
        header_lay = QVBoxLayout(header)
        header_lay.setContentsMargins(10, 14, 10, 10)
        header_lay.setSpacing(4)

        self.match_label = QLabel("Load a fight to see how you compare.")
        self.match_label.setStyleSheet(f"color:{TEXT_PRI}; font-size:13px;")
        header_lay.addWidget(self.match_label)

        self.match_subtitle = QLabel("")
        self.match_subtitle.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        header_lay.addWidget(self.match_subtitle)

        self.warning_label = QLabel("")
        self.warning_label.setStyleSheet(
            f"color:{ACCENT3}; font-size:11px; font-weight:600;"
        )
        self.warning_label.setVisible(False)
        header_lay.addWidget(self.warning_label)

        root.addWidget(header)

        # Two side-by-side tables: totals (left) and top abilities (right).
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        # ── Left: per-minute totals ─────────────────────────────────────────
        totals_box = QGroupBox("Totals (per minute, where applicable)")
        totals_lay = QVBoxLayout(totals_box)
        totals_lay.setContentsMargins(8, 14, 8, 8)
        self.totals_table = QTableWidget()
        self.totals_table.setColumnCount(5)
        self.totals_table.setHorizontalHeaderLabels(
            ["Metric", "You", "Cohort median", "Cohort range", "Δ"]
        )
        self.totals_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.totals_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.totals_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.totals_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.totals_table.verticalHeader().setVisible(False)
        totals_lay.addWidget(self.totals_table, 1)
        splitter.addWidget(totals_box)

        # ── Right: top-N abilities ──────────────────────────────────────────
        abilities_box = QGroupBox(
            f"Top {TOP_ABILITY_LIMIT} abilities — sorted by abs(Δ) descending"
        )
        ab_lay = QVBoxLayout(abilities_box)
        ab_lay.setContentsMargins(8, 14, 8, 8)
        self.abilities_table = QTableWidget()
        self.abilities_table.setColumnCount(4)
        self.abilities_table.setHorizontalHeaderLabels(
            ["Ability", "Your uses", "Cohort median", "Δ"]
        )
        self.abilities_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.abilities_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.abilities_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.abilities_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.abilities_table.verticalHeader().setVisible(False)
        ab_lay.addWidget(self.abilities_table, 1)
        splitter.addWidget(abilities_box)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([520, 620])

        root.addWidget(splitter, 1)

    # ── Public API: tab system calls this when a fight becomes active ────────

    def load_fight(
        self, fight: Fight, hide_companions: bool = False, hide_npcs: bool = False
    ) -> None:
        # hide_companions/hide_npcs don't apply here — the cohort is built from
        # DB rows, not the live fight's entity_stats. Signature matches the duck
        # type for consistency.
        self._fight = fight
        self._refresh()

    # ── Refresh: the actual work ────────────────────────────────────────────

    def _refresh(self) -> None:
        fight = self._fight
        if fight is None:
            self._render_empty("Load a fight to see how you compare.")
            return

        # Identify "you" — the local player on the log. Phase H is about
        # comparing the user's spec, so without a local player there's
        # nothing meaningful to show.
        player_name = fight.player_name
        if not player_name or player_name not in fight.entity_stats:
            self._render_empty(
                "This fight has no local-player data — nothing to compare."
            )
            return

        # Pull the user's per-fight class/discipline from the DB. Phase C
        # wrote it; we re-read here rather than recomputing.
        encounter_key = encounter_key_for(fight)
        class_name, discipline_name = self._read_user_class_for_fight(
            encounter_key, player_name
        )
        encounter_name = fight.custom_name or fight.boss_name or "Unknown Encounter"

        if not class_name:
            self._render_empty(
                f"No class detected for {player_name} in this fight. "
                "Re-run aggregation from the Inspector tab to backfill."
            )
            return

        # Build the cohort. v1: same class + same discipline + same boss.
        # Discipline is optional in build_cohort but required for v1's
        # promised behaviour — if discipline is missing we drop to class-only
        # so the user still sees something useful.
        cohort = build_cohort(
            class_name, encounter_name, discipline_name=discipline_name or None
        )
        profile = cohort_benchmark(cohort, mode="median")

        # Update header strip.
        spec = (
            f"{class_name}/{discipline_name}" if discipline_name else class_name
        )
        self.match_label.setText(
            f"Boss: {encounter_name}   ·   Your spec: {spec}"
        )
        self.match_subtitle.setText(
            f"Cohort: {cohort.sample_size} matching fight"
            + ("s" if cohort.sample_size != 1 else "")
            + " in your DB"
        )

        # The "you minus you" subtlety: the cohort can include the user's
        # own fight (and other fights of theirs). For v1 we let it — it's
        # how "self vs history" implicitly works. We just note it in the
        # warning when the cohort is small enough that it matters.
        if cohort.sample_size == 0:
            self._render_no_cohort(spec, encounter_name)
            return
        if cohort.sample_size < COHORT_MIN_MEANINGFUL:
            self.warning_label.setText(
                f"⚠ Only {cohort.sample_size} matching fight"
                + ("s" if cohort.sample_size != 1 else "")
                + f" — medians below are noisy. Need ≥{COHORT_MIN_MEANINGFUL} "
                "for stable comparison."
            )
            self.warning_label.setVisible(True)
        else:
            self.warning_label.setVisible(False)

        # Compute precise durations for the cohort fights so we can
        # normalize damage/healing to per-minute rates.
        encounter_keys = {f.encounter_key for f in cohort.fights}
        durations = cohort_durations(encounter_keys, precise=True)

        # Render both tables.
        self._render_totals_table(fight, player_name, cohort, profile, durations)
        self._render_abilities_table(fight, player_name, profile)

    # ── Empty-state renderers ───────────────────────────────────────────────

    def _render_empty(self, message: str) -> None:
        self.match_label.setText(message)
        self.match_subtitle.setText("")
        self.warning_label.setVisible(False)
        self.totals_table.setRowCount(0)
        self.abilities_table.setRowCount(0)

    def _render_no_cohort(self, spec: str, encounter_name: str) -> None:
        self.match_label.setText(f"Boss: {encounter_name}   ·   Your spec: {spec}")
        self.match_subtitle.setText(
            "No other fights of this spec on this boss are stored yet."
        )
        self.warning_label.setText(
            "⚠ As you import more logs, this comparison will become available."
        )
        self.warning_label.setVisible(True)
        self.totals_table.setRowCount(0)
        self.abilities_table.setRowCount(0)

    # ── Totals table ────────────────────────────────────────────────────────

    def _render_totals_table(
        self,
        fight: Fight,
        player_name: str,
        cohort: Cohort,
        profile: BenchmarkProfile,
        durations: dict[str, float],
    ) -> None:
        # Your numbers from the loaded fight.
        stats = fight.entity_stats[player_name]
        your_duration = max(fight.duration_seconds, 0.001)
        your_dmg_per_min = (stats.damage_dealt / your_duration) * 60
        your_heal_per_min = (stats.healing_done / your_duration) * 60
        # Taunts/interrupts: compute the same way the DB ingestion does, via
        # analyse_tank. This guarantees apples-to-apples with cohort values.
        try:
            tank_metrics = analyse_tank(fight, player_name)
            your_taunts = float(tank_metrics.taunt_count or 0)
            your_interrupts = float(tank_metrics.interrupt_count or 0)
        except Exception:
            your_taunts = 0.0
            your_interrupts = 0.0

        # Cohort: convert each PlayerInFight from totals to per-minute using
        # the duration table we just looked up. We re-derive the per-minute
        # samples here rather than reusing profile.damage_done because the
        # profile was built from totals — it doesn't know about durations.
        dmg_per_min_samples = []
        heal_per_min_samples = []
        for pif in cohort.fights:
            d = durations.get(pif.encounter_key, 0.0)
            if d > 0:
                dmg_per_min_samples.append((pif.damage_done / d) * 60)
                heal_per_min_samples.append((pif.healing_done / d) * 60)

        # Taunts and interrupts stay as raw counts. Cohort values come
        # straight from the profile (already aggregated).
        rows: list[tuple[str, float, list[float], str]] = [
            ("Damage / min", your_dmg_per_min, dmg_per_min_samples, "{:,.0f}"),
            ("Healing / min", your_heal_per_min, heal_per_min_samples, "{:,.0f}"),
            (
                "Taunts (count)",
                your_taunts,
                [float(p.taunts) for p in cohort.fights],
                "{:,.0f}",
            ),
            (
                "Interrupts (count)",
                your_interrupts,
                [float(p.interrupts) for p in cohort.fights],
                "{:,.0f}",
            ),
        ]

        self.totals_table.setRowCount(len(rows))
        for r, (label, your_val, samples, fmt) in enumerate(rows):
            if samples:
                samples_sorted = sorted(samples)
                median_val = samples_sorted[len(samples_sorted) // 2]
                lo, hi = samples_sorted[0], samples_sorted[-1]
                range_str = f"{fmt.format(lo)} – {fmt.format(hi)}"
                delta = your_val - median_val
            else:
                median_val = 0.0
                range_str = "—"
                delta = 0.0

            self.totals_table.setItem(r, 0, _label_cell(label))
            self.totals_table.setItem(r, 1, _value_cell(fmt.format(your_val)))
            self.totals_table.setItem(
                r, 2, _value_cell(fmt.format(median_val) if samples else "—")
            )
            self.totals_table.setItem(r, 3, _value_cell(range_str))
            self.totals_table.setItem(r, 4, _delta_cell(delta, fmt))

    # ── Abilities table ─────────────────────────────────────────────────────

    def _render_abilities_table(
        self, fight: Fight, player_name: str, profile: BenchmarkProfile
    ) -> None:
        # Your use counts: rotation activations (matches Phase E's use_count).
        rotation = build_rotation(fight, player_name)
        your_counts: dict[str, int] = {}
        for entry in rotation:
            your_counts[entry.ability_name] = your_counts.get(entry.ability_name, 0) + 1

        # The candidate set is the union of:
        #   - abilities you used (we want to show every ability you pressed)
        #   - abilities the cohort used commonly (so the user sees abilities
        #     they should have been using but weren't)
        # Sort by abs(delta) descending so the biggest gaps surface first.
        cohort_counts = profile.ability_use_counts
        all_abilities = set(your_counts) | set(cohort_counts)

        rows = []
        for ability in all_abilities:
            you = your_counts.get(ability, 0)
            theirs = cohort_counts.get(ability, 0.0)
            delta = float(you) - theirs
            rows.append((ability, you, theirs, delta))

        # Biggest absolute gaps first. Then by your usage descending so when
        # everything's balanced the abilities you actually pressed bubble up.
        rows.sort(key=lambda r: (-abs(r[3]), -r[1]))
        rows = rows[:TOP_ABILITY_LIMIT]

        self.abilities_table.setRowCount(len(rows))
        for r, (ability, you, theirs, delta) in enumerate(rows):
            self.abilities_table.setItem(r, 0, _label_cell(ability))
            self.abilities_table.setItem(r, 1, _value_cell(f"{you}"))
            self.abilities_table.setItem(
                r,
                2,
                _value_cell(
                    f"{theirs:.1f}" if theirs and not theirs.is_integer()
                    else f"{int(theirs)}" if theirs else "—"
                ),
            )
            self.abilities_table.setItem(r, 3, _delta_cell(delta, "{:.1f}"))

    # ── DB helper ────────────────────────────────────────────────────────────

    @staticmethod
    def _read_user_class_for_fight(
        encounter_key: str, player_name: str
    ) -> tuple[str, str]:
        """
        Look up the user's class and discipline for this specific fight in the
        database. Phase C populates pce.class_name + pce.discipline_name; if
        either is empty we fall back to per-character pc.class_name.

        Returns ("", "") when no row exists (fight isn't in the DB yet, or
        player isn't in player_characters).
        """
        sql = (
            "SELECT pce.class_name, pce.discipline_name, pc.class_name "
            "FROM player_character_encounters pce "
            "JOIN player_characters pc ON pc.character_id = pce.character_id "
            "WHERE pce.encounter_key = ? "
            "  AND pc.character_name = ? COLLATE NOCASE "
            "LIMIT 1"
        )
        try:
            with _connect_db() as conn:
                row = conn.execute(sql, (encounter_key, player_name)).fetchone()
        except sqlite3.Error:
            return ("", "")
        if row is None:
            return ("", "")
        pf_class, pf_disc, pc_class = row
        klass = pf_class or pc_class or ""
        return (klass, pf_disc or "")


# ─── Cell helpers ────────────────────────────────────────────────────────────


def _label_cell(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(str(text))
    item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    item.setForeground(QBrush(QColor(TEXT_PRI)))
    return item


def _value_cell(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(str(text))
    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    item.setForeground(QBrush(QColor(TEXT_PRI)))
    return item


def _delta_cell(delta: float, fmt: str) -> QTableWidgetItem:
    """
    Format a delta with a sign and color it. Green if you're above median,
    orange if below, neutral if zero.

    `fmt` is the magnitude format (e.g. "{:,.0f}" or "{:.1f}") — the sign
    is added by this function so callers don't have to think about it.
    """
    if abs(delta) < 0.05:
        text = "0"
        color = TEXT_SEC
    else:
        sign = "+" if delta > 0 else "−"
        text = sign + fmt.format(abs(delta))
        color = ACCENT2 if delta > 0 else ACCENT3

    item = QTableWidgetItem(text)
    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    item.setForeground(QBrush(QColor(color)))
    item.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
    return item
