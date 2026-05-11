"""
W.I.S.E. Panda — Training UI Tabs
Rotation Tracker, DPS Metrics, Tank Metrics, Healer Metrics, Comparison
"""

from typing import List, Optional, Dict
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QFrame,
    QGroupBox, QScrollArea, QSizePolicy, QSplitter, QCheckBox,
)
from PyQt6.QtCore import Qt, QRectF, QPointF, QSize
from PyQt6.QtGui import (
    QColor, QFont, QBrush, QPainter, QPen, QFontMetrics, QIcon, QPixmap,
)

from engine.aggregator import Fight, EntityKind
from engine.analysis import (
    build_rotation, detect_role, analyse_dps, analyse_tank, analyse_healer,
    compare_entities,
    RotationEntry, DpsMetrics, TankMetrics, HealerMetrics, get_db,
    ComparisonResult, AbilityComparison, MetricComparison, ComparisonInsight,
    DEAD_TIME_WARN,
)

# ── Theme constants (mirror main.py) ─────────────────────────────────────────
BG_DARK   = "#0e1117"
BG_PANEL  = "#161b22"
BG_WIDGET = "#1c2128"
ACCENT    = "#58a6ff"
ACCENT2   = "#3fb950"
ACCENT3   = "#f78166"
ACCENT4   = "#d2a8ff"
TEXT_PRI  = "#e6edf3"
TEXT_SEC  = "#8b949e"
BORDER    = "#30363d"
# Ability icon rendering is disabled while we verify app performance.
# from ability_icons import get_ability_icon_library
# _ABILITY_ICONS = get_ability_icon_library()
# _ABILITY_QICONS: dict[str, QIcon] = {}

from engine.ability_icons import get_ability_icon_library
_ABILITY_ICONS = get_ability_icon_library()
_ABILITY_QICONS: dict[str, QIcon] = {}
_ROT_ICON_SIZE = QSize(24, 24)


def _get_qicon(ability_name: str) -> QIcon | None:
    if ability_name in _ABILITY_QICONS:
        return _ABILITY_QICONS[ability_name]
    path = _ABILITY_ICONS.icon_path(ability_name=ability_name)
    if path is None:
        return None
    pixmap = QPixmap(str(path))
    if pixmap.isNull():
        return None
    icon = QIcon(pixmap.scaled(_ROT_ICON_SIZE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
    _ABILITY_QICONS[ability_name] = icon
    return icon

KIND_BADGE = {
    EntityKind.PLAYER:       ("YOU",   "#1158c7", "#58a6ff"),
    EntityKind.GROUP_MEMBER: ("GROUP", "#2f1f4d", "#c9a9ff"),
    EntityKind.COMPANION:    ("COMP",  "#2d4a1e", "#3fb950"),
    EntityKind.NPC:          ("NPC",   "#3d1f1f", "#f78166"),
    EntityKind.HAZARD:       ("HAZ",   "#3a2d00", "#ffa657"),
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cell(txt, align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
          color=None, bold=False):
    item = QTableWidgetItem(str(txt))
    item.setTextAlignment(align)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    if color:
        item.setForeground(QBrush(QColor(color)))
    if bold:
        f = QFont("Segoe UI", 10, QFont.Weight.Bold)
        item.setFont(f)
    return item


def _lcell(txt, color=None, bold=False):
    return _cell(txt, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                 color, bold)


def _ability_lcell(ability_name: str, ability_id: str = "", color=None, bold=True):
    item = _lcell(ability_name, color=color, bold=bold)
    icon = _get_qicon(ability_name)
    if icon:
        item.setIcon(icon)
    return item


def _make_table(headers: list, stretch_col: int = 0) -> QTableWidget:
    t = QTableWidget()
    t.setColumnCount(len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.horizontalHeader().setSectionResizeMode(stretch_col, QHeaderView.ResizeMode.Stretch)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    t.horizontalHeader().setSectionResizeMode(stretch_col, QHeaderView.ResizeMode.Stretch)
    t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    t.verticalHeader().setVisible(False)
    t.setAlternatingRowColors(True)
    t.setStyleSheet(f"QTableWidget {{ alternate-background-color: {BG_WIDGET}; }}")
    return t


# ── Multi-track rotation timeline widget ─────────────────────────────────────

# Per-track accent colours (distinct from ability type colours)
TRACK_ACCENTS = [
    "#58a6ff",   # blue
    "#3fb950",   # green
    "#f78166",   # orange
    "#d2a8ff",   # purple
]

TRACK_LABEL_BG = [
    "#0d1f33",
    "#0d2010",
    "#2a1510",
    "#1a0d2a",
]


class MultiRotationTimeline(QWidget):
    """
    Stacked horizontal timeline — up to 4 entity tracks rendered like
    video-editor lanes.  Shared time ruler along the top.
    """
    TRACK_H      = 52
    RULER_H      = 22
    LABEL_W      = 120
    PADDING      = 8
    MIN_PILL_W   = 32

    TYPE_COLORS = {
        "damage":    ("#1f4a7a", "#58a6ff"),
        "heal":      ("#1a3d1a", "#3fb950"),
        "buff":      ("#3a2d00", "#ffa657"),
        "debuff":    ("#3a1a00", "#f78166"),
        "taunt":     ("#2a0d3a", "#d2a8ff"),
        "interrupt": ("#0d2a2a", "#79c0ff"),
        "cooldown":  ("#3a1a1a", "#ff7b72"),
        "unknown":   ("#1c2128", "#8b949e"),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        # List of (entity_name, rotation, track_index)
        self._tracks: List[tuple] = []
        self._duration: float = 1.0
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._update_height()

    def load(self, tracks: List[tuple], duration: float):
        """
        tracks: list of (entity_name: str, rotation: List[RotationEntry])
        """
        self._tracks = tracks
        self._duration = max(duration, 1.0)
        self._update_height()
        self.update()

    def _update_height(self):
        n = max(len(self._tracks), 1)
        h = self.RULER_H + n * self.TRACK_H + self.PADDING * 2
        self.setMinimumHeight(h)
        self.setFixedHeight(h)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        dur = self._duration

        # Full background
        p.fillRect(self.rect(), QColor(BG_PANEL))

        track_area_x = self.LABEL_W + self.PADDING
        track_area_w = w - track_area_x - self.PADDING
        if track_area_w < 50:
            p.end()
            return
        px_per_sec = track_area_w / dur

        # ── Time ruler ───────────────────────────────────────────────────────
        ruler_y = self.PADDING
        p.setPen(QColor(BORDER))
        p.drawLine(track_area_x, ruler_y + self.RULER_H - 1,
                   w - self.PADDING, ruler_y + self.RULER_H - 1)

        font_ruler = QFont("Segoe UI", 8)
        p.setFont(font_ruler)
        p.setPen(QColor(TEXT_SEC))

        # Tick marks every N seconds (adaptive)
        if dur <= 30:
            tick_step = 5
        elif dur <= 120:
            tick_step = 10
        elif dur <= 300:
            tick_step = 30
        else:
            tick_step = 60

        t = 0
        while t <= dur:
            x = track_area_x + t * px_per_sec
            p.setPen(QColor(BORDER))
            p.drawLine(int(x), ruler_y + self.RULER_H - 6,
                       int(x), ruler_y + self.RULER_H - 1)
            p.setPen(QColor(TEXT_SEC))
            lbl = f"{int(t // 60)}:{int(t % 60):02d}" if t >= 60 else f"{int(t)}s"
            p.drawText(int(x) - 20, ruler_y, 40, self.RULER_H - 6,
                       Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignBottom, lbl)
            t += tick_step

        if not self._tracks:
            p.setPen(QColor(TEXT_SEC))
            p.setFont(QFont("Segoe UI", 11))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "Select entities above to see rotations")
            p.end()
            return

        font_name  = QFont("Segoe UI", 8, QFont.Weight.Bold)
        font_small = QFont("Segoe UI", 7)
        font_label = QFont("Segoe UI", 9, QFont.Weight.Bold)

        for track_idx, (entity_name, rotation) in enumerate(self._tracks):
            track_y = self.PADDING + self.RULER_H + track_idx * self.TRACK_H
            accent  = TRACK_ACCENTS[track_idx % len(TRACK_ACCENTS)]
            label_bg = TRACK_LABEL_BG[track_idx % len(TRACK_LABEL_BG)]

            # Track background (alternating slight shade)
            bg_shade = QColor(BG_PANEL if track_idx % 2 == 0 else BG_WIDGET)
            bg_shade.setAlpha(180)
            p.fillRect(self.PADDING, track_y, w - self.PADDING * 2, self.TRACK_H, bg_shade)

            # Thin accent line on left edge
            p.fillRect(self.PADDING, track_y, 3, self.TRACK_H, QColor(accent))

            # Entity name label area
            p.fillRect(self.PADDING + 3, track_y, self.LABEL_W - 3, self.TRACK_H,
                       QColor(label_bg))
            p.setPen(QColor(accent))
            p.setFont(font_label)
            fm = QFontMetrics(font_label)
            elided_name = fm.elidedText(entity_name,
                                        Qt.TextElideMode.ElideRight,
                                        self.LABEL_W - 14)
            p.drawText(self.PADDING + 8, track_y,
                       self.LABEL_W - 12, self.TRACK_H,
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       elided_name)

            # Separator line below label
            p.setPen(QColor(BORDER))
            p.drawLine(self.PADDING, track_y + self.TRACK_H - 1,
                       w - self.PADDING, track_y + self.TRACK_H - 1)

            # ── Draw pills for this track ────────────────────────────────────
            pill_y = track_y + 4
            pill_h = self.TRACK_H - 8

            for i, entry in enumerate(rotation):
                x1 = track_area_x + entry.t_offset * px_per_sec

                pill_w = max(self.MIN_PILL_W,
                             min(entry.gap_after, 2.0) * px_per_sec - 2
                             if entry.gap_after > 0 else self.MIN_PILL_W)

                # Dead-time gap highlight
                if entry.gap_before > DEAD_TIME_WARN and i > 0:
                    gap_x = track_area_x + (entry.t_offset - entry.gap_before) * px_per_sec
                    gap_w = entry.gap_before * px_per_sec
                    p.fillRect(int(gap_x), pill_y, int(gap_w), pill_h,
                               QColor(60, 20, 20, 100))
                    p.setPen(QColor("#f78166"))
                    p.setFont(font_small)
                    if gap_w > 28:
                        p.drawText(int(gap_x), pill_y, int(gap_w), pill_h,
                                   Qt.AlignmentFlag.AlignCenter,
                                   f"{entry.gap_before:.1f}s")

                # Pill colour from ability type
                ab_type = entry.ability_info.type
                bg_hex, fg_hex = self.TYPE_COLORS.get(ab_type, self.TYPE_COLORS["unknown"])

                rect = QRectF(x1, pill_y, pill_w, pill_h)
                p.setBrush(QColor(bg_hex))
                p.setPen(QPen(QColor(fg_hex), 1))
                p.drawRoundedRect(rect, 4, 4)

                # Ability name
                p.setPen(QColor(fg_hex))
                p.setFont(font_name)
                fm2 = QFontMetrics(font_name)
                ab_label = fm2.elidedText(entry.ability_name,
                                          Qt.TextElideMode.ElideRight,
                                          int(pill_w) - 6)
                p.drawText(int(x1) + 3, pill_y,
                           int(pill_w) - 6, pill_h // 2,
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                           ab_label)

                # Result on second line
                p.setFont(font_small)
                p.setPen(QColor(TEXT_SEC))
                p.drawText(int(x1) + 3, pill_y + pill_h // 2,
                           int(pill_w) - 6, pill_h // 2,
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                           entry.result_str)

        p.end()


# ── Rotation tab (multi-track) ───────────────────────────────────────────────

MAX_TRACKS = 4


class RotationTab(QWidget):
    def __init__(self):
        super().__init__()
        self._fight: Optional[Fight] = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        # ── Entity selector row: up to 4 checkable combos ────────────────────
        ctrl = QHBoxLayout()
        ctrl.setSpacing(6)

        ctrl.addWidget(QLabel("Entities:"))

        self._track_checks: List[QCheckBox] = []
        self._track_combos: List[QComboBox]  = []

        for i in range(MAX_TRACKS):
            accent = TRACK_ACCENTS[i]

            chk = QCheckBox()
            chk.setChecked(i == 0)  # first track on by default
            chk.setStyleSheet(
                f"QCheckBox::indicator {{width:14px;height:14px;}}"
                f"QCheckBox::indicator:checked {{background:{accent};border:1px solid {accent};border-radius:3px;}}"
                f"QCheckBox::indicator:unchecked {{background:{BG_WIDGET};border:1px solid {BORDER};border-radius:3px;}}"
            )
            chk.stateChanged.connect(self._on_selection_changed)
            self._track_checks.append(chk)

            combo = QComboBox()
            combo.setMinimumWidth(130)
            combo.setStyleSheet(
                f"QComboBox {{border-left:3px solid {accent};}}"
            )
            combo.currentIndexChanged.connect(self._on_selection_changed)
            self._track_combos.append(combo)

            ctrl.addWidget(chk)
            ctrl.addWidget(combo)
            if i < MAX_TRACKS - 1:
                ctrl.addSpacing(6)

        ctrl.addStretch()

        # Legend
        for label, (bg, fg) in [
            ("Damage", ("#1f4a7a", "#58a6ff")),
            ("Heal",   ("#1a3d1a", "#3fb950")),
            ("Taunt",  ("#2a0d3a", "#d2a8ff")),
            ("Interr", ("#0d2a2a", "#79c0ff")),
            ("CD",     ("#3a1a1a", "#ff7b72")),
            ("Gap ⚠",  ("#3c1414", "#f78166")),
        ]:
            lbl = QLabel(f" {label} ")
            lbl.setStyleSheet(
                f"background:{bg}; color:{fg}; font-size:10px; font-weight:700;"
                f"border-radius:3px; padding:2px 5px;"
            )
            ctrl.addWidget(lbl)

        root.addLayout(ctrl)

        # ── Multi-track timeline ─────────────────────────────────────────────
        timeline_grp = QGroupBox("Ability Timeline  (select up to 4 entities)")
        tl_lay = QVBoxLayout(timeline_grp)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setMinimumHeight(100)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.timeline = MultiRotationTimeline()
        self.timeline.setMinimumWidth(1000)
        self.scroll.setWidget(self.timeline)
        tl_lay.addWidget(self.scroll)
        root.addWidget(timeline_grp)

        # ── Ability press log (shows selected / first checked entity) ────────
        list_grp = QGroupBox("Ability Press Log  (primary entity)")
        list_lay = QVBoxLayout(list_grp)

        self.log_entity_label = QLabel("")
        self.log_entity_label.setStyleSheet(
            f"color:{ACCENT}; font-size:11px; font-weight:600; padding:2px 0;"
        )
        list_lay.addWidget(self.log_entity_label)

        self.table = _make_table(
            ["#", "Time", "Ability", "Type", "Result", "Gap Before", "Flag"],
            stretch_col=2
        )
        self.table.setColumnWidth(0, 36)
        self.table.setIconSize(_ROT_ICON_SIZE)
        list_lay.addWidget(self.table)
        root.addWidget(list_grp)

        # ── Stats summary row ────────────────────────────────────────────────
        stats_row = QHBoxLayout()
        self.lbl_presses  = self._stat_lbl("Ability Presses", "—")
        self.lbl_dead_pct = self._stat_lbl("Dead Time", "—")
        self.lbl_dead_s   = self._stat_lbl("Total Dead (s)", "—")
        self.lbl_longest  = self._stat_lbl("Longest Gap", "—")
        self.lbl_crit     = self._stat_lbl("Crit Rate", "—")
        for w in (self.lbl_presses, self.lbl_dead_pct, self.lbl_dead_s,
                  self.lbl_longest, self.lbl_crit):
            stats_row.addWidget(w)
        root.addLayout(stats_row)

    def _stat_lbl(self, label: str, value: str) -> QFrame:
        f = QFrame()
        f.setStyleSheet(
            f"QFrame{{background:{BG_WIDGET};border:1px solid {BORDER};"
            f"border-radius:6px;padding:6px;}}"
        )
        lay = QVBoxLayout(f)
        lay.setSpacing(2)
        val_w = QLabel(value)
        val_w.setStyleSheet(f"color:{TEXT_PRI};font-size:16px;font-weight:700;")
        lbl_w = QLabel(label)
        lbl_w.setStyleSheet(f"color:{TEXT_SEC};font-size:10px;")
        lay.addWidget(val_w)
        lay.addWidget(lbl_w)
        f._val_lbl = val_w
        f._key     = label
        return f

    def _set_stat(self, frame: QFrame, val: str):
        frame._val_lbl.setText(val)

    # ── Load fight ───────────────────────────────────────────────────────────
    def load_fight(self, fight: Fight, hide_companions: bool = False, hide_npcs: bool = False):
        self._fight = fight
        self._hide_companions = hide_companions
        names = sorted(fight.entity_stats.keys())
        if hide_companions:
            names = [n for n in names
                     if fight.entity_stats[n].kind != EntityKind.COMPANION]
        if hide_npcs:
            names = [n for n in names
                     if fight.entity_stats[n].kind != EntityKind.NPC]

        # Block signals while repopulating combos
        for combo in self._track_combos:
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("(none)")
            for n in names:
                combo.addItem(n)
            combo.blockSignals(False)

        # Auto-assign first few entities to tracks
        for i in range(MAX_TRACKS):
            if i < len(names):
                self._track_combos[i].setCurrentIndex(i + 1)  # +1 for "(none)"
                self._track_checks[i].setChecked(True)
            else:
                self._track_combos[i].setCurrentIndex(0)
                self._track_checks[i].setChecked(False)

        # Prefer players/group by default
        pc_names = [n for n in names
                    if fight.entity_stats[n].kind in
                    (EntityKind.PLAYER, EntityKind.GROUP_MEMBER)]
        if not hide_companions:
            pc_names = [n for n in names
                        if fight.entity_stats[n].kind in
                        (EntityKind.PLAYER, EntityKind.GROUP_MEMBER, EntityKind.COMPANION)]
        for i in range(MAX_TRACKS):
            if i < len(pc_names):
                # Find this name in combo
                idx = self._track_combos[i].findText(pc_names[i])
                if idx >= 0:
                    self._track_combos[i].setCurrentIndex(idx)
                self._track_checks[i].setChecked(True)
            elif i < len(names):
                # Fall back to any entity
                self._track_combos[i].setCurrentIndex(i + 1)
                self._track_checks[i].setChecked(i == 0)
            else:
                self._track_combos[i].setCurrentIndex(0)
                self._track_checks[i].setChecked(False)

        self._on_selection_changed()

    # ── Selection changed → rebuild tracks ───────────────────────────────────
    def _on_selection_changed(self, _=None):
        if not self._fight:
            return

        # Gather active tracks
        active_tracks: List[tuple] = []
        primary_name = None

        for i in range(MAX_TRACKS):
            if not self._track_checks[i].isChecked():
                continue
            name = self._track_combos[i].currentText()
            if not name or name == "(none)":
                continue
            rotation = build_rotation(self._fight, name)
            active_tracks.append((name, rotation))
            if primary_name is None:
                primary_name = name

        # Update timeline
        self.timeline.load(active_tracks, self._fight.duration_seconds)

        # Update scroll area height to fit tracks
        self.scroll.setFixedHeight(
            max(100, self.timeline.RULER_H + len(active_tracks) * self.timeline.TRACK_H
                + self.timeline.PADDING * 2 + 4)
        )

        # Update ability log table for primary entity
        if primary_name:
            self._load_entity_log(primary_name)
        else:
            self.table.setRowCount(0)
            self.log_entity_label.setText("")
            for w in (self.lbl_presses, self.lbl_dead_pct, self.lbl_dead_s,
                      self.lbl_longest, self.lbl_crit):
                self._set_stat(w, "—")

    # ── Load ability press log for one entity ────────────────────────────────
    def _load_entity_log(self, name: str):
        if not self._fight:
            return

        self.log_entity_label.setText(f"Showing press log for: {name}")

        rotation = build_rotation(self._fight, name)
        metrics  = analyse_dps(self._fight, name)

        self.table.setRowCount(len(rotation))
        for row, entry in enumerate(rotation):
            flag = ""
            flag_color = None
            if entry.gap_before > DEAD_TIME_WARN:
                flag = f"⚠ {entry.gap_before:.1f}s dead"
                flag_color = ACCENT3

            self.table.setItem(row, 0, _cell(str(row + 1)))
            self.table.setItem(row, 1, _cell(f"{entry.t_offset:.2f}s"))
            self.table.setItem(row, 2, _ability_lcell(entry.ability_name))
            self.table.setItem(row, 3, _lcell(
                entry.ability_info.type, color=TEXT_SEC
            ))
            self.table.setItem(row, 4, _cell(
                entry.result_str,
                color=ACCENT2 if entry.heal else (ACCENT if entry.damage else TEXT_SEC)
            ))
            self.table.setItem(row, 5, _cell(
                f"{entry.gap_before:.2f}s",
                color=ACCENT3 if entry.gap_before > DEAD_TIME_WARN else TEXT_SEC
            ))
            self.table.setItem(row, 6, _lcell(flag, color=flag_color))

        self.table.resizeRowsToContents()

        # Summary stats
        self._set_stat(self.lbl_presses, str(len(rotation)))
        self._set_stat(self.lbl_dead_pct, f"{metrics.dead_time_pct:.0%}")
        self._set_stat(self.lbl_dead_s, f"{metrics.dead_time_total:.1f}s")
        self._set_stat(self.lbl_longest, f"{metrics.longest_gap:.1f}s")
        self._set_stat(self.lbl_crit, f"{metrics.crit_rate:.0%}")


# ── DPS training tab ──────────────────────────────────────────────────────────

class DpsTrainingTab(QWidget):
    def __init__(self):
        super().__init__()
        self._fight: Optional[Fight] = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Entity:"))
        self.entity_combo = QComboBox()
        self.entity_combo.currentTextChanged.connect(self._load_entity)
        ctrl.addWidget(self.entity_combo)
        ctrl.addStretch()
        root.addLayout(ctrl)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: ability usage breakdown
        left = QGroupBox("Ability Breakdown")
        left_lay = QVBoxLayout(left)
        self.ab_table = _make_table(
            ["Ability", "Uses", "Total DMG", "Avg Hit", "% of Total"],
            stretch_col=0
        )
        left_lay.addWidget(self.ab_table)
        splitter.addWidget(left)

        # Right: damage type breakdown + coaching tips
        right = QWidget()
        right_lay = QVBoxLayout(right)

        dmg_grp = QGroupBox("Damage Type Breakdown")
        dmg_lay = QVBoxLayout(dmg_grp)
        self.dmg_type_table = _make_table(["Type", "Amount", "%"], stretch_col=0)
        dmg_lay.addWidget(self.dmg_type_table)
        right_lay.addWidget(dmg_grp)

        tips_grp = QGroupBox("🎯  Coaching Tips")
        tips_lay = QVBoxLayout(tips_grp)
        self.tips_label = QLabel("Select a fight to see tips.")
        self.tips_label.setWordWrap(True)
        self.tips_label.setStyleSheet(
            f"color:{TEXT_PRI}; font-size:12px; padding:8px;"
            f"background:{BG_WIDGET}; border-radius:6px;"
        )
        self.tips_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        tips_lay.addWidget(self.tips_label)
        right_lay.addWidget(tips_grp)
        splitter.addWidget(right)

        root.addWidget(splitter)

    def load_fight(self, fight: Fight, hide_companions: bool = False, hide_npcs: bool = False):
        self._fight = fight
        self.entity_combo.blockSignals(True)
        self.entity_combo.clear()
        for name in sorted(fight.entity_stats.keys()):
            if hide_companions and fight.entity_stats[name].kind == EntityKind.COMPANION:
                continue
            if hide_npcs and fight.entity_stats[name].kind == EntityKind.NPC:
                continue
            self.entity_combo.addItem(name)
        self.entity_combo.blockSignals(False)
        self._load_entity()

    def _load_entity(self):
        if not self._fight:
            return
        name = self.entity_combo.currentText()
        if not name:
            return

        metrics = analyse_dps(self._fight, name)

        # Ability table
        rows = sorted(
            [(ab, cnt) for ab, cnt in metrics.ability_usage.items()],
            key=lambda x: -self._fight.entity_stats.get(name, type('', (), {'abilities_damage': {}})()).abilities_damage.get(x[0], type('', (), {'total_amount': 0})()).total_amount
        )
        total_dmg = metrics.total_damage or 1

        self.ab_table.setRowCount(len(rows))
        for row, (ab, uses) in enumerate(rows):
            s = self._fight.entity_stats.get(name)
            ab_dmg = s.abilities_damage.get(ab) if s else None
            total  = ab_dmg.total_amount if ab_dmg else 0
            avg    = ab_dmg.avg_hit if ab_dmg else 0
            pct    = total / total_dmg

            bar = "█" * int(pct * 20)
            self.ab_table.setItem(row, 0, _ability_lcell(ab))
            self.ab_table.setItem(row, 1, _cell(str(uses)))
            self.ab_table.setItem(row, 2, _cell(f"{total:,}", color=ACCENT))
            self.ab_table.setItem(row, 3, _cell(f"{avg:,.0f}"))
            self.ab_table.setItem(row, 4, _cell(f"{pct:.0%}  {bar}", color=ACCENT2))
        self.ab_table.resizeRowsToContents()

        # Damage type table
        dt_rows = sorted(metrics.damage_by_type.items(), key=lambda x: -x[1])
        self.dmg_type_table.setRowCount(len(dt_rows))
        for row, (dtype, amt) in enumerate(dt_rows):
            self.dmg_type_table.setItem(row, 0, _lcell(dtype.capitalize()))
            self.dmg_type_table.setItem(row, 1, _cell(f"{amt:,}", color=ACCENT3))
            self.dmg_type_table.setItem(row, 2, _cell(f"{amt/total_dmg:.0%}"))
        self.dmg_type_table.resizeRowsToContents()

        # Coaching tips
        tips = self._generate_dps_tips(metrics, name)
        self.tips_label.setText("\n\n".join(tips) if tips else "✅  No significant issues detected.")

    def _generate_dps_tips(self, m: DpsMetrics, name: str) -> List[str]:
        tips = []
        if m.dead_time_pct > 0.15:
            tips.append(
                f"⚠  Dead time is {m.dead_time_pct:.0%} of the fight ({m.dead_time_total:.1f}s). "
                f"Longest gap: {m.longest_gap:.1f}s. Try to queue your next ability before "
                f"the current one finishes to eliminate gaps."
            )
        if m.crit_rate < 0.25:
            tips.append(
                f"📉  Crit rate is {m.crit_rate:.0%}. Consider checking your gear for "
                f"critical chance augments or alacrity adjustments."
            )
        # Check for underused high-damage abilities
        db = get_db()
        for ab, uses in sorted(m.ability_usage.items(), key=lambda x: x[1]):
            info = db.get(ab)
            if info.cooldown_sec > 20 and uses < 2:
                tips.append(
                    f"💡  '{ab}' was only used {uses}x. "
                    f"High-cooldown abilities should be used on cooldown for maximum DPS."
                )
        if m.dead_time_pct <= 0.15 and m.crit_rate >= 0.25:
            tips.append("✅  Rotation looks clean. Keep pressing abilities without gaps!")
        return tips


# ── Tank training tab ─────────────────────────────────────────────────────────

class TankTrainingTab(QWidget):
    def __init__(self):
        super().__init__()
        self._fight: Optional[Fight] = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Entity:"))
        self.entity_combo = QComboBox()
        self.entity_combo.currentTextChanged.connect(self._load_entity)
        ctrl.addWidget(self.entity_combo)
        ctrl.addStretch()
        root.addLayout(ctrl)

        # Stat cards
        cards = QHBoxLayout()
        self.card_taken    = self._card("DMG Taken",    "—", ACCENT3)
        self.card_absorbed = self._card("DMG Absorbed", "—", ACCENT4)
        self.card_taunts   = self._card("Taunts Used",  "—", ACCENT2)
        self.card_interrupts = self._card("Interrupts", "—", "#ffa657")
        for c in (self.card_taken, self.card_absorbed, self.card_taunts, self.card_interrupts):
            cards.addWidget(c)
        root.addLayout(cards)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: defensive cooldown log
        left = QGroupBox("Defensive Cooldowns Used")
        left_lay = QVBoxLayout(left)
        self.cd_table = _make_table(["Time", "Ability"], stretch_col=1)
        left_lay.addWidget(self.cd_table)
        splitter.addWidget(left)

        # Right: coaching tips
        right = QGroupBox("🛡  Coaching Tips")
        right_lay = QVBoxLayout(right)
        self.tips_label = QLabel("Select a fight to see tips.")
        self.tips_label.setWordWrap(True)
        self.tips_label.setStyleSheet(
            f"color:{TEXT_PRI};font-size:12px;padding:8px;"
            f"background:{BG_WIDGET};border-radius:6px;"
        )
        self.tips_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        right_lay.addWidget(self.tips_label)
        splitter.addWidget(right)

        root.addWidget(splitter)

    def _card(self, label, value, color) -> QFrame:
        f = QFrame()
        f.setStyleSheet(
            f"QFrame{{background:{BG_WIDGET};border:1px solid {BORDER};"
            f"border-radius:6px;padding:8px;}}"
        )
        lay = QVBoxLayout(f)
        lay.setSpacing(2)
        f._val = QLabel(value)
        f._val.setStyleSheet(f"color:{color};font-size:18px;font-weight:700;")
        f._lbl = QLabel(label)
        f._lbl.setStyleSheet(f"color:{TEXT_SEC};font-size:10px;")
        lay.addWidget(f._val)
        lay.addWidget(f._lbl)
        return f

    def load_fight(self, fight: Fight, hide_companions: bool = False, hide_npcs: bool = False):
        self._fight = fight
        self.entity_combo.blockSignals(True)
        self.entity_combo.clear()
        for name in sorted(fight.entity_stats.keys()):
            if hide_companions and fight.entity_stats[name].kind == EntityKind.COMPANION:
                continue
            if hide_npcs and fight.entity_stats[name].kind == EntityKind.NPC:
                continue
            self.entity_combo.addItem(name)
        self.entity_combo.blockSignals(False)
        self._load_entity()

    def _load_entity(self):
        if not self._fight:
            return
        name = self.entity_combo.currentText()
        if not name:
            return

        m = analyse_tank(self._fight, name)

        self.card_taken._val.setText(f"{m.damage_taken:,}")
        self.card_absorbed._val.setText(f"{m.damage_absorbed:,}")
        self.card_taunts._val.setText(str(m.taunt_count))
        self.card_interrupts._val.setText(str(m.interrupt_count))

        self.cd_table.setRowCount(len(m.defensive_cooldowns))
        for row, (t, ab) in enumerate(m.defensive_cooldowns):
            self.cd_table.setItem(row, 0, _cell(f"{t:.2f}s"))
            self.cd_table.setItem(row, 1, _ability_lcell(ab))
        self.cd_table.resizeRowsToContents()

        tips = self._generate_tank_tips(m)
        self.tips_label.setText("\n\n".join(tips) if tips else "✅  No significant issues detected.")

    def _generate_tank_tips(self, m: TankMetrics) -> List[str]:
        tips = []
        dur = m.fight_duration
        if m.taunt_count == 0:
            tips.append(
                "⚠  No taunts used this fight. As a tank, using Taunt keeps "
                "threat on you and protects your DPS players."
            )
        if m.interrupt_count == 0:
            tips.append(
                "⚠  No interrupts recorded. Interrupting enemy casts (Disruption) "
                "is one of the most important tank responsibilities."
            )
        if not m.defensive_cooldowns:
            tips.append(
                "💡  No defensive cooldowns used. If you took significant damage, "
                "consider using Saber Ward or other cooldowns proactively."
            )
        if m.damage_absorbed == 0:
            tips.append(
                "📉  No damage was absorbed this fight. Ensure your shield generator "
                "is equipped and shield absorb stats are properly geared."
            )
        if not tips:
            tips.append(
                f"✅  Good performance! You taunted {m.taunt_count}x, "
                f"interrupted {m.interrupt_count}x, and used "
                f"{len(m.defensive_cooldowns)} defensive cooldown(s)."
            )
        return tips


# ── Healer training tab ───────────────────────────────────────────────────────

class HealerTrainingTab(QWidget):
    def __init__(self):
        super().__init__()
        self._fight: Optional[Fight] = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Entity:"))
        self.entity_combo = QComboBox()
        self.entity_combo.currentTextChanged.connect(self._load_entity)
        ctrl.addWidget(self.entity_combo)
        ctrl.addStretch()
        root.addLayout(ctrl)

        # Stat cards
        cards = QHBoxLayout()
        self.card_hps      = self._card("HPS",          "—", ACCENT2)
        self.card_heal     = self._card("Total Healed",  "—", ACCENT4)
        self.card_overheal = self._card("Overheal %",    "—", ACCENT3)
        self.card_crit     = self._card("Crit Rate",     "—", "#ffa657")
        for c in (self.card_hps, self.card_heal, self.card_overheal, self.card_crit):
            cards.addWidget(c)
        root.addLayout(cards)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: ability healing breakdown
        left = QGroupBox("Ability Healing Breakdown")
        left_lay = QVBoxLayout(left)
        self.ab_table = _make_table(
            ["Ability", "Uses", "Effective Heal", "% of Total"],
            stretch_col=0
        )
        left_lay.addWidget(self.ab_table)
        splitter.addWidget(left)

        # Right: triage (who you healed) + tips
        right = QWidget()
        right_lay = QVBoxLayout(right)

        triage_grp = QGroupBox("Triage — Who You Healed")
        triage_lay = QVBoxLayout(triage_grp)
        self.triage_table = _make_table(["Target", "Healing Received", "%"], stretch_col=0)
        triage_lay.addWidget(self.triage_table)
        right_lay.addWidget(triage_grp)

        tips_grp = QGroupBox("💊  Coaching Tips")
        tips_lay = QVBoxLayout(tips_grp)
        self.tips_label = QLabel("Select a fight to see tips.")
        self.tips_label.setWordWrap(True)
        self.tips_label.setStyleSheet(
            f"color:{TEXT_PRI};font-size:12px;padding:8px;"
            f"background:{BG_WIDGET};border-radius:6px;"
        )
        self.tips_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        tips_lay.addWidget(self.tips_label)
        right_lay.addWidget(tips_grp)
        splitter.addWidget(right)

        root.addWidget(splitter)

    def _card(self, label, value, color) -> QFrame:
        f = QFrame()
        f.setStyleSheet(
            f"QFrame{{background:{BG_WIDGET};border:1px solid {BORDER};"
            f"border-radius:6px;padding:8px;}}"
        )
        lay = QVBoxLayout(f)
        lay.setSpacing(2)
        f._val = QLabel(value)
        f._val.setStyleSheet(f"color:{color};font-size:18px;font-weight:700;")
        f._lbl = QLabel(label)
        f._lbl.setStyleSheet(f"color:{TEXT_SEC};font-size:10px;")
        lay.addWidget(f._val)
        lay.addWidget(f._lbl)
        return f

    def load_fight(self, fight: Fight, hide_companions: bool = False, hide_npcs: bool = False):
        self._fight = fight
        self.entity_combo.blockSignals(True)
        self.entity_combo.clear()
        for name in sorted(fight.entity_stats.keys()):
            if hide_companions and fight.entity_stats[name].kind == EntityKind.COMPANION:
                continue
            if hide_npcs and fight.entity_stats[name].kind == EntityKind.NPC:
                continue
            self.entity_combo.addItem(name)
        self.entity_combo.blockSignals(False)
        self._load_entity()

    def _load_entity(self):
        if not self._fight:
            return
        name = self.entity_combo.currentText()
        if not name:
            return

        m = analyse_healer(self._fight, name)

        self.card_hps._val.setText(f"{m.hps:,.0f}")
        self.card_heal._val.setText(f"{m.healing_done:,}")
        self.card_overheal._val.setText(f"{m.overheal_pct:.0%}")
        self.card_crit._val.setText(f"{m.crit_rate:.0%}")

        # Ability table
        total = m.healing_done or 1
        rows = sorted(m.ability_healing.items(), key=lambda x: -x[1])
        self.ab_table.setRowCount(len(rows))
        for row, (ab, heal) in enumerate(rows):
            uses = m.ability_usage.get(ab, 0)
            pct  = heal / total
            bar  = "█" * int(pct * 15)
            self.ab_table.setItem(row, 0, _ability_lcell(ab))
            self.ab_table.setItem(row, 1, _cell(str(uses)))
            self.ab_table.setItem(row, 2, _cell(f"{heal:,}", color=ACCENT2))
            self.ab_table.setItem(row, 3, _cell(f"{pct:.0%}  {bar}", color=ACCENT4))
        self.ab_table.resizeRowsToContents()

        # Triage table
        triage_rows = sorted(m.targets_healed.items(), key=lambda x: -x[1])
        self.triage_table.setRowCount(len(triage_rows))
        for row, (tgt, heal) in enumerate(triage_rows):
            self.triage_table.setItem(row, 0, _lcell(tgt, bold=True))
            self.triage_table.setItem(row, 1, _cell(f"{heal:,}", color=ACCENT2))
            self.triage_table.setItem(row, 2, _cell(f"{heal/total:.0%}"))
        self.triage_table.resizeRowsToContents()

        tips = self._generate_healer_tips(m)
        self.tips_label.setText("\n\n".join(tips) if tips else "✅  No significant issues detected.")

    def _generate_healer_tips(self, m: HealerMetrics) -> List[str]:
        tips = []
        if m.overheal_pct > 0.30:
            tips.append(
                f"⚠  Overheal is {m.overheal_pct:.0%}. This means nearly a third of your "
                f"healing is wasted. Try to heal reactively — wait until targets are "
                f"actually damaged before casting big heals."
            )
        if m.crit_rate < 0.25:
            tips.append(
                f"📉  Crit rate is {m.crit_rate:.0%}. Healing crits double your effective output "
                f"— consider alacrity/crit stat distribution."
            )
        if m.hps > 0 and m.overheal_pct < 0.15:
            tips.append(
                f"✅  Very low overheal ({m.overheal_pct:.0%}) — you're healing efficiently "
                f"and not wasting casts on topped-off targets."
            )
        # Check triage priority
        if len(m.targets_healed) > 1:
            tgt_list = sorted(m.targets_healed.items(), key=lambda x: -x[1])
            top_tgt, top_heal = tgt_list[0]
            total = m.healing_done or 1
            if top_heal / total > 0.70:
                tips.append(
                    f"💡  {top_heal/total:.0%} of your healing went to '{top_tgt}'. "
                    f"Make sure other group members aren't being neglected."
                )
        if not tips:
            tips.append("✅  Healing looks solid. Good efficiency and spread!")
        return tips


# ── Side-by-Side Comparison tab ──────────────────────────────────────────────

# Colour helpers for deltas
_GOOD  = "#3fb950"
_BAD   = "#f78166"
_NEUT  = "#8b949e"
_WARN  = "#ffa657"

SEVERITY_STYLE = {
    "high":   (f"background:#3d1f1f; color:{_BAD};  border-left:3px solid {_BAD};"),
    "medium": (f"background:#3a2d00; color:{_WARN}; border-left:3px solid {_WARN};"),
    "low":    (f"background:{BG_WIDGET}; color:{TEXT_SEC}; border-left:3px solid {BORDER};"),
}


class ComparisonTab(QWidget):
    """
    Flagship feature: side-by-side comparison of two entities in the same fight.
    Shows metric deltas, ability-by-ability comparison, and ranked coaching insights.
    """

    def __init__(self):
        super().__init__()
        self._fight: Optional[Fight] = None
        self._result: Optional[ComparisonResult] = None
        self._build_ui()

    # ── Build UI ─────────────────────────────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        # ── Entity selector row ──────────────────────────────────────────────
        ctrl = QHBoxLayout()
        ctrl.setSpacing(6)

        ctrl.addWidget(QLabel("You:"))
        self.user_combo = QComboBox()
        self.user_combo.setMinimumWidth(160)
        ctrl.addWidget(self.user_combo)

        ctrl.addWidget(QLabel("  vs  "))

        ctrl.addWidget(QLabel("Reference:"))
        self.ref_combo = QComboBox()
        self.ref_combo.setMinimumWidth(160)
        ctrl.addWidget(self.ref_combo)

        ctrl.addSpacing(12)
        self.role_label = QLabel("")
        self.role_label.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        ctrl.addWidget(self.role_label)

        ctrl.addStretch()

        # Compare button
        from PyQt6.QtWidgets import QPushButton
        self.compare_btn = QPushButton("⚔  Compare")
        self.compare_btn.setStyleSheet(
            f"QPushButton{{background:#1158c7; color:white; border:1px solid {ACCENT};"
            f"border-radius:6px; padding:6px 18px; font-weight:600;}}"
            f"QPushButton:hover{{background:#1a6de0;}}"
        )
        self.compare_btn.clicked.connect(self._run_comparison)
        ctrl.addWidget(self.compare_btn)

        root.addLayout(ctrl)

        # ── Main content area (hidden until comparison runs) ─────────────────
        self.content = QWidget()
        content_lay = QVBoxLayout(self.content)
        content_lay.setContentsMargins(0, 0, 0, 0)
        content_lay.setSpacing(8)

        # Metric cards row
        self.metric_cards_layout = QHBoxLayout()
        self.metric_cards_layout.setSpacing(8)
        content_lay.addLayout(self.metric_cards_layout)

        # Splitter: abilities table (left) + insights (right)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: ability comparison table
        left = QGroupBox("⚡  Ability-by-Ability Comparison")
        left_lay = QVBoxLayout(left)

        # Filter row inside abilities
        ab_filter = QHBoxLayout()
        ab_filter.addWidget(QLabel("Show:"))
        self.ab_filter_combo = QComboBox()
        self.ab_filter_combo.addItems(["All Abilities", "Damage Only", "Healing Only",
                                        "Differences Only (|Δ| > 1)"])
        self.ab_filter_combo.currentIndexChanged.connect(self._refresh_ability_table)
        ab_filter.addWidget(self.ab_filter_combo)
        ab_filter.addStretch()
        left_lay.addLayout(ab_filter)

        self.ab_table = _make_table(
            ["Ability", "Type", "You", "Ref", "Δ Count",
             "Your DMG", "Ref DMG", "Δ DMG", "Comment"],
            stretch_col=0,
        )
        self.ab_table.setColumnWidth(1, 70)
        left_lay.addWidget(self.ab_table)
        splitter.addWidget(left)

        # Right: coaching insights
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(8)

        # Metric comparison table
        metric_grp = QGroupBox("📊  Key Metrics")
        metric_lay = QVBoxLayout(metric_grp)
        self.metric_table = _make_table(
            ["Metric", "You", "Reference", "Δ", ""],
            stretch_col=0,
        )
        self.metric_table.setMaximumHeight(300)
        metric_lay.addWidget(self.metric_table)
        right_lay.addWidget(metric_grp)

        # Coaching insights
        insight_grp = QGroupBox("🎯  Coaching Insights (ranked by impact)")
        insight_lay = QVBoxLayout(insight_grp)
        self.insights_scroll = QScrollArea()
        self.insights_scroll.setWidgetResizable(True)
        self.insights_scroll.setStyleSheet(
            f"QScrollArea{{border:none; background:{BG_PANEL};}}"
        )
        self.insights_inner = QWidget()
        self.insights_layout = QVBoxLayout(self.insights_inner)
        self.insights_layout.setSpacing(6)
        self.insights_layout.setContentsMargins(4, 4, 4, 4)
        self.insights_scroll.setWidget(self.insights_inner)
        insight_lay.addWidget(self.insights_scroll)
        right_lay.addWidget(insight_grp)

        splitter.addWidget(right)
        splitter.setSizes([600, 400])

        content_lay.addWidget(splitter)

        root.addWidget(self.content)

        # Placeholder
        self.placeholder = QLabel(
            "Select two entities and press Compare to see the side-by-side analysis."
        )
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setStyleSheet(
            f"color:{TEXT_SEC}; font-size:14px; padding:40px;"
        )
        root.addWidget(self.placeholder)
        self.content.setVisible(False)

    # ── Load fight ───────────────────────────────────────────────────────────
    def load_fight(self, fight: Fight, hide_companions: bool = False, hide_npcs: bool = False):
        self._fight = fight
        self._result = None
        self.content.setVisible(False)
        self.placeholder.setVisible(True)

        names = sorted(fight.entity_stats.keys())
        if hide_companions:
            names = [n for n in names
                     if fight.entity_stats[n].kind != EntityKind.COMPANION]
        if hide_npcs:
            names = [n for n in names
                     if fight.entity_stats[n].kind != EntityKind.NPC]

        # Populate combos
        self.user_combo.blockSignals(True)
        self.ref_combo.blockSignals(True)
        self.user_combo.clear()
        self.ref_combo.clear()
        for n in names:
            self.user_combo.addItem(n)
            self.ref_combo.addItem(n)

        # Auto-select: user = PLAYER kind, ref = first different entity
        player_idx = 0
        ref_idx = 1 if len(names) > 1 else 0
        for i, n in enumerate(names):
            s = fight.entity_stats.get(n)
            if s and s.kind == EntityKind.PLAYER:
                player_idx = i
                break
        # Try to find a GROUP_MEMBER as default reference
        for i, n in enumerate(names):
            s = fight.entity_stats.get(n)
            if s and s.kind == EntityKind.GROUP_MEMBER and i != player_idx:
                ref_idx = i
                break
        else:
            # Fallback to any non-same entity
            for i in range(len(names)):
                if i != player_idx:
                    ref_idx = i
                    break

        self.user_combo.setCurrentIndex(player_idx)
        self.ref_combo.setCurrentIndex(ref_idx)
        self.user_combo.blockSignals(False)
        self.ref_combo.blockSignals(False)

        self.role_label.setText("")

    # ── Run comparison ───────────────────────────────────────────────────────
    def _run_comparison(self):
        if not self._fight:
            return
        user_name = self.user_combo.currentText()
        ref_name  = self.ref_combo.currentText()
        if not user_name or not ref_name:
            return
        if user_name == ref_name:
            self.placeholder.setText("⚠  Pick two different entities to compare.")
            self.placeholder.setVisible(True)
            self.content.setVisible(False)
            return

        self._result = compare_entities(self._fight, user_name, ref_name)
        self.placeholder.setVisible(False)
        self.content.setVisible(True)

        # Role info
        self.role_label.setText(
            f"Roles:  {user_name} = {self._result.user_role.upper()}  |  "
            f"{ref_name} = {self._result.ref_role.upper()}"
        )

        self._populate_metrics()
        self._refresh_ability_table()
        self._populate_insights()

    # ── Populate metric comparison table ─────────────────────────────────────
    def _populate_metrics(self):
        if not self._result:
            return
        r = self._result

        self.metric_table.setRowCount(len(r.metrics))
        for row, m in enumerate(r.metrics):
            # Format values
            if m.unit == "%":
                u_str = f"{m.user_value:.0%}"
                r_str = f"{m.ref_value:.0%}"
                d_str = f"{m.delta:+.0%}"
            elif m.unit == "s":
                u_str = f"{m.user_value:.1f}s"
                r_str = f"{m.ref_value:.1f}s"
                d_str = f"{m.delta:+.1f}s"
            elif m.unit in ("dps", "hps", "apm"):
                u_str = f"{m.user_value:,.0f}"
                r_str = f"{m.ref_value:,.0f}"
                d_str = f"{m.delta:+,.0f}"
            elif m.unit in ("dmg", "heal"):
                u_str = f"{m.user_value:,.0f}"
                r_str = f"{m.ref_value:,.0f}"
                d_str = f"{m.delta:+,.0f}"
            else:
                u_str = f"{m.user_value:,.0f}"
                r_str = f"{m.ref_value:,.0f}"
                d_str = f"{m.delta:+,.0f}"

            # Delta colour
            if m.better_when == "higher":
                d_color = _GOOD if m.delta >= 0 else _BAD
            else:
                d_color = _GOOD if m.delta <= 0 else _BAD
            if abs(m.pct_delta) < 0.02:
                d_color = _NEUT

            self.metric_table.setItem(row, 0, _lcell(m.metric_name, bold=True))
            self.metric_table.setItem(row, 1, _cell(u_str, color=ACCENT))
            self.metric_table.setItem(row, 2, _cell(r_str, color=ACCENT4))
            self.metric_table.setItem(row, 3, _cell(d_str, color=d_color, bold=True))
            self.metric_table.setItem(row, 4, _lcell(m.comment, color=d_color))

        self.metric_table.resizeRowsToContents()

    # ── Populate ability comparison table ────────────────────────────────────
    def _refresh_ability_table(self):
        if not self._result:
            return
        r = self._result
        filter_mode = self.ab_filter_combo.currentIndex()

        filtered: List[AbilityComparison] = []
        for ac in r.abilities:
            if filter_mode == 1 and ac.user_damage == 0 and ac.ref_damage == 0:
                continue
            if filter_mode == 2 and ac.user_healing == 0 and ac.ref_healing == 0:
                continue
            if filter_mode == 3 and abs(ac.delta_count) <= 1:
                continue
            filtered.append(ac)

        self.ab_table.setRowCount(len(filtered))
        for row, ac in enumerate(filtered):
            # Delta colour for count
            if ac.delta_count < -2:
                cnt_color = _BAD
            elif ac.delta_count > 3:
                cnt_color = _WARN
            elif abs(ac.delta_count) <= 1:
                cnt_color = _NEUT
            else:
                cnt_color = TEXT_PRI

            # Delta colour for damage
            d_dmg_color = _GOOD if ac.delta_damage >= 0 else _BAD
            if ac.delta_damage == 0:
                d_dmg_color = _NEUT

            # Use damage or healing for "DMG" columns depending on what's present
            u_val = ac.user_damage if ac.user_damage else ac.user_healing
            r_val = ac.ref_damage if ac.ref_damage else ac.ref_healing
            d_val = ac.delta_damage if ac.delta_damage else ac.delta_healing
            if ac.user_healing and not ac.user_damage:
                d_dmg_color = _GOOD if ac.delta_healing >= 0 else _BAD
                if ac.delta_healing == 0:
                    d_dmg_color = _NEUT

            self.ab_table.setItem(row, 0, _ability_lcell(ac.ability_name))
            self.ab_table.setItem(row, 1, _lcell(ac.ability_type, color=TEXT_SEC))
            self.ab_table.setItem(row, 2, _cell(str(ac.user_count), color=ACCENT))
            self.ab_table.setItem(row, 3, _cell(str(ac.ref_count), color=ACCENT4))
            self.ab_table.setItem(row, 4, _cell(
                f"{ac.delta_count:+d}" if ac.delta_count != 0 else "=",
                color=cnt_color, bold=True,
            ))
            self.ab_table.setItem(row, 5, _cell(
                f"{u_val:,}" if u_val else "—", color=ACCENT,
            ))
            self.ab_table.setItem(row, 6, _cell(
                f"{r_val:,}" if r_val else "—", color=ACCENT4,
            ))
            self.ab_table.setItem(row, 7, _cell(
                f"{d_val:+,}" if d_val != 0 else "=", color=d_dmg_color, bold=True,
            ))
            self.ab_table.setItem(row, 8, _lcell(ac.comment, color=TEXT_SEC))

        self.ab_table.resizeRowsToContents()

    # ── Populate coaching insights ───────────────────────────────────────────
    def _populate_insights(self):
        if not self._result:
            return

        # Clear existing
        while self.insights_layout.count():
            child = self.insights_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        if not self._result.insights:
            lbl = QLabel("✅  No significant differences found. Nice work!")
            lbl.setStyleSheet(
                f"color:{_GOOD}; font-size:13px; padding:12px;"
                f"background:{BG_WIDGET}; border-radius:6px;"
            )
            lbl.setWordWrap(True)
            self.insights_layout.addWidget(lbl)
        else:
            for i, insight in enumerate(self._result.insights):
                card = self._make_insight_card(i + 1, insight)
                self.insights_layout.addWidget(card)

        self.insights_layout.addStretch()

    def _make_insight_card(self, rank: int, insight: ComparisonInsight) -> QFrame:
        card = QFrame()
        style = SEVERITY_STYLE.get(insight.severity, SEVERITY_STYLE["low"])
        card.setStyleSheet(
            f"QFrame{{{style} border-radius:6px; padding:8px; margin:2px 0;}}"
        )
        lay = QVBoxLayout(card)
        lay.setSpacing(4)
        lay.setContentsMargins(10, 8, 10, 8)

        # Header: rank + category + severity badge
        hdr = QHBoxLayout()
        rank_lbl = QLabel(f"#{rank}")
        rank_lbl.setStyleSheet(f"font-size:14px; font-weight:700;")
        hdr.addWidget(rank_lbl)

        cat_lbl = QLabel(insight.category.upper())
        cat_lbl.setStyleSheet(
            f"font-size:9px; font-weight:700; padding:2px 6px;"
            f"background:{BORDER}; border-radius:3px;"
        )
        hdr.addWidget(cat_lbl)

        sev_colors = {"high": _BAD, "medium": _WARN, "low": _NEUT}
        sev_lbl = QLabel(insight.severity.upper())
        sev_lbl.setStyleSheet(
            f"font-size:9px; font-weight:700; padding:2px 6px;"
            f"color:{sev_colors.get(insight.severity, _NEUT)};"
            f"background:{BG_DARK}; border-radius:3px;"
        )
        hdr.addWidget(sev_lbl)

        if insight.impact_estimate > 0:
            impact_lbl = QLabel(f"~{insight.impact_estimate:,.0f} impact")
            impact_lbl.setStyleSheet(f"font-size:9px; color:{TEXT_SEC};")
            hdr.addWidget(impact_lbl)

        hdr.addStretch()
        lay.addLayout(hdr)

        # Message
        msg = QLabel(insight.message)
        msg.setWordWrap(True)
        msg.setStyleSheet(f"font-size:12px; color:{TEXT_PRI}; padding-top:2px;")
        lay.addWidget(msg)

        return card
