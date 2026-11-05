from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path


def startup_command(directory: Path) -> list[str]:
    if not getattr(sys, "frozen", False):
        raise ValueError("Launch at login is available in the installed app, not a development Python session.")
    executable = Path(sys.executable).resolve()
    if sys.platform == "darwin" and not str(executable).startswith("/Applications/"):
        raise ValueError("Copy Crate Transfer to Applications before enabling launch at login.")
    return [str(executable), "--background", "--data-dir", str(directory.resolve())]


def enabled() -> bool:
    if sys.platform == "darwin":
        return (Path.home() / "Library/LaunchAgents/app.cratetransfer.desktop.plist").exists()
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                winreg.QueryValueEx(key, "CrateTransfer")
            return True
        except FileNotFoundError:
            return False
    return False


def configure(enable: bool, directory: Path) -> None:
    command = startup_command(directory) if enable else None
    if sys.platform == "darwin":
        path = Path.home() / "Library/LaunchAgents/app.cratetransfer.desktop.plist"
        if enable:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            with temporary.open("wb") as output:
                os.chmod(temporary, 0o600)
                plistlib.dump({"Label": "app.cratetransfer.desktop", "ProgramArguments": command,
                               "RunAtLoad": True, "ProcessType": "Interactive"}, output)
            temporary.replace(path)
        else:
            path.unlink(missing_ok=True)
    elif sys.platform == "win32":
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            if enable:
                winreg.SetValueEx(key, "CrateTransfer", 0, winreg.REG_SZ, subprocess.list2cmdline(command))
            else:
                try:
                    winreg.DeleteValue(key, "CrateTransfer")
                except FileNotFoundError:
                    pass
    else:
        raise ValueError("Launch at login is supported on macOS and Windows only.")
