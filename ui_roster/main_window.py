"""
ui_roster.main_window
=====================

The Roster's top-level window. Three vertical regions stacked in a
QSplitter so the user can re-balance them:

  ┌──────────────────────────────────┐
  │ Players      (search + table)    │  ~30%
  ├──────────────────────────────────┤
  │ Bosses       (per-player table)  │  ~30%
  ├──────────────────────────────────┤
  │ Comparison   (role cohort table) │  ~40%
  └──────────────────────────────────┘

Wiring is one-way top-to-bottom:
    PlayersPanel.player_selected  →  BossesPanel.set_player
    BossesPanel.boss_selected     →  ComparisonPanel.set_context

The MainWindow itself owns no state beyond the panels' references. All
DB reads happen inside the panels.

Read-only window. No menus, no toolbars, no Save/Open/Edit affordances.
The whole point of the Roster is staying out of the way.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QMainWindow, QSplitter, QStatusBar, QWidget

from ui_roster.bosses_panel import BossesPanel
from ui_roster.comparison_panel import ComparisonPanel
from ui_roster.players_panel import PlayersPanel


# Default initial sizes for the three regions, in the splitter's pixel space.
# Qt re-distributes proportionally on resize, so the absolute numbers only
# matter for ratios. 30/30/40 puts the analytical region at the bottom with
# the most room — that's where the user spends the most time once they've
# drilled in.
DEFAULT_REGION_SIZES = (300, 300, 400)


class RosterMainWindow(QMainWindow):
    """
    The Roster window. Construct it, .show() it, done.

    Constructor takes an optional parent so it can be embedded as a child
    window of the main parser app later. v1 launches it standalone.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Roster — SWTOR Player History")
        self.resize(900, 1000)

        # Build the three panels first, then wire signals. Building them
        # in this order means each one renders its initial empty state
        # without firing cascade-clear signals that would re-trigger
        # rebuilds for no benefit.
        self._players = PlayersPanel(self)
        self._bosses = BossesPanel(self)
        self._comparison = ComparisonPanel(self)

        # Wiring: top → middle → bottom, one-way.
        self._players.player_selected.connect(self._bosses.set_player)
        self._bosses.boss_selected.connect(self._on_boss_selected)

        # Splitter holds the three regions stacked vertically.
        splitter = QSplitter(Qt.Orientation.Vertical, self)
        splitter.setChildrenCollapsible(False)  # Don't let the user accidentally crush a panel to 0px
        splitter.setHandleWidth(4)
        splitter.addWidget(self._players)
        splitter.addWidget(self._bosses)
        splitter.addWidget(self._comparison)
        splitter.setSizes(list(DEFAULT_REGION_SIZES))

        # Stretch factors mirror the default sizes so manual resizes feel
        # natural. Without these, dragging the window taller dumps all the
        # new pixels into whichever panel was last touched.
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 4)

        self.setCentralWidget(splitter)

        # Status bar — quiet hint that the window is read-only. Helps the
        # user understand why there's no Save button anywhere.
        status = QStatusBar(self)
        status.showMessage("Read-only · Roster never writes to the database")
        self.setStatusBar(status)

        # Keyboard shortcut: Ctrl+R refreshes the player list. Useful when
        # the main parser app has just imported a new log behind us.
        refresh_shortcut = QShortcut(QKeySequence("Ctrl+R"), self)
        refresh_shortcut.activated.connect(self._players.refresh)

    # ── Wiring helpers ──────────────────────────────────────────────────────

    def _on_boss_selected(
        self, boss_name: str, class_name: str, discipline_name: str
    ) -> None:
        """
        Bridge from BossesPanel's three-arg signal to ComparisonPanel's
        four-arg context setter. The fourth piece — the player name — is
        held by PlayersPanel and we pull it freshly each time rather than
        caching, because the user may have changed the player selection
        between boss clicks (yes, even on signal-cascade clears).
        """
        player_name = self._players.selected_player_name()
        self._comparison.set_context(
            player_name=player_name,
            boss_name=boss_name,
            class_name=class_name,
            discipline_name=discipline_name,
        )
