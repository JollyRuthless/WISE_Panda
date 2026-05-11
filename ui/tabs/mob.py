"""
ui/tabs/mob.py — MobContributionTab: per-mob damage breakdown with contributor drill-down.
"""

from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QTableWidget, QTableWidgetItem, QLabel, QGroupBox, QHeaderView,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QBrush

from engine.aggregator import Fight, EntityKind, build_mob_damage_breakdown
from ui.theme import TEXT_SEC, KIND_BADGE


class MobContributionTab(QWidget):
    def __init__(self):
        super().__init__()
        self._fight: Optional[Fight] = None
        self._mob_rows: list[dict] = []
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(8, 8, 8, 8)

        ctrl = QHBoxLayout()
        self.summary_label = QLabel("Select a fight to inspect mob contributions.")
        self.summary_label.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        ctrl.addWidget(self.summary_label)
        ctrl.addStretch()
        root.addLayout(ctrl)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        # ── Left: mob list ────────────────────────────────────────────────
        left = QGroupBox("Mobs In This Fight")
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(8, 8, 8, 8)
        self.mob_table = QTableWidget()
        self.mob_table.setColumnCount(8)
        self.mob_table.setHorizontalHeaderLabels([
            "Mob", "NPC ID", "Instances", "Defeats", "Damage Taken",
            "Top Contributor", "Top %", "Max HP",
        ])
        self.mob_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.mob_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.mob_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.mob_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.mob_table.verticalHeader().setVisible(False)
        self.mob_table.itemSelectionChanged.connect(self._load_selected_mob)
        left_lay.addWidget(self.mob_table, 1)
        splitter.addWidget(left)

        # ── Right: contributor breakdown ──────────────────────────────────
        right = QGroupBox("Player Contribution")
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(8, 8, 8, 8)
        self.detail_label = QLabel("Select a mob to see who contributed damage to it.")
        self.detail_label.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        right_lay.addWidget(self.detail_label)
        self.detail_table = QTableWidget()
        self.detail_table.setColumnCount(8)
        self.detail_table.setHorizontalHeaderLabels([
            "Contributor", "Type", "Damage", "Share", "Hits", "Crit%", "Avg Hit", "Max Hit",
        ])
        self.detail_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.detail_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.detail_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.detail_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.detail_table.verticalHeader().setVisible(False)
        right_lay.addWidget(self.detail_table, 1)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([620, 520])
        root.addWidget(splitter, 1)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _cell(txt, align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
               fg: Optional[str] = None, bold: bool = False):
        item = QTableWidgetItem(str(txt))
        item.setTextAlignment(align)
        if fg:   item.setForeground(QBrush(QColor(fg)))
        if bold: item.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        return item

    # ── Public API ────────────────────────────────────────────────────────────

    def load_fight(self, fight: Fight, hide_companions: bool = False, hide_npcs: bool = False):
        self._fight = fight
        self._mob_rows = build_mob_damage_breakdown(fight, hide_companions=hide_companions)

        total_damage  = sum(r["total_damage_taken"] for r in self._mob_rows)
        total_defeats = sum(r["defeats"] for r in self._mob_rows)
        self.summary_label.setText(
            f"{len(self._mob_rows):,} mobs"
            f"  •  {total_damage:,} total incoming damage"
            f"  •  {total_defeats:,} observed defeats"
        )

        cell = self._cell
        self.mob_table.setRowCount(len(self._mob_rows))
        npc_fg = KIND_BADGE[EntityKind.NPC][2]
        for row, mob in enumerate(self._mob_rows):
            self.mob_table.setItem(row, 0, cell(mob["mob_name"],       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, fg=npc_fg, bold=True))
            self.mob_table.setItem(row, 1, cell(mob["npc_entity_id"] or "—", Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter))
            self.mob_table.setItem(row, 2, cell(mob["instances_seen"]))
            self.mob_table.setItem(row, 3, cell(mob["defeats"]))
            self.mob_table.setItem(row, 4, cell(f"{mob['total_damage_taken']:,}"))
            self.mob_table.setItem(row, 5, cell(mob["top_contributor"], Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter))
            self.mob_table.setItem(row, 6, cell(f"{mob['top_share']:.0%}"))
            self.mob_table.setItem(row, 7, cell(f"{mob['max_hp_seen']:,}" if mob["max_hp_seen"] else "—"))

        self.mob_table.resizeRowsToContents()
        if self._mob_rows:
            self.mob_table.selectRow(0)
            self._load_selected_mob()
        else:
            self.detail_label.setText("No NPC damage targets are available for this fight with the current filters.")
            self.detail_table.setRowCount(0)

    def _load_selected_mob(self):
        selected = self.mob_table.selectionModel().selectedRows()
        if not selected or not self._mob_rows:
            self.detail_label.setText("Select a mob to see who contributed damage to it.")
            self.detail_table.setRowCount(0)
            return

        mob = self._mob_rows[selected[0].row()]
        contributors = mob["contributors"]
        self.detail_label.setText(
            f"{mob['mob_name']}  •  {len(contributors)} contributors"
            f"  •  {mob['total_damage_taken']:,} logged damage taken"
        )
        self.detail_table.setRowCount(len(contributors))

        cell = self._cell
        for row, c in enumerate(contributors):
            badge_lbl, _badge_bg, badge_fg = KIND_BADGE.get(c["kind"], KIND_BADGE[EntityKind.NPC])
            self.detail_table.setItem(row, 0, cell(c["name"],     Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, fg=badge_fg, bold=True))
            self.detail_table.setItem(row, 1, cell(badge_lbl,    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, fg=badge_fg))
            self.detail_table.setItem(row, 2, cell(f"{c['damage']:,}"))
            self.detail_table.setItem(row, 3, cell(f"{c['share']:.0%}"))
            self.detail_table.setItem(row, 4, cell(c["hits"]))
            self.detail_table.setItem(row, 5, cell(f"{c['crit_rate']:.0%}"))
            self.detail_table.setItem(row, 6, cell(f"{c['avg_hit']:,.0f}"))
            self.detail_table.setItem(row, 7, cell(f"{c['max_hit']:,}"))

        self.detail_table.resizeRowsToContents()
