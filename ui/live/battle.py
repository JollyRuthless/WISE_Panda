"""
ui/live/battle.py — LiveEntityBar + LiveBattleWindow: always-on-top frameless DPS overlay.
"""

from typing import Optional, List

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QApplication
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter, QBrush

from engine.aggregator import EntityKind
from ui.theme import ACCENT, ACCENT3, TEXT_PRI, TEXT_SEC, BORDER, KIND_BADGE
from ui.settings import settings
from ui.live.tracker import LiveFightTracker

# Layout constants
LIVE_WIDTH      = 360
LIVE_BAR_H      = 42
LIVE_BAR_GAP    = 5
LIVE_HEADER_H   = 40
LIVE_PADDING    = 10
LIVE_REFRESH_MS = 500

# DPS metric cycle for the header toggle. Order matches button click flow.
# Each entry: (metric key passed to tracker, short button label, column header label)
LIVE_METRICS = [
    ("rolling",   "Roll DPS", "ROLL DPS"),
    ("encounter", "Enc DPS",  "ENC DPS"),
    ("active",    "Act DPS",  "ACT DPS"),
]
LIVE_METRIC_KEYS = [m[0] for m in LIVE_METRICS]


def _fmt_big(n: int) -> str:
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000:     return f"{n/1_000:.1f}k"
    return str(n)


class LiveEntityBar(QWidget):
    """One animated row in the live overlay: rank | badge | name | DPS | total."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(LIVE_BAR_H)
        self._data: Optional[dict] = None

    def set_data(self, data: dict):
        self._data = data
        self.update()

    def paintEvent(self, _event):
        if not self._data:
            return
        d = self._data
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        # Track background
        p.setBrush(QColor("#1c2128"))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(0, 0, w, h, 6, 6)

        # Filled DPS bar
        _, _bg, fg = KIND_BADGE[d["kind"]]
        bar_color  = QColor(fg)
        bar_color.setAlpha(70)
        bar_w = max(int(w * d["pct"]), 0)
        if bar_w:
            p.setBrush(bar_color)
            p.drawRoundedRect(0, 0, bar_w, h, 6, 6)

        # Rank number
        p.setPen(QColor(fg))
        p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        p.drawText(6, 0, 22, h, Qt.AlignmentFlag.AlignVCenter, f"#{d['rank']}")

        # Badge pill
        badge_lbl, badge_bg, badge_fg = KIND_BADGE[d["kind"]]
        p.setBrush(QColor(badge_bg))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(30, h // 2 - 9, 46, 18, 3, 3)
        p.setPen(QColor(badge_fg))
        p.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        p.drawText(30, h // 2 - 9, 46, 18, Qt.AlignmentFlag.AlignCenter, badge_lbl)

        # Name (elided)
        dps_w, tot_w, name_x = 64, 72, 82
        name_w = w - name_x - dps_w - tot_w - 8
        p.setPen(QColor(TEXT_PRI))
        p.setFont(QFont("Segoe UI", 10))
        elided = p.fontMetrics().elidedText(d["name"], Qt.TextElideMode.ElideRight, name_w)
        p.drawText(name_x, 0, name_w, h, Qt.AlignmentFlag.AlignVCenter, elided)

        # DPS value
        p.setPen(QColor(fg))
        p.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        p.drawText(w - dps_w - tot_w, 0, dps_w, h,
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                   f"{d['dps']:,.0f}")

        # Total damage
        p.setPen(QColor(TEXT_SEC))
        p.setFont(QFont("Segoe UI", 9))
        p.drawText(w - tot_w + 4, 0, tot_w - 4, h,
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                   _fmt_big(d["total_damage"]))
        p.end()


class LiveBattleWindow(QWidget):
    """
    Always-on-top frameless overlay — live DPS bars + total damage.
    Updates every LIVE_REFRESH_MS ms. Position + filter toggles are persisted.
    """

    def __init__(self, tracker: LiveFightTracker):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._tracker   = tracker
        self._drag_pos  = None
        self._show_npcs = bool(settings.get("live_show_npcs", True))
        self._show_companions = bool(settings.get("live_show_companions", False))
        saved_metric = settings.get("live_metric", "rolling")
        self._metric = saved_metric if saved_metric in LIVE_METRIC_KEYS else "rolling"
        self._bars: List[LiveEntityBar] = []

        self._build_ui()
        self._restore_or_default_position()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(LIVE_REFRESH_MS)

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.setFixedWidth(LIVE_WIDTH)
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(LIVE_PADDING, LIVE_PADDING, LIVE_PADDING, LIVE_PADDING)
        self._root.setSpacing(LIVE_BAR_GAP)

        # Header row
        hdr = QHBoxLayout()
        self._title_lbl = QLabel("⚔  LIVE BATTLE")
        self._title_lbl.setStyleSheet(
            f"color:{ACCENT}; font-weight:700; font-size:12px; font-family:'Segoe UI';"
        )
        self._elapsed_lbl = QLabel("0:00")
        self._elapsed_lbl.setStyleSheet(
            f"color:{TEXT_SEC}; font-size:11px; font-family:'Segoe UI';"
        )
        self._metric_btn = QPushButton(self._metric_btn_label())
        self._metric_btn.setFixedSize(62, 20)
        self._metric_btn.setToolTip(
            "DPS metric:\n"
            "  Roll DPS — 6-second rolling window (what you're hitting RIGHT NOW)\n"
            "  Enc DPS — averaged over the whole fight\n"
            "  Act DPS — averaged over time spent actually swinging"
        )
        self._style_metric_btn()
        self._metric_btn.clicked.connect(self._cycle_metric)

        self._npc_btn = QPushButton("NPCs ✓" if self._show_npcs else "NPCs ✗")
        self._npc_btn.setFixedSize(58, 20)
        self._style_npc_btn()
        self._npc_btn.clicked.connect(self._toggle_npcs)

        self._comp_btn = QPushButton("Comp ✓" if self._show_companions else "Comp ✗")
        self._comp_btn.setFixedSize(62, 20)
        self._style_companion_btn()
        self._comp_btn.clicked.connect(self._toggle_companions)

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(20, 20)
        close_btn.setStyleSheet(
            f"QPushButton{{background:transparent;border:none;color:{TEXT_SEC};font-size:11px;}}"
            f"QPushButton:hover{{color:{ACCENT3};}}"
        )
        close_btn.clicked.connect(self.hide)

        hdr.addWidget(self._title_lbl)
        hdr.addStretch()
        hdr.addWidget(self._elapsed_lbl)
        hdr.addSpacing(6)
        hdr.addWidget(self._metric_btn)
        hdr.addSpacing(4)
        hdr.addWidget(self._npc_btn)
        hdr.addSpacing(4)
        hdr.addWidget(self._comp_btn)
        hdr.addSpacing(4)
        hdr.addWidget(close_btn)
        self._root.addLayout(hdr)

        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background:{BORDER};")
        self._root.addWidget(sep)

        # Column header labels
        col = QHBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)

        def ch(t, w=None):
            lbl = QLabel(t)
            lbl.setStyleSheet(f"color:{TEXT_SEC};font-size:9px;font-weight:600;")
            if w:
                lbl.setFixedWidth(w)
            return lbl

        col.addSpacing(72)
        col.addWidget(ch("NAME"))
        col.addStretch()
        self._dps_col_lbl = ch(self._metric_col_label(), 60)
        col.addWidget(self._dps_col_lbl)
        col.addWidget(ch("TOTAL", 68))
        self._root.addLayout(col)

        # Bar area
        self._bar_area = QVBoxLayout()
        self._bar_area.setSpacing(LIVE_BAR_GAP)
        self._root.addLayout(self._bar_area)

        # Idle label
        self._idle_lbl = QLabel("Waiting for combat…")
        self._idle_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._idle_lbl.setStyleSheet(f"color:{TEXT_SEC};font-size:11px;padding:14px;")
        self._root.addWidget(self._idle_lbl)

        self._update_size()

    # ── Refresh ───────────────────────────────────────────────────────────────

    def _refresh(self):
        rows = self._tracker.snapshot(metric=self._metric)
        if not self._show_npcs:
            rows = [r for r in rows if r["kind"] != EntityKind.NPC]
        if not self._show_companions:
            rows = [r for r in rows if r["kind"] != EntityKind.COMPANION]
        for i, r in enumerate(rows, 1):
            r["rank"] = i
        top = rows[0]["dps"] if rows else 1.0
        for r in rows:
            r["pct"] = r["dps"] / top if top > 0 else 0.0

        elapsed = int(self._tracker.elapsed)
        self._elapsed_lbl.setText(f"{elapsed // 60}:{elapsed % 60:02d}")
        self.update()  # repaint border colour

        if not rows:
            self._idle_lbl.setVisible(True)
            for b in self._bars:
                b.setVisible(False)
            self._update_size()
            return

        self._idle_lbl.setVisible(False)
        while len(self._bars) < len(rows):
            bar = LiveEntityBar()
            self._bar_area.addWidget(bar)
            self._bars.append(bar)

        for i, row in enumerate(rows):
            self._bars[i].set_data(row)
            self._bars[i].setVisible(True)
        for bar in self._bars[len(rows):]:
            bar.setVisible(False)

        self._update_size()

    def _update_size(self):
        vis = sum(1 for b in self._bars if b.isVisible())
        h = (LIVE_HEADER_H + 1 + 20
             + vis * (LIVE_BAR_H + LIVE_BAR_GAP)
             + (36 if self._idle_lbl.isVisible() else 0)
             + LIVE_PADDING * 2 + 8)
        self.setFixedHeight(max(h, LIVE_HEADER_H + 60))

    # ── Metric cycle (Roll / Enc / Act DPS) ──────────────────────────────────

    def _metric_index(self) -> int:
        try:
            return LIVE_METRIC_KEYS.index(self._metric)
        except ValueError:
            return 0

    def _metric_btn_label(self) -> str:
        return LIVE_METRICS[self._metric_index()][1]

    def _metric_col_label(self) -> str:
        return LIVE_METRICS[self._metric_index()][2]

    def _style_metric_btn(self):
        self._metric_btn.setText(self._metric_btn_label())
        # Neutral dark pill — this isn't an on/off state, it's a mode cycle.
        self._metric_btn.setStyleSheet(
            "background-color: #1f2937;"
            f"color: {ACCENT};"
            "border: none;"
            "border-radius: 4px;"
            "font-size: 10px;"
            "font-weight: 700;"
        )

    def _cycle_metric(self):
        idx = (self._metric_index() + 1) % len(LIVE_METRICS)
        self._metric = LIVE_METRIC_KEYS[idx]
        settings.set("live_metric", self._metric)
        self._style_metric_btn()
        if hasattr(self, "_dps_col_lbl") and self._dps_col_lbl is not None:
            self._dps_col_lbl.setText(self._metric_col_label())
        self._refresh()

    # ── NPC toggle ────────────────────────────────────────────────────────────

    def _style_npc_btn(self):
        on  = self._show_npcs
        self._npc_btn.setText("NPCs ✓" if on else "NPCs ✗")
        bg  = "#2d4a1e" if on else "#3d1f1f"
        fg  = "#3fb950" if on else "#f78166"
        self._npc_btn.setStyleSheet(
            f"background-color: {bg};"
            f"color: {fg};"
            "border: none;"
            "border-radius: 4px;"
            "font-size: 10px;"
            "font-weight: 700;"
        )

    def _style_companion_btn(self):
        on  = self._show_companions
        self._comp_btn.setText("Comp ✓" if on else "Comp ✗")
        bg  = "#1d3557" if on else "#3d1f1f"
        fg  = "#79c0ff" if on else "#f78166"
        self._comp_btn.setStyleSheet(
            f"background-color: {bg};"
            f"color: {fg};"
            "border: none;"
            "border-radius: 4px;"
            "font-size: 10px;"
            "font-weight: 700;"
        )

    def _toggle_npcs(self):
        self._show_npcs = not self._show_npcs
        settings.set("live_show_npcs", self._show_npcs)
        self._style_npc_btn()
        self._refresh()

    def _toggle_companions(self):
        self._show_companions = not self._show_companions
        settings.set("live_show_companions", self._show_companions)
        self._style_companion_btn()
        self._refresh()

    # ── Position persistence ──────────────────────────────────────────────────

    def _restore_or_default_position(self):
        saved  = settings.get("live_window_pos")
        screen = QApplication.primaryScreen().availableGeometry()
        if saved and isinstance(saved, list) and len(saved) == 2:
            x = max(screen.left(), min(saved[0], screen.right()  - self.width()))
            y = max(screen.top(),  min(saved[1], screen.bottom() - 100))
            self.move(x, y)
        else:
            self.move(screen.left() + 20, screen.top() + 60)

    def _save_position(self):
        p = self.pos()
        settings.set("live_window_pos", [p.x(), p.y()])

    # ── Paint (border shows combat state) ────────────────────────────────────

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(14, 17, 23, 225))
        p.setPen(QColor(ACCENT3 if self._tracker.in_combat else BORDER))
        p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 10, 10)
        p.end()

    # ── Drag ──────────────────────────────────────────────────────────────────

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag_pos and e.buttons() == Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None
        self._save_position()
