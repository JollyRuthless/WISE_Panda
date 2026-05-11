"""
ui/tabs/timeline.py — TimelineTab: cumulative damage chart + scrollable event log.
"""

from typing import Optional

import numpy as np
import pyqtgraph as pg

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem, QGroupBox, QHeaderView,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QBrush

from engine.aggregator import Fight, EntityKind, elapsed_seconds
from ui.theme import BG_PANEL, BG_WIDGET, TEXT_SEC, BORDER, ACCENT3, ACCENT4, ENTITY_COLORS


class TimelineTab(QWidget):
    def __init__(self):
        super().__init__()
        self._fight: Optional[Fight] = None
        self._build_ui()

    def _build_ui(self):
        pg.setConfigOptions(antialias=True, background=BG_PANEL, foreground=TEXT_SEC)

        root = QVBoxLayout(self)
        root.setSpacing(8)

        # ── Cumulative damage chart ───────────────────────────────────────
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setLabel("left",   "Cumulative Damage")
        self.plot_widget.setLabel("bottom", "Time (s)")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.2)
        root.addWidget(self.plot_widget, stretch=1)

        # ── Event log ─────────────────────────────────────────────────────
        grp = QGroupBox("Event Log")
        grp_lay = QVBoxLayout(grp)
        self.event_table = QTableWidget()
        self.event_table.setColumnCount(5)
        self.event_table.setHorizontalHeaderLabels(["Time", "Source", "Target", "Ability", "Amount"])
        self.event_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.event_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.event_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.event_table.verticalHeader().setVisible(False)
        grp_lay.addWidget(self.event_table)
        root.addWidget(grp, stretch=1)

    # ── Public API ────────────────────────────────────────────────────────────

    def load_fight(self, fight: Fight, hide_companions: bool = False, hide_npcs: bool = False):
        self._fight = fight
        self.plot_widget.clear()

        legend = self.plot_widget.addLegend(offset=(10, 10))
        legend.setBrush(pg.mkBrush(BG_WIDGET))
        legend.setPen(pg.mkPen(BORDER))

        for idx, (name, stats) in enumerate(fight.entity_stats.items()):
            if hide_companions and stats.kind == EntityKind.COMPANION:
                continue
            if not stats.damage_timeline:
                continue
            color  = ENTITY_COLORS[idx % len(ENTITY_COLORS)]
            times  = [t for t, _ in stats.damage_timeline]
            amounts = [a for _, a in stats.damage_timeline]
            cum    = np.cumsum(amounts)
            self.plot_widget.plot(times, cum, pen=pg.mkPen(color=color, width=2), name=name)

        # ── Event log ─────────────────────────────────────────────────────
        dmg_events = [
            ev for ev in fight.events
            if (ev.is_damage or ev.is_heal) and ev.result
            and not (ev.is_damage and ev.result.is_miss)
        ]
        if hide_companions:
            dmg_events = [ev for ev in dmg_events
                          if not ev.source.companion and not ev.target.companion]
        dmg_events.sort(key=lambda e: e.timestamp)

        self.event_table.setRowCount(len(dmg_events))
        for row, ev in enumerate(dmg_events):
            t_off   = elapsed_seconds(fight.start_time, ev.timestamp)
            is_heal = ev.is_heal
            color   = ACCENT4 if is_heal else ACCENT3

            def cell(txt, clr=None, align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter):
                item = QTableWidgetItem(str(txt))
                item.setTextAlignment(align)
                if clr:
                    item.setForeground(QBrush(QColor(clr)))
                return item

            src    = ev.source.display_name
            tgt    = ev.target.display_name
            if tgt == "self":
                tgt = src
            ab     = ev.ability.name if ev.ability else "?"
            amt    = ev.result.amount if ev.result else 0
            suffix = "*" if (ev.result and ev.result.is_crit) else ""

            left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            self.event_table.setItem(row, 0, cell(f"{t_off:.2f}s", align=left))
            self.event_table.setItem(row, 1, cell(src,             align=left))
            self.event_table.setItem(row, 2, cell(tgt,             align=left))
            self.event_table.setItem(row, 3, cell(ab,              align=left))
            self.event_table.setItem(row, 4, cell(f"{amt:,}{suffix}", clr=color))

        self.event_table.resizeRowsToContents()
