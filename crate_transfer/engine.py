"""Bounded native Engine DJ Database2 adapter, independently implemented.

Schema/path reference: jrgutier/rb2engine (MIT), commit
1602d73e75306519a44b610ec1d616b167eb4f8e, Engine-authored 3.0.1/3.0.2
captures (Engine DJ 4.3 desktop/device). Track paths are relative to the
Engine Library folder, not Database2. libdjinterop (LGPL-3.0) documents
performance blobs; no implementation is copied and these blobs are untouched.
New tracks deliberately remain unanalysed. Native application acceptance,
playback, reanalysis and device portability still need real-library validation.
"""
from __future__ import annotations

import os
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

import psutil

from .exchange import ExchangeLibrary, _check_metadata
from .library import LibraryError, backup_database, sha256_file, verify_backup_contents
from .protocol import validate_manifest


TRACK_COLUMNS = """id playOrder length bpm year path filename bitrate bpmAnalyzed
albumArtId fileBytes title artist album genre comment label composer remixer key
rating albumArt timeLastPlayed isPlayed fileType isAnalyzed dateCreated dateAdded
isAvailable isMetadataOfPackedTrackChanged isPerfomanceDataOfPackedTrackChanged
playedIndicator isMetadataImported pdbImportKey streamingSource uri isBeatGridLocked
originDatabaseUuid originTrackId streamingFlags explicitLyrics lastEditTime""".split()
TABLE_COLUMNS = {
    "Information": "id uuid schemaVersionMajor schemaVersionMinor schemaVersionPatch currentPlayedIndiciator lastRekordBoxLibraryImportReadCounter".split(),
    "Playlist": "id title parentListId isPersisted nextListId lastEditTime isExplicitlyExported".split(),
    "PlaylistEntity": "id listId trackId databaseUuid nextEntityId membershipReference".split(),
    "PerformanceData": "trackId trackData overviewWaveFormData beatData quickCues loops thirdPartySourceId activeOnLoadLoops".split(),
}
REQUIRED_TRIGGERS = {
    "trigger_after_insert_Track_fix_origin",
    "trigger_after_insert_Track_insert_performance_data",
    "trigger_before_insert_List", "trigger_after_insert_List",
}
CAVEAT = (
    "Engine native import creates unanalysed tracks and ordered playlists only. "
    "Cues, loops, musical keys, beatgrids and waveforms are not written; analyze "
    "received music in Engine DJ. Native application playback/acceptance is not yet validated."
)
SOURCE_WARNING = (
    "Engine source preserves local playlist order, basic tags and scalar BPM. "
    "Performance blobs (cues, loops, keys, beatgrids and waveforms) are not converted."
)
LIMIT = 200000
TEXT_COLUMNS = set("""uuid databaseUuid path filename title artist album genre comment label
composer remixer albumArt fileType streamingSource uri originDatabaseUuid""".split())
BOOLEAN_COLUMNS = set("""isPlayed isAnalyzed isAvailable isMetadataOfPackedTrackChanged
isPerfomanceDataOfPackedTrackChanged isMetadataImported isBeatGridLocked explicitLyrics
isPersisted isExplicitlyExported""".split())
TIME_COLUMNS = set("timeLastPlayed dateCreated dateAdded lastEditTime".split())
BLOB_COLUMNS = set("trackData overviewWaveFormData beatData quickCues loops".split())


def assert_engine_closed():
    try:
        for process in psutil.process_iter(["name"]):
            try:
                name = (process.info["name"] or "").casefold()
            except psutil.NoSuchProcess:
                continue
            if name.startswith(("engine dj", "engine prime", "engine desktop", "engine.exe")) or name == "engine":
                raise LibraryError("Quit Engine DJ on this computer before accessing its library.")
    except psutil.Error as error:
        raise LibraryError("Cannot check running applications; Engine DJ access refused for safety.") from error


def _integrity(db):
    if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise LibraryError("Engine database failed SQLite quick_check.")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise LibraryError("Engine database contains broken foreign keys.")


def _schema(db, writable=False):
    expected = dict(TABLE_COLUMNS)
    info_columns = [row[1] for row in db.execute('PRAGMA table_info("Information")')]
    if info_columns != expected["Information"]:
        raise LibraryError("Unsupported Engine Information schema.")
    information = db.execute(
        "SELECT uuid,schemaVersionMajor,schemaVersionMinor,schemaVersionPatch FROM Information"
    ).fetchall()
    if len(information) != 1 or tuple(information[0][1:]) not in ((3, 0, 1), (3, 0, 2)):
        raise LibraryError("Unsupported Engine schema; only Database2 3.0.1 and 3.0.2 are supported.")
    database_uuid = information[0][0]
    try:
        uuid.UUID(database_uuid)
    except (ValueError, TypeError, AttributeError) as error:
        raise LibraryError("Engine database has no valid library UUID.") from error
    expected["Track"] = TRACK_COLUMNS + (["albumArtSourceHash"] if information[0][3] == 2 else [])
    for table, columns in expected.items():
        actual = db.execute(f'PRAGMA table_info("{table}")').fetchall()
        if [row[1] for row in actual] != columns or actual[0][5] != 1:
            raise LibraryError(f"Unsupported Engine {table} schema.")
        for column in actual:
            name = column[1]
            kind = ("TEXT" if name in TEXT_COLUMNS else "BOOLEAN" if name in BOOLEAN_COLUMNS else
                    "DATETIME" if name in TIME_COLUMNS else "BLOB" if name in BLOB_COLUMNS else
                    "REAL" if name == "bpmAnalyzed" else
                    "CHAR(40)" if name == "albumArtSourceHash" else "INTEGER")
            if (column[2].upper() != kind or column[3] != 0 or column[4] is not None
                    or column[5] != (1 if name == columns[0] else 0)):
                raise LibraryError(f"Unsupported Engine {table}.{name} declaration.")
        if table != "PerformanceData":
            sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
            if "AUTOINCREMENT" not in sql.upper():
                raise LibraryError(f"Unsupported Engine {table} identity allocation.")
        if db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] > LIMIT:
            raise LibraryError("Engine library exceeds this adapter's 200,000-row safety bound.")
    if writable:
        triggers = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        if not REQUIRED_TRIGGERS <= triggers:
            raise LibraryError("Unsupported Engine schema: native registration/list insertion triggers are missing.")
        for table, unique in (
            ("Track", {("path",), ("originDatabaseUuid", "originTrackId")}),
            ("Playlist", {("title", "parentListId"), ("parentListId", "nextListId")}),
            ("PlaylistEntity", {("listId", "databaseUuid", "trackId")}),
        ):
            indexes = set()
            for row in db.execute(f'PRAGMA index_list("{table}")'):
                if row[2]:
                    name = row[1].replace('"', '""')
                    indexes.add(tuple(col[2] for col in db.execute(f'PRAGMA index_info("{name}")')))
            if not unique <= indexes:
                raise LibraryError(f"Unsupported Engine {table} uniqueness constraints.")
        for table, required in (
            ("Track", {("AlbumArt", "albumArtId", "id", "NO ACTION", "RESTRICT")}),
            ("PerformanceData", {("Track", "trackId", "id", "CASCADE", "CASCADE")}),
            ("PlaylistEntity", {("Playlist", "listId", "id", "NO ACTION", "CASCADE")}),
        ):
            actual = {tuple(row[2:7]) for row in db.execute(f'PRAGMA foreign_key_list("{table}")')}
            if actual != required:
                raise LibraryError(f"Unsupported Engine {table} foreign keys.")
    return database_uuid


def _rows(db, table):
    columns = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
    return [dict(zip(columns, row)) for row in db.execute(f'SELECT * FROM "{table}" ORDER BY 1')]


def _chain(rows, pointer, context):
    if not rows:
        return []
    by_id = {row["id"]: row for row in rows}
    successors = [row[pointer] for row in rows]
    if (any(type(value) is not int or value < 0 or (value and value not in by_id) for value in successors)
            or len(successors) != len(set(successors))):
        raise LibraryError(f"Broken Engine {context} ordering.")
    heads = set(by_id) - set(successors)
    if len(heads) != 1:
        raise LibraryError(f"Broken Engine {context} ordering.")
    result, current = [], heads.pop()
    while current:
        if len(result) >= len(rows):
            raise LibraryError(f"Cyclic Engine {context} ordering.")
        result.append(by_id[current])
        current = by_id[current][pointer]
    if len(result) != len(rows):
        raise LibraryError(f"Disconnected Engine {context} ordering.")
    return result


def _catalog(db, database_uuid):
    tracks = {row["id"]: row for row in _rows(db, "Track")}
    lists, entities = _rows(db, "Playlist"), _rows(db, "PlaylistEntity")
    siblings, memberships = {}, {}
    ids = {row["id"] for row in lists}
    for row in lists:
        if type(row["id"]) is not int or row["id"] <= 0 or row["parentListId"] not in ids | {0}:
            raise LibraryError("Broken Engine playlist hierarchy.")
        siblings.setdefault(row["parentListId"], []).append(row)
    for row in entities:
        if row["listId"] not in ids:
            raise LibraryError("Engine playlist entity has no parent playlist.")
        memberships.setdefault(row["listId"], []).append(row)
    ordered = {parent: _chain(rows, "nextListId", "playlist") for parent, rows in siblings.items()}
    result, visited, paths = [], set(), set()

    def visit(parent, prefix):
        for row in ordered.get(parent, []):
            if row["id"] in visited or len(prefix) >= 32:
                raise LibraryError("Cyclic or excessive Engine playlist hierarchy.")
            visited.add(row["id"])
            title = row["title"]
            if not isinstance(title, str) or not title.strip():
                raise LibraryError("Engine playlist has an invalid title.")
            path = prefix + [title]
            normalized = tuple(part.casefold() for part in path)
            if normalized in paths:
                raise LibraryError("Engine has ambiguous case-insensitive playlist names.")
            paths.add(normalized)
            members = []
            for entity in _chain(memberships.get(row["id"], []), "nextEntityId", "membership"):
                if entity["databaseUuid"] != database_uuid or entity["trackId"] not in tracks:
                    raise LibraryError("Engine playlist references another database or a missing track; consolidate it in Engine DJ.")
                members.append(entity["trackId"])
            result.append({"id": row["id"], "path": path, "tracks": len(members),
                           "members": members, "persisted": row["isPersisted"]})
            visit(row["id"], path)

    visit(0, [])
    if visited != ids:
        raise LibraryError("Disconnected Engine playlist hierarchy.")
    return result, tracks


class EngineLibrary:
    def __init__(self, path):
        candidate = Path(path).expanduser().resolve()
        if candidate.name.casefold() == "m.db":
            self.path = candidate
        elif candidate.name.casefold() == "database2":
            self.path = candidate / "m.db"
        else:
            self.path = candidate / "Database2" / "m.db"
            if not self.path.is_file() and (candidate / "Engine Library" / "Database2" / "m.db").is_file():
                self.path = candidate / "Engine Library" / "Database2" / "m.db"
        if self.path.parent.name.casefold() != "database2" or not self.path.is_file():
            raise LibraryError("Choose an existing Engine-created Engine Library/Database2/m.db; no empty library is created.")
        self.directory = self.path.parent.parent
        self.backup_paths = [self.path]
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            _schema(db)

    def _connect(self, writable=False):
        assert_engine_closed()
        try:
            db = sqlite3.connect(self.path.as_uri() + ("?mode=rw" if writable else "?mode=ro"),
                                 uri=True, timeout=5)
            db.execute("PRAGMA foreign_keys=ON")
            return db
        except sqlite3.Error as error:
            raise LibraryError(f"Cannot open existing Engine database: {error}.") from error

    def _media(self, row):
        value = row["path"]
        if (not isinstance(value, str) or not value or "\0" in value or "\\" in value
                or "://" in value or row["streamingSource"] not in (None, "")):
            raise LibraryError("Engine track is not an unambiguous local media path; streaming tracks cannot transfer.")
        path = Path(value)
        if not path.is_absolute() and len(value) > 1 and value[1] == ":":
            raise LibraryError("Engine media path belongs to a different operating system.")
        return (path if path.is_absolute() else self.directory / path).resolve()

    def crates(self):
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            database_uuid = _schema(db)
            catalog, _ = _catalog(db, database_uuid)
            return [{key: row[key] for key in ("id", "path", "tracks")} for row in catalog if row["persisted"]]

    def export(self, selected, sender_id, name, progress=lambda text: None):
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            database_uuid = _schema(db)
            catalog, tracks = _catalog(db, database_uuid)
            adapter = object.__new__(ExchangeLibrary)
            adapter.warnings = [SOURCE_WARNING]
            adapter._catalog = [{key: row[key] for key in ("id", "path", "tracks")}
                                for row in catalog if row["persisted"]]
            chosen = set(selected)
            prefixes = [row["path"] for row in adapter._catalog if row["id"] in chosen]
            source_tracks = {}
            relevant = [row for row in catalog if row["persisted"] and
                        any(row["path"][:len(prefix)] == prefix for prefix in prefixes)]
            for track_id in {key for row in relevant for key in row["members"]}:
                row = tracks[track_id]
                metadata = {key: row[key] for key in ("title", "artist", "album")
                            if row[key] not in (None, "")}
                if row["bpmAnalyzed"] not in (None, 0):
                    metadata["bpm"] = row["bpmAnalyzed"]
                source_tracks[track_id] = {"path": self._media(row),
                                           "metadata": _check_metadata(metadata, adapter.warnings)}
            adapter._playlists = [{"path": row["path"], "members": row["members"], "tracks": source_tracks}
                                  for row in relevant]
            return adapter.export(selected, sender_id, name, progress)

    def validate_destination(self, music):
        music = Path(music).expanduser().resolve()
        if music.is_relative_to(self.path.parent) or self.path.parent.is_relative_to(music):
            raise LibraryError("Keep received music separate from Engine Database2.")
        if music.exists() and not music.is_dir():
            raise LibraryError("Received Engine music must be a folder.")
        try:
            os.path.relpath(music, self.directory)
        except ValueError as error:
            raise LibraryError("Engine received music must be on the library's drive.") from error
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            database_uuid = _schema(db, writable=True)
            _integrity(db)
            _catalog(db, database_uuid)

    def import_crates(self, manifest, installed, backup_dir, transfer_id, guard=lambda: None):
        guard()
        assert_engine_closed()
        validate_manifest(manifest)
        if not isinstance(transfer_id, str) or not transfer_id:
            raise LibraryError("A nonempty Engine transfer identity is required.")
        records = {row["id"]: row for row in manifest["files"]}
        if set(installed) != set(records):
            raise LibraryError("Received Engine files do not match the manifest.")
        received, states = {}, {}
        for key, candidate in installed.items():
            path = Path(candidate).resolve()
            self.validate_destination(path.parent)
            if not path.is_file():
                raise LibraryError("Received Engine media is not a final local file.")
            before = path.stat()
            digest = sha256_file(path)
            after = path.stat()
            state = (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_ino)
            if state != (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_ino):
                raise LibraryError("Received Engine media changed while hashing.")
            if digest != records[key]["sha256"] or after.st_size != records[key]["size"]:
                raise LibraryError("Received Engine media failed checksum/size verification.")
            if path in received.values():
                raise LibraryError("Multiple received files resolve to the same Engine track.")
            received[key], states[key] = path, state
        backup_dir = Path(backup_dir).resolve()
        if backup_dir.is_relative_to(self.directory) or any(path.is_relative_to(backup_dir) for path in received.values()):
            raise LibraryError("Keep Engine backups separate from the library and received music.")
        warnings = set(manifest.get("warnings", [])) | {CAVEAT}
        try:
            with closing(self._connect()) as source:
                source.execute("BEGIN")
                database_uuid = _schema(source, writable=True)
                _integrity(source)
                catalog, _ = _catalog(source, database_uuid)
                self._check_names(catalog, manifest["crates"])
                source.rollback()
                backup_dir.mkdir(parents=True, exist_ok=False)
                with closing(sqlite3.connect(backup_dir / "m.db")) as target:
                    backup_database(source, target, "m.db")
                source.rollback()
            with closing(self._connect(writable=True)) as db:
                try:
                    db.execute("BEGIN IMMEDIATE")
                    database_uuid = _schema(db, writable=True)
                    _integrity(db)
                    with closing(sqlite3.connect((backup_dir / "m.db").as_uri() + "?mode=ro", uri=True)) as backup:
                        verify_backup_contents(db, backup)
                        catalog, tracks = _catalog(db, database_uuid)
                        self._check_names(catalog, manifest["crates"])
                        guard()
                        assert_engine_closed()
                        identities = {}
                        for row in tracks.values():
                            if row["streamingSource"] in (None, "") and row["path"]:
                                path = self._media(row)
                                if path in identities:
                                    raise LibraryError("Existing Engine tracks have ambiguous local paths.")
                                identities[path] = row["id"]
                        track_ids, added_tracks = {}, set()
                        for key, path in received.items():
                            if path in identities:
                                track_ids[key] = identities[path]
                                warnings.add("Existing Engine tracks at received paths were reused without modifying metadata.")
                                continue
                            metadata = records[key].get("metadata", {})
                            native_path = Path(os.path.relpath(path, self.directory)).as_posix()
                            timestamp = int(time.time())
                            cursor = db.execute(
                                "INSERT INTO Track(path,filename,fileBytes,title,artist,album,bpmAnalyzed,"
                                "fileType,isAnalyzed,isAvailable,isPlayed,isMetadataImported,"
                                "isBeatGridLocked,streamingFlags,dateCreated,dateAdded,lastEditTime) "
                                "VALUES(?,?,?,?,?,?,?,?,0,1,0,1,0,0,?,?,?)",
                                (native_path, path.name, records[key]["size"], metadata.get("title") or path.stem,
                                 metadata.get("artist"), metadata.get("album"), metadata.get("bpm"),
                                 path.suffix.lstrip(".").lower(), timestamp, timestamp, timestamp),
                            )
                            track_ids[key] = cursor.lastrowid
                            added_tracks.add(cursor.lastrowid)
                            row = db.execute("SELECT originDatabaseUuid,originTrackId,isAnalyzed FROM Track WHERE id=?",
                                             (cursor.lastrowid,)).fetchone()
                            performance = db.execute("SELECT * FROM PerformanceData WHERE trackId=?",
                                                     (cursor.lastrowid,)).fetchall()
                            if row != (database_uuid, cursor.lastrowid, 0) or performance != [(cursor.lastrowid,) + (None,) * 7]:
                                raise LibraryError("Engine native track registration triggers did not produce the expected unanalysed record.")
                        intended, tail_changes, added_lists, added_entities = self._insert_lists(
                            db, manifest["crates"], track_ids, database_uuid, warnings)
                        _schema(db, writable=True)
                        published, published_tracks = _catalog(db, database_uuid)
                        readback = {tuple(row["path"]): row["members"] for row in published}
                        if any(readback.get(path) != members for path, members in intended.items()):
                            raise LibraryError("Engine ordered playlist readback differs from the transfer.")
                        if any(self._media(published_tracks[track_ids[key]]) != path for key, path in received.items()):
                            raise LibraryError("Engine track readback does not resolve to received music.")
                        self._preserved(db, backup, tail_changes, {
                            "Track": added_tracks, "PerformanceData": added_tracks,
                            "Playlist": added_lists, "PlaylistEntity": added_entities,
                        })
                    _integrity(db)
                    guard()
                    assert_engine_closed()
                    for key, path in received.items():
                        state = path.stat()
                        if states[key] != (state.st_size, state.st_mtime_ns, state.st_ctime_ns, state.st_ino):
                            raise LibraryError("Received Engine media changed during import.")
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
        except sqlite3.Error as error:
            raise LibraryError(f"Engine SQLite import failed; no transaction changes committed: {error}.") from error
        return {"crates": len(manifest["crates"]), "files": len(received),
                "memberships": sum(len(value) for value in intended.values()),
                "backup": str(backup_dir), "warnings": [CAVEAT] + sorted(warnings - {CAVEAT})[:99]}

    @staticmethod
    def _check_names(catalog, crates):
        existing_roots = {row["path"][0].casefold() for row in catalog}
        if any(crate["path"][0].casefold() in existing_roots for crate in crates):
            raise LibraryError("Engine destination playlist already exists; rename the incoming root playlist. Existing playlists are never merged.")
        prefixes = {}
        for crate in crates:
            for depth in range(1, len(crate["path"]) + 1):
                path = tuple(crate["path"][:depth])
                normalized = tuple(part.casefold() for part in path)
                if normalized in prefixes and prefixes[normalized] != path:
                    raise LibraryError("Incoming Engine playlist hierarchy has inconsistent capitalization.")
                prefixes[normalized] = path

    @staticmethod
    def _insert_lists(db, crates, track_ids, database_uuid, warnings):
        new_ids, intended, tail_changes, added_entities = {}, {}, {}, set()
        for crate in crates:
            parent = 0
            for depth, title in enumerate(crate["path"], 1):
                prefix = tuple(part.casefold() for part in crate["path"][:depth])
                if prefix not in new_ids:
                    tail = db.execute("SELECT id FROM Playlist WHERE parentListId=? AND nextListId=0",
                                      (parent,)).fetchone()
                    cursor = db.execute(
                        "INSERT INTO Playlist(title,parentListId,isPersisted,nextListId,lastEditTime,isExplicitlyExported)"
                        " VALUES(?,?,1,0,?,1)", (title, parent, int(time.time())),
                    )
                    new_ids[prefix] = cursor.lastrowid
                    if tail:
                        tail_changes[tail[0]] = cursor.lastrowid
                parent = new_ids[prefix]
            ordered = list(dict.fromkeys(track_ids[key] for key in crate["tracks"]))
            if len(ordered) != len(crate["tracks"]):
                warnings.add("Engine playlists do not support duplicate tracks; kept each track's first occurrence.")
            intended[tuple(crate["path"])] = ordered
            previous = None
            for track_id in ordered:
                cursor = db.execute("INSERT INTO PlaylistEntity(listId,trackId,databaseUuid,nextEntityId,membershipReference)"
                                    " VALUES(?,?,?,0,0)", (parent, track_id, database_uuid))
                added_entities.add(cursor.lastrowid)
                if previous is not None:
                    db.execute("UPDATE PlaylistEntity SET nextEntityId=? WHERE id=?", (cursor.lastrowid, previous))
                previous = cursor.lastrowid
        return intended, tail_changes, set(new_ids.values()), added_entities

    @staticmethod
    def _preserved(db, backup, tail_changes, additions):
        schema = "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        if db.execute(schema).fetchall() != backup.execute(schema).fetchall():
            raise LibraryError("Engine import unexpectedly changed the database schema.")
        allowed = {"Track", "PerformanceData", "Playlist", "PlaylistEntity", "sqlite_sequence"}
        for table, in backup.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            quoted = '"' + table.replace('"', '""') + '"'
            before = backup.execute(f"SELECT * FROM {quoted} NOT INDEXED").fetchall()
            after = db.execute(f"SELECT * FROM {quoted} NOT INDEXED").fetchall()
            if table not in allowed:
                if before != after:
                    raise LibraryError(f"Engine import unexpectedly modified {table}.")
            elif table != "sqlite_sequence":
                by_id = {row[0]: row for row in after}
                if set(by_id) != {row[0] for row in before} | additions[table]:
                    raise LibraryError(f"Engine import unexpectedly inserted or removed {table} records.")
                for row in before:
                    expected = list(row)
                    if table == "Playlist" and row[0] in tail_changes:
                        expected[4] = tail_changes[row[0]]
                    if by_id.get(row[0]) != tuple(expected):
                        raise LibraryError(f"Engine import unexpectedly changed an existing {table} record.")
            else:
                for name, sequence in before:
                    if name not in {"Track", "Playlist", "PlaylistEntity"} and (name, sequence) not in after:
                        raise LibraryError("Engine import unexpectedly changed an unrelated sequence.")
                if {row[0] for row in after} - {row[0] for row in before} - {"Track", "Playlist", "PlaylistEntity"}:
                    raise LibraryError("Engine import unexpectedly allocated an unrelated sequence.")


def make_engine_library(path):
    """Construct an existing native Engine library for the application's router."""
    return EngineLibrary(path)
