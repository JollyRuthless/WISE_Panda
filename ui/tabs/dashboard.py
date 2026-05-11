"""
ui/tabs/dashboard.py — startup dashboard for the app shell.
"""

from dataclasses import dataclass

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QGridLayout, QFrame,
)

from ui.theme import TEXT_SEC


@dataclass
class DashboardSnapshot:
    current_log_name: str
    loaded_fight_count: int
    encounter_count: int
    imported_log_count: int
    imported_event_count: int
    imported_character_count: int
    seen_player_count: int
    latest_status: str


class DashboardStatCard(QFrame):
    def __init__(self, label: str, accent: str):
        super().__init__()
        self.setObjectName("dashboardCard")
        self.setStyleSheet(
            f"QFrame#dashboardCard {{ border: 1px solid {accent}; border-radius: 10px; padding: 8px; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(4)

        self.value_label = QLabel("0")
        self.value_label.setStyleSheet("font-size: 24px; font-weight: 700;")
        layout.addWidget(self.value_label)

        caption = QLabel(label)
        caption.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        layout.addWidget(caption)

    def set_value(self, value: str):
        self.value_label.setText(value)


class DashboardTab(QWidget):
    open_log_requested = pyqtSignal()
    import_logs_requested = pyqtSignal()
    import_all_logs_requested = pyqtSignal()
    characters_requested = pyqtSignal()
    seen_players_requested = pyqtSignal()
    import_history_requested = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(16)

        title = QLabel("Dashboard")
        title.setObjectName("title")
        root.addWidget(title)

        subtitle = QLabel(
            "Start here. This dashboard gives the app a home base for imports, roster work, and the current session."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        root.addWidget(subtitle)

        actions = QHBoxLayout()
        self.open_log_btn = QPushButton("Open Combat Log")
        self.open_log_btn.clicked.connect(self.open_log_requested.emit)
        actions.addWidget(self.open_log_btn)

        self.import_logs_btn = QPushButton("Import Logs")
        self.import_logs_btn.clicked.connect(self.import_logs_requested.emit)
        actions.addWidget(self.import_logs_btn)

        self.import_all_btn = QPushButton("Import All")
        self.import_all_btn.clicked.connect(self.import_all_logs_requested.emit)
        actions.addWidget(self.import_all_btn)

        self.characters_btn = QPushButton("Characters")
        self.characters_btn.clicked.connect(self.characters_requested.emit)
        actions.addWidget(self.characters_btn)

        self.players_btn = QPushButton("Seen Players")
        self.players_btn.clicked.connect(self.seen_players_requested.emit)
        actions.addWidget(self.players_btn)

        self.history_btn = QPushButton("Import History")
        self.history_btn.clicked.connect(self.import_history_requested.emit)
        actions.addWidget(self.history_btn)
        actions.addStretch()
        root.addLayout(actions)

        self.current_log_label = QLabel("No combat log loaded for this session.")
        self.current_log_label.setStyleSheet("font-size: 13px; font-weight: 600;")
        root.addWidget(self.current_log_label)

        self.status_label = QLabel("No recent status yet.")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        root.addWidget(self.status_label)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)

        self.loaded_fights_card = DashboardStatCard("Loaded fights in current session", "#4c8bf5")
        self.encounters_card = DashboardStatCard("Encounter rows stored", "#f78166")
        self.imported_logs_card = DashboardStatCard("Imported combat logs", "#3fb950")
        self.imported_events_card = DashboardStatCard("Imported log rows", "#d29922")
        self.characters_card = DashboardStatCard("Tracked player characters", "#58a6ff")
        self.players_card = DashboardStatCard("Seen players cached", "#bc8cff")

        cards = [
            self.loaded_fights_card,
            self.encounters_card,
            self.imported_logs_card,
            self.imported_events_card,
            self.characters_card,
            self.players_card,
        ]
        for idx, card in enumerate(cards):
            grid.addWidget(card, idx // 3, idx % 3)
        root.addLayout(grid)
        root.addStretch()

    def refresh_snapshot(self, snapshot: DashboardSnapshot):
        current_log = snapshot.current_log_name or "No combat log loaded for this session."
        self.current_log_label.setText(current_log)
        latest_status = snapshot.latest_status or "No recent status yet."
        self.status_label.setText(latest_status)
        self.loaded_fights_card.set_value(f"{snapshot.loaded_fight_count:,}")
        self.encounters_card.set_value(f"{snapshot.encounter_count:,}")
        self.imported_logs_card.set_value(f"{snapshot.imported_log_count:,}")
        self.imported_events_card.set_value(f"{snapshot.imported_event_count:,}")
        self.characters_card.set_value(f"{snapshot.imported_character_count:,}")
        self.players_card.set_value(f"{snapshot.seen_player_count:,}")
