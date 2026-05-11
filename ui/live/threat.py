"""
ui/live/threat.py — LiveThreatWindow: always-on-top NPC threat board overlay.
Anchors to LiveBattleWindow and snaps when dragged close.
"""

from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QTableWidget, QTableWidgetItem, QHeaderView, QApplication,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QBrush, QPainter

from engine.great_hunt import classification_for_npc as hunt_classification_for_npc
from ui.theme import ACCENT, ACCENT2, ACCENT3, TEXT_PRI, TEXT_SEC, BORDER
from ui.settings import settings
from ui.live.tracker import LiveFightTracker
from ui.live.battle import LiveBattleWindow, LIVE_HEADER_H, LIVE_PADDING, LIVE_REFRESH_MS

THREAT_GAP       = 10
THREAT_MIN_WIDTH  = 220
THREAT_MAX_WIDTH  = 760
THREAT_MOB_MIN_WIDTH = 260


class LiveThreatWindow(QWidget):
    """
    Sister overlay: one row per NPC showing player threat / target state.
    Stays attached to the live battle window when visible.
    """

    def __init__(self, tracker: LiveFightTracker, anchor: LiveBattleWindow):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._tracker           = tracker
        self._anchor            = anchor
        self._drag_pos          = None
        self._snapped_to_anchor = bool(settings.get("live_threat_window_snapped", False))
        self._snap_side         = str(settings.get("live_threat_window_snap_side", "right") or "right")
        self._status_color      = BORDER

        self._build_ui()
        self._restore_or_default_position()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(LIVE_REFRESH_MS)
        self._refresh()

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.setFixedWidth(THREAT_MIN_WIDTH)
        self.setFixedHeight(130)
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(LIVE_PADDING, LIVE_PADDING, LIVE_PADDING, LIVE_PADDING)
        self._root.setSpacing(6)

        hdr = QHBoxLayout()
        title = QLabel("THREAT BOARD")
        title.setStyleSheet(
            f"color:{ACCENT3}; font-weight:700; font-size:12px; font-family:'Segoe UI';"
        )
        self._count_lbl = QLabel("0 mobs")
        self._count_lbl.setStyleSheet(
            f"color:{TEXT_SEC}; font-size:11px; font-family:'Segoe UI';"
        )
        hdr.addWidget(title)
        hdr.addStretch()
        hdr.addWidget(self._count_lbl)
        self._root.addLayout(hdr)

        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background:{BORDER};")
        self._root.addWidget(sep)

        self._table = QTableWidget()
        self._table.setColumnCount(3)
        self._table.setHorizontalHeaderLabels(["Mob", "Strength", "Target"])
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._root.addWidget(self._table, 1)

        self._idle_lbl = QLabel("Waiting for combat...")
        self._idle_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._idle_lbl.setStyleSheet(f"color:{TEXT_SEC};font-size:11px;padding:10px;")
        self._root.addWidget(self._idle_lbl)

    # ── Snap / position helpers ───────────────────────────────────────────────

    def _sync_to_anchor(self):
        if not self._anchor or not self._anchor.isVisible():
            return
        screen  = QApplication.primaryScreen().availableGeometry()
        right_x = self._anchor.x() + self._anchor.width() + THREAT_GAP
        left_x  = self._anchor.x() - self.width() - THREAT_GAP
        if self._snap_side == "left":
            preferred_x = max(screen.left(), left_x)
            fallback_x  = min(screen.right() - self.width(), right_x)
        else:
            preferred_x = min(screen.right() - self.width(), right_x)
            fallback_x  = max(screen.left(), left_x)
        if self._snap_side == "right" and right_x + self.width() <= screen.right():
            x = right_x
        elif self._snap_side == "left" and left_x >= screen.left():
            x = left_x
        else:
            x = fallback_x if fallback_x >= screen.left() else preferred_x
        y = max(screen.top(), min(self._anchor.y(), screen.bottom() - self.height()))
        if self.x() != x or self.y() != y:
            self.move(x, y)

    def _restore_or_default_position(self):
        screen = QApplication.primaryScreen().availableGeometry()
        saved  = settings.get("live_threat_window_pos")
        if saved and isinstance(saved, list) and len(saved) == 2:
            x = max(screen.left(), min(saved[0], screen.right()  - self.width()))
            y = max(screen.top(),  min(saved[1], screen.bottom() - self.height()))
            self.move(x, y)
            return
        if self._anchor and self._anchor.isVisible():
            self._sync_to_anchor()
        else:
            self.move(screen.left() + 40, screen.top() + 80)

    def _save_position(self):
        p = self.pos()
        settings.set("live_threat_window_pos", [p.x(), p.y()])
        settings.set("live_threat_window_snapped", self._snapped_to_anchor)
        settings.set("live_threat_window_snap_side", self._snap_side)

    def _try_snap_to_anchor(self):
        if not self._anchor or not self._anchor.isVisible():
            self._snapped_to_anchor = False
            self._save_position()
            return
        anchor_top = self._anchor.y()
        right_x    = self._anchor.x() + self._anchor.width() + THREAT_GAP
        left_x     = self._anchor.x() - self.width() - THREAT_GAP
        close_right = abs(self.x() - right_x) <= 30 and abs(self.y() - anchor_top) <= 40
        close_left  = abs(self.x() - left_x)  <= 30 and abs(self.y() - anchor_top) <= 40
        if close_right:
            self._snapped_to_anchor = True
            self._snap_side = "right"
            self._sync_to_anchor()
        elif close_left:
            self._snapped_to_anchor = True
            self._snap_side = "left"
            self._sync_to_anchor()
        else:
            self._snapped_to_anchor = False
        self._save_position()

    # ── Refresh ───────────────────────────────────────────────────────────────

    def _refresh(self):
        if not self._anchor or not self._anchor.isVisible():
            if self.isVisible():
                self.hide()
            return
        if self._snapped_to_anchor:
            self._sync_to_anchor()

        rows = self._tracker.threat_snapshot()
        targeted_count = sum(1 for r in rows if r["targeting_player"])

        if not self._tracker.in_combat or not rows:
            self._status_color = BORDER
        elif targeted_count == len(rows):
            self._status_color = ACCENT3
        elif targeted_count == 0:
            self._status_color = ACCENT2
        else:
            self._status_color = ACCENT

        self._count_lbl.setText(f"{len(rows)} mobs")
        self._table.setRowCount(len(rows))
        self._idle_lbl.setVisible(not rows)
        self._table.setVisible(bool(rows))

        def cell(text: str, *, fg: Optional[str] = None, bold: bool = False, align=None):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item.setTextAlignment(align or (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter))
            if fg:
                item.setForeground(QBrush(QColor(fg)))
            if bold:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            return item

        for row_idx, row in enumerate(rows):
            mob_name = row["name"]
            if row["targeting_player"]:
                mob_name = f"{mob_name}  👁"
            self._table.setItem(row_idx, 0, cell(mob_name, bold=row["targeting_player"]))
            strength = hunt_classification_for_npc(row.get("npc_entity_id", ""))
            self._table.setItem(row_idx, 1, cell(
                strength, fg=ACCENT if strength else TEXT_SEC, bold=bool(strength)
            ))
            target_text = row["current_target"] or row["last_threat_target"] or ""
            self._table.setItem(row_idx, 2, cell(target_text, fg=TEXT_PRI, bold=bool(target_text)))

        self._table.resizeColumnsToContents()
        if self._table.columnWidth(0) < THREAT_MOB_MIN_WIDTH:
            self._table.setColumnWidth(0, THREAT_MOB_MIN_WIDTH)
        self._table.resizeRowsToContents()

        frame_width  = self._table.frameWidth() * 2
        content_w    = self._table.horizontalHeader().length()
        desired_w    = content_w + frame_width + LIVE_PADDING * 2 + 12
        self.setFixedWidth(max(THREAT_MIN_WIDTH, min(desired_w, THREAT_MAX_WIDTH)))

        visible_rows = max(len(rows), 1)
        height = LIVE_HEADER_H + 12 + min(visible_rows, 8) * 28 + LIVE_PADDING * 2 + 36
        self.setFixedHeight(max(height, 130))

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(14, 17, 23, 225))
        p.setPen(QColor(self._status_color))
        p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 10, 10)
        p.end()

    # ── Drag ──────────────────────────────────────────────────────────────────

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._snapped_to_anchor = False

    def mouseMoveEvent(self, e):
        if self._drag_pos and e.buttons() == Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None
        self._try_snap_to_anchor()
