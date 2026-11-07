"""Legacy Serato crate TLV adapter; database V2 remains owned by Serato.

Format facts: Mixxx Serato reader, Serato-lib and CueRithm legacy reader.
Independent bounded codec; no third-party implementation is copied.
"""
from __future__ import annotations

import ntpath
import os
import re
import shutil
import struct
import sys
from pathlib import Path

from .exchange import ExchangeLibrary, _check_metadata
from .library import LibraryError, NativeImportUncertain, assert_serato_closed, sha256_file
from .protocol import validate_manifest

CRATE_VERSION = "1.0/Serato ScratchLive Crate"
LIMIT = 32 * 1024 * 1024
CAVEAT = (
    "Serato DJ 3 legacy preview writes native .crate files, not the Serato 4 SQLite library. "
    "Serato must reload/scan received media; database V2, neworder.pref, smart rules and analysis are "
    "not rewritten. Database-only cues, loops, grids and tags are not imported; embedded media is unchanged. "
    "Verify crate visibility and playback in Serato DJ 3."
)


def fields(data):
    if len(data) > LIMIT:
        raise LibraryError("Legacy Serato file exceeds the 32 MiB safety limit.")
    result, offset = [], 0
    while offset < len(data):
        if len(data) - offset < 8:
            raise LibraryError("Truncated legacy Serato field header.")
        tag, length = struct.unpack_from(">4sI", data, offset)
        offset += 8
        if length > len(data) - offset or not all(32 <= char <= 126 for char in tag):
            raise LibraryError("Invalid legacy Serato field tag or length.")
        result.append((tag, data[offset:offset + length]))
        offset += length
        if len(result) > 200000:
            raise LibraryError("Too many legacy Serato fields.")
    return result


def text(data):
    try:
        value = data.decode("utf-16-be")
    except UnicodeError as error:
        raise LibraryError("Invalid legacy Serato UTF-16BE text.") from error
    if "\0" in value:
        raise LibraryError("NUL in legacy Serato text.")
    return value


def field(tag, payload):
    return tag + struct.pack(">I", len(payload)) + payload


def crate_bytes(paths):
    return field(b"vrsn", CRATE_VERSION.encode("utf-16-be")) + b"".join(
        field(b"otrk", field(b"ptrk", path.encode("utf-16-be"))) for path in paths)


def crate_paths(data):
    rows = fields(data)
    if not rows or rows[0] != (b"vrsn", CRATE_VERSION.encode("utf-16-be")):
        raise LibraryError("Unsupported legacy Serato crate version.")
    paths = []
    for tag, payload in rows[1:]:
        if tag == b"otrk":
            references = [text(value) for child, value in fields(payload) if child == b"ptrk"]
            if len(references) != 1:
                raise LibraryError("Legacy Serato track needs exactly one ptrk.")
            paths.extend(references)
    return paths


def volume_root(path):
    path = path.resolve()
    if sys.platform == "win32":
        if path.anchor.startswith("\\\\"):
            raise LibraryError("Serato DJ 3 requires a local drive, not a network share.")
        return Path(path.anchor)
    if path.parts[1:2] == ("Volumes",) and len(path.parts) > 2:
        return Path("/Volumes") / path.parts[2]
    return Path("/")


class Serato3Library(ExchangeLibrary):
    def __init__(self, path):
        candidate = Path(path).expanduser().resolve()
        if candidate.name == "Subcrates":
            candidate = candidate.parent
        if candidate.name != "_Serato_" or not (candidate / "Subcrates").is_dir():
            raise LibraryError("Choose an existing Serato DJ 3 _Serato_ folder containing Subcrates.")
        self.path = self.directory = candidate
        self.format = "serato3"
        self.volume = volume_root(candidate)
        assert_serato_closed()
        self._read()

    @property
    def backup_paths(self):
        return list(self._originals())

    def _originals(self):
        paths = [self.directory / "database V2", self.directory / "neworder.pref"]
        paths.extend(sorted((self.directory / "Subcrates").glob("*.crate")))
        result = {}
        for path in paths:
            if path.is_symlink():
                raise LibraryError("Symbolic links in legacy Serato libraries are unsupported.")
            if path.exists():
                if not path.is_file():
                    raise LibraryError("Legacy Serato library artifact is not a file.")
                result[path] = sha256_file(path)
        if len(result) > 5002:
            raise LibraryError("Legacy Serato library exceeds 5,000 crates.")
        return result

    def _media(self, raw):
        portable = raw.replace("\\", "/")
        drive, tail = ntpath.splitdrive(portable)
        if (not portable.strip() or any(ord(char) < 32 for char in portable) or "://" in portable
                or drive or portable.startswith("//") or ".." in portable.split("/")):
            raise LibraryError("Unsupported legacy Serato path; use local volume-relative media.")
        return self.volume / tail.lstrip("/")

    def _read(self):
        before = self._originals()
        tags = {}
        database = self.directory / "database V2"
        if database.is_file():
            with database.open("rb") as source:
                rows = fields(source.read(LIMIT + 1))
            if not rows or rows[0][0] != b"vrsn" or text(rows[0][1]) != "2.0/Serato Scratch LIVE Database":
                raise LibraryError("Unsupported Serato database V2 header; use a supported DJ 3 library.")
            for tag, payload in rows[1:]:
                if tag != b"otrk":
                    continue
                values = fields(payload)
                paths = [text(value) for key, value in values if key == b"pfil"]
                if len(paths) != 1:
                    raise LibraryError("Legacy Serato database track needs one pfil.")
                path = self._media(paths[0])
                if path in tags:
                    raise LibraryError("Ambiguous legacy Serato database media identity.")
                metadata = {}
                for native, normalized in ((b"tsng", "title"), (b"tart", "artist"),
                                           (b"talb", "album"), (b"tkey", "key"), (b"tbpm", "bpm")):
                    matching = [text(value) for key, value in values if key == native]
                    if len(matching) > 1:
                        raise LibraryError("Duplicate legacy Serato metadata field.")
                    if matching and matching[0]:
                        metadata[normalized] = matching[0]
                tags[path] = metadata
        self.warnings = [CAVEAT]
        self._playlists = []
        for path in before:
            if path.suffix.lower() != ".crate":
                continue
            parts = path.stem.replace("≫≫", "%%").split("%%")
            with path.open("rb") as source:
                members = [self._media(raw) for raw in crate_paths(source.read(LIMIT + 1))]
            tracks = {member: {"path": member, "metadata": _check_metadata(dict(tags.get(member, {})), self.warnings)}
                      for member in members}
            self._playlists.append({"path": parts, "members": members, "tracks": tracks})
        self._playlists.sort(key=lambda playlist: tuple(part.casefold() for part in playlist["path"]))
        assert_serato_closed()
        if before != self._originals():
            raise LibraryError("Serato DJ 3 library changed while reading; close Serato and retry.")
        self._catalog = self._build_catalog()

    def crates(self):
        self._read()
        return super().crates()

    def export(self, selected, machine_id, name, progress=lambda text: None):
        self._read()
        return super().export(selected, machine_id, name, progress)

    def validate_destination(self, music):
        assert_serato_closed()
        self._read()
        self._validate_music(music)

    def _validate_music(self, music):
        music = Path(music).expanduser().resolve()
        if self.directory == music or self.directory in music.parents:
            raise LibraryError("Keep received music outside _Serato_.")
        parent = music
        while not parent.exists():
            parent = parent.parent
        if not parent.is_dir() or os.stat(parent).st_dev != os.stat(self.directory).st_dev:
            raise LibraryError("Serato DJ 3 media and _Serato_ must be on the same local volume.")
        if volume_root(parent) != self.volume:
            raise LibraryError("Serato DJ 3 destination media must use the library's local volume root.")

    def import_crates(self, manifest, installed, backup_dir, transfer_id, guard=assert_serato_closed):
        validate_manifest(manifest)
        if not isinstance(transfer_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", transfer_id):
            raise LibraryError("Invalid legacy Serato transfer identity.")
        guard()
        assert_serato_closed()
        before = self._originals()
        if set(installed) != {row["id"] for row in manifest["files"]}:
            raise LibraryError("Received Serato DJ 3 media differs from the manifest.")
        paths, states = {}, {}
        for row in manifest["files"]:
            path = Path(installed[row["id"]]).resolve(strict=True)
            self._validate_music(path.parent)
            stat = path.stat()
            if not path.is_file() or stat.st_dev != os.stat(self.directory).st_dev:
                raise LibraryError("Received Serato DJ 3 media must be a file on the library's volume.")
            states[path] = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
            if stat.st_size != row["size"] or sha256_file(path) != row["sha256"]:
                raise LibraryError("Received Serato DJ 3 media failed checksum verification.")
            paths[row["id"]] = path.relative_to(self.volume).as_posix()
        folder = self.directory / "Subcrates"
        occupied = set()
        for path in folder.glob("*.crate"):
            parts = path.stem.replace("≫≫", "%%").split("%%")
            occupied.update("%%".join(parts[:index]).casefold() for index in range(1, len(parts) + 1))
        outputs, prefixes, memberships = {}, {}, 0
        from .portable import safe_component
        warnings = [CAVEAT]
        for crate in manifest["crates"]:
            original_prefix, native_prefix = (), ()
            for part in crate["path"]:
                original_prefix += (part,)
                if original_prefix not in prefixes:
                    safe = safe_component(part).replace("%%", "_").replace("≫≫", "_")
                    candidate, number = safe, 2
                    while "%%".join((*native_prefix, candidate)).casefold() in occupied:
                        candidate = f"{safe} ({number})"
                        number += 1
                    native = (*native_prefix, candidate)
                    stem = "%%".join(native)
                    if len((stem + ".crate").encode("utf-8")) > 240:
                        raise LibraryError("Serato DJ 3 crate filename is too long; shorten its hierarchy.")
                    prefixes[original_prefix] = native
                    occupied.add(stem.casefold())
                    outputs[folder / (stem + ".crate")] = crate_bytes([])
                native_prefix = prefixes[original_prefix]
            members = list(dict.fromkeys(paths[key] for key in crate["tracks"]))
            memberships += len(members)
            if len(members) != len(crate["tracks"]):
                warnings.append("Serato DJ 3 keeps one occurrence of each track per crate; repeats were omitted.")
            outputs[folder / ("%%".join(native_prefix) + ".crate")] = crate_bytes(members)
        backup_dir = Path(backup_dir).resolve()
        if self.directory == backup_dir or self.directory in backup_dir.parents:
            raise LibraryError("Keep Serato DJ 3 backups outside _Serato_.")
        backup_dir.mkdir(parents=True, exist_ok=False)
        for path, digest in before.items():
            target = backup_dir / path.relative_to(self.directory)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            if sha256_file(target) != digest:
                raise LibraryError("Serato DJ 3 backup verification failed.")
        from .config import atomic_json
        atomic_json(backup_dir / "created-crates.json", [path.relative_to(self.directory).as_posix() for path in outputs])
        staged, published = [], []
        try:
            for index, (path, data) in enumerate(outputs.items()):
                temporary = folder / (".crate-transfer-" + transfer_id + f"-{index}.tmp")
                staged.append(temporary)
                with temporary.open("xb") as target:
                    target.write(data)
                    target.flush()
                    os.fsync(target.fileno())
                if crate_paths(temporary.read_bytes()) != crate_paths(data):
                    raise LibraryError("Serato DJ 3 staged crate readback failed.")
            guard()
            assert_serato_closed()
            if before != self._originals():
                raise LibraryError("Serato DJ 3 library changed after backup; import refused.")
            for path, expected in states.items():
                stat = path.stat()
                if expected != (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino):
                    raise LibraryError("Received Serato DJ 3 media changed during import.")
            for (path, _), temporary in zip(outputs.items(), staged):
                os.link(temporary, path)
                published.append(path)
        except BaseException as error:
            try:
                for path in reversed(published):
                    path.unlink()
            except OSError as rollback_error:
                raise NativeImportUncertain(f"Serato DJ 3 rollback failed; retain media and inspect {backup_dir}: "
                                            f"{rollback_error}") from error
            raise
        finally:
            try:
                for temporary in staged:
                    temporary.unlink(missing_ok=True)
            except OSError as cleanup_error:
                if published:
                    raise NativeImportUncertain(
                        f"Serato DJ 3 crates may be published but staging cleanup failed; inspect {backup_dir}: "
                        f"{cleanup_error}"
                    ) from cleanup_error
                raise
        return {"crates": len(manifest["crates"]), "files": len(paths), "memberships": memberships,
                "backup": str(backup_dir), "warnings": list(dict.fromkeys(warnings))}
