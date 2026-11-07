"""Bounded native Traktor collection support.

References: digital-dj-tools/dj-data-converter traktor/cue.clj and
OpenFadeR compatibility/traktor (NML playlists and primary keys).
Native application acceptance has not been tested.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import psutil

from .exchange import (ExchangeLibrary, _check_metadata, _number, _read_xml,
                       _traktor_key, _traktor_location, _traktor_path)
from .library import LibraryError, NativeImportUncertain, sha256_file
from .protocol import validate_manifest


def _closed(prefixes, app):
    try:
        for process in psutil.process_iter(["name"]):
            try:
                name = (process.info["name"] or "").casefold()
            except psutil.NoSuchProcess:
                continue
            if name.startswith(prefixes):
                raise LibraryError(f"Quit {app} before accessing its library.")
    except psutil.Error as error:
        raise LibraryError(f"Cannot check whether {app} is running; import refused.") from error


def assert_traktor_closed():
    _closed(("traktor",), "Traktor")


def _within(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _destination(directory, music):
    music = Path(music).expanduser().resolve()
    if _within(music, directory) or _within(directory, music):
        raise LibraryError("Keep received music separate from the library folder.")
    if music.exists() and not music.is_dir():
        raise LibraryError("Received music must be a folder.")


def _installed(manifest, installed, directory):
    validate_manifest(manifest)
    records = {r["id"]: r for r in manifest["files"]}
    if set(installed) != set(records):
        raise LibraryError("Received files do not match the manifest.")
    paths, states = {}, {}
    for key, candidate in installed.items():
        path = Path(candidate).resolve()
        if _within(path, directory) or not path.is_file() or path in paths.values():
            raise LibraryError("Received tracks must be distinct local files outside the library folder.")
        before = _state(path)
        digest = sha256_file(path)
        after = _state(path)
        if before != after or after[0] != records[key]["size"] or digest != records[key]["sha256"]:
            raise LibraryError("Received file changed or failed checksum verification.")
        paths[key], states[path] = path, after
    return records, paths, states


def _state(path):
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns, stat.st_ino, stat.st_ctime_ns)


def _xml(path):
    # Check the safety limits and declarations before parsing again with comments retained.
    _read_xml(path)
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    return ET.parse(path, parser=parser).getroot()


def _location(path, root_volume=None):
    volume, folder, filename = _traktor_location(path)
    if sys.platform == "darwin":
        parts = path.parts
        if len(parts) > 3 and parts[1] == "Volumes":
            volume = parts[2]
            remaining = parts[3:-1]
        else:
            if root_volume is None:
                try:
                    result = subprocess.run(["/usr/sbin/diskutil", "info", "-plist", "/"],
                                            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
                    root_volume = plistlib.loads(result.stdout)["VolumeName"]
                except (OSError, KeyError, ValueError, subprocess.SubprocessError) as error:
                    raise LibraryError("Cannot determine the macOS root volume name for Traktor.") from error
            volume = root_volume
            remaining = parts[1:-1]
        if not isinstance(volume, str) or not volume or "/" in volume:
            raise LibraryError("Unsupported macOS volume name for Traktor.")
        folder = "/:" + "/:".join(remaining) + "/:" if remaining else "/:"
    return volume, folder, filename


def _source_path(location, key):
    volume = location.get("VOLUME", "")
    directory = location.get("DIR", "")
    filename = location.get("FILE", "")
    if sys.platform == "darwin" and volume and "/" not in volume and directory.startswith("/:"):
        candidate = Path("/Volumes") / volume
        if candidate.is_dir():
            return _traktor_path("", "/:Volumes/:" + volume + directory, filename, f"Traktor track {key}")
    return _traktor_path(volume, directory, filename, f"Traktor track {key}")


def _bytes(root):
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _snapshot(path):
    state = _state(path)
    data = path.read_bytes()
    if _state(path) != state:
        raise LibraryError("Library changed while reading; retry with the application closed.")
    return state, data


def _publish(original, outputs, backup_dir, guard, closed, states, snapshot):
    """Stage/verify all files, back up originals, then publish with rollback on error."""
    backup_dir = Path(backup_dir).expanduser().resolve()
    directory = original.parent.resolve()
    if _within(backup_dir, directory) or _within(directory, backup_dir):
        raise LibraryError("Keep backups separate from the library folder.")
    original_state, original_bytes = snapshot
    if _state(original) != original_state or original.read_bytes() != original_bytes:
        raise LibraryError("Library changed during import; nothing was overwritten.")
    backup_dir.mkdir(parents=True, exist_ok=False)
    backup = backup_dir / original.name
    with backup.open("xb") as target:
        target.write(original_bytes)
        target.flush()
        os.fsync(target.fileno())
    if backup.read_bytes() != original_bytes:
        raise LibraryError("Library backup verification failed.")
    staged, published, made_dirs = {}, [], []
    try:
        for path, data in outputs.items():
            parent = path.parent
            missing = []
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            if parent.is_symlink() or not parent.is_dir():
                raise LibraryError("Unsafe library output directory.")
            for folder in reversed(missing):
                folder.mkdir()
                made_dirs.append(folder)
            if path != original and (path.exists() or path.is_symlink()):
                raise LibraryError("Playlist destination appeared during import.")
            fd, name = tempfile.mkstemp(prefix=".crate-transfer-", dir=str(path.parent))
            staged[path] = Path(name)
            with os.fdopen(fd, "wb") as target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            if path == original:
                os.chmod(staged[path], original.stat().st_mode & 0o777)
            if staged[path].read_bytes() != data:
                raise LibraryError("Staged library readback failed.")
            _read_xml(staged[path])
        guard()
        closed()
        if _state(original) != original_state or original.read_bytes() != original_bytes:
            raise LibraryError("Library changed during import; nothing was overwritten.")
        if any(_state(path) != state for path, state in states.items()):
            raise LibraryError("Received media changed during import.")
        # New files are published without overwriting; existing database is replaced last.
        for path in [p for p in outputs if p != original]:
            os.link(staged[path], path)
            published.append(path)
        os.replace(staged[original], original)
    except BaseException as error:
        try:
            for path in reversed(published):
                path.unlink(missing_ok=True)
        except OSError as rollback_error:
            raise NativeImportUncertain(
                f"Native playlist rollback failed; retain received media and restore from {backup_dir}: {rollback_error}"
            ) from error
        raise
    finally:
        for path in staged.values():
            path.unlink(missing_ok=True)
        for folder in reversed(made_dirs):
            try:
                folder.rmdir()
            except OSError:
                pass
    return str(backup_dir)


def _tree(root):
    if root.tag != "NML" or root.get("VERSION") not in ("19", "20"):
        raise LibraryError("Unsupported Traktor collection version.")
    collection, playlists = root.find("COLLECTION"), root.find("PLAYLISTS")
    if collection is None or playlists is None:
        raise LibraryError("Unsupported Traktor collection: missing COLLECTION or PLAYLISTS.")
    nodes = playlists.findall("NODE")
    if len(nodes) != 1 or nodes[0].get("TYPE") != "FOLDER" or nodes[0].get("NAME") != "$ROOT":
        raise LibraryError("Unsupported Traktor playlist root.")
    subnodes = nodes[0].find("SUBNODES")
    if subnodes is None:
        raise LibraryError("Unsupported Traktor playlist root children.")
    keys, uuids = set(), set()
    for entry in collection.findall("ENTRY"):
        loc = entry.find("LOCATION")
        if loc is None or not loc.get("FILE") or not loc.get("DIR"):
            raise LibraryError("Unsupported Traktor track location.")
        key = _traktor_key(loc)
        if key in keys:
            raise LibraryError("Duplicate Traktor track path.")
        keys.add(key)
    def visit(parent):
        for node in parent.findall("NODE"):
            if not node.get("NAME") or node.get("TYPE") not in ("PLAYLIST", "FOLDER"):
                raise LibraryError("Unsupported Traktor playlist node.")
            ident = node.get("UUID")
            if node.get("TYPE") == "PLAYLIST" and node.find("PLAYLIST") is not None:
                ident = node.find("PLAYLIST").get("UUID", ident)
            if ident:
                if ident in uuids:
                    raise LibraryError("Duplicate Traktor playlist UUID.")
                uuids.add(ident)
            if node.get("TYPE") == "FOLDER":
                child = node.find("SUBNODES")
                if child is None:
                    raise LibraryError("Traktor folder has no SUBNODES.")
                visit(child)
            else:
                playlist = node.find("PLAYLIST")
                if playlist is None:
                    raise LibraryError("Traktor playlist has no PLAYLIST.")
                for member in playlist.findall("ENTRY"):
                    ref = member.find("PRIMARYKEY")
                    if ref is None or ref.get("TYPE") != "TRACK" or ref.get("KEY") not in keys:
                        raise LibraryError("Traktor playlist has an unknown track reference.")
    visit(subnodes)
    return collection, subnodes


def _unique(parent, name):
    used = {n.get("NAME", "").casefold() for n in parent.findall("NODE")}
    candidate, number = name, 2
    while candidate.casefold() in used:
        candidate = f"{name} ({number})"
        number += 1
    return candidate


class TraktorLibrary(ExchangeLibrary):
    def __init__(self, path):
        candidate = Path(path).expanduser().resolve()
        self.path = candidate / "collection.nml" if candidate.is_dir() else candidate
        self.directory = self.path.parent
        self.format = "traktor"

    @property
    def backup_paths(self):
        return [self.path]

    def _load(self):
        if not self.path.is_file() or self.path.is_symlink():
            raise LibraryError("Existing Traktor collection.nml not found.")
        root = _xml(self.path)
        _tree(root)
        return root

    def _source(self):
        root = self._load()
        collection, subnodes = _tree(root)
        self.warnings = []
        tracks = {}
        for entry in collection.findall("ENTRY"):
            loc = entry.find("LOCATION")
            key = _traktor_key(loc)
            path = _source_path(loc, key)
            metadata = {k: entry.get(a) for a, k in (("TITLE", "title"), ("ARTIST", "artist")) if entry.get(a) is not None}
            album, tempo, info = entry.find("ALBUM"), entry.find("TEMPO"), entry.find("INFO")
            if album is not None and album.get("TITLE") is not None:
                metadata["album"] = album.get("TITLE")
            bpm = _number(tempo.get("BPM") if tempo is not None else None, positive=True)
            if bpm is None and info is not None:
                bpm = _number(info.get("BPM"), positive=True)
            if bpm:
                metadata["bpm"] = bpm
            if info is not None and info.get("KEY"):
                metadata["key"] = info.get("KEY")
            cues = []
            for cue in entry.findall("CUE_V2"):
                start = _number(cue.get("START"))
                length = _number(cue.get("LEN"))
                kind = cue.get("TYPE")
                if start is None or kind not in ("0", "5") or (kind == "5" and not length):
                    self.warnings.append("Unsupported Traktor cue or beatgrid data was omitted.")
                    continue
                item = {"kind": "loop" if kind == "5" else "cue", "position": start / 1000, "name": cue.get("NAME", "")}
                if kind == "5":
                    item["end"] = (start + length) / 1000
                try:
                    slot = int(cue.get("HOTCUE", "-1"))
                    if 0 <= slot <= 7:
                        item["slot"] = slot
                except ValueError:
                    pass
                cues.append(item)
            if cues:
                metadata["cues"] = cues
            tracks[key] = {"path": path, "metadata": _check_metadata(metadata, self.warnings)}
        self._playlists = []
        def visit(parent, prefix):
            for node in parent.findall("NODE"):
                parts = prefix + [node.get("NAME")]
                if node.get("TYPE") == "FOLDER":
                    visit(node.find("SUBNODES"), parts)
                else:
                    members = [e.find("PRIMARYKEY").get("KEY") for e in node.find("PLAYLIST").findall("ENTRY")]
                    self._playlists.append({"path": parts, "members": members, "tracks": tracks})
        visit(subnodes, [])
        self.warnings = list(dict.fromkeys(self.warnings))[:100]
        self._catalog = self._build_catalog()

    def crates(self):
        self._source()
        return super().crates()

    def export(self, selected, machine_id, name, progress=lambda text: None):
        self._source()
        return super().export(selected, machine_id, name, progress)

    def validate_destination(self, music):
        _destination(self.directory, music)
        self._load()

    def import_crates(self, manifest, installed, backup_dir, transfer_id, guard=lambda: None):
        guard()
        assert_traktor_closed()
        if not isinstance(transfer_id, str) or not transfer_id:
            raise LibraryError("A transfer identity is required.")
        records, paths, states = _installed(manifest, installed, self.directory)
        if not self.path.is_file() or self.path.is_symlink():
            raise LibraryError("Existing Traktor collection.nml not found.")
        snapshot = _snapshot(self.path)
        root = self._load()
        collection, top = _tree(root)
        existing = {_traktor_key(e.find("LOCATION")) for e in collection.findall("ENTRY")}
        keys, warnings = {}, list(manifest.get("warnings", []))
        warnings.append("Traktor native import is unverified in the application; analysis and waveforms are not generated.")
        root_volume = None
        for key, record in records.items():
            volume, folder, filename = _location(paths[key], root_volume)
            if sys.platform == "darwin" and paths[key].parts[1:2] != ("Volumes",):
                root_volume = volume
            loc = ET.Element("LOCATION", DIR=folder, FILE=filename)
            if volume:
                loc.set("VOLUME", volume)
                loc.set("VOLUMEID", volume)
            native_key = _traktor_key(loc)
            keys[key] = native_key
            if native_key in existing:
                warnings.append("Existing Traktor track metadata was retained.")
                continue
            existing.add(native_key)
            metadata = record.get("metadata", {})
            entry = ET.SubElement(collection, "ENTRY", TITLE=metadata.get("title", paths[key].stem),
                                  ARTIST=metadata.get("artist", ""))
            entry.append(loc)
            if "album" in metadata:
                ET.SubElement(entry, "ALBUM", TITLE=metadata["album"])
            if "bpm" in metadata:
                ET.SubElement(entry, "TEMPO", BPM=str(metadata["bpm"]))
            if "key" in metadata or metadata.get("beatgrid"):
                warnings.append("Traktor musical key and beatgrid metadata were omitted.")
            for index, cue in enumerate(metadata.get("cues", [])):
                slot = cue.get("slot", -1)
                if slot > 7:
                    warnings.append("Traktor cues outside eight hotcue slots were omitted.")
                    continue
                if cue["position"] > 7200 or cue.get("end", cue["position"]) > 7200:
                    warnings.append("Traktor cues beyond the supported two-hour range were omitted.")
                    continue
                length = cue["end"] - cue["position"] if cue["kind"] == "loop" else 0
                ET.SubElement(entry, "CUE_V2", NAME=cue.get("name", ""), DISPL_ORDER=str(index),
                              TYPE="5" if cue["kind"] == "loop" else "0",
                              START=str(cue["position"] * 1000), LEN=str(length * 1000),
                              REPEATS="-1", HOTCUE=str(slot))
        collection.set("ENTRIES", str(len(collection.findall("ENTRY"))))
        folders = {(): top}
        for crate in manifest["crates"]:
            prefix = ()
            for name in crate["path"][:-1]:
                next_prefix = prefix + (name,)
                if next_prefix not in folders:
                    parent = folders[prefix]
                    node = ET.SubElement(parent, "NODE", TYPE="FOLDER", NAME=_unique(parent, name))
                    folders[next_prefix] = ET.SubElement(node, "SUBNODES", COUNT="0")
                    parent.set("COUNT", str(len(parent.findall("NODE"))))
                prefix = next_prefix
            parent = folders[prefix]
            node = ET.SubElement(parent, "NODE", TYPE="PLAYLIST", NAME=_unique(parent, crate["path"][-1]))
            playlist = ET.SubElement(node, "PLAYLIST", ENTRIES=str(len(crate["tracks"])), TYPE="LIST",
                                     UUID=uuid.uuid4().hex)
            for key in crate["tracks"]:
                ET.SubElement(ET.SubElement(playlist, "ENTRY"), "PRIMARYKEY", TYPE="TRACK", KEY=keys[key])
            parent.set("COUNT", str(len(parent.findall("NODE"))))
        _tree(root)
        backup = _publish(self.path, {self.path: _bytes(root)}, backup_dir, guard, assert_traktor_closed, states, snapshot)
        return {"crates": len(manifest["crates"]), "files": len(paths),
                "memberships": sum(len(c["tracks"]) for c in manifest["crates"]),
                "backup": backup, "warnings": list(dict.fromkeys(warnings))[:100]}
