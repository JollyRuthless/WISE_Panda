"""
Roster — SWTOR Player History (entry point)
============================================

Launches the Roster window as a standalone app.

Usage:
    py roster.py

The window is read-only against the same SQLite database the main parser
app (Yoda / W.I.S.E. Panda) writes to. Both apps can be open at once;
nothing here writes to the DB so concurrent reads with the parser's
writes are safe under SQLite's default WAL mode.

Why standalone:
    The parser app is large. Adding more tabs to it makes the cognitive
    load worse, not better. Roster is a deliberate clean window into the
    same data, organised around "how does this player compare to others on
    this boss." If/when it earns embedding, RosterMainWindow takes a parent
    arg already and can dock inside the parser without changes.
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from ui_roster.main_window import RosterMainWindow


def main() -> int:
    # QApplication.instance() check lets this entry point be imported and
    # called from tests or from the parser app's "Tools → Open Roster"
    # menu without falling over a duplicate-app error.
    app = QApplication.instance()
    owns_app = app is None
    if owns_app:
        app = QApplication(sys.argv)

    window = RosterMainWindow()
    window.show()

    if owns_app:
        return app.exec()
    return 0


if __name__ == "__main__":
    sys.exit(main())
