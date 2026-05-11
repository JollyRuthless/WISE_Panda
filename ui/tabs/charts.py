"""
ui/tabs/charts.py — ChartsTab: rolling DPS/HPS/damage-taken line charts using pyqtgraph.
"""

from typing import Optional

import numpy as np
import pyqtgraph as pg

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox
from PyQt6.QtCore import Qt

from engine.aggregator import Fight, EntityKind, elapsed_seconds
from ui.theme import BG_PANEL, BG_WIDGET, TEXT_SEC, BORDER, ENTITY_COLORS


class ChartsTab(QWidget):
    def __init__(self):
        super().__init__()
        self._fight: Optional[Fight] = None
        self._hide_companions = False
        self._build_ui()

    def _build_ui(self):
        pg.setConfigOptions(antialias=True, background=BG_PANEL, foreground=TEXT_SEC)

        root = QVBoxLayout(self)
        root.setSpacing(8)

        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("View:"))
        self.combo = QComboBox()
        self.combo.addItems(["Damage per Second", "Healing per Second", "Damage Taken"])
        self.combo.currentIndexChanged.connect(self._redraw)
        ctrl.addWidget(self.combo)
        ctrl.addStretch()
        root.addLayout(ctrl)

        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setLabel("left",   "Amount / s")
        self.plot_widget.setLabel("bottom", "Time (s)")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.2)
        self.plot_widget.getAxis("left").setPen(pg.mkPen(color=BORDER))
        self.plot_widget.getAxis("bottom").setPen(pg.mkPen(color=BORDER))
        root.addWidget(self.plot_widget)

    def load_fight(self, fight: Fight, hide_companions: bool = False, hide_npcs: bool = False):
        self._fight = fight
        self._hide_companions = hide_companions
        self._redraw()

    def _redraw(self):
        if not self._fight:
            return
        self.plot_widget.clear()
        fight = self._fight
        mode  = self.combo.currentIndex()  # 0=DPS  1=HPS  2=taken

        legend = self.plot_widget.addLegend(offset=(10, 10))
        legend.setBrush(pg.mkBrush(BG_WIDGET))
        legend.setPen(pg.mkPen(BORDER))

        WINDOW = 3.0  # rolling window in seconds

        for idx, (name, stats) in enumerate(fight.entity_stats.items()):
            if self._hide_companions and stats.kind == EntityKind.COMPANION:
                continue
            color = ENTITY_COLORS[idx % len(ENTITY_COLORS)]

            if mode == 0:
                timeline = stats.damage_timeline
            elif mode == 1:
                timeline = stats.heal_timeline
            else:
                # Build damage-taken timeline for this entity as target
                timeline = []
                for ev in fight.events:
                    if (ev.is_damage and ev.result and not ev.result.is_miss
                            and ev.target.display_name == name):
                        t = elapsed_seconds(fight.start_time, ev.timestamp)
                        timeline.append((t, ev.result.amount))

            if not timeline:
                continue

            times   = [t for t, _ in timeline]
            amounts = [a for _, a in timeline]
            dur = fight.duration_seconds
            x = np.linspace(0, dur, max(int(dur * 4), 50))
            y = np.zeros(len(x))
            for i, xi in enumerate(x):
                w_start = xi - WINDOW
                total = sum(a for t, a in zip(times, amounts) if w_start <= t <= xi)
                y[i] = total / WINDOW

            self.plot_widget.plot(x, y, pen=pg.mkPen(color=color, width=2), name=name)
