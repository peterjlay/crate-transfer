from __future__ import annotations

import json
import os
import socket
import sys
import threading
import uuid
from pathlib import Path


def data_directory() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "CrateTransfer"
    return Path.home() / "Library/Application Support/CrateTransfer"


def library_directory() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Serato/Library"
    return Path.home() / "Library/Application Support/Serato/Library"


def atomic_json(path: Path, value: dict | list) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        os.chmod(temporary, 0o600)
        json.dump(value, output, ensure_ascii=True, indent=2)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


class Settings:
    def __init__(self, directory: Path | None = None):
        self.directory = directory or data_directory()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "settings.json"
        self.lock = threading.RLock()
        self.value = {
            "id": str(uuid.uuid4()),
            "name": socket.gethostname().split(".")[0],
            "library": str(library_directory()),
            "music": str(Path.home() / "Music/Serato Transfers"),
            "port": 49783,
            "peers": {},
            "trusted": {},
        }
        if self.path.exists():
            with self.path.open(encoding="utf-8-sig") as source:
                self.value.update(json.load(source))
        self.save()

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.value))

    def update(self, **values) -> None:
        with self.lock:
            self.value.update(values)
            self.save()

    def save(self) -> None:
        with self.lock:
            atomic_json(self.path, self.value)
