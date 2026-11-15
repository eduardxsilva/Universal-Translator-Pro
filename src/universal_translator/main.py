"""Ponto de entrada principal do pacote Universal Translator (compatível com PyInstaller --onefile)."""
from __future__ import annotations

import sys
from pathlib import Path


def get_resource_path(relative_path: str) -> Path:
    """Resolve caminho de recursos tanto em execução normal quanto empacotado via PyInstaller (_MEIPASS)."""
    base_path = getattr(sys, "_MEIPASS", None)
    if base_path:
        return Path(base_path) / relative_path
    return Path(__file__).resolve().parents[2] / relative_path


def main() -> int:
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from .presentation.windows.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setOrganizationName("UniversalTranslator")
    app.setApplicationName("UniversalTranslator")
    app.setQuitOnLastWindowClosed(False)

    icon_path = get_resource_path("assets/icon.ico")
    if icon_path.exists():
        app_icon = QIcon(str(icon_path))
        app.setWindowIcon(app_icon)

    window = MainWindow()
    if icon_path.exists():
        window.setWindowIcon(QIcon(str(icon_path)))
        window.tray.setIcon(QIcon(str(icon_path)))

    app.aboutToQuit.connect(window.cleanup)
    window.show()
    return int(app.exec())


if __name__ == "__main__":
    sys.exit(main())
