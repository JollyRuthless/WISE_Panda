"""
ui/splash.py - Lightweight startup splash for W.I.S.E. Panda.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

ACCENT = "#58a6ff"
ACCENT2 = "#3fb950"
BG_DARK = "#0e1117"
BG_PANEL = "#161b22"
BORDER = "#30363d"
TEXT_PRI = "#e6edf3"
TEXT_SEC = "#8b949e"


class AppSplash(QWidget):
    """Small frameless loading window shown while the app starts."""

    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.SplashScreen
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(460, 240)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        panel = QFrame()
        panel.setObjectName("splashPanel")
        panel.setStyleSheet(
            f"""
            QFrame#splashPanel {{
                background: {BG_DARK};
                border: 1px solid {BORDER};
                border-radius: 8px;
            }}
            QLabel {{
                background: transparent;
                color: {TEXT_PRI};
            }}
            QProgressBar {{
                background: {BG_PANEL};
                border: 1px solid {BORDER};
                border-radius: 4px;
                height: 8px;
                text-align: center;
            }}
            QProgressBar::chunk {{
                background: {ACCENT};
                border-radius: 3px;
            }}
            """
        )
        outer.addWidget(panel)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)

        mark = QLabel("PANDA")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setStyleSheet(
            f"color:{ACCENT2}; font-size:11px; font-weight:700; letter-spacing: 2px;"
        )
        layout.addWidget(mark)

        title = QLabel("W.I.S.E. Panda")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(f"color:{ACCENT}; font-size:28px; font-weight:800;")
        layout.addWidget(title)

        subtitle = QLabel("Workflow Insight & Skill Engine")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setStyleSheet(f"color:{TEXT_SEC}; font-size:12px;")
        layout.addWidget(subtitle)

        layout.addStretch(1)

        self.message_label = QLabel("Starting...")
        self.message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message_label.setStyleSheet(f"color:{TEXT_SEC}; font-size:12px;")
        layout.addWidget(self.message_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)

    def show_message(self, message: str):
        self.message_label.setText(message)
        self.repaint()
