from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re
import sys

from .library import Library


@dataclass(frozen=True)
class SourceFormat:
    key: str
    name: str
    enabled: bool = True
    reason: str = ""


SOURCES = (
    SourceFormat("serato", "Serato DJ 4+ library"),
    SourceFormat("serato3", "Serato DJ 3 legacy crates (preview)"),
    SourceFormat("rekordbox-native", "rekordbox native database"),
    SourceFormat("traktor", "Traktor native collection"),
    SourceFormat("djay", "Algoriddim djay Pro native library (preview)"),
    SourceFormat("engine", "Engine DJ native library (preview)"),
    SourceFormat("virtualdj-native", "VirtualDJ native database (preview)"),
    SourceFormat("mixxx", "Mixxx native library (preview)"),
    SourceFormat("rekordbox-xml", "rekordbox XML (export file)"),
    SourceFormat("traktor-nml", "Traktor NML (collection or playlist file)"),
    SourceFormat("m3u", "M3U playlists (including VirtualDJ exports)"),
)

OUTPUT_NAMES = {
    "serato": "Serato library (automatic import)",
    "serato3": "Serato DJ 3 legacy crates (preview native import)",
    "rekordbox-native": "rekordbox native library (runtime/schema-gated automatic import)",
    "traktor": "Traktor native collection (automatic import)",
    "djay": "djay Pro native library (preview automatic import)",
    "engine": "Engine DJ native library (schema-gated preview)",
    "virtualdj-native": "VirtualDJ native library (preview automatic import)",
    "mixxx": "Mixxx native playlists (preview automatic import)",
    "m3u": "M3U playlists + files",
    "rekordbox-xml": "rekordbox XML + files (manual app import)",
    "traktor-nml": "Traktor NML + files (manual app import)",
}


def source_definition(format: str) -> SourceFormat:
    source = next((source for source in SOURCES if source.key == format), None)
    if source is None:
        raise ValueError("Unknown source DJ format.")
    return source


def default_source_paths(format: str, home: Path | None = None, platform: str | None = None) -> list[Path]:
    home = home or Path.home()
    platform = platform or sys.platform
    local = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local")) if platform == "win32" else home / "Library"
    documents = home / "Documents"
    if format == "serato":
        return [local / "Serato/Library"] if platform == "win32" else [local / "Application Support/Serato/Library"]
    if format == "serato3":
        return [home / "Music/_Serato_"]
    if format == "rekordbox-native":
        return [home / "AppData/Roaming/Pioneer/rekordbox/master.db"] if platform == "win32" else [
            local / "Pioneer/rekordbox/master.db"]
    if format == "engine":
        return [home / "Music/Engine Library/Database2"]
    if format == "djay":
        return [home / "Music/djay/djay Media Library.djayMediaLibrary"]
    if format == "virtualdj-native":
        return [documents / "VirtualDJ/database.xml"]
    if format == "mixxx":
        if platform == "win32":
            return [local / "Mixxx/mixxxdb.sqlite"]
        return [home / "Library/Containers/org.mixxx.mixxx/Data/Library/Application Support/Mixxx/mixxxdb.sqlite",
                home / "Library/Application Support/Mixxx/mixxxdb.sqlite"]
    return []


def default_destination_path(format: str, home: Path | None = None, platform: str | None = None) -> str:
    platform = platform or sys.platform
    if format == "rekordbox-native" and platform == "darwin":
        return str(default_source_paths(format, home, platform)[0].parent)
    if format == "traktor":
        collection = find_source(format, home)
        return str(collection) if collection is not None else ""
    return ""


def find_source(format: str, home: Path | None = None) -> Path | None:
    """Check usual paths without opening a library."""
    home = home or Path.home()
    candidates = default_source_paths(format, home)
    if format in ("traktor-nml", "traktor"):
        native = home / "Documents/Native Instruments"
        if native.is_dir():
            candidates = sorted(
                native.glob("Traktor */collection.nml"),
                key=lambda path: tuple(int(part) for part in re.findall(r"\d+", path.parent.name)),
                reverse=True,
            )
        return next((path for path in candidates if path.is_file()), None)
    return next((path for path in candidates if path.exists()), None)


def source_exists(format: str, path: str) -> bool:
    if not path.strip():
        return False
    candidate = Path(path).expanduser()
    if format == "serato":
        return any((folder / "root.sqlite").is_file() and (folder / "master.sqlite").is_file()
                   for folder in (candidate, candidate / "Library"))
    if format == "serato3":
        folder = candidate.parent if candidate.name == "Subcrates" else candidate
        return folder.name == "_Serato_" and (folder / "Subcrates").is_dir()
    if format == "djay":
        return (candidate if candidate.name == "MediaLibrary.db" else candidate / "MediaLibrary.db").is_file()
    if format in ("traktor", "traktor-nml"):
        return (candidate / "collection.nml").is_file() if candidate.is_dir() else candidate.is_file()
    if format == "virtualdj-native":
        return (candidate / "database.xml").is_file() if candidate.is_dir() else candidate.is_file()
    if format == "rekordbox-native":
        return (candidate / "master.db").is_file() if candidate.is_dir() else candidate.is_file()
    if format == "mixxx":
        return (candidate / "mixxxdb.sqlite").is_file() if candidate.is_dir() else candidate.is_file()
    if format == "engine":
        return any(path.is_file() for path in (candidate, candidate / "m.db", candidate / "Database2/m.db"))
    return candidate.exists()


def source_guidance(format: str) -> str:
    if format == "serato":
        return ("Serato DJ 4 or higher: supported SQLite schema required. Read-only browsing normally works "
                "while Serato is open; close Serato if snapshot access is required. Close it on the destination before import.")
    if format == "serato3":
        return ("Close Serato DJ before reading/importing DJ 3 legacy crates. Choose _Serato_, not DJ 4's "
                "SQLite Library. Media must be on the same local volume. DJ 3 reloads/scans received media; "
                "database-only performance metadata is not converted.")
    if format in ("traktor-nml", "traktor"):
        return "Close Traktor before reading its live collection.nml so your latest saved playlists are available."
    if format == "rekordbox-xml":
        return "Export your current library to rekordbox XML first. rekordbox need not be closed to read that export file."
    if format == "m3u":
        return "Choose an exported M3U/M3U8 playlist or folder. The DJ app need not be closed to read exported playlists."
    if format == "mixxx":
        return "Close Mixxx first to save current playlists/crates. Native reading is read-only; cues/loops/grid are not converted yet."
    if format == "djay":
        return ("Close djay before reading/importing its native library. Preview TSAF adapter: new-media file access "
                "and playback must be verified in djay; full analysis and sandbox bookmarks are not generated.")
    if format == "rekordbox-native":
        return ("Close rekordbox before accessing its native library. Supported schema and runtime are required; "
                "check Activity for unsupported metadata or analysis warnings.")
    if format in ("virtualdj-native", "engine"):
        return ("Close the source DJ application before accessing its native library. This is a schema/runtime-gated "
                "preview; check Activity for unsupported metadata or analysis warnings.")
    return "Native integration is not yet available here. Do not modify the library; use an available exchange format."


def source_library(format: str, path: str | Path):
    source = source_definition(format)
    if not source.enabled:
        raise ValueError(source.reason)
    if not str(path).strip():
        raise ValueError("Choose the source library or playlist file first.")
    if format == "serato":
        return Library(path)
    if format == "mixxx":
        from .mixxx import MixxxLibrary
        return MixxxLibrary(path)
    if format in ("serato3", "djay", "traktor", "virtualdj-native", "engine", "rekordbox-native"):
        from .native import native_library
        library, guard = native_library(format, str(path))
        guard()
        return library
    from .exchange import ExchangeLibrary
    return ExchangeLibrary(format, path)
