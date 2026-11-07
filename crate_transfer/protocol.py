from __future__ import annotations

import hashlib
import math
import re
import uuid

MAX_JSON = 8 * 1024 * 1024
MAX_FILES = 20000
MAX_TOTAL = 500 * 1024**3
MAX_FILE = 20 * 1024**3
HEX = re.compile(r"[0-9a-f]{64}")
AUDIO = {".mp3", ".flac", ".wav", ".aiff", ".aif", ".m4a", ".aac", ".ogg", ".alac", ".mp4"}
VIDEO = {".mpg", ".mpeg"}
PROTOCOL = "crate-transfer/2026-11"
DESTINATIONS = ("serato", "m3u", "rekordbox-xml", "traktor-nml", "djay",
                "traktor", "virtualdj-native", "engine", "rekordbox-native", "mixxx", "serato3")
NATIVE_DESTINATIONS = ("serato", "djay", "traktor", "virtualdj-native", "engine", "rekordbox-native", "mixxx", "serato3")


class ProtocolError(ValueError):
    pass


def identifier(value) -> str:
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise ProtocolError("Invalid file identifier.")
    return value


def machine_id(value) -> str:
    if not isinstance(value, str):
        raise ProtocolError("Invalid machine identity.")
    try:
        return str(uuid.UUID(value))
    except ValueError as error:
        raise ProtocolError("Invalid machine identity.") from error


def label(value, maximum=180) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ProtocolError("Invalid name.")
    return value


def validate_manifest(value: dict) -> dict:
    if not isinstance(value, dict) or value.get("protocol") != PROTOCOL or "version" in value:
        raise ProtocolError("Incompatible transfer protocol. Update Crate Transfer on both computers.")
    destination = value.get("destination")
    if not isinstance(destination, dict) or set(destination) != {"format"} or destination["format"] not in DESTINATIONS:
        raise ProtocolError("Unsupported transfer destination. No import started.")
    if "source_format" in value and value["source_format"] not in DESTINATIONS:
        raise ProtocolError("Unsupported source format for compatibility checks.")
    warnings = value.get("warnings", [])
    if not isinstance(warnings, list) or len(warnings) > 100:
        raise ProtocolError("Invalid conversion warnings.")
    for warning in warnings:
        label(warning, 1000)
    machine_id(value.get("sender_id"))
    label(value.get("sender_name"), 100)
    files, crates = value.get("files"), value.get("crates")
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise ProtocolError(f"A transfer supports up to {MAX_FILES} files.")
    if not isinstance(crates, list) or not 1 <= len(crates) <= 5000:
        raise ProtocolError("Choose between 1 and 5,000 crates.")
    keys, total = set(), 0
    for item in files:
        if not isinstance(item, dict):
            raise ProtocolError("Invalid file record.")
        key, digest = identifier(item.get("id")), identifier(item.get("sha256"))
        name = label(item.get("name"))
        if any(c in name for c in '/\\<>:"|?*') or name in (".", "..") or name.endswith((".", " ")):
            raise ProtocolError(f"Filename is not portable between macOS and Windows: {name}")
        if name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            raise ProtocolError(f"Reserved Windows filename: {name}")
        if "." not in name or ("." + name.rsplit(".", 1)[1].lower()) not in AUDIO | VIDEO:
            raise ProtocolError(f"Unsupported media file: {name}")
        if key != hashlib.sha256((digest + "\0" + name).encode()).hexdigest() or key in keys:
            raise ProtocolError("Duplicate or inconsistent file identifier.")
        size = item.get("size")
        if type(size) is not int or not 0 < size <= MAX_FILE:
            raise ProtocolError("Files must be nonempty and no larger than 20 GiB.")
        validate_metadata(item.get("metadata", {}))
        keys.add(key)
        total += size
    if total > MAX_TOTAL:
        raise ProtocolError("A transfer cannot exceed 500 GiB.")
    paths, references, memberships = set(), set(), 0
    for crate in crates:
        if not isinstance(crate, dict):
            raise ProtocolError("Invalid crate.")
        path, tracks = crate.get("path"), crate.get("tracks")
        if not isinstance(path, list) or not 1 <= len(path) <= 32:
            raise ProtocolError("Invalid crate hierarchy.")
        normalized = tuple(label(part).casefold() for part in path)
        if normalized in paths:
            raise ProtocolError("Duplicate crate names (case-insensitive).")
        paths.add(normalized)
        if not isinstance(tracks, list) or len(tracks) > MAX_FILES:
            raise ProtocolError("Invalid crate membership.")
        for key in tracks:
            if identifier(key) not in keys:
                raise ProtocolError("Crate references an unknown file.")
            references.add(key)
        memberships += len(tracks)
    if memberships > 200000 or references != keys:
        raise ProtocolError("Too many memberships, or unreferenced transfer files.")
    return value


def validate_metadata(metadata) -> None:
    if not isinstance(metadata, dict) or set(metadata) - {"title", "artist", "album", "key", "bpm", "cues", "beatgrid"}:
        raise ProtocolError("Unsupported track metadata.")
    for key in ("title", "artist", "album", "key"):
        if key in metadata:
            text = metadata[key]
            if not isinstance(text, str) or len(text) > 1000 or any(ord(c) < 32 for c in text):
                raise ProtocolError(f"Invalid track {key}.")

    def number(value, positive=False):
        if type(value) not in (int, float):
            raise ProtocolError("Invalid cue, tempo or beatgrid position.")
        try:
            finite = math.isfinite(value)
        except OverflowError as error:
            raise ProtocolError("Cue, tempo or beatgrid value exceeds supported numeric bounds.") from error
        if not finite or value < 0 or (positive and value == 0):
            raise ProtocolError("Invalid cue, tempo or beatgrid position.")

    if "bpm" in metadata:
        number(metadata["bpm"], positive=True)
    cues = metadata.get("cues", [])
    if not isinstance(cues, list) or len(cues) > 256:
        raise ProtocolError("Too many or invalid cues.")
    for cue in cues:
        if not isinstance(cue, dict) or set(cue) - {"kind", "position", "end", "name", "slot"}:
            raise ProtocolError("Invalid cue record.")
        if cue.get("kind") not in ("cue", "loop"):
            raise ProtocolError("Unsupported cue kind.")
        number(cue.get("position"))
        name = cue.get("name", "")
        if not isinstance(name, str) or len(name) > 180 or any(ord(c) < 32 for c in name):
            raise ProtocolError("Invalid cue name.")
        if "slot" in cue and (type(cue["slot"]) is not int or not 0 <= cue["slot"] <= 255):
            raise ProtocolError("Invalid cue slot.")
        if cue["kind"] == "loop" or "end" in cue:
            number(cue.get("end"))
            if cue["end"] <= cue["position"]:
                raise ProtocolError("Invalid loop length.")
    grid = metadata.get("beatgrid", [])
    if not isinstance(grid, list) or len(grid) > 4096:
        raise ProtocolError("Too many or invalid beatgrid markers.")
    previous = -1
    for marker in grid:
        if not isinstance(marker, dict) or set(marker) != {"position", "bpm", "beat"}:
            raise ProtocolError("Invalid beatgrid marker.")
        number(marker["position"])
        number(marker["bpm"], positive=True)
        if type(marker["beat"]) is not int or marker["beat"] < 1 or marker["position"] < previous:
            raise ProtocolError("Invalid beatgrid ordering or beat.")
        previous = marker["position"]
