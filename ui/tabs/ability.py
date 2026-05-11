"""
ui/tabs/ability.py — AbilityTab: per-player ability breakdown + cross-player compare panel.
"""

from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QTableWidget, QTableWidgetItem, QLabel, QComboBox,
    QGroupBox, QHeaderView, QSizePolicy,
)
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QColor, QFont, QBrush, QIcon, QPixmap

from engine.aggregator import Fight, EntityStats, AbilityStats, EntityKind
from engine.analysis import build_rotation
from ui.theme import BG_WIDGET, TEXT_PRI, TEXT_SEC, KIND_BADGE
from engine.ability_icons import get_ability_icon_library

_ABILITY_ICONS = get_ability_icon_library()
_ABILITY_QICONS: dict[str, QIcon] = {}

_ICON_SIZE = QSize(24, 24)


def _get_qicon(ability_name: str, ability_id: str = "") -> QIcon | None:
    """Look up a cached QIcon for the given ability, loading from disk if needed."""
    cache_key = ability_id or ability_name
    if cache_key in _ABILITY_QICONS:
        return _ABILITY_QICONS[cache_key]
    path = _ABILITY_ICONS.icon_path(ability_name=ability_name, ability_id=ability_id)
    if path is None:
        return None
    pixmap = QPixmap(str(path))
    if pixmap.isNull():
        return None
    icon = QIcon(pixmap.scaled(_ICON_SIZE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
    _ABILITY_QICONS[cache_key] = icon
    return icon


class AbilityTab(QWidget):
    def __init__(self):
        super().__init__()
        self._fight: Optional[Fight] = None
        self._hide_companions = False
        self._hide_npcs = False
        self._grouped_abilities: dict[str, list[tuple[str, EntityStats, AbilityStats]]] = {}
        self._use_counts_by_player: dict[str, dict[str, int]] = {}
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(8, 8, 8, 8)

        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Player:"))
        self.entity_combo = QComboBox()
        self.entity_combo.currentTextChanged.connect(self._load_table)
        ctrl.addWidget(self.entity_combo)
        ctrl.addSpacing(12)
        ctrl.addWidget(QLabel("Type:"))
        self.type_combo = QComboBox()
        self.type_combo.addItems(["Damage", "Healing"])
        self.type_combo.currentIndexChanged.connect(self._load_table)
        ctrl.addWidget(self.type_combo)
        ctrl.addSpacing(12)
        self.summary_label = QLabel("")
        self.summary_label.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        ctrl.addWidget(self.summary_label)
        ctrl.addStretch()
        root.addLayout(ctrl)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # ── Left: selected player abilities ───────────────────────────────
        left = QGroupBox("Selected Player Abilities")
        left.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(8, 8, 8, 8)
        self.table = QTableWidget()
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels(["Ability", "Shared", "Uses", "Hits", "Crits", "Crit%", "Total", "Max Hit"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.setIconSize(_ICON_SIZE)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.table.setStyleSheet(f"QTableWidget {{ alternate-background-color: {BG_WIDGET}; }}")
        self.table.itemSelectionChanged.connect(self._load_selected_ability)
        left_lay.addWidget(self.table, 1)
        splitter.addWidget(left)

        # ── Right: cross-player compare ───────────────────────────────────
        right = QGroupBox("Cross-Player Ability Compare")
        right.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(8, 8, 8, 8)
        self.detail_label = QLabel("Select a highlighted shared ability to compare all players who used it.")
        self.detail_label.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        right_lay.addWidget(self.detail_label)
        self.detail_table = QTableWidget()
        self.detail_table.setColumnCount(9)
        self.detail_table.setHorizontalHeaderLabels(["Player", "Type", "Uses", "Hits", "Crits", "Crit%", "Total", "Avg Hit", "Max Hit"])
        self.detail_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.detail_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.detail_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.detail_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.detail_table.verticalHeader().setVisible(False)
        self.detail_table.setAlternatingRowColors(True)
        self.detail_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.detail_table.setStyleSheet(f"QTableWidget {{ alternate-background-color: {BG_WIDGET}; }}")
        right_lay.addWidget(self.detail_table, 1)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([700, 520])
        root.addWidget(splitter, 1)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _cell(txt, align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
               fg: Optional[str] = None, bg: Optional[str] = None, bold: bool = False):
        item = QTableWidgetItem(str(txt))
        item.setTextAlignment(align)
        if fg:   item.setForeground(QBrush(QColor(fg)))
        if bg:   item.setBackground(QBrush(QColor(bg)))
        if bold: item.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        return item

    @staticmethod
    def _ability_cell(ability_name: str, fg: Optional[str] = None, bg: Optional[str] = None):
        return AbilityTab._cell(
            ability_name,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            fg=fg,
            bg=bg,
            bold=True,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def load_fight(self, fight: Fight, hide_companions: bool = False, hide_npcs: bool = False):
        self._fight = fight
        self._hide_companions = hide_companions
        self._hide_npcs = hide_npcs
        self.entity_combo.blockSignals(True)
        self.entity_combo.clear()
        for name in sorted(fight.entity_stats.keys()):
            if hide_companions and fight.entity_stats[name].kind == EntityKind.COMPANION:
                continue
            if hide_npcs and fight.entity_stats[name].kind == EntityKind.NPC:
                continue
            self.entity_combo.addItem(name)
        self.entity_combo.blockSignals(False)
        self._load_table()

    def _load_table(self):
        if not self._fight:
            return
        mode          = self.type_combo.currentIndex()  # 0=damage 1=heal
        selected_name = self.entity_combo.currentText()
        if not selected_name:
            self.table.setRowCount(0)
            self.detail_table.setRowCount(0)
            self.summary_label.setText("No player selected.")
            return

        entity_rows = []
        for name, stats in self._fight.entity_stats.items():
            if self._hide_companions and stats.kind == EntityKind.COMPANION:
                continue
            if self._hide_npcs and stats.kind == EntityKind.NPC:
                continue
            abilities = stats.abilities_damage if mode == 0 else stats.abilities_heal
            if abilities:
                entity_rows.append((name, stats, abilities))

        grouped: dict[str, list] = {}
        for name, stats, abilities in entity_rows:
            for ability_name, ab in abilities.items():
                grouped.setdefault(ability_name, []).append((name, stats, ab))
        self._grouped_abilities = grouped

        use_counts_by_player: dict[str, dict[str, int]] = {}
        for name, _stats, _abilities in entity_rows:
            rotation = build_rotation(self._fight, name)
            counts: dict[str, int] = {}
            for entry in rotation:
                counts[entry.ability_name] = counts.get(entry.ability_name, 0) + 1
            use_counts_by_player[name] = counts
        self._use_counts_by_player = use_counts_by_player

        selected_stats     = self._fight.entity_stats.get(selected_name)
        selected_abilities = (
            selected_stats.abilities_damage if (selected_stats and mode == 0)
            else selected_stats.abilities_heal if selected_stats
            else {}
        )
        rows = sorted(selected_abilities.values(), key=lambda ab: -ab.total_amount)
        player_total = sum(ab.total_amount for ab in rows)
        self.summary_label.setText(
            f"{selected_name}  •  {len(rows):,} abilities"
            f"  •  Total {'damage' if mode == 0 else 'healing'}: {player_total:,}"
        )

        cell = self._cell
        self.table.setRowCount(len(rows))
        for row, ab in enumerate(rows):
            players   = sorted(self._grouped_abilities.get(ab.name, []), key=lambda item: -item[2].total_amount)
            is_shared = any(pn != selected_name for pn, _, _ in players)
            shared_fg = "#c9a9ff" if is_shared else TEXT_SEC
            shared_bg = "#221735" if is_shared else None

            ability_cell = self._ability_cell(ab.name, fg=TEXT_PRI)
            icon = _get_qicon(ab.name)
            if icon:
                ability_cell.setIcon(icon)
            self.table.setItem(row, 0, ability_cell)
            self.table.setItem(row, 1, cell("Compare" if is_shared else "Solo", Qt.AlignmentFlag.AlignCenter, fg=shared_fg, bg=shared_bg, bold=is_shared))
            uses = use_counts_by_player.get(selected_name, {}).get(ab.name, 0)
            self.table.setItem(row, 2, cell(uses))
            self.table.setItem(row, 3, cell(ab.hits))
            self.table.setItem(row, 4, cell(ab.crits))
            self.table.setItem(row, 5, cell(f"{ab.crit_rate:.0%}"))
            self.table.setItem(row, 6, cell(f"{ab.total_amount:,}"))
            self.table.setItem(row, 7, cell(f"{ab.max_hit:,}"))

        self.table.resizeRowsToContents()
        if rows:
            self.table.selectRow(0)
            self._load_selected_ability()
        else:
            self.detail_label.setText("No ability data available for this player.")
            self.detail_table.setRowCount(0)

    def _load_selected_ability(self):
        if not self._fight:
            return
        selected = self.table.selectionModel().selectedRows()
        if not selected:
            self.detail_label.setText("Select an ability to see which players contributed.")
            self.detail_table.setRowCount(0)
            return

        ability_name  = self.table.item(selected[0].row(), 0).text()
        players       = sorted(self._grouped_abilities.get(ability_name, []), key=lambda item: -item[2].total_amount)
        selected_name = self.entity_combo.currentText()
        shared_players = [item for item in players if item[0] != selected_name]
        if not shared_players:
            self.detail_label.setText(f"{ability_name} is only used by {selected_name} in this encounter.")
            self.detail_table.setRowCount(0)
            return

        self.detail_label.setText(f"{ability_name}  •  {len(players)} players used this ability")
        self.detail_table.setRowCount(len(players))

        cell = self._cell
        for row, (player_name, stats, ab) in enumerate(players):
            badge_lbl, _badge_bg, badge_fg = KIND_BADGE.get(stats.kind, KIND_BADGE[EntityKind.NPC])
            is_selected = player_name == selected_name
            self.detail_table.setItem(row, 0, cell(player_name, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, fg=badge_fg, bold=is_selected))
            self.detail_table.setItem(row, 1, cell(badge_lbl,   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, fg=badge_fg, bold=is_selected))
            uses = self._use_counts_by_player.get(player_name, {}).get(ability_name, 0)
            self.detail_table.setItem(row, 2, cell(uses))
            self.detail_table.setItem(row, 3, cell(ab.hits))
            self.detail_table.setItem(row, 4, cell(ab.crits))
            self.detail_table.setItem(row, 5, cell(f"{ab.crit_rate:.0%}"))
            self.detail_table.setItem(row, 6, cell(f"{ab.total_amount:,}"))
            self.detail_table.setItem(row, 7, cell(f"{ab.avg_hit:,.0f}"))
            self.detail_table.setItem(row, 8, cell(f"{ab.max_hit:,}"))
        self.detail_table.resizeRowsToContents()
