from __future__ import annotations

from pathlib import Path


NATIVE_NAMES = {
    "serato": "Serato",
    "serato3": "Serato DJ 3",
    "djay": "djay Pro",
    "traktor": "Traktor",
    "virtualdj-native": "VirtualDJ",
    "engine": "Engine DJ",
    "rekordbox-native": "rekordbox",
    "mixxx": "Mixxx",
}


def native_library(format: str, path: str):
    if format == "serato3":
        from .serato3 import Serato3Library, assert_serato_closed
        assert_serato_closed()
        return Serato3Library(path), assert_serato_closed
    if format == "mixxx":
        from .mixxx import MixxxLibrary, assert_mixxx_closed
        assert_mixxx_closed()
        return MixxxLibrary(path), assert_mixxx_closed
    if format == "djay":
        from .djay import DjayLibrary, assert_djay_closed
        assert_djay_closed()
        return DjayLibrary(path), assert_djay_closed
    if format == "traktor":
        from .traktor import TraktorLibrary, assert_traktor_closed
        assert_traktor_closed()
        return TraktorLibrary(path), assert_traktor_closed
    if format == "virtualdj-native":
        from .virtualdj import VirtualDJLibrary, assert_virtualdj_closed
        assert_virtualdj_closed()
        return VirtualDJLibrary(path), assert_virtualdj_closed
    if format == "engine":
        from .engine import EngineLibrary, assert_engine_closed
        assert_engine_closed()
        return EngineLibrary(path), assert_engine_closed
    if format == "rekordbox-native":
        from .rekordbox import RekordboxLibrary, assert_rekordbox_closed
        assert_rekordbox_closed()
        return RekordboxLibrary(path), assert_rekordbox_closed
    raise ValueError("Unsupported native DJ destination.")


def destination_path(settings: dict, format: str) -> str:
    if format == "djay":
        return settings["djay_library"]
    return settings["native_libraries"].get(format, "")


def destination_enabled(settings: dict, format: str) -> bool:
    if format == "djay":
        return settings["djay_receive"] is True
    return settings["native_receive"].get(format) is True


def backup_size(library) -> int:
    paths = getattr(library, "backup_paths", [library.path])
    return sum(Path(path).stat().st_size for path in paths if Path(path).is_file())
