"""Native djay MediaLibrary.db (TSAF v3), with additive playlist imports.

Format reference: CueRithm 9d96cc95d4b94959ecd6f9f839f56150c01b5d15,
crates/cuerithm-dj-sync/src/djay_pro.rs and djay_pro/bookmark.rs. This is an
independent, deliberately bounded implementation of that observed format.
New locations use file URIs, not security-scoped bookmarks. Playback/sandbox
authorization and full djay analysis must be verified in the native application.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
import struct
import uuid
import wave
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

import psutil

from .library import LibraryError, backup_database, sha256_file
from .protocol import PROTOCOL, validate_manifest

ROOT = "mediaItemPlaylist-root"
PLAYLISTS = "mediaItemPlaylists"
LINKS = "mediaItemPlaylistItems"
INDEXES = {
    "mediaView", "mediaItemsView", "mediaItemOriginalSourceView", "mediaItemIndex",
    "mediaItemUserDataIndex", "mediaItemTitleIDSearchIndex", "mediaItemAnalyzedDataIndex",
    "mediaItemKeySignatureIndexView", "searchIndex", "relationship",
    "mediaItemPlaylistsView", "mediaItemPlaylistView", "mediaItemPlaylistIndex",
}
CLASSES = {
    "mediaItems": "ADCMediaItem", "mediaItemTitleIDs": "ADCMediaItemTitleID",
    "mediaItemUserData": "ADCMediaItemUserData",
    "mediaItemAnalyzedData": "ADCMediaItemAnalyzedData",
    "localMediaItemLocations": "ADCMediaItemLocation",
    "mediaArtists": "ADCMediaArtist", "mediaAlbums": "ADCMediaAlbum",
    "mediaGenres": "ADCMediaGenre", "mediaLabels": "ADCMediaLabel",
    PLAYLISTS: "ADCMediaItemPlaylist", LINKS: "ADCMediaItemPlaylistItem",
}
ARRAY_TAGS = {10, 11, 12, 26, 27}
NUMBERS = {16: "H", 17: "I", 18: "Q", 19: "f", 20: "d", 48: "d"}
MAX_BLOB = 16 * 1024 * 1024
CAVEAT = ("djay locations use file URIs without new sandbox bookmarks. Native app playback "
          "has not been validated; authorize the received music in djay and verify playback. "
          "Full analysis and waveforms are not generated.")


def assert_djay_closed() -> None:
    try:
        for process in psutil.process_iter(["name"]):
            try:
                name = (process.info["name"] or "").casefold()
            except psutil.NoSuchProcess:
                continue
            if name.startswith(("djay", "algoriddim djay")):
                raise LibraryError("Quit djay Pro on this computer before accessing its library.")
    except psutil.AccessDenied as error:
        raise LibraryError("Cannot check running applications; djay import refused for safety.") from error
    except psutil.Error as error:
        raise LibraryError(f"Cannot check whether djay is running: {error}.") from error


@dataclass
class Value:
    tag: int
    value: object = None


@dataclass
class Object:
    class_name: str
    fields: dict


def text(value):
    return Value(8, value)


def array(values):
    return Value(27 if all(v.tag == 8 for v in values) else 10, values)


def native(class_name, **fields):
    return Object(class_name, fields)


class _Decoder:
    def __init__(self, data):
        if (not isinstance(data, bytes) or not 21 <= len(data) <= MAX_BLOB
                or data[:8] != b"TSAF\x03\x00\x03\x00" or data[8:16] != bytes(8)):
            raise LibraryError("Unsupported djay TSAF header or object size.")
        self.data, self.at, self.strings, self.numbers = data, 20, [], []

    def take(self, size):
        if size < 0 or self.at + size > len(self.data):
            raise LibraryError("Truncated djay TSAF object.")
        result = self.data[self.at:self.at + size]
        self.at += size
        return result

    def aligned(self, fmt):
        size = struct.calcsize("<" + fmt)
        self.take((-self.at) % size)
        return struct.unpack("<" + fmt, self.take(size))[0]

    def count(self):
        count = self.aligned("I")
        if count > len(self.data) - self.at:
            raise LibraryError("Invalid djay TSAF collection length.")
        return count

    def fields(self, depth, count=None):
        fields = {}
        while count is None or len(fields) < count:
            if count is None and self.data[self.at:self.at + 1] == b"\0":
                self.take(1)
                break
            value, name = self.read(depth + 1), self.read(depth + 1)
            if name.tag != 8 or name.value in fields:
                raise LibraryError("Ambiguous or invalid djay TSAF field name.")
            fields[name.value] = value
        return fields

    def read(self, depth=0):
        if depth >= 64:
            raise LibraryError("djay TSAF nesting limit exceeded.")
        tag = self.take(1)[0]
        if tag == 8:
            end = self.data.find(b"\0", self.at)
            if end == -1:
                raise LibraryError("Unterminated djay TSAF string.")
            try:
                value = self.take(end - self.at).decode("utf-8")
            except UnicodeError as error:
                raise LibraryError("Invalid UTF-8 in djay TSAF string.") from error
            self.take(1)
            self.strings.append(value)
        elif tag in (2, 5):
            index = self.take(1)[0]
            pool = self.strings if tag == 5 else self.numbers
            if index >= len(pool):
                raise LibraryError("Invalid djay TSAF reference.")
            return text(pool[index]) if tag == 5 else pool[index]
        elif tag in NUMBERS:
            value = self.aligned(NUMBERS[tag])
            if isinstance(value, float) and not math.isfinite(value):
                raise LibraryError("Non-finite djay TSAF number.")
            if tag in (19, 48):
                self.numbers.append(Value(tag, value))
        elif tag in (0, 13, 14, 45, 46):
            value = {0: None, 13: True, 14: False, 45: 1, 46: 0}[tag]
        elif tag == 15:
            value = self.take(1)[0]
        elif tag == 33:
            if self.take(1) != b"\x08":
                raise LibraryError("Invalid djay file URI encoding.")
            self.at -= 1
            value = self.read(depth + 1).value
            self.strings.pop()
            if self.take(1) != b"\0":
                raise LibraryError("Invalid djay file URI terminator.")
        elif tag == 21:
            size = self.count()
            value = self.take(size)
            self.take((-self.at) % 4)
        elif tag == 43:
            name = self.read(depth + 1)
            if name.tag != 8:
                raise LibraryError("Invalid djay TSAF class name.")
            value = Object(name.value, self.fields(depth + 1))
        elif tag == 24:
            value = self.fields(depth + 1, self.count())
        elif tag in ARRAY_TAGS:
            value = [self.read(depth + 1) for _ in range(self.count())]
            if tag == 27 and any(v.tag != 8 for v in value):
                raise LibraryError("Invalid djay string array.")
        else:
            raise LibraryError(f"Unsupported djay TSAF tag 0x{tag:02x}.")
        return Value(tag, value)


def decode(data: bytes) -> Object:
    decoder = _Decoder(data)
    root = decoder.read()
    if root.tag != 43 or decoder.at != len(data):
        raise LibraryError("Invalid djay TSAF root or trailing bytes.")
    return root.value


def encode(obj: Object) -> bytes:
    output = bytearray(b"TSAF\x03\x00\x03\x00" + bytes(12))
    strings = 0

    def aligned(fmt, value):
        size = struct.calcsize("<" + fmt)
        output.extend(bytes((-len(output)) % size))
        output.extend(struct.pack("<" + fmt, value))

    def emit(value, depth=0):
        nonlocal strings
        if depth >= 64:
            raise LibraryError("djay TSAF nesting limit exceeded.")
        tag, payload = value.tag, value.value
        output.append(tag)
        if tag == 8:
            if not isinstance(payload, str) or "\0" in payload:
                raise LibraryError("Invalid djay TSAF string.")
            output.extend(payload.encode("utf-8") + b"\0")
            strings += 1
        elif tag in NUMBERS:
            if isinstance(payload, float) and not math.isfinite(payload):
                raise LibraryError("Non-finite djay TSAF number.")
            aligned(NUMBERS[tag], payload)
        elif tag == 15:
            output.append(payload)
        elif tag in (0, 13, 14, 45, 46):
            pass
        elif tag == 33:
            emit(text(payload), depth + 1)
            output.append(0)
        elif tag == 21:
            aligned("I", len(payload))
            output.extend(payload)
            output.extend(bytes((-len(output)) % 4))
        elif tag in ARRAY_TAGS:
            aligned("I", len(payload))
            for member in payload:
                emit(member, depth + 1)
        elif tag in (24, 43):
            fields = payload.fields if tag == 43 else payload
            if tag == 43:
                emit(text(payload.class_name), depth + 1)
            else:
                aligned("I", len(fields))
            for name, member in fields.items():
                if tag == 43 and member.tag == 0:
                    raise LibraryError("Null djay class fields conflict with the TSAF object terminator.")
                emit(member, depth + 1)
                emit(text(name), depth + 1)
            if tag == 43:
                output.append(0)
        else:
            raise LibraryError(f"Unsupported djay TSAF tag 0x{tag:02x}.")
        if len(output) > MAX_BLOB:
            raise LibraryError("djay TSAF size limit exceeded.")

    emit(Value(43, obj))
    output[16:20] = struct.pack("<I", strings)
    result = bytes(output)
    decode(result)
    return result


def _get(obj, field, default=None):
    value = obj.fields.get(field)
    return default if value is None else value.value


def _ids(obj, field):
    value = obj.fields.get(field)
    if value is None:
        return []
    if value.tag not in ARRAY_TAGS or any(v.tag != 8 for v in value.value):
        raise LibraryError(f"Unsupported djay {field} references.")
    return [v.value for v in value.value]


def _file_uri(uri):
    if not isinstance(uri, str) or re.search(r"%(?![0-9a-fA-F]{2})", uri):
        raise LibraryError("Invalid djay file URI.")
    parts = urlsplit(uri)
    if (parts.scheme != "file" or parts.netloc not in ("", "localhost")
            or parts.query or parts.fragment):
        raise LibraryError("djay track is not an unambiguous local file URI.")
    try:
        raw = unquote(parts.path, errors="strict")
    except UnicodeError as error:
        raise LibraryError("Invalid UTF-8 in djay file URI.") from error
    if "\0" in raw or "\\" in raw or not raw.startswith("/") or raw.startswith("//"):
        raise LibraryError("Unsupported djay file URI path.")
    if re.match(r"^/[A-Za-z]:", raw):
        # pathlib on Windows accepts the URI's leading slash only after removal.
        import sys
        if sys.platform != "win32":
            raise LibraryError("djay Windows file URI is not local to this computer.")
        raw = raw[1:]
    if any(part in (".", "..") for part in raw.split("/")):
        raise LibraryError("Ambiguous djay file URI path.")
    return Path(raw).resolve()


def _bookmark_path(data):
    """Read stored POSIX components only; never resolve aliases or authorization."""
    if not isinstance(data, bytes) or len(data) > 1024 * 1024:
        raise LibraryError("Unsupported djay bookmark size or type.")
    paths, cursor, signatures = set(), 0, 0
    while True:
        start = data.find(b"book", cursor)
        if start < 0:
            break
        signatures += 1
        if signatures > 32:
            raise LibraryError("Too many djay bookmark signatures.")
        blob = data[start:]

        def word(at):
            if at < 0 or at + 4 > len(blob):
                raise LibraryError("Truncated djay bookmark.")
            return struct.unpack_from("<I", blob, at)[0]

        length, base = word(4), word(12)
        if not 16 <= base < length <= len(blob):
            raise LibraryError("Invalid djay bookmark header.")
        blob = blob[:length]

        def item(offset, kind):
            at = base + offset
            size = word(at)
            if word(at + 4) & 0xffffff00 != kind or at + 8 + size > len(blob):
                raise LibraryError("Unsupported djay bookmark item.")
            return blob[at + 8:at + 8 + size]

        toc, seen, entries, found = word(base), set(), 0, set()
        while toc:
            if toc in seen or len(seen) >= 128:
                raise LibraryError("Cyclic djay bookmark TOC.")
            seen.add(toc)
            at = base + toc
            if word(at + 4) != 0xfffffffe:
                raise LibraryError("Unsupported djay bookmark TOC.")
            count = word(at + 16)
            entries += count
            if entries > 4096 or at + 20 + count * 12 > len(blob):
                raise LibraryError("Invalid djay bookmark TOC length.")
            for entry in range(at + 20, at + 20 + count * 12, 12):
                if word(entry) != 0x1004:
                    continue
                offsets = item(word(entry + 4), 0x600)
                if not offsets or len(offsets) > 1024 or len(offsets) % 4:
                    raise LibraryError("Invalid djay bookmark path array.")
                components = []
                for (offset,) in struct.iter_unpack("<I", offsets):
                    try:
                        component = item(offset, 0x100).decode("utf-8")
                    except UnicodeError as error:
                        raise LibraryError("Invalid djay bookmark UTF-8.") from error
                    if (not component or component in (".", "..")
                            or any(c in component for c in "/\\\0")):
                        raise LibraryError("Unsafe djay bookmark path component.")
                    components.append(component)
                found.add("/" + "/".join(components))
            toc = word(at + 12)
        if len(found) != 1:
            raise LibraryError("Missing or ambiguous djay bookmark path.")
        paths.update(found)
        cursor = start + length
    if len(paths) > 1:
        raise LibraryError("Conflicting embedded djay bookmark paths.")
    if paths and os.name == "nt":
        raise LibraryError("macOS djay bookmark paths cannot be mapped to a Windows drive. "
                           "Use a native library with valid destination-local file URIs.")
    return Path(next(iter(paths))).resolve() if paths else None


def _integrity(db):
    if ([row[0] for row in db.execute("PRAGMA integrity_check")] != ["ok"]
            or db.execute("PRAGMA foreign_key_check").fetchone() is not None):
        raise LibraryError("djay database failed SQLite integrity checks; import refused.")


def _schema(db, writable=False):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, required in (
        ("database2", {"rowid", "collection", "key", "data", "metadata"}),
        ("yap2", {"extension", "key", "data"}),
    ):
        info = db.execute(f'PRAGMA table_info("{table}")').fetchall()
        if {r[1] for r in info} != required:
            raise LibraryError(f"Unsupported djay database schema: {table}.")
        if table == "database2" and not any(r[1] == "rowid" and r[2].upper() == "INTEGER" and r[5] for r in info):
            raise LibraryError("Unsupported djay native row identity.")
    if db.execute("SELECT 1 FROM database2 GROUP BY collection,key HAVING count(*)>1").fetchone():
        raise LibraryError("Ambiguous djay collection/key identity.")
    if not writable:
        return
    extensions = db.execute("SELECT extension,data FROM yap2 WHERE key='versionTag'").fetchall()
    seen = set()
    for extension, value in extensions:
        if extension not in INDEXES or extension in seen or not isinstance(value, (str, bytes, int)):
            raise LibraryError(f"Unfamiliar djay YapDatabase index policy: {extension}.")
        seen.add(extension)
    for table in tables:
        if table == "secondaryIndex_mediaItemLocationIndex":
            if {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')} != {"rowid", "fileName"}:
                raise LibraryError("Unsupported djay filename index schema.")
        elif table.startswith(("secondaryIndex_", "view_", "searchIndex_", "relationship_")):
            if not any(table.endswith("_" + ext) or ("_" + ext + "_") in table
                       for ext in seen):
                raise LibraryError(f"Unfamiliar djay index table: {table}.")
    for table, in db.execute("SELECT tbl_name FROM sqlite_master WHERE type='trigger'"):
        if table in {"database2", "yap2", "secondaryIndex_mediaItemLocationIndex"}:
            raise LibraryError("Unsupported djay database mutation trigger.")


def _objects(db):
    result = {collection: {} for collection in CLASSES}
    for rowid, collection, key, data in db.execute(
            "SELECT rowid,collection,key,data FROM database2 ORDER BY rowid"):
        if collection not in result:
            continue
        obj = decode(data)
        if obj.class_name != CLASSES[collection] or _get(obj, "uuid") != key:
            raise LibraryError(f"Unsupported or ambiguous djay object identity: {collection}/{key}.")
        result[collection][key] = (rowid, obj)
    return result


def _catalog(objects):
    playlists = objects[PLAYLISTS]
    if not playlists:
        return []
    if ROOT not in playlists or _get(playlists[ROOT][1], "type") != 1:
        raise LibraryError("Missing supported djay playlist root.")
    parents, paths = {}, {}
    for key, (_, obj) in playlists.items():
        kind = _get(obj, "type")
        if type(kind) is not int or kind not in (0, 1):
            raise LibraryError("Unsupported djay smart playlist or folder type.")
        children = _ids(obj, "childUUIDs")
        if len(set(children)) != len(children) or kind == 0 and children:
            raise LibraryError("Ambiguous djay playlist children.")
        for child in children:
            if child not in playlists or child in parents or child == ROOT:
                raise LibraryError("Ambiguous djay playlist hierarchy.")
            if _get(playlists[child][1], "parentUUID") != key:
                raise LibraryError("Conflicting djay playlist parent references.")
            parents[child] = key
    if set(parents) != set(playlists) - {ROOT}:
        raise LibraryError("Detached or unsupported djay playlist hierarchy.")

    def path(key, seen):
        if key == ROOT:
            return []
        if key in seen or len(seen) >= 32:
            raise LibraryError("Cyclic or excessively deep djay playlist hierarchy.")
        if key not in paths:
            obj = playlists[key][1]
            name = _get(obj, "name")
            if not isinstance(name, str) or not name.strip():
                raise LibraryError("Invalid djay playlist name.")
            paths[key] = path(parents[key], seen | {key}) + [name]
        return paths[key]

    result, normalized, owned_links = [], set(), set()
    for key, (rowid, obj) in playlists.items():
        if key == ROOT:
            continue
        names = path(key, set())
        identity = tuple(name.casefold() for name in names)
        if identity in normalized:
            raise LibraryError("Ambiguous djay playlist names.")
        normalized.add(identity)
        members = []
        for link_id in _ids(obj, "itemUUIDs"):
            if link_id in owned_links or link_id not in objects[LINKS]:
                raise LibraryError("Ambiguous or missing djay playlist membership.")
            owned_links.add(link_id)
            link = objects[LINKS][link_id][1]
            media = _get(link, "mediaItemUUID")
            if _get(link, "playlistUUID") != key or media not in objects["mediaItems"]:
                raise LibraryError("Broken djay playlist membership.")
            members.append(media)
        if _get(obj, "type") == 1 and members:
            raise LibraryError("Unsupported djay folder memberships.")
        result.append({"id": rowid, "path": names, "tracks": len(members),
                       "members": members, "uuid": key})
    return result


def _locations(objects):
    paths, identities = {}, {}
    for key, (_, obj) in objects["localMediaItemLocations"].items():
        uris = obj.fields.get("sourceURIs", array([]))
        if uris.tag not in ARRAY_TAGS:
            raise LibraryError("Unsupported djay location URI collection.")
        local = set()
        for value in uris.value:
            if value.tag not in (8, 33):
                raise LibraryError("Unsupported djay location reference.")
            if isinstance(value.value, str) and value.value.startswith("file:"):
                local.add(_file_uri(value.value))
        bookmark = _get(obj, "urlBookmarkData")
        if bookmark is not None:
            stored = _bookmark_path(bookmark)
            if stored is None:
                raise LibraryError("Cannot establish opaque djay bookmark identity safely.")
            local.add(stored)
        if len(local) > 1:
            raise LibraryError("Conflicting djay URI/bookmark or multiple local identities.")
        if not local:
            continue
        location = next(iter(local))
        if location in identities and identities[location] != key:
            raise LibraryError("Ambiguous djay local track identity.")
        if key not in objects["mediaItems"]:
            raise LibraryError("djay location references an unknown media item.")
        identities[location], paths[key] = key, location
    return paths, identities


def _key_name(index):
    if type(index) is not int or not 0 <= index < 24:
        raise LibraryError("Unsupported djay key signature index.")
    names = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
    return names[(index // 2 - (3 if index % 2 else 0)) % 12] + ("m" if index % 2 else "")


def _key_index(name):
    normalized = name.strip().replace("♯", "#").replace("♭", "b").casefold()
    for index in range(24):
        if _key_name(index).casefold() == normalized:
            return index
    camelot = ((23, 13, 3, 17, 7, 21, 11, 1, 15, 5, 19, 9),
               (22, 12, 2, 16, 6, 20, 10, 0, 14, 4, 18, 8))
    match = re.fullmatch(r"(1[0-2]|[1-9])([ab])", normalized)
    if match:
        return camelot[match[2] == "b"][int(match[1]) - 1]
    return None


def _metadata(key, objects, warnings):
    item = objects["mediaItems"][key][1]
    user = objects["mediaItemUserData"].get(key, (None, native("ADCMediaItemUserData")))[1]
    analyzed = objects["mediaItemAnalyzedData"].get(key, (None, native("ADCMediaItemAnalyzedData")))[1]
    metadata = {}
    title = _get(item, "title")
    if title is not None:
        metadata["title"] = title
    artists = _ids(item, "artistUUIDs")
    if artists:
        try:
            metadata["artist"] = ", ".join(_get(objects["mediaArtists"][artist][1], "name") for artist in artists)
        except (KeyError, TypeError) as error:
            raise LibraryError("Broken djay artist metadata reference.") from error
    album = _get(item, "albumUUID")
    if album is not None:
        if album not in objects["mediaAlbums"]:
            raise LibraryError("Broken djay album metadata reference.")
        metadata["album"] = _get(objects["mediaAlbums"][album][1], "name")
    bpm = _get(user, "manualBPM", _get(analyzed, "bpm", _get(item, "bpm")))
    if bpm is not None and bpm > 0:
        metadata["bpm"] = bpm
    signature = _get(user, "manualKeySignatureIndex2", _get(analyzed, "keySignatureIndex"))
    if signature is not None:
        metadata["key"] = _key_name(signature)
    cues = user.fields.get("cuePoints", array([]))
    if cues.tag not in ARRAY_TAGS:
        raise LibraryError("Unsupported djay cue array.")
    exported = []
    for cue in cues.value:
        if cue.tag != 43 or cue.value.class_name != "ADCCuePoint":
            raise LibraryError("Unsupported djay cue object.")
        obj = cue.value
        start, end, number = _get(obj, "time"), _get(obj, "endTime", -1), _get(obj, "number")
        if type(start) not in (float, int) or start < 0:
            warnings.add("Negative or unsupported djay cue positions were omitted.")
            continue
        record = {"kind": "loop" if end > start else "cue", "position": start,
                  "name": _get(obj, "comment", "")}
        if end > start:
            record["end"] = end
        if type(number) in (float, int) and number == int(number) and 1 <= number <= 256:
            record["slot"] = int(number) - 1
        exported.append(record)
    if exported:
        metadata["cues"] = exported
    if "beatGridEdits" in user.fields or "loopRegions" in user.fields:
        warnings.add("djay native beat-grid edits and legacy loop regions are not converted.")
    return metadata


class DjayLibrary:
    def __init__(self, path):
        candidate = Path(path).expanduser().resolve()
        self.path = candidate if candidate.name == "MediaLibrary.db" else candidate / "MediaLibrary.db"
        self.directory = self.path.parent

    def _connect(self, writable=False):
        if not self.path.is_file():
            raise LibraryError(f"djay library not found: {self.path}. Choose the bundle/folder containing MediaLibrary.db.")
        try:
            db = sqlite3.connect(self.path.as_uri() + ("?mode=rw" if writable else "?mode=ro"),
                                 uri=True, timeout=5)
            db.execute("PRAGMA foreign_keys=ON")
            return db
        except sqlite3.Error as error:
            raise LibraryError(f"Cannot open djay library: {error}.") from error

    def crates(self) -> list[dict]:
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            _schema(db)
            return [{k: crate[k] for k in ("id", "path", "tracks")}
                    for crate in _catalog(_objects(db))]

    def export(self, selected, sender_id, sender_name, progress=lambda text: None):
        files, paths, warnings, output = {}, {}, set(), []
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            _schema(db)
            objects = _objects(db)
            crates = _catalog(objects)
            chosen = set(selected)
            if not chosen or not chosen <= {crate["id"] for crate in crates}:
                raise LibraryError("Choose valid djay playlists; refresh if the library changed.")
            prefixes = [crate["path"] for crate in crates if crate["id"] in chosen]
            locations, _ = _locations(objects)
            cache = {}
            for crate in crates:
                if not any(crate["path"][:len(prefix)] == prefix for prefix in prefixes):
                    continue
                members = []
                for media in crate["members"]:
                    if media not in cache:
                        path = locations.get(media)
                        if path is None or not path.is_file():
                            raise LibraryError(f"Missing local djay track: {media}. Streaming/bookmark-only unresolved tracks cannot transfer.")
                        progress(f"Checking {path.name}")
                        before = path.stat()
                        digest = sha256_file(path)
                        after = path.stat()
                        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                            raise LibraryError("djay source file changed while hashing.")
                        key = hashlib.sha256((digest + "\0" + path.name).encode()).hexdigest()
                        record = {"id": key, "sha256": digest, "name": path.name,
                                  "size": after.st_size, "metadata": _metadata(media, objects, warnings)}
                        if key in files and files[key] != record:
                            raise LibraryError("Identical djay files have conflicting native metadata.")
                        files[key], paths[key], cache[media] = record, path, key
                    members.append(cache[media])
                output.append({"path": crate["path"], "tracks": members})
        manifest = {"protocol": PROTOCOL, "destination": {"format": "serato"},
                    "sender_id": sender_id, "sender_name": sender_name, "files": list(files.values()),
                    "crates": output, "warnings": sorted(warnings)}
        validate_manifest(manifest)
        return manifest, paths

    def validate_destination(self, music: Path):
        music = music.resolve()
        if music.is_relative_to(self.directory) or self.directory.is_relative_to(music):
            raise LibraryError("Keep received music separate from the djay database folder.")
        if music.exists() and not music.is_dir():
            raise LibraryError("Received music must be a folder.")
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            _schema(db, writable=True)
            _integrity(db)
            objects = _objects(db)
            _catalog(objects)
            _locations(objects)

    def import_crates(self, manifest, installed, backup_dir, transfer_id, guard=lambda: None):
        guard()
        validate_manifest(manifest)
        if not isinstance(transfer_id, str) or not transfer_id:
            raise LibraryError("A nonempty djay transfer identity is required.")
        records = {record["id"]: record for record in manifest["files"]}
        if set(installed) != set(records):
            raise LibraryError("Received djay files do not match the transfer manifest.")
        received, file_states = {}, {}
        for key, candidate in installed.items():
            path = Path(candidate).resolve()
            if path.is_relative_to(self.directory) or not path.is_file():
                raise LibraryError("Received djay tracks must be final local files outside the database folder.")
            record = records[key]
            before = path.stat()
            digest = sha256_file(path)
            state = path.stat()
            signature = (state.st_size, state.st_mtime_ns, state.st_ino, state.st_ctime_ns)
            if signature != (before.st_size, before.st_mtime_ns, before.st_ino, before.st_ctime_ns):
                raise LibraryError("Received djay file changed during checksum verification.")
            if state.st_size != record["size"] or digest != record["sha256"]:
                raise LibraryError("Received djay file failed size/checksum verification.")
            if path in received.values():
                raise LibraryError("Multiple received files resolve to one djay native identity.")
            received[key] = path
            file_states[key] = signature
        backup_dir = Path(backup_dir).resolve()
        if backup_dir.is_relative_to(self.directory):
            raise LibraryError("Keep djay backups outside its database folder.")
        warnings = set(manifest.get("warnings", []))
        warnings.add(CAVEAT)
        try:
            with closing(self._connect()) as source:
                source.execute("BEGIN")
                _schema(source, writable=True)
                _integrity(source)
                before = source.execute("PRAGMA data_version").fetchone()[0]
                # End validation's read transaction; backup_database pins its own snapshot.
                source.rollback()
                backup_dir.mkdir(parents=True, exist_ok=False)
                with closing(sqlite3.connect(backup_dir / "MediaLibrary.db")) as backup:
                    backup_database(source, backup, "MediaLibrary.db")
                    source.rollback()
                if source.execute("PRAGMA data_version").fetchone()[0] != before:
                    raise LibraryError("djay database changed during backup; quit djay and retry.")
                with closing(self._connect(writable=True)) as db:
                    try:
                        db.execute("BEGIN IMMEDIATE")
                        _schema(db, writable=True)
                        _integrity(db)
                        # The backup must represent precisely the pre-import database.
                        with closing(sqlite3.connect((backup_dir / "MediaLibrary.db").as_uri() + "?mode=ro", uri=True)) as backup:
                            from .library import verify_backup_contents
                            verify_backup_contents(db, backup)
                        objects = _objects(db)
                        catalog = _catalog(objects)
                        _, identities = _locations(objects)
                        track_ids = {}
                        for key, path in received.items():
                            if path in identities:
                                track_ids[key] = identities[path]
                                warnings.add("Existing djay media at a received path was reused without changing its metadata.")
                                continue
                            track_ids[key] = self._register(db, objects, path, records[key], warnings)
                        count = self._playlists(db, objects, catalog, manifest["crates"], track_ids)
                        for extension in INDEXES:
                            db.execute("UPDATE yap2 SET data=0 WHERE extension=? AND key='versionTag'", (extension,))
                        _integrity(db)
                        published = _objects(db)
                        _catalog(published)
                        registered, _ = _locations(published)
                        if any(registered.get(track_ids[key]) != path for key, path in received.items()):
                            raise LibraryError("djay native readback did not resolve every received media path.")
                        guard()
                        for key, path in received.items():
                            state = path.stat()
                            if file_states[key] != (state.st_size, state.st_mtime_ns, state.st_ino, state.st_ctime_ns):
                                raise LibraryError("Received djay audio changed during import; transaction rolled back.")
                        db.commit()
                    except BaseException:
                        db.rollback()
                        raise
        except sqlite3.Error as error:
            raise LibraryError(f"djay SQLite import failed; transaction rolled back: {error}.") from error
        return {"crates": count, "files": len(received),
                "memberships": sum(len(crate["tracks"]) for crate in manifest["crates"]),
                "backup": str(backup_dir), "warnings": sorted(warnings)}

    @staticmethod
    def _put(db, objects, collection, key, obj, update=False):
        if key in objects[collection]:
            if not update:
                raise LibraryError("djay generated identity collision; import refused.")
            db.execute("UPDATE database2 SET data=? WHERE collection=? AND key=?", (encode(obj), collection, key))
            return objects[collection][key][0]
        rowid = db.execute("INSERT INTO database2(collection,key,data,metadata) VALUES(?,?,?,NULL)",
                           (collection, key, encode(obj))).lastrowid
        objects[collection][key] = (rowid, obj)
        return rowid

    def _register(self, db, objects, path, record, warnings):
        key = uuid.uuid4().hex
        metadata = record.get("metadata", {})
        title = native("ADCMediaItemTitleID", uuid=text(key),
                       title=text(metadata.get("title") or path.stem),
                       artist=text(metadata.get("artist", "")))
        item = native("ADCMediaItem", uuid=text(key), titleID=Value(43, title),
                      title=title.fields["title"], originSourceID=text("file"))
        content = {".mp3": "public.mp3", ".wav": "com.microsoft.waveform-audio",
                   ".m4a": "com.apple.m4a-audio", ".flac": "org.xiph.flac",
                   ".mp4": "com.apple.m4v-video"}
        item.fields["contentType"] = text(content.get(path.suffix.lower(), "public.data"))
        if path.suffix.lower() not in content:
            warnings.add("This media extension uses a generic content type; verify decoder support and playback in djay.")
        if path.suffix.lower() == ".wav":
            try:
                with wave.open(str(path), "rb") as audio:
                    duration = audio.getnframes() / audio.getframerate()
                    if duration > 0:
                        title.fields["duration"] = item.fields["duration"] = Value(20, duration)
                        item.fields["sampleRate"] = Value(17, audio.getframerate())
            except (wave.Error, EOFError) as error:
                warnings.add(f"Duration unavailable for received WAV; djay must analyze it ({type(error).__name__}).")
        if "duration" not in title.fields:
            warnings.add("Unknown durations were omitted, not fabricated; djay must analyze received audio.")
        user = native("ADCMediaItemUserData", uuid=text(key), titleIDs=array([Value(43, title)]))
        analysis = native("ADCMediaItemAnalyzedData", uuid=text(key), titleIDs=array([Value(43, title)]))
        location = native("ADCMediaItemLocation", uuid=text(key), titleIDs=array([Value(43, title)]),
                          sourceURIs=array([Value(33, path.as_uri())]))
        if "bpm" in metadata:
            item.fields["bpm"] = analysis.fields["bpm"] = Value(20, metadata["bpm"])
            user.fields["manualBPM"] = Value(20, metadata["bpm"])
        if metadata.get("key"):
            index = _key_index(metadata["key"])
            if index is None:
                warnings.add("An unsupported musical key was omitted from djay metadata.")
            else:
                user.fields["manualKeySignatureIndex2"] = analysis.fields["keySignatureIndex"] = Value(16, index)
        cues, slots = [], set()
        for position, cue in enumerate(metadata.get("cues", [])):
            slot = cue.get("slot", position)
            if slot >= 8 or slot in slots:
                warnings.add("djay supports eight unique cue/loop slots; unsupported slots were omitted.")
                continue
            slots.add(slot)
            cues.append(Value(43, native("ADCCuePoint", time=Value(20, cue["position"]),
                                        endTime=Value(20, cue.get("end", -1)),
                                        comment=text(cue.get("name", "")), number=Value(20, slot + 1))))
        if cues:
            user.fields["cuePoints"] = array(cues)
        if metadata.get("beatgrid"):
            warnings.add("Incoming beat-grid markers were not converted to djay native grid edits.")
        for field, collection, reference in (
            ("artist", "mediaArtists", "artistUUIDs"), ("album", "mediaAlbums", "albumUUID"),
        ):
            name = metadata.get(field)
            if not name:
                continue
            matches = [identity for identity, (_, obj) in objects[collection].items() if _get(obj, "name") == name]
            if len(matches) > 1:
                raise LibraryError("Ambiguous djay entity identity.")
            identity = matches[0] if matches else uuid.uuid4().hex
            if not matches:
                self._put(db, objects, collection, identity,
                          native(CLASSES[collection], uuid=text(identity), name=text(name)))
            item.fields[reference] = array([text(identity)]) if reference.endswith("s") else text(identity)
        for collection, obj in (("mediaItems", item), ("mediaItemTitleIDs", title),
                                ("mediaItemUserData", user), ("mediaItemAnalyzedData", analysis)):
            self._put(db, objects, collection, key, obj)
        rowid = self._put(db, objects, "localMediaItemLocations", key, location)
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='secondaryIndex_mediaItemLocationIndex'").fetchone():
            db.execute("INSERT INTO secondaryIndex_mediaItemLocationIndex(rowid,fileName) VALUES(?,?)",
                       (rowid, path.name))
        return key

    def _playlists(self, db, objects, catalog, crates, track_ids):
        if ROOT not in objects[PLAYLISTS]:
            root = native(CLASSES[PLAYLISTS], uuid=text(ROOT), name=text("Playlists"),
                          type=Value(45, 1), childUUIDs=array([]), itemUUIDs=array([]))
            self._put(db, objects, PLAYLISTS, ROOT, root)
        # Never merge into existing folders: the complete incoming branch is new.
        occupied = {crate["path"][0].casefold() for crate in catalog}
        mapped, nodes = {}, {}
        for crate in crates:
            first = crate["path"][0]
            if first not in mapped:
                name, number = first, 2
                while name.casefold() in occupied:
                    name, number = f"{first} ({number})", number + 1
                mapped[first] = name
                occupied.add(name.casefold())
        paths = {tuple([mapped[crate["path"][0]]] + crate["path"][1:]): crate["tracks"] for crate in crates}
        all_paths = set(paths)
        for path in list(paths):
            all_paths.update(path[:length] for length in range(1, len(path)))
        for path in sorted(all_paths, key=lambda p: (len(p), tuple(n.casefold() for n in p))):
            folder = any(other[:len(path)] == path and len(other) > len(path) for other in all_paths)
            if folder and paths.get(path):
                raise LibraryError("djay cannot represent a playlist with both tracks and children.")
            identity = str(uuid.uuid4()).upper()
            parent = ROOT if len(path) == 1 else nodes[path[:-1]]
            obj = native(CLASSES[PLAYLISTS], uuid=text(identity), name=text(path[-1]),
                         type=Value(45 if folder else 46, int(folder)), parentUUID=text(parent),
                         itemUUIDs=array([]))
            if folder:
                obj.fields["childUUIDs"] = array([])
            self._put(db, objects, PLAYLISTS, identity, obj)
            nodes[path] = identity
            parent_obj = objects[PLAYLISTS][parent][1]
            parent_obj.fields["childUUIDs"] = array([text(child) for child in _ids(parent_obj, "childUUIDs")] + [text(identity)])
            self._put(db, objects, PLAYLISTS, parent, parent_obj, update=True)
            links = []
            for track in paths.get(path, []):
                link_id = str(uuid.uuid4()).upper()
                link = native(CLASSES[LINKS], uuid=text(link_id), playlistUUID=text(identity),
                              mediaItemUUID=text(track_ids[track]))
                self._put(db, objects, LINKS, link_id, link)
                links.append(text(link_id))
            if links:
                obj.fields["itemUUIDs"] = array(links)
                self._put(db, objects, PLAYLISTS, identity, obj, update=True)
        return len(crates)
