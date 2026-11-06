from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from .config import Settings, data_directory
from .ui import STYLE, Window, app_icon


def main():
    parser = argparse.ArgumentParser(description="Crate Transfer desktop agent")
    parser.add_argument("--data-dir", type=Path, help="Override agent settings directory (not Serato's Library)")
    parser.add_argument("--smoke-test", action="store_true", help="Render UI without LAN services and quit")
    parser.add_argument("--background", action="store_true", help="Start quietly in the menu bar / system tray")
    parser.add_argument("--screenshot", type=Path, help="Save a screenshot during --smoke-test")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Crate Transfer")
    app.setOrganizationName("CrateTransfer")
    app.setWindowIcon(app_icon())
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    try:
        if sys.platform == "win32":
            import ctypes
            result = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("app.cratetransfer.desktop")
            if result != 0:
                raise OSError(f"Could not set Windows application identity (HRESULT {result:#x}).")
        directory = args.data_dir or data_directory()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = QLockFile(str(directory / "agent.lock"))
        if not lock.tryLock(0):
            raise RuntimeError("Crate Transfer is already running. Open it from the menu bar / system tray.")
        settings = Settings(directory)
        handler = RotatingFileHandler(settings.directory / "application.log", maxBytes=2 * 1024**2, backupCount=3)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)
        window = Window(settings, start_services=not args.smoke_test)
    except Exception as error:
        logging.exception("Startup failed")
        QMessageBox.critical(None, "Crate Transfer could not start", str(error))
        return 1
    app.setQuitOnLastWindowClosed(False)
    if not args.background or args.smoke_test or not QSystemTrayIcon.isSystemTrayAvailable():
        window.show()
    if args.smoke_test:
        def finish():
            if args.screenshot:
                window.grab().save(str(args.screenshot))
            window.quit()
        QTimer.singleShot(500, finish)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
