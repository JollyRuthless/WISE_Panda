"""
ui/tabs/overview.py — OverviewTab: combatant leaderboard, stat cards, encounter highlights.
"""

from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QLabel, QPushButton, QGroupBox, QHeaderView, QFrame, QSizePolicy,
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QBrush

from engine.aggregator import Fight, EntityStats, AbilityStats, EntityKind, _kind_from_entity
from ui.theme import (
    BG_WIDGET, BORDER, TEXT_PRI, TEXT_SEC,
    ACCENT, ACCENT2, ACCENT3, ACCENT4,
    KIND_BADGE, KIND_ROW_BG,
)
from ui.widgets import StatCard


class OverviewTab(QWidget):
    filters_changed = pyqtSignal(bool, bool)  # show_npcs, show_companions

    def __init__(self):
        super().__init__()
        self._fight: Optional[Fight] = None
        self._show_npcs = False
        self._show_companions = False
        self._overview_columns_customized = False
        self._applying_overview_layout = False
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(12)

        # ── Stat cards ────────────────────────────────────────────────────
        cards_layout = QHBoxLayout()
        cards_layout.setSpacing(10)
        self.card_duration   = StatCard("Duration",      color=ACCENT)
        self.card_dps        = StatCard("Encounter DPS", color=ACCENT2)
        self.card_active_dps = StatCard("Active DPS",    color="#7ee787")
        self.card_boss_dps   = StatCard("Boss DPS",      color="#ff8fc7")
        self.card_hps        = StatCard("Healer HPS",    color="#79c0ff")
        self.card_dmg        = StatCard("Total DMG",     color=ACCENT3)
        self.card_heal       = StatCard("Total Heal",    color=ACCENT4)
        self.card_crits      = StatCard("Crit Rate",     color="#ffa657")
        for c in (self.card_duration, self.card_dps, self.card_active_dps, self.card_boss_dps,
                  self.card_hps, self.card_dmg, self.card_heal, self.card_crits):
            cards_layout.addWidget(c)
        root.addLayout(cards_layout)

        # ── Legend / filter row ───────────────────────────────────────────
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        legend_lbl = QLabel("Legend:")
        legend_lbl.setStyleSheet(f"color: {TEXT_SEC}; font-size: 11px;")
        filter_row.addWidget(legend_lbl)
        self._legend_badges: dict[EntityKind, QWidget] = {}
        for kind, (badge, _bg, _fg) in KIND_BADGE.items():
            if kind == EntityKind.NPC:
                btn = QPushButton(badge)
                btn.clicked.connect(self._toggle_npcs)
                self._legend_badges[kind] = btn
                filter_row.addWidget(btn)
            elif kind == EntityKind.COMPANION:
                btn = QPushButton(badge)
                btn.clicked.connect(self._toggle_companions)
                self._legend_badges[kind] = btn
                filter_row.addWidget(btn)
            else:
                lbl = QLabel(f"  {badge}  ")
                lbl.setStyleSheet(
                    f"background:{KIND_BADGE[kind][1]}; color:{KIND_BADGE[kind][2]}; "
                    "font-size:10px; font-weight:700; border-radius:3px; padding:2px 6px;"
                )
                self._legend_badges[kind] = lbl
                filter_row.addWidget(lbl)
        filter_row.addStretch()
        root.addLayout(filter_row)

        # ── Highlights ────────────────────────────────────────────────────
        highlights_grp = QGroupBox("Encounter Highlights")
        highlights_lay = QHBoxLayout(highlights_grp)
        highlights_lay.setSpacing(10)
        self.hit_highlight   = self._make_highlight_card("Largest Hit")
        self.heal_highlight  = self._make_highlight_card("Largest Heal")
        self.death_highlight = self._make_highlight_card("Deaths")
        for card in (self.hit_highlight, self.heal_highlight, self.death_highlight):
            highlights_lay.addWidget(card)
        root.addWidget(highlights_grp)

        # ── Combatants table ──────────────────────────────────────────────
        grp = QGroupBox("Combatants")
        grp_lay = QVBoxLayout(grp)
        self.table = QTableWidget()
        self.table.setColumnCount(11)
        self.table.setHorizontalHeaderLabels([
            "Type", "Entity", "DMG Dealt", "Encounter DPS", "Active DPS",
            "Boss DPS", "Crits", "DMG Taken", "HPS", "Healing Done", "Absorbed",
        ])
        self.table.setColumnWidth(0, 62)
        self.table.setColumnWidth(1, 260)
        header = self.table.horizontalHeader()
        header.setSectionsMovable(True)
        header.setSectionsClickable(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        header.sectionMoved.connect(self._on_overview_columns_changed)
        header.sectionResized.connect(self._on_overview_columns_changed)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self._style_overview_headers()
        self._apply_default_overview_layout()
        grp_lay.addWidget(self.table)
        root.addWidget(grp)
        self._style_filter_buttons()

    # ── Header styling ────────────────────────────────────────────────────────

    def _style_overview_headers(self):
        dps_cols = {3, 4, 5}
        new_metric_cols = {4, 5}
        tooltips = {
            3: "Encounter DPS = damage dealt / full fight duration.",
            4: "Active DPS = damage dealt / time between this entity's first and last damaging hit.",
            5: "Boss DPS = damage dealt to the primary boss target / full fight duration.",
            8: "HPS = effective healing done / full fight duration.",
        }
        for idx in range(self.table.columnCount()):
            item = self.table.horizontalHeaderItem(idx)
            if item is None:
                item = QTableWidgetItem()
                self.table.setHorizontalHeaderItem(idx, item)
            item.setToolTip(tooltips.get(idx, ""))
            if idx in new_metric_cols:
                item.setBackground(QBrush(QColor("#2b1633")))
                item.setForeground(QBrush(QColor("#ffb7de")))
            elif idx in dps_cols:
                item.setBackground(QBrush(QColor("#142431")))
                item.setForeground(QBrush(QColor("#9ed0ff")))
            else:
                item.setBackground(QBrush(QColor(BG_WIDGET)))
                item.setForeground(QBrush(QColor(TEXT_SEC)))

    def _on_overview_columns_changed(self, *_args):
        if self._applying_overview_layout:
            return
        self._overview_columns_customized = True

    def _apply_default_overview_layout(self):
        if self._overview_columns_customized:
            return
        header = self.table.horizontalHeader()
        viewport_width = max(self.table.viewport().width(), 600)
        base_widths = {0: 62, 1: 260, 2: 96, 3: 108, 4: 96, 5: 90, 6: 72, 7: 90, 8: 84, 9: 102, 10: 90}
        extra = max(viewport_width - sum(base_widths.values()) - 24, 0)
        flex_cols = {1: 0.45, 2: 0.08, 3: 0.10, 4: 0.10, 5: 0.09, 7: 0.07, 9: 0.11}
        self._applying_overview_layout = True
        try:
            for logical_index, base in base_widths.items():
                grow = int(extra * flex_cols.get(logical_index, 0.0))
                header.resizeSection(logical_index, base + grow)
        finally:
            self._applying_overview_layout = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_default_overview_layout()

    # ── Highlight card helpers ────────────────────────────────────────────────

    def _make_highlight_card(self, title: str) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(
            f"QFrame{{background:{BG_WIDGET}; border:1px solid {BORDER}; border-radius:8px; padding:8px;}}"
        )
        lay = QVBoxLayout(frame)
        lay.setSpacing(2)
        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(f"color:{TEXT_SEC}; font-size:10px; font-weight:600;")
        value_lbl = QLabel("—")
        value_lbl.setStyleSheet(f"color:{TEXT_PRI}; font-size:16px; font-weight:700;")
        detail_lbl = QLabel("")
        detail_lbl.setWordWrap(True)
        detail_lbl.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        lay.addWidget(title_lbl)
        lay.addWidget(value_lbl)
        lay.addWidget(detail_lbl)
        frame._value_lbl  = value_lbl
        frame._detail_lbl = detail_lbl
        return frame

    def _set_highlight(self, frame: QFrame, value: str, detail: str):
        frame._value_lbl.setText(value)
        frame._detail_lbl.setText(detail)

    # ── Filter button logic ───────────────────────────────────────────────────

    def _entity_visible_in_overview(self, entity, hide_companions: bool, hide_npcs: bool) -> bool:
        kind = _kind_from_entity(entity, self._fight.player_name if self._fight else None)
        if hide_companions and kind == EntityKind.COMPANION:
            return False
        if hide_npcs and kind == EntityKind.NPC:
            return False
        return True

    def _compute_highlights(self, fight: Fight, hide_companions: bool, hide_npcs: bool):
        best_hit  = (0, "", "")
        best_heal = (0, "", "")
        deaths: dict[str, int] = {}
        hp_by_target: dict[str, int] = {}

        for ev in fight.events:
            if (ev.is_damage and ev.result and not ev.result.is_miss
                    and self._entity_visible_in_overview(ev.source, hide_companions, hide_npcs)):
                if ev.result.amount > best_hit[0]:
                    best_hit = (
                        ev.result.amount,
                        ev.source.display_name,
                        ev.ability.name if ev.ability else "Unknown",
                    )
            if (ev.is_heal and ev.result
                    and self._entity_visible_in_overview(ev.source, hide_companions, hide_npcs)):
                effective = ev.result.amount - (ev.result.overheal or 0)
                if effective > best_heal[0]:
                    best_heal = (
                        effective,
                        ev.source.display_name,
                        ev.ability.name if ev.ability else "Unknown",
                    )
            tgt = ev.target
            if tgt and tgt.hp is not None and self._entity_visible_in_overview(tgt, hide_companions, hide_npcs):
                key = tgt.unique_id or tgt.display_name
                previous_hp = hp_by_target.get(key)
                if tgt.hp <= 0 and (previous_hp is None or previous_hp > 0):
                    name = tgt.display_name or key
                    deaths[name] = deaths.get(name, 0) + 1
                hp_by_target[key] = tgt.hp

        return best_hit, best_heal, deaths

    def _toggle_npcs(self):
        self._show_npcs = not self._show_npcs
        self._style_filter_buttons()
        self.filters_changed.emit(self._show_npcs, self._show_companions)
        if self._fight:
            self.load_fight(self._fight, hide_companions=not self._show_companions,
                            hide_npcs=not self._show_npcs)

    def _toggle_companions(self):
        self._show_companions = not self._show_companions
        self._style_filter_buttons()
        self.filters_changed.emit(self._show_npcs, self._show_companions)
        if self._fight:
            self.load_fight(self._fight, hide_companions=not self._show_companions,
                            hide_npcs=not self._show_npcs)

    def _style_filter_buttons(self):
        def style_toggle(btn: QPushButton, bg: str, fg: str, enabled: bool):
            base_bg = bg if enabled else "#20252d"
            base_fg = fg if enabled else "#6e7681"
            btn.setStyleSheet(
                f"QPushButton {{ background-color: {base_bg}; color: {base_fg}; border: 1px solid {BORDER}; "
                "border-radius: 4px; padding: 3px 10px; font-size: 10px; font-weight: 700; }"
                f"QPushButton:hover {{ border-color: {fg}; }}"
            )

        _npc_badge,  npc_bg,  npc_fg  = KIND_BADGE[EntityKind.NPC]
        _comp_badge, comp_bg, comp_fg = KIND_BADGE[EntityKind.COMPANION]
        style_toggle(self._legend_badges[EntityKind.NPC],       npc_bg,  npc_fg,  self._show_npcs)
        style_toggle(self._legend_badges[EntityKind.COMPANION], comp_bg, comp_fg, self._show_companions)

    # ── Public API ────────────────────────────────────────────────────────────

    def load_fight(self, fight: Fight, hide_companions: bool = False, hide_npcs: bool = False):
        self._fight = fight
        self._hide_companions = hide_companions
        self._show_npcs = not hide_npcs
        self._show_companions = not hide_companions
        self._style_filter_buttons()

        all_stats = sorted(fight.entity_stats.items(), key=lambda x: -x[1].damage_dealt)
        visible   = [(n, s) for n, s in all_stats
                     if (not hide_npcs        or s.kind != EntityKind.NPC)
                     and (not hide_companions  or s.kind != EntityKind.COMPANION)]

        pc_kinds  = (EntityKind.PLAYER, EntityKind.COMPANION, EntityKind.GROUP_MEMBER)
        pc_stats  = [s for _, s in all_stats if s.kind in pc_kinds]
        best_dps        = max((fight.dps(n)        for n, s in all_stats if s.kind in pc_kinds), default=0)
        best_active_dps = max((fight.active_dps(n) for n, s in all_stats if s.kind in pc_kinds), default=0)
        best_boss_dps   = max((fight.boss_dps(n)   for n, s in all_stats if s.kind in pc_kinds), default=0)
        best_hps        = max((fight.hps(n)        for n, s in all_stats if s.kind in pc_kinds), default=0)
        total_dmg   = sum(s.damage_dealt  for _, s in all_stats)
        total_heal  = sum(s.healing_done  for _, s in all_stats)
        total_hits  = sum(s.hits  for s in pc_stats)
        total_crits = sum(s.crits for s in pc_stats)
        crit_pct    = total_crits / total_hits if total_hits else 0

        self.card_duration.set_value(fight.duration_str)
        self.card_dps.set_value(f"{best_dps:,.0f}")
        self.card_active_dps.set_value(f"{best_active_dps:,.0f}")
        self.card_boss_dps.set_value(f"{best_boss_dps:,.0f}")
        self.card_hps.set_value(f"{best_hps:,.0f}")
        self.card_dmg.set_value(f"{total_dmg:,}")
        self.card_heal.set_value(f"{total_heal:,}")
        self.card_crits.set_value(f"{crit_pct:.0%}")

        best_hit, best_heal, deaths = self._compute_highlights(fight, hide_companions, hide_npcs)
        hit_detail   = f"{best_hit[1]} · {best_hit[2]}"   if best_hit[0]  else "No damaging hits recorded."
        heal_detail  = f"{best_heal[1]} · {best_heal[2]}" if best_heal[0] else "No healing recorded."
        death_total  = sum(deaths.values())
        death_names  = ", ".join(
            f"{name} x{count}"
            for name, count in list(sorted(deaths.items(), key=lambda item: (-item[1], item[0])))[:3]
        )
        self._set_highlight(self.hit_highlight,   f"{best_hit[0]:,}"  if best_hit[0]  else "—", hit_detail)
        self._set_highlight(self.heal_highlight,  f"{best_heal[0]:,}" if best_heal[0] else "—", heal_detail)
        self._set_highlight(self.death_highlight, str(death_total) if death_total else "0",
                            death_names if death_names else "No deaths recorded.")

        self.table.setRowCount(len(visible))
        for row, (name, s) in enumerate(visible):
            badge_lbl, badge_bg, badge_fg = KIND_BADGE[s.kind]
            row_bg = KIND_ROW_BG[s.kind]

            def cell(txt, bg=row_bg, align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter):
                item = QTableWidgetItem(str(txt))
                item.setTextAlignment(align)
                item.setBackground(QBrush(QColor(bg)))
                return item

            badge_item = QTableWidgetItem(badge_lbl)
            badge_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            badge_item.setForeground(QBrush(QColor(badge_fg)))
            badge_item.setBackground(QBrush(QColor(badge_bg)))
            badge_item.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))

            name_item = QTableWidgetItem(name)
            name_item.setForeground(QBrush(QColor(badge_fg)))
            name_item.setBackground(QBrush(QColor(row_bg)))
            name_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

            self.table.setItem(row, 0,  badge_item)
            self.table.setItem(row, 1,  name_item)
            self.table.setItem(row, 2,  cell(f"{s.damage_dealt:,}"))
            self.table.setItem(row, 3,  cell(f"{fight.dps(name):,.1f}"))
            self.table.setItem(row, 4,  cell(f"{fight.active_dps(name):,.1f}"))
            self.table.setItem(row, 5,  cell(f"{fight.boss_dps(name):,.1f}"))
            self.table.setItem(row, 6,  cell(f"{s.crit_rate:.0%}"))
            self.table.setItem(row, 7,  cell(f"{s.damage_taken:,}"))
            self.table.setItem(row, 8,  cell(f"{fight.hps(name):,.1f}"))
            self.table.setItem(row, 9,  cell(f"{s.healing_done:,}"))
            self.table.setItem(row, 10, cell(f"{s.damage_absorbed:,}"))

        self.table.resizeRowsToContents()
