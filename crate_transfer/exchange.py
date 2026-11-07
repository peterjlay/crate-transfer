"""Exchange adapters for exported DJ playlist files; never writes native libraries.

The XML mappings follow AlphaTheta/Pioneer DJ's rekordbox XML format specification
(https://pyrekordbox.readthedocs.io/en/latest/formats/xml.html) and the
Traktor cue mappings at
https://github.com/digital-dj-tools/dj-data-converter/blob/master/src/converter/traktor/cue.clj.
Unsupported metadata is dropped with a warning instead of being guessed.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from .library import sha256_file
from .protocol import (
    PROTOCOL,
    ProtocolError,
    machine_id as validate_machine_id,
    validate_manifest,
    validate_metadata,
)


_XML_LIMIT = 128 * 1024 * 1024
_BAD_XML_DECLARATION = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)


def _warn(warnings: list[str], message: str) -> None:
    if message not in warnings and len(warnings) < 100:
        warnings.append(message)


def _number(value: str | None, *, positive: bool = False) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        return None
    return result


def _int(value: str | None, *, minimum: int = 0, maximum: int | None = None) -> int | None:
    try:
        result = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    if result is None or result < minimum or (maximum is not None and result > maximum):
        return None
    return result


def _local_file_uri(value: str, context: str) -> Path:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() != "file" or parsed.netloc.lower() not in ("", "localhost"):
        raise ValueError(f"{context} must be a local file URL.")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{context} has an ambiguous file URL.")
    raw_path = urllib.parse.unquote(parsed.path, errors="strict")
    if os.name == "nt" and re.match(r"^/[A-Za-z]:/", raw_path):
        raw_path = raw_path[1:]
    if not raw_path or not Path(raw_path).is_absolute():
        raise ValueError(f"{context} must have an absolute local path.")
    return Path(raw_path)


def _resolve_existing(path: Path, context: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"{context} does not resolve to an existing media file.") from error
    if not resolved.is_file():
        raise ValueError(f"{context} does not point to a regular media file.")
    return resolved


def _read_xml(path: Path) -> ET.Element:
    try:
        with path.open("rb") as source:
            data = source.read(_XML_LIMIT + 1)
    except OSError as error:
        raise ValueError(f"Cannot read exchange file: {path.name}.") from error
    if len(data) > _XML_LIMIT:
        raise ValueError("Exchange XML exceeds the 128 MiB safety limit.")
    if _BAD_XML_DECLARATION.search(data.replace(b"\x00", b"")):
        raise ValueError("XML DTDs and entity declarations are not allowed.")
    try:
        return ET.fromstring(data)
    except ET.ParseError as error:
        raise ValueError(f"Malformed exchange XML: {error}.") from error


def _metadata_text(attributes: dict, key: str, result: dict) -> None:
    value = attributes.get(key)
    if value is not None:
        result[key] = value


def _metadata_number(value: str | None, key: str, result: dict, warnings: list[str],
                     *, positive: bool = False) -> float | None:
    number = _number(value, positive=positive)
    if value is not None and number is None:
        _warn(warnings, f"Dropped invalid {key} metadata.")
    return number


def _check_metadata(metadata: dict, warnings: list[str]) -> dict:
    """Keep only protocol-valid fields; malformed optional values are explicitly dropped."""
    for key in ("title", "artist", "album", "key"):
        value = metadata.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > 1000
                                  or any(ord(char) < 32 for char in value)):
            metadata.pop(key, None)
            _warn(warnings, f"Dropped invalid {key} metadata.")
    if "bpm" in metadata:
        bpm = _number(metadata["bpm"], positive=True)
        if bpm is None:
            metadata.pop("bpm")
            _warn(warnings, "Dropped invalid bpm metadata.")
        else:
            metadata["bpm"] = bpm
    cues = []
    for cue in metadata.pop("cues", []):
        try:
            validate_metadata({"cues": [cue]})
        except (ProtocolError, TypeError):
            _warn(warnings, "Dropped invalid cue metadata.")
        else:
            cues.append(cue)
    if cues:
        metadata["cues"] = cues
    grid = []
    previous = -1.0
    for marker in metadata.pop("beatgrid", []):
        try:
            validate_metadata({"beatgrid": [marker]})
            position = marker["position"]
            if position < previous:
                raise ProtocolError("Unordered beatgrid")
        except (ProtocolError, TypeError, KeyError):
            _warn(warnings, "Dropped invalid beatgrid metadata.")
        else:
            grid.append(marker)
            previous = position
    if grid:
        metadata["beatgrid"] = grid
    return metadata


def _parse_rekordbox(path: Path, warnings: list[str]) -> list[dict]:
    root = _read_xml(path)
    if root.tag != "DJ_PLAYLISTS":
        raise ValueError("Not a rekordbox DJ_PLAYLISTS XML exchange file.")
    collection = root.find("COLLECTION")
    playlist_root = root.find("PLAYLISTS")
    if collection is None or playlist_root is None:
        raise ValueError("rekordbox XML is missing COLLECTION or PLAYLISTS.")
    tracks: dict[str, dict] = {}
    for item in collection.findall("TRACK"):
        track_key = item.get("TrackID")
        if not track_key or track_key in tracks:
            raise ValueError("rekordbox XML contains a missing or duplicate TrackID.")
        location = item.get("Location")
        if location is None:
            raise ValueError(f"rekordbox track {track_key} is missing Location.")
        source = _resolve_existing(_local_file_uri(location, f"rekordbox track {track_key}"), "rekordbox track")
        metadata: dict = {}
        for attr, key in (("Name", "title"), ("Artist", "artist"), ("Album", "album")):
            _metadata_text(item.attrib, key, metadata)
            if key not in metadata and attr in item.attrib:
                metadata[key] = item.attrib[attr]
        bpm = _metadata_number(item.get("AverageBpm"), "bpm", {}, warnings, positive=True)
        beatgrid = []
        tempos = item.findall("TEMPO")
        for tempo in tempos:
            if tempo.get("Metro", "4/4") != "4/4":
                _warn(warnings, "Dropped rekordbox beatgrid markers with unsupported musical meter.")
                continue
            position = _number(tempo.get("Inizio"))
            tempo_bpm = _number(tempo.get("Bpm"), positive=True)
            beat = _int(tempo.get("Battito"), minimum=1)
            if position is None or tempo_bpm is None or beat is None:
                _warn(warnings, "Dropped invalid rekordbox beatgrid metadata.")
                continue
            beatgrid.append({"position": position, "bpm": tempo_bpm, "beat": beat})
        if bpm is not None:
            metadata["bpm"] = bpm
        elif beatgrid:
            metadata["bpm"] = beatgrid[0]["bpm"]
        if beatgrid:
            metadata["beatgrid"] = beatgrid
        cues = []
        for mark in item.findall("POSITION_MARK"):
            kind = {"0": "cue", "4": "loop"}.get(mark.get("Type"))
            start = _number(mark.get("Start"))
            if kind is None or start is None:
                if mark.get("Type") not in ("0", "4"):
                    _warn(warnings, f"Dropped unsupported rekordbox POSITION_MARK type {mark.get('Type')!r}.")
                else:
                    _warn(warnings, "Dropped invalid rekordbox cue metadata.")
                continue
            cue = {"kind": kind, "position": start, "name": mark.get("Name", "")}
            slot = _int(mark.get("Num"), maximum=255)
            if slot is not None:
                cue["slot"] = slot
            end = _number(mark.get("End"))
            if kind == "loop" or end is not None:
                if end is None or end <= start:
                    _warn(warnings, "Dropped invalid rekordbox loop metadata.")
                    continue
                cue["end"] = end
            cues.append(cue)
        if cues:
            metadata["cues"] = cues
        metadata = _check_metadata(metadata, warnings)
        tracks[track_key] = {"path": source, "metadata": metadata}

    playlists: list[dict] = []
    root_nodes = playlist_root.findall("NODE")
    if not root_nodes:
        raise ValueError("rekordbox PLAYLISTS has no root NODE.")

    def visit(node: ET.Element, prefix: list[str]) -> None:
        kind, name = node.get("Type"), node.get("Name")
        if kind not in ("0", "1") or not name:
            raise ValueError("rekordbox playlist NODE has missing or invalid Type/Name.")
        if kind == "0":
            folder_path = prefix if name == "ROOT" and not prefix else prefix + [name]
            for child in node.findall("NODE"):
                visit(child, folder_path)
            return
        members = []
        if node.get("KeyType", "0") != "0":
            raise ValueError("rekordbox location-keyed playlists are not supported; export TrackID-keyed XML.")
        for entry in node.findall("TRACK"):
            key = entry.get("Key")
            if key not in tracks:
                raise ValueError(f"rekordbox playlist references unknown TrackID {key!r}.")
            members.append(key)
        playlists.append({"path": prefix + [name], "members": members})
        if node.findall("NODE"):
            _warn(warnings, "Dropped nested children from a rekordbox playlist node.")

    for node in root_nodes:
        visit(node, [])
    if not playlists:
        raise ValueError("rekordbox XML contains no playlists.")
    for node in root.iter():
        if node.tag not in {"DJ_PLAYLISTS", "PRODUCT", "COLLECTION", "TRACK", "TEMPO", "POSITION_MARK",
                            "PLAYLISTS", "NODE"}:
            _warn(warnings, f"Dropped unsupported rekordbox XML record {node.tag!r}.")
    return [{"path": item["path"], "members": item["members"], "tracks": tracks} for item in playlists]


def _traktor_path(volume: str, directory: str, filename: str, context: str) -> Path:
    if not filename or filename in (".", "..") or "/" in filename or "\\" in filename:
        raise ValueError(f"{context} has an invalid Traktor LOCATION.")
    if re.fullmatch(r"[A-Za-z]:", volume) and os.name == "nt":
        base = Path(volume + os.sep)
    else:
        base = Path(Path.cwd().anchor)
    if directory == "/:":
        parts = []
    elif directory.startswith("/:") and directory.endswith("/:"):
        parts = directory[2:-2].split("/:") if directory[2:-2] else []
    else:
        raise ValueError(f"{context} has an unsupported Traktor directory encoding.")
    if any(part in ("", ".", "..") or "/" in part or "\\" in part for part in parts):
        raise ValueError(f"{context} has an unsafe Traktor directory.")
    return _resolve_existing(base.joinpath(*parts, filename), context)


def _traktor_key(location: ET.Element) -> str:
    volume = location.get("VOLUME", "")
    return volume + location.get("DIR", "") + location.get("FILE", "")


def _parse_traktor(path: Path, warnings: list[str]) -> list[dict]:
    root = _read_xml(path)
    if root.tag != "NML":
        raise ValueError("Not a Traktor NML exchange file.")
    collection = root.find("./COLLECTION")
    sets = root.find("./SETS")
    if collection is None:
        raise ValueError("Traktor NML is missing COLLECTION.")
    tracks: dict[str, dict] = {}
    for entry in collection.findall("ENTRY"):
        location = entry.find("LOCATION")
        if location is None:
            raise ValueError("Traktor track is missing LOCATION.")
        primary = _traktor_key(location)
        if primary in tracks:
            raise ValueError("Traktor NML contains a duplicate track location.")
        media = _traktor_path(location.get("VOLUME", ""), location.get("DIR", ""),
                              location.get("FILE", ""), f"Traktor track {primary}")
        metadata = {}
        for attr, key in (("TITLE", "title"), ("ARTIST", "artist"), ("ALBUM", "album")):
            if attr in entry.attrib:
                metadata[key] = entry.attrib[attr]
        album = entry.find("ALBUM")
        if album is not None and album.get("TITLE") is not None:
            metadata["album"] = album.get("TITLE")
        info = entry.find("INFO")
        tempo = entry.find("TEMPO")
        bpm_value = (tempo.get("BPM") if tempo is not None else None)
        if bpm_value is None and info is not None:
            bpm_value = info.get("BPM")
        if bpm_value is None:
            bpm_value = entry.get("BPM")
        bpm = _metadata_number(bpm_value, "bpm", {}, warnings, positive=True)
        if bpm is not None:
            metadata["bpm"] = bpm
        if info is not None and info.get("KEY") is not None:
            metadata["key"] = info.get("KEY")
        musical_key = entry.find("MUSICAL_KEY")
        if musical_key is not None and info is None:
            _warn(warnings, "Dropped Traktor numeric musical-key metadata.")
        cues = []
        if entry.find("GRID") is not None:
            _warn(warnings, "Dropped unsupported Traktor beatgrid metadata.")
        cue_records = entry.findall("CUE_V2")
        for cue in cue_records:
            cue_type = cue.get("TYPE")
            start = _number(cue.get("START"))
            if start is None:
                _warn(warnings, "Dropped invalid Traktor cue metadata.")
                continue
            start /= 1000
            if cue_type == "0":
                item = {"kind": "cue", "position": start, "name": cue.get("NAME", "")}
                slot = _int(cue.get("HOTCUE"), maximum=255)
                if slot is not None:
                    item["slot"] = slot
                cues.append(item)
            elif cue_type == "5":
                length = _number(cue.get("LEN"), positive=True)
                if length is None:
                    _warn(warnings, "Dropped invalid Traktor loop metadata.")
                    continue
                item = {"kind": "loop", "position": start, "end": start + length / 1000,
                        "name": cue.get("NAME", "")}
                slot = _int(cue.get("HOTCUE"), maximum=255)
                if slot is not None:
                    item["slot"] = slot
                cues.append(item)
            else:
                _warn(warnings, f"Dropped unsupported Traktor cue type {cue_type!r}.")
        if cues:
            metadata["cues"] = cues
        metadata = _check_metadata(metadata, warnings)
        tracks[primary] = {"path": media, "metadata": metadata}

    playlists: list[dict] = []

    def visit(node: ET.Element, prefix: list[str]) -> None:
        kind, name = node.get("TYPE"), node.get("NAME")
        if kind not in ("FOLDER", "PLAYLIST") or not name:
            raise ValueError("Traktor playlist NODE has missing or invalid TYPE/NAME.")
        path_parts = prefix if (kind == "FOLDER" and name in ("$ROOT", "Playlists")
                                and not prefix) else prefix + [name]
        if kind == "PLAYLIST":
            members = []
            playlist = node.find("PLAYLIST")
            if playlist is None:
                raise ValueError(f"Traktor playlist {name!r} is missing PLAYLIST.")
            for ref in playlist.findall("ENTRY/PRIMARYKEY"):
                if ref.get("TYPE") != "TRACK" or ref.get("KEY") not in tracks:
                    raise ValueError(f"Traktor playlist references unknown track key {ref.get('KEY')!r}.")
                members.append(ref.get("KEY"))
            playlists.append({"path": path_parts, "members": members})
        else:
            subnodes = node.find("SUBNODES")
            if subnodes is None:
                return
            for child in subnodes.findall("NODE"):
                visit(child, path_parts)

    top_nodes = sets.findall("NODE") if sets is not None else []
    for node in top_nodes:
        visit(node, [])
    if not playlists:
        playlists.append({"path": [path.stem], "members": list(tracks)})
    return [{"path": item["path"], "members": item["members"], "tracks": tracks} for item in playlists]


def _parse_m3u_line(line: str, playlist: Path) -> Path | None:
    value = line.strip()
    if not value or value.startswith("#"):
        return None
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme:
        if parsed.scheme.lower() != "file" or parsed.netloc.lower() not in ("", "localhost"):
            raise ValueError(f"Playlist {playlist.name} contains a non-local media URI.")
        if parsed.query or parsed.fragment:
            raise ValueError(f"Playlist {playlist.name} contains an ambiguous media URI.")
        value = urllib.parse.unquote(parsed.path, errors="strict")
        if not Path(value).is_absolute():
            raise ValueError(f"Playlist {playlist.name} contains a relative file URI.")
        return Path(value)
    return playlist.parent / urllib.parse.unquote(value, errors="strict")


def _parse_m3u(path: Path, warnings: list[str]) -> list[dict]:
    if path.is_dir():
        playlist_files = sorted((item for item in path.rglob("*")
                                 if item.is_file() and item.suffix.lower() in (".m3u", ".m3u8")),
                                key=lambda item: item.relative_to(path).as_posix().casefold())
        root = path
    elif path.is_file() and path.suffix.lower() in (".m3u", ".m3u8"):
        playlist_files, root = [path], path.parent
    else:
        raise ValueError("Choose an M3U/M3U8 file or a directory containing exported playlists.")
    if not playlist_files:
        raise ValueError("No M3U or M3U8 playlists were found.")
    result = []
    for playlist in playlist_files:
        try:
            text = playlist.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as error:
            raise ValueError(f"Cannot read M3U playlist {playlist.name}.") from error
        members = []
        for line in text.splitlines():
            media = _parse_m3u_line(line, playlist)
            if media is not None:
                members.append(_resolve_existing(media, f"Playlist {playlist.name}"))
            if line.startswith("#EXTINF:"):
                _warn(warnings, "Dropped M3U EXTINF display metadata.")
            elif line.startswith("#") and line.strip() not in ("#EXTM3U",):
                _warn(warnings, "Dropped unsupported M3U directive metadata.")
        relative = playlist.relative_to(root)
        parts = list(relative.parent.parts)
        parts.append(playlist.stem)
        result.append({"path": parts, "members": members, "tracks": {}})
    return result


class ExchangeLibrary:
    """Read exported rekordbox XML, Traktor NML or M3U/M3U8 files only."""

    def __init__(self, format: str, path: str | Path):
        if format not in ("rekordbox-xml", "traktor-nml", "m3u"):
            raise ValueError(f"Unsupported exchange format: {format!r}.")
        self.format = format
        self.path = Path(path).expanduser()
        self.warnings: list[str] = []
        if format == "rekordbox-xml":
            self._playlists = _parse_rekordbox(self.path, self.warnings)
        elif format == "traktor-nml":
            self._playlists = _parse_traktor(self.path, self.warnings)
        else:
            self._playlists = _parse_m3u(self.path, self.warnings)
        self._catalog = self._build_catalog()

    def _build_catalog(self) -> list[dict]:
        seen, result = set(), []
        for index, playlist in enumerate(self._playlists, 1):
            path = playlist["path"]
            key = tuple(part.casefold() for part in path)
            if not all(path) or key in seen:
                raise ValueError("Exchange file contains an empty or duplicate playlist path.")
            seen.add(key)
            result.append({"id": index, "path": path, "tracks": len(playlist["members"])})
        return result

    def crates(self) -> list[dict]:
        return [{"id": item["id"], "path": list(item["path"]), "tracks": item["tracks"]}
                for item in self._catalog]

    def export(self, selected: list[int], machine_id: str, name: str,
               progress=lambda text: None) -> tuple[dict, dict[str, Path]]:
        try:
            validate_machine_id(machine_id)
        except ProtocolError as error:
            raise ValueError(str(error)) from error
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError("Invalid sender name.")
        if not isinstance(selected, list) or not selected:
            raise ValueError("Choose at least one playlist.")
        chosen = set(selected)
        if len(chosen) != len(selected) or not all(type(item) is int for item in selected):
            raise ValueError("Playlist selections must be unique integer IDs.")
        available = {item["id"] for item in self._catalog}
        if not chosen <= available:
            raise ValueError("Choose valid playlist IDs.")
        prefixes = [item["path"] for item in self._catalog if item["id"] in chosen]
        playlists = [playlist for playlist in self._playlists
                     if any(playlist["path"][:len(prefix)] == prefix for prefix in prefixes)]

        files: dict[str, dict] = {}
        paths: dict[str, Path] = {}
        path_ids: dict[Path, str] = {}
        crates = []
        for playlist in playlists:
            members = []
            for member in playlist["members"]:
                track = playlist["tracks"].get(member) if playlist["tracks"] else None
                source = track["path"] if track else member
                resolved = _resolve_existing(Path(source), "Playlist media")
                source_metadata = _check_metadata(dict(track["metadata"]), self.warnings) if track else {}
                file_id = path_ids.get(resolved)
                if file_id is None:
                    before = resolved.stat()
                    digest = sha256_file(resolved)
                    after = resolved.stat()
                    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                            after.st_size, after.st_mtime_ns, after.st_ino):
                        raise ValueError(f"Media file changed while being checked: {resolved.name}.")
                    filename = resolved.name
                    file_id = hashlib.sha256((digest + "\0" + filename).encode()).hexdigest()
                    if file_id not in files:
                        if not digest or resolved.stat().st_size <= 0:
                            raise ValueError(f"Media file is empty: {resolved.name}.")
                        record = {"id": file_id, "sha256": digest, "name": filename,
                                  "size": after.st_size}
                        if source_metadata:
                            record["metadata"] = source_metadata
                        files[file_id] = record
                        paths[file_id] = resolved
                    elif files[file_id].get("metadata", {}) != source_metadata:
                        _warn(self.warnings, "Conflicting metadata for identical files; kept the first track record.")
                    path_ids[resolved] = file_id
                    progress(f"Checking {filename}")
                elif files[file_id].get("metadata", {}) != source_metadata:
                    _warn(self.warnings, "Conflicting metadata for repeated media; kept the first track record.")
                members.append(file_id)
            crates.append({"path": playlist["path"], "tracks": members})
        if not crates:
            raise ValueError("Choose at least one playlist.")
        manifest = {"protocol": PROTOCOL, "destination": {"format": "serato"},
                    "sender_id": machine_id, "sender_name": name, "crates": crates,
                    "files": list(files.values())}
        if self.warnings:
            manifest["warnings"] = list(self.warnings)
        try:
            validate_manifest(manifest)
        except ProtocolError as error:
            raise ValueError(str(error)) from error
        return manifest, paths


def _tree_for_playlists(crates: list[dict]) -> dict:
    tree = {}
    for crate in crates:
        current = tree
        for index, part in enumerate(crate["path"]):
            node = current.setdefault(part, {"_children": {}})
            if index == len(crate["path"]) - 1:
                node["_tracks"] = crate["tracks"]
            current = node["_children"]
    return tree


def _ordered_tree_nodes(tree: dict) -> list[tuple[str, dict]]:
    return list(tree.items())


def _node_count(branch: dict) -> int:
    return sum(2 if value.get("_tracks") is not None and value["_children"] else 1
               for value in branch.values())


def _warn_manifest(manifest: dict, warning: str) -> None:
    warnings = manifest.setdefault("warnings", [])
    if warning not in warnings and len(warnings) < 100:
        warnings.append(warning)


def _write_rekordbox(manifest: dict, media_paths: dict[str, Path], directory: Path) -> Path:
    root = ET.Element("DJ_PLAYLISTS", Version="1.0.0")
    ET.SubElement(root, "PRODUCT", Name="Crate Transfer", Version=PROTOCOL, Company="Crate Transfer")
    collection = ET.SubElement(root, "COLLECTION", Entries=str(len(manifest["files"])))
    keys = {item["id"]: str(index) for index, item in enumerate(manifest["files"], 1)}
    for record in manifest["files"]:
        metadata = record.get("metadata", {})
        if "key" in metadata:
            _warn_manifest(manifest, "rekordbox XML export omitted per-track musical key.")
        source = Path(media_paths[record["id"]]).expanduser().absolute()
        attrs = {
            "TrackID": keys[record["id"]], "Name": metadata.get("title", Path(record["name"]).stem),
            "Artist": metadata.get("artist", ""), "Album": metadata.get("album", ""),
            "Kind": source.suffix.lstrip(".").upper(), "Size": str(record["size"]),
            "TotalTime": "0", "AverageBpm": str(metadata.get("bpm", 0)),
            "DateAdded": "2000-01-01", "BitRate": "0", "SampleRate": "0",
            "Comments": "", "PlayCount": "0", "Rating": "0", "Location": source.as_uri(),
        }
        track = ET.SubElement(collection, "TRACK", attrs)
        for cue in metadata.get("cues", []):
            mark_attrs = {"Name": cue.get("name", ""), "Type": "4" if cue["kind"] == "loop" else "0",
                          "Start": f"{cue['position']:.6f}"}
            if "slot" in cue:
                mark_attrs["Num"] = str(cue["slot"])
            if cue.get("end") is not None:
                mark_attrs["End"] = f"{cue['end']:.6f}"
            ET.SubElement(track, "POSITION_MARK", mark_attrs)
        for marker in metadata.get("beatgrid", []):
            if marker["beat"] > 4:
                _warn_manifest(manifest, "rekordbox XML export omitted beatgrid markers outside supported 4/4 meter.")
                continue
            ET.SubElement(track, "TEMPO", {
                "Inizio": f"{marker['position']:.6f}", "Bpm": f"{marker['bpm']:.6f}",
                "Metro": "4/4", "Battito": str(marker["beat"]),
            })
    playlists = ET.SubElement(root, "PLAYLISTS")
    tree = _tree_for_playlists(manifest["crates"])
    root_node = ET.SubElement(playlists, "NODE", Type="0", Name="ROOT", Count=str(_node_count(tree)))

    def add_nodes(parent: ET.Element, branch: dict) -> None:
        for name, value in _ordered_tree_nodes(branch):
            children = value["_children"]
            tracks = value.get("_tracks")
            if tracks is not None and children:
                playlist_node = ET.SubElement(parent, "NODE", Type="1", Name=name,
                                              Entries=str(len(tracks)), KeyType="0")
                for file_id in tracks:
                    ET.SubElement(playlist_node, "TRACK", Key=keys[file_id])
                folder = ET.SubElement(parent, "NODE", Type="0", Name=name, Count=str(_node_count(children)))
                add_nodes(folder, children)
            elif children:
                folder = ET.SubElement(parent, "NODE", Type="0", Name=name, Count=str(_node_count(children)))
                add_nodes(folder, children)
            else:
                playlist_node = ET.SubElement(parent, "NODE", Type="1", Name=name,
                                              Entries=str(len(tracks or [])), KeyType="0")
                for file_id in tracks or []:
                    ET.SubElement(playlist_node, "TRACK", Key=keys[file_id])
    add_nodes(root_node, tree)
    output = directory / "rekordbox.xml"
    if output.exists() or output.is_symlink():
        raise ValueError(f"Exchange output already exists: {output.name}.")
    ET.ElementTree(root).write(output, encoding="utf-8", xml_declaration=True)
    return output


def _traktor_location(path: Path) -> tuple[str, str, str]:
    absolute = path.expanduser().absolute()
    if not absolute.is_absolute():
        raise ValueError("Traktor media destinations must be absolute paths.")
    parts = absolute.parts
    volume = absolute.drive.rstrip("\\/") if absolute.drive else ""
    relative = parts[1:] if absolute.anchor else parts
    directory = "/:" + "/:".join(relative[:-1]) + "/:" if len(relative) > 1 else "/:"
    return volume, directory, absolute.name


def _write_traktor(manifest: dict, media_paths: dict[str, Path], directory: Path) -> Path:
    root = ET.Element("NML", VERSION="19")
    ET.SubElement(root, "HEAD", COMPANY="www.native-instruments.com", FILE="collection",
                  DJMIX="false", VER1="2", VER2="12", VER3="0", VER4="0")
    collection = ET.SubElement(root, "COLLECTION", ENTRIES=str(len(manifest["files"])))
    keys = {}
    for record in manifest["files"]:
        metadata = record.get("metadata", {})
        if "key" in metadata:
            _warn_manifest(manifest, "Traktor NML export omitted per-track musical key.")
        if metadata.get("beatgrid"):
            _warn_manifest(manifest, "Traktor NML export omitted beatgrid positions; only track BPM is supported.")
        track = ET.SubElement(collection, "ENTRY", {
            "TITLE": metadata.get("title", Path(record["name"]).stem),
            "ARTIST": metadata.get("artist", ""), "PLAYTIME": "0",
        })
        volume, folder, filename = _traktor_location(Path(media_paths[record["id"]]))
        location = ET.SubElement(track, "LOCATION", DIR=folder, FILE=filename)
        if volume:
            location.set("VOLUME", volume)
            location.set("VOLUMEID", "")
        keys[record["id"]] = _traktor_key(location)
        if "album" in metadata:
            ET.SubElement(track, "ALBUM", TITLE=metadata["album"])
        if "bpm" in metadata:
            ET.SubElement(track, "INFO", BPM=f"{metadata['bpm']:.6f}")
            ET.SubElement(track, "TEMPO", BPM=f"{metadata['bpm']:.6f}", BPM_QUALITY="100.000000")
        for index, cue in enumerate(metadata.get("cues", [])):
            slot = cue.get("slot", -1)
            if slot >= 8:
                _warn_manifest(manifest, "Traktor NML export omitted cues outside its supported eight hot-cue slots.")
                continue
            cue_type = "5" if cue["kind"] == "loop" else "0"
            length = cue["end"] - cue["position"] if cue["kind"] == "loop" else 0
            ET.SubElement(track, "CUE_V2", NAME=cue.get("name", ""), DISPL_ORDER=str(index),
                          TYPE=cue_type, START=f"{cue['position'] * 1000:.6f}", LEN=f"{length * 1000:.6f}",
                          REPEATS="-1", HOTCUE=str(slot))
    sets = ET.SubElement(root, "SETS")
    root_folder = ET.SubElement(sets, "NODE", TYPE="FOLDER", NAME="$ROOT")
    root_nodes = ET.SubElement(root_folder, "SUBNODES", COUNT="1")
    folder = ET.SubElement(root_nodes, "NODE", TYPE="FOLDER", NAME="Playlists")
    tree = _tree_for_playlists(manifest["crates"])
    subnodes = ET.SubElement(folder, "SUBNODES", COUNT=str(_node_count(tree)))

    def add_nodes(parent: ET.Element, branch: dict) -> None:
        for name, value in _ordered_tree_nodes(branch):
            children, tracks = value["_children"], value.get("_tracks")
            if tracks is not None and not children:
                node = ET.SubElement(parent, "NODE", TYPE="PLAYLIST", NAME=name,
                                     UUID=uuid.uuid4().hex)
                playlist = ET.SubElement(node, "PLAYLIST", ENTRIES=str(len(tracks)))
                for file_id in tracks:
                    ET.SubElement(playlist, "ENTRY").append(
                        ET.Element("PRIMARYKEY", TYPE="TRACK", KEY=keys[file_id]))
                continue
            if tracks is not None:
                node = ET.SubElement(parent, "NODE", TYPE="PLAYLIST", NAME=name,
                                     UUID=uuid.uuid4().hex)
                playlist = ET.SubElement(node, "PLAYLIST", ENTRIES=str(len(tracks)))
                for file_id in tracks:
                    ET.SubElement(playlist, "ENTRY").append(
                        ET.Element("PRIMARYKEY", TYPE="TRACK", KEY=keys[file_id]))
            node = ET.SubElement(parent, "NODE", TYPE="FOLDER", NAME=name)
            children_node = ET.SubElement(node, "SUBNODES", COUNT=str(_node_count(children)))
            add_nodes(children_node, children)
    add_nodes(subnodes, tree)
    output = directory / "collection.nml"
    if output.exists() or output.is_symlink():
        raise ValueError(f"Exchange output already exists: {output.name}.")
    ET.ElementTree(root).write(output, encoding="utf-8", xml_declaration=True)
    return output


def write_exchange(format: str, manifest: dict, media_paths: dict[str, Path],
                   directory: Path) -> Path:
    """Write an exchange document only; media paths are final and are never copied."""
    if format not in ("rekordbox-xml", "traktor-nml"):
        raise ValueError(f"Exchange writing is unsupported for {format!r}; use portable M3U export.")
    try:
        validate_manifest(manifest)
    except ProtocolError as error:
        raise ValueError(str(error)) from error
    expected = {item["id"] for item in manifest["files"]}
    if set(media_paths) != expected:
        raise ValueError("Exchange media paths do not match the manifest files.")
    for file_id, path in media_paths.items():
        if not isinstance(path, (str, Path)):
            raise ValueError(f"Invalid final media path for {file_id}.")
        if not Path(path).expanduser().is_absolute():
            raise ValueError(f"Final media path for {file_id} must be absolute.")
    directory = Path(directory).expanduser()
    if not directory.is_dir():
        raise ValueError("Choose an existing folder for the exchange document.")
    if format == "rekordbox-xml":
        return _write_rekordbox(manifest, media_paths, directory)
    return _write_traktor(manifest, media_paths, directory)
