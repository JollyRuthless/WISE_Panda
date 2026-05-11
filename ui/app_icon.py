"""
ui/app_icon.py - Application icon loading and customization helpers.
"""

from pathlib import Path

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QWidget

from ui.settings import settings


APP_USER_MODEL_ID = "wise.panda.swtor.parser"
ICON_SETTING_KEY = "app_icon_path"
PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_ICON_PATHS = (
    PROJECT_ROOT / "assets" / "app_icon.ico",
    PROJECT_ROOT / "assets" / "app_icon.png",
)


def set_windows_app_user_model_id():
    """Help Windows group taskbar windows under this app's icon."""
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass


def configured_icon_path() -> Path | None:
    custom_path = settings.get(ICON_SETTING_KEY, "")
    if custom_path:
        path = Path(custom_path)
        if path.exists():
            return path

    for path in DEFAULT_ICON_PATHS:
        if path.exists():
            return path
    return None


def load_app_icon() -> QIcon:
    path = configured_icon_path()
    if path is None:
        return QIcon()
    icon = QIcon(str(path))
    if icon.isNull():
        return QIcon()
    return icon


def apply_app_icon(widget: QWidget | None = None) -> QIcon:
    icon = load_app_icon()
    if icon.isNull():
        return icon

    app = QApplication.instance()
    if app is not None:
        app.setWindowIcon(icon)
        for top_level in app.topLevelWidgets():
            top_level.setWindowIcon(icon)

    if widget is not None:
        widget.setWindowIcon(icon)
    return icon


def set_custom_app_icon(path: str, widget: QWidget | None = None) -> bool:
    icon = QIcon(path)
    if icon.isNull():
        return False
    settings.set(ICON_SETTING_KEY, str(Path(path)))
    apply_app_icon(widget)
    return True
