"""
W.I.S.E. Panda — SWTOR Combat Parser
Workflow Insight & Skill Engine

Install: pip install PyQt6 pyqtgraph watchdog
Run:     python main.py
"""

import sys
from PyQt6.QtWidgets import QApplication
from ui.app_icon import apply_app_icon, set_windows_app_user_model_id
from ui.splash import AppSplash


def main():
    set_windows_app_user_model_id()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    apply_app_icon()
    splash = AppSplash()
    splash.show()
    splash.show_message("Loading application modules...")
    app.processEvents()

    from ui.main_window import MainWindow

    initial_path = sys.argv[1] if len(sys.argv) > 1 else None
    window = MainWindow(
        initial_path=initial_path,
        startup_status=splash.show_message,
        startup_finished=splash.close,
    )
    splash.show_message("Opening main window...")
    app.processEvents()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
