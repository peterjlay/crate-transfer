"""Bounded VirtualDJ 8 database and virtual-folder support.

Format references: VirtualDJ VDJPedia VDJ_database and lists, and OpenFadeR
compatibility/virtualdj at 47867004d604bcdb68180eca98013a09e5a49573.
Application restart/playback acceptance has not been tested.
"""
from __future__ import annotations

import math
import os
import xml.etree.ElementTree as ET
from pathlib import Path

from .exchange import ExchangeLibrary, _check_metadata, _number, _parse_m3u_line, _resolve_existing
from .library import LibraryError
from .portable import safe_component
from .traktor import (_bytes, _closed, _destination, _installed, _publish, _snapshot, _xml)


def assert_virtualdj_closed():
    _closed(("virtualdj", "virtual dj"), "VirtualDJ")


def _database(root):
    if root.tag != "VirtualDJ_Database" or root.get("Version") not in ("8.0", "8.1", "8.2", "8.3", "8.4", "8.5"):
        raise LibraryError("Unsupported VirtualDJ database version.")
    songs = {}
    for song in root.findall("Song"):
        path = song.get("FilePath")
        if not path or not Path(path).is_absolute():
            raise LibraryError("Unsupported VirtualDJ song path; only local absolute paths are supported.")
        key = os.path.normcase(os.path.normpath(path))
        if key in songs:
            raise LibraryError("Duplicate VirtualDJ song path.")
        songs[key] = song
    return songs


def _metadata(song, warnings):
    data = {}
    tags, scan = song.find("Tags"), song.find("Scan")
    if tags is not None:
        for attr, key in (("Title", "title"), ("Author", "artist"), ("Album", "album"), ("Key", "key")):
            if tags.get(attr) is not None:
                data[key] = tags.get(attr)
    period = _number(tags.get("Bpm") if tags is not None else None, positive=True)
    if not period and scan is not None:
        period = _number(scan.get("Bpm"), positive=True)
    if period:
        data["bpm"] = 60 / period
    cues = []
    for poi in song.findall("Poi"):
        kind = poi.get("Type", "cue")
        pos = _number(poi.get("Pos"))
        if kind not in ("cue", "loop") or pos is None:
            if kind not in ("beatgrid",):
                warnings.append("Unsupported VirtualDJ POI data was omitted.")
            continue
        item = {"kind": kind, "position": pos, "name": poi.get("Name", "")}
        if kind == "loop":
            size = _number(poi.get("Size"), positive=True)
            if not size or not period:
                warnings.append("VirtualDJ loop without tempo or size was omitted.")
                continue
            item["end"] = pos + size * period
            slot_value = poi.get("Slot", "-1")
        else:
            slot_value = poi.get("Num", "-1")
        try:
            slot = int(slot_value)
            if 1 <= slot <= 256:
                item["slot"] = slot - 1
        except ValueError:
            pass
        cues.append(item)
    if cues:
        data["cues"] = cues
    return _check_metadata(data, warnings)


class VirtualDJLibrary(ExchangeLibrary):
    def __init__(self, path):
        candidate = Path(path).expanduser().resolve()
        self.path = candidate / "database.xml" if candidate.is_dir() else candidate
        self.directory = self.path.parent
        self.format = "virtualdj"

    @property
    def backup_paths(self):
        return [self.path]

    def _load(self):
        if not self.path.is_file() or self.path.is_symlink():
            raise LibraryError("Existing VirtualDJ database.xml not found.")
        root = _xml(self.path)
        _database(root)
        return root

    def _source(self):
        songs = _database(self._load())
        self.warnings = ["VirtualDJ beatgrid and other native analysis are not fully converted."]
        self._playlists = []
        for category in ("MyLists", "Folders", "Playlists"):
            base = self.directory / category
            if not base.exists():
                continue
            if base.is_symlink() or not base.is_dir():
                raise LibraryError("Unsafe VirtualDJ list directory.")
            files = sorted(base.rglob("*"), key=lambda p: str(p).casefold())
            for playlist in files:
                if playlist.is_symlink():
                    raise LibraryError("Symbolic links in VirtualDJ list folders are unsupported.")
                if not playlist.is_file() or playlist.suffix.lower() not in (".vdjfolder", ".m3u", ".m3u8"):
                    continue
                if len(playlist.relative_to(base).parts) > 32:
                    raise LibraryError("VirtualDJ playlist hierarchy is too deep.")
                if playlist.suffix.lower() == ".vdjfolder":
                    root = _xml(playlist)
                    if root.tag != "VirtualFolder":
                        raise LibraryError("Unsupported VirtualDJ virtual folder.")
                    ordered = []
                    for index, entry in enumerate(root.findall("song")):
                        value = entry.get("path")
                        if not value:
                            raise LibraryError("VirtualDJ list entry has no path.")
                        try:
                            order = int(entry.get("idx", str(index)))
                        except ValueError as error:
                            raise LibraryError("Invalid VirtualDJ list order.") from error
                        ordered.append((order, index, value))
                    values = [value for _, _, value in sorted(ordered)]
                    paths = [Path(value) if Path(value).is_absolute() else playlist.parent / value for value in values]
                else:
                    try:
                        lines = playlist.read_text(encoding="utf-8-sig").splitlines()
                    except (OSError, UnicodeError) as error:
                        raise LibraryError("Cannot read VirtualDJ playlist.") from error
                    paths = [value for value in (_parse_m3u_line(line, playlist) for line in lines) if value is not None]
                tracks, members = {}, []
                for path in paths:
                    resolved = _resolve_existing(path, "VirtualDJ playlist track")
                    identity = os.path.normcase(os.path.normpath(str(resolved)))
                    song = songs.get(identity)
                    tracks[identity] = {"path": resolved, "metadata": _metadata(song, self.warnings) if song is not None else {}}
                    members.append(identity)
                relative = playlist.relative_to(base)
                self._playlists.append({"path": [category, *relative.parts[:-1], relative.stem],
                                        "tracks": tracks, "members": members})
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
        assert_virtualdj_closed()
        if not isinstance(transfer_id, str) or not transfer_id:
            raise LibraryError("A transfer identity is required.")
        records, paths, states = _installed(manifest, installed, self.directory)
        if not self.path.is_file() or self.path.is_symlink():
            raise LibraryError("Existing VirtualDJ database.xml not found.")
        snapshot = _snapshot(self.path)
        root = self._load()
        songs = _database(root)
        warnings = list(manifest.get("warnings", []))
        warnings.append("VirtualDJ native import is unverified in the application; analysis and waveforms are not generated.")
        for key, record in records.items():
            path = paths[key]
            identity = os.path.normcase(os.path.normpath(str(path)))
            if identity in songs:
                warnings.append("Existing VirtualDJ track metadata was retained.")
                continue
            metadata = record.get("metadata", {})
            song = ET.SubElement(root, "Song", FilePath=str(path), FileSize=str(record["size"]))
            attrs = {}
            for field, attr in (("title", "Title"), ("artist", "Author"), ("album", "Album"), ("key", "Key")):
                if field in metadata:
                    attrs[attr] = metadata[field]
            bpm = metadata.get("bpm")
            if bpm:
                period = 60 / bpm
                if math.isfinite(period) and period > 0:
                    attrs["Bpm"] = str(period)
                else:
                    warnings.append("VirtualDJ tempo outside supported numeric bounds was omitted.")
                    bpm = None
            if attrs:
                ET.SubElement(song, "Tags", attrs)
            if metadata.get("beatgrid"):
                warnings.append("VirtualDJ beatgrid metadata was omitted.")
            for cue in metadata.get("cues", []):
                attrs = {"Name": cue.get("name", ""), "Pos": str(cue["position"]),
                         "Num": str(cue["slot"] + 1 if "slot" in cue else -1)}
                if cue["kind"] == "loop":
                    if not bpm:
                        warnings.append("VirtualDJ loops without BPM were omitted.")
                        continue
                    size = (cue["end"] - cue["position"]) * bpm / 60
                    if not math.isfinite(size) or size <= 0:
                        warnings.append("VirtualDJ loop size outside supported numeric bounds was omitted.")
                        continue
                    attrs["Type"] = "loop"
                    attrs["Size"] = str(size)
                    attrs["Slot"] = attrs["Num"]
                    attrs["Num"] = "-1"
                ET.SubElement(song, "Poi", attrs)
        base = self.directory / ("Folders" if (self.directory / "Folders").is_dir()
                                 and not (self.directory / "MyLists").exists() else "MyLists")
        if base.is_symlink():
            raise LibraryError("Unsafe VirtualDJ list folder.")
        outputs, folders, occupied = {}, {(): base}, {}
        def reserve(parent, name, suffix=""):
            if parent not in occupied:
                occupied[parent] = {p.name.casefold() for p in parent.iterdir()} if parent.exists() else set()
            name = safe_component(name, maximum_bytes=150)
            candidate, number = name + suffix, 2
            while candidate.casefold() in occupied[parent]:
                candidate = f"{name} ({number}){suffix}"
                number += 1
            occupied[parent].add(candidate.casefold())
            return parent / candidate
        for crate in manifest["crates"]:
            prefix = ()
            for part in crate["path"][:-1]:
                child = prefix + (part,)
                if child not in folders:
                    folders[child] = reserve(folders[prefix], part)
                prefix = child
            file = reserve(folders[prefix], crate["path"][-1], ".vdjfolder")
            listing = ET.Element("VirtualFolder", noDuplicates="no", ordered="yes")
            for index, key in enumerate(crate["tracks"]):
                metadata = records[key].get("metadata", {})
                ET.SubElement(listing, "song", path=str(paths[key]), size=str(records[key]["size"]),
                              artist=metadata.get("artist", ""), title=metadata.get("title", ""), idx=str(index))
            outputs[file] = _bytes(listing)
        _database(root)
        outputs[self.path] = _bytes(root)
        backup = _publish(self.path, outputs, backup_dir, guard, assert_virtualdj_closed, states, snapshot)
        return {"crates": len(manifest["crates"]), "files": len(paths),
                "memberships": sum(len(c["tracks"]) for c in manifest["crates"]),
                "backup": backup, "warnings": list(dict.fromkeys(warnings))[:100]}
