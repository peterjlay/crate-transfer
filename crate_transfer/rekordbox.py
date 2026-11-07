"""Bounded rekordbox 6/7 master.db adapter.

Native writes exclusively use the public pyrekordbox 0.4.4 API (Python >=3.8):
https://pypi.org/project/pyrekordbox/0.4.4/
Its add_content registers files; create_playlist/create_playlist_folder and
commit maintain registry USNs and masterPlaylists6.xml. No encrypted SQL writes
or analysis-file generation are implemented here.

Distribution dependencies: pyrekordbox==0.4.4 and
sqlcipher3-wheels==0.5.7. PyPI publishes CPython 3.9 SQLCipher wheels for macOS
Intel/ARM/universal2, Windows and Linux. These must be included and tested in
each frozen application; installing the Python SDK alone is not sufficient.
pyrekordbox 0.5.0 requires Python >=3.11 and is deliberately not substituted.
Plain exported native schemas can be read without these optional dependencies.
"""
from __future__ import annotations

import os
import logging
import shutil
import sqlite3
from contextlib import closing
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import psutil

from .exchange import ExchangeLibrary, _check_metadata
from .library import LibraryError, NativeImportUncertain, sha256_file
from .protocol import validate_manifest


SDK_REQUIREMENT = "pyrekordbox==0.4.4 and sqlcipher3-wheels==0.5.7"
RUNTIME_HELP = (
    "Native rekordbox access requires " + SDK_REQUIREMENT +
    " with a working compiled SQLCipher driver in this application's Python runtime. "
    "Install/use a Crate Transfer build bundling these optional dependencies; "
    "otherwise export rekordbox XML in rekordbox and choose the XML exchange adapter. "
    "An unencrypted exported master.db is supported as a read-only source without the SDK."
)
CAVEAT = (
    "rekordbox native conversion preserves normal playlist order and basic tags only. "
    "Smart playlists, cues, loops, beatgrids, waveforms and analysis are not converted. "
    "Analyze received tracks in rekordbox and verify playback."
)
SCHEMA = {
    "djmdContent": {"ID", "FolderPath", "Title", "ArtistID", "AlbumID", "KeyID", "BPM"},
    "djmdPlaylist": {"ID", "ParentID", "Name", "Attribute", "Seq"},
    "djmdSongPlaylist": {"ID", "PlaylistID", "ContentID", "TrackNo"},
    "djmdArtist": {"ID", "Name"},
    "djmdAlbum": {"ID", "Name"},
    "djmdKey": {"ID", "ScaleName"},
}
MEDIA = {".mp3", ".m4a", ".flac", ".wav", ".aiff", ".aif"}


def assert_rekordbox_closed():
    try:
        for process in psutil.process_iter(["name"]):
            try:
                name = (process.info["name"] or "").casefold()
            except psutil.NoSuchProcess:
                continue
            if name.startswith("rekordbox"):
                raise LibraryError("Quit rekordbox on this computer before accessing its library.")
    except psutil.Error as error:
        raise LibraryError("Cannot verify that rekordbox is closed; native access refused.") from error


def _sdk_factory():
    try:
        if version("pyrekordbox") != "0.4.4":
            raise LibraryError(RUNTIME_HELP)
        if version("sqlcipher3-wheels") != "0.5.7":
            raise LibraryError(RUNTIME_HELP)
        from sqlcipher3 import dbapi2
        from pyrekordbox import Rekordbox6Database
        from sqlalchemy import create_engine
        from sqlalchemy.exc import SQLAlchemyError
        engine = create_engine("sqlite+pysqlcipher://:native-runtime-test@/:memory:", module=dbapi2)
        try:
            with engine.connect() as db:
                if not db.exec_driver_sql("PRAGMA cipher_version").fetchone():
                    raise LibraryError(RUNTIME_HELP)
        except SQLAlchemyError as error:
            raise LibraryError(RUNTIME_HELP) from error
        finally:
            engine.dispose()
        return Rekordbox6Database
    except (ImportError, PackageNotFoundError, OSError) as error:
        raise LibraryError(RUNTIME_HELP) from error


def _validate_sdk_schema(db):
    """Require the complete mapped SDK schema; extras are allowed, missing fields are not."""
    from sqlalchemy import inspect
    from pyrekordbox.db6.tables import Base
    inspector = inspect(db.engine)
    present = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in present:
            raise LibraryError(f"Unsupported rekordbox 6/7 schema: missing {table.name}.")
        columns = {item["name"] for item in inspector.get_columns(table.name)}
        missing = set(table.columns.keys()) - columns
        if missing:
            raise LibraryError(f"Unsupported rekordbox 6/7 schema: {table.name} lacks "
                               f"{', '.join(sorted(missing))}.")


def _close_sdk(db):
    try:
        if db.session is not None:
            db.close()
    finally:
        db.engine.dispose()


def _signature(path):
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise LibraryError(f"rekordbox database/sidecar must be a regular file: {path.name}.")
    state = path.stat()
    return state.st_size, state.st_mtime_ns, state.st_ctime_ns, sha256_file(path)


class RekordboxLibrary(ExchangeLibrary):
    def __init__(self, path):
        candidate = Path(path).expanduser().absolute()
        self.path = candidate / "master.db" if candidate.is_dir() else candidate
        self.directory = self.path.parent
        self.format = "rekordbox-native"
        self.warnings = [CAVEAT]

    @property
    def backup_paths(self):
        return [self.path, *(Path(str(self.path) + suffix) for suffix in ("-wal", "-shm", "-journal")),
                self.directory / "masterPlaylists6.xml"]

    def _plain(self):
        if self.path.is_symlink() or not self.path.is_file():
            raise LibraryError("Choose an existing rekordbox master.db or its containing folder.")
        with self.path.open("rb") as source:
            return source.read(16) == b"SQLite format 3\0"

    def _open_sdk(self, factory=None):
        assert_rekordbox_closed()
        plain = self._plain()
        db = (factory or _sdk_factory())(path=self.path, db_dir=self.directory, unlock=not plain)
        try:
            _validate_sdk_schema(db)
        except BaseException:
            _close_sdk(db)
            raise
        return db

    def _read(self):
        assert_rekordbox_closed()
        if self._plain():
            try:
                with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
                    db.row_factory = sqlite3.Row
                    db.execute("PRAGMA query_only=ON")
                    db.execute("BEGIN")
                    for table, required in SCHEMA.items():
                        columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
                        if not required <= columns:
                            raise LibraryError(f"Unsupported rekordbox native schema: {table}.")
                    tags = {}
                    for table, field in (("djmdArtist", "Name"), ("djmdAlbum", "Name"), ("djmdKey", "ScaleName")):
                        tags[table] = {str(row["ID"]): row[field] for row in db.execute(f'SELECT * FROM "{table}"')}
                    tracks = {}
                    for row in db.execute("SELECT * FROM djmdContent"):
                        metadata = {"title": row["Title"] or ""}
                        for column, table, field in (("ArtistID", "djmdArtist", "artist"),
                                                      ("AlbumID", "djmdAlbum", "album"),
                                                      ("KeyID", "djmdKey", "key")):
                            if row[column] is not None:
                                if str(row[column]) not in tags[table]:
                                    raise LibraryError("rekordbox track references an unknown metadata row.")
                                metadata[field] = tags[table][str(row[column])] or ""
                        if row["BPM"]:
                            metadata["bpm"] = row["BPM"] / 100
                        tracks[str(row["ID"])] = self._track(row["FolderPath"], metadata)
                    playlists = [dict(row) for row in db.execute("SELECT * FROM djmdPlaylist")]
                    songs = [dict(row) for row in db.execute("SELECT * FROM djmdSongPlaylist")]
            except sqlite3.Error as error:
                raise LibraryError(f"Cannot read exported rekordbox native database: {error}.") from error
        else:
            db = self._open_sdk()
            try:
                tracks = {}
                for row in db.get_content():
                    metadata = {key: getattr(row, attr) or "" for key, attr in (
                        ("title", "Title"), ("artist", "ArtistName"), ("album", "AlbumName"), ("key", "KeyName"))}
                    if row.BPM:
                        metadata["bpm"] = row.BPM / 100
                    tracks[str(row.ID)] = self._track(row.FolderPath, metadata)
                playlists, songs = [], []
                for row in db.get_playlist():
                    playlists.append({key: getattr(row, key) for key in SCHEMA["djmdPlaylist"]})
                    songs.extend({key: getattr(song, key) for key in SCHEMA["djmdSongPlaylist"]}
                                 for song in row.Songs)
            finally:
                _close_sdk(db)
        self._playlists = self._catalog_native(playlists, songs, tracks)
        self._catalog = self._build_catalog()

    def _track(self, value, metadata):
        if not isinstance(value, str) or not Path(value).is_absolute() or "\0" in value:
            raise LibraryError("rekordbox track must have an absolute local media path.")
        return {"path": Path(value), "metadata": _check_metadata(metadata, self.warnings)}

    def _catalog_native(self, playlists, songs, tracks):
        nodes = {}
        for row in playlists:
            key = str(row["ID"])
            if not key or key in nodes:
                raise LibraryError("Duplicate rekordbox playlist identity.")
            nodes[key] = row
        paths = {}

        def hierarchy(key, visiting=()):
            if key in paths:
                return paths[key]
            if key in visiting or len(visiting) >= 32:
                raise LibraryError("Cyclic or excessively deep rekordbox playlist hierarchy.")
            row = nodes[key]
            parent = str(row["ParentID"])
            name = row["Name"]
            if not isinstance(name, str) or not name.strip():
                raise LibraryError("rekordbox playlist has no valid name.")
            if parent == "root":
                path = [name]
            else:
                if parent not in nodes or nodes[parent]["Attribute"] != 1:
                    raise LibraryError("rekordbox playlist has an unknown/non-folder parent.")
                path = hierarchy(parent, visiting + (key,)) + [name]
            paths[key] = path
            return path

        memberships = {}
        identities = set()
        for row in songs:
            pid, cid, sid = str(row["PlaylistID"]), str(row["ContentID"]), str(row["ID"])
            if sid in identities or pid not in nodes or cid not in tracks:
                raise LibraryError("rekordbox playlist has duplicate or dangling memberships.")
            identities.add(sid)
            if type(row["TrackNo"]) is not int or row["TrackNo"] < 1 or nodes[pid]["Attribute"] != 0:
                raise LibraryError("Unsupported rekordbox playlist membership/order.")
            memberships.setdefault(pid, []).append(row)
        result = []
        for key, row in sorted(nodes.items(), key=lambda pair: (pair[1]["Seq"] or 0, pair[0])):
            path = hierarchy(key)
            if row["Attribute"] == 1:
                continue
            if row["Attribute"] != 0:
                if "Smart/unsupported rekordbox playlists were omitted." not in self.warnings:
                    self.warnings.append("Smart/unsupported rekordbox playlists were omitted.")
                continue
            members = sorted(memberships.get(key, []), key=lambda item: (item["TrackNo"], str(item["ID"])))
            if len({item["TrackNo"] for item in members}) != len(members):
                raise LibraryError("Ambiguous rekordbox playlist track order.")
            result.append({"path": path, "members": [str(item["ContentID"]) for item in members],
                           "tracks": tracks})
        return result

    def crates(self):
        self._read()
        return super().crates()

    def export(self, selected, machine_id, name, progress=lambda text: None):
        self._read()
        return super().export(selected, machine_id, name, progress)

    def validate_destination(self, music):
        assert_rekordbox_closed()
        self._plain()
        music = Path(music).expanduser().resolve()
        parent = music
        while not parent.exists():
            parent = parent.parent
        if not parent.is_dir() or not os.access(parent, os.W_OK):
            raise LibraryError("Choose a writable received-music folder or parent directory.")
        if music == self.directory.resolve() or self.directory.resolve() in music.parents:
            raise LibraryError("Keep received music outside the rekordbox database folder.")
        if not os.access(self.path, os.W_OK) or not os.access(self.directory, os.W_OK):
            raise LibraryError("rekordbox database and containing folder must be writable.")
        db = self._open_sdk()
        try:
            if db.playlist_xml is None:
                raise LibraryError("Native writes require the original masterPlaylists6.xml beside master.db.")
        finally:
            _close_sdk(db)

    def import_crates(self, manifest, installed, backup_dir, transfer_id, guard=lambda: None):
        validate_manifest(manifest)
        if manifest["destination"]["format"] != "rekordbox-native":
            raise LibraryError("This transfer does not target native rekordbox.")
        assert_rekordbox_closed()
        self._plain()
        factory = _sdk_factory()
        records = {item["id"]: item for item in manifest["files"]}
        if set(installed) != set(records):
            raise LibraryError("Received rekordbox files do not match the manifest.")
        received, states = {}, {}
        for key, record in records.items():
            path = Path(installed[key]).expanduser()
            if not path.is_absolute() or path.is_symlink():
                raise LibraryError("rekordbox requires exact absolute, regular received media paths.")
            path = path.resolve(strict=True)
            if self.directory.resolve() == path.parent or self.directory.resolve() in path.parents:
                raise LibraryError("Received media must be outside the rekordbox database folder.")
            state = _signature(path)
            if state is None or state[0] != record["size"] or state[3] != record["sha256"]:
                raise LibraryError("Received rekordbox file failed size/checksum verification.")
            if path.suffix.lower() not in MEDIA:
                raise LibraryError(f"Public rekordbox SDK cannot register {path.suffix} media.")
            if path in received.values() or len(str(path)) > 255:
                raise LibraryError("Duplicate or excessively long received rekordbox media path.")
            received[key], states[key] = path, state
        backup_dir = Path(backup_dir).expanduser().resolve()
        if backup_dir == self.directory.resolve() or self.directory.resolve() in backup_dir.parents:
            raise LibraryError("Keep rekordbox backups outside its database folder.")
        if any(backup_dir == path or backup_dir in path.parents for path in received.values()):
            raise LibraryError("Keep rekordbox backups separate from received media.")
        originals = {path: _signature(path) for path in self.backup_paths}
        if originals[self.directory / "masterPlaylists6.xml"] is None:
            raise LibraryError("Native writes require the original masterPlaylists6.xml beside master.db.")
        backup_dir.mkdir(parents=True, exist_ok=False)
        for path, state in originals.items():
            if state is not None:
                shutil.copy2(path, backup_dir / path.name)
                if sha256_file(backup_dir / path.name) != state[3]:
                    raise LibraryError("rekordbox backup checksum failed; no import started.")
        if originals != {path: _signature(path) for path in originals}:
            raise LibraryError("rekordbox library changed during backup; quit rekordbox and retry.")
        guard()
        warnings = set(manifest.get("warnings", [])) | {CAVEAT}
        db, started = None, False
        try:
            assert_rekordbox_closed()
            started = True
            db = self._open_sdk(factory)
            if db.playlist_xml is None:
                raise LibraryError("SDK could not read masterPlaylists6.xml; import refused.")
            track_ids = self._register(db, received, records, warnings)
            expected = self._add_playlists(db, manifest["crates"], track_ids)
            if any(row[1] != crate["path"][-1] for row, crate in zip(expected, manifest["crates"])):
                warnings.add("Existing rekordbox playlists were preserved; colliding imports received new names.")
            db.flush()
            guard()
            assert_rekordbox_closed()
            if any(states[key] != _signature(path) for key, path in received.items()):
                raise LibraryError("Received media changed during rekordbox import.")
            db.commit()
            _close_sdk(db)
            db = None
            guard()
            assert_rekordbox_closed()
            db = self._open_sdk(factory)
            for key, path in received.items():
                row = db.get_content(ID=track_ids[key])
                if row is None or row.FolderPath != str(path):
                    raise LibraryError("rekordbox registration readback failed.")
            for pid, name, parent_id, members in expected:
                row = db.get_playlist(ID=pid)
                if row is None or row.Name != name or str(row.ParentID) != parent_id or row.Attribute != 0:
                    raise LibraryError("rekordbox playlist readback failed.")
                xml_row = db.playlist_xml.get(pid)
                expected_parent = "0" if parent_id == "root" else f"{int(parent_id):X}"
                if xml_row is None or xml_row["ParentId"] != expected_parent or xml_row["Attribute"] != 0:
                    raise LibraryError("rekordbox masterPlaylists6.xml readback failed.")
                actual = [str(song.ContentID) for song in sorted(row.Songs, key=lambda song: song.TrackNo)]
                if actual != members:
                    raise LibraryError("rekordbox playlist membership readback failed.")
            _close_sdk(db)
            db = None
        except BaseException as error:
            close_error = None
            if db is not None:
                try:
                    db.rollback()
                except BaseException:
                    logging.getLogger(__name__).exception("rekordbox SDK rollback failed; restoring backup after close")
                try:
                    _close_sdk(db)
                except BaseException as failure:
                    close_error = failure
            if started:
                try:
                    # Never restore into a live native app or an unclosed SDK connection.
                    assert_rekordbox_closed()
                    if close_error:
                        raise close_error
                    for path, state in originals.items():
                        if state is None:
                            if path.exists():
                                path.unlink()
                        else:
                            shutil.copy2(backup_dir / path.name, path)
                    if any((_signature(path) or (None, None, None, None))[3] !=
                           (state or (None, None, None, None))[3] for path, state in originals.items()):
                        raise LibraryError("Restored backup checksum mismatch.")
                except BaseException as restore_error:
                    raise NativeImportUncertain(f"rekordbox import failed; automatic restore was unsafe/failed. "
                                       f"Quit rekordbox and restore all originals from {backup_dir}: "
                                       f"{restore_error}.") from error
            raise LibraryError(f"rekordbox import failed; originals restored from {backup_dir}: {error}.") from error
        return {"crates": len(expected), "files": len(received),
                "memberships": sum(len(crate["tracks"]) for crate in manifest["crates"]),
                "backup": str(backup_dir), "warnings": sorted(warnings)}

    @staticmethod
    def _register(db, received, records, warnings):
        identities = {}
        for row in db.get_content():
            if row.FolderPath in identities:
                raise LibraryError("Duplicate rekordbox native media paths; repair the library first.")
            identities[row.FolderPath] = str(row.ID)
        result = {}
        for key, path in received.items():
            if str(path) in identities:
                result[key] = identities[str(path)]
                warnings.add("Existing rekordbox media paths were reused without changing metadata.")
                continue
            metadata = records[key].get("metadata", {})
            fields = {"Title": metadata.get("title") or path.stem}
            for field in ("title", "artist", "album", "key"):
                if len(metadata.get(field, "")) > 255:
                    raise LibraryError(f"rekordbox {field} exceeds the native 255-character limit.")
            if "bpm" in metadata:
                if metadata["bpm"] > 21474836.47:
                    raise LibraryError("rekordbox BPM exceeds the native integer range.")
                fields["BPM"] = round(metadata["bpm"] * 100)
            artist = None
            if metadata.get("artist"):
                artist = db.get_artist(Name=metadata["artist"]).first()
                artist = artist or db.add_artist(metadata["artist"])
                fields["ArtistID"] = artist.ID
            if metadata.get("album"):
                album = db.get_album(Name=metadata["album"]).first()
                album = album or db.add_album(metadata["album"], artist=artist)
                fields["AlbumID"] = album.ID
            if metadata.get("key"):
                native_key = db.get_key(ScaleName=metadata["key"]).first()
                if native_key:
                    fields["KeyID"] = native_key.ID
                else:
                    warnings.add("Unmatched rekordbox musical key was omitted rather than guessed.")
            result[key] = str(db.add_content(path, **fields).ID)
        return result

    @staticmethod
    def _add_playlists(db, crates, track_ids):
        nodes = list(db.get_playlist())
        folders, expected = {}, []

        def children(parent):
            pid = "root" if parent is None else str(parent.ID)
            return [row for row in nodes if str(row.ParentID) == pid]

        def unique(name, parent):
            occupied = {row.Name.casefold() for row in children(parent)}
            number, candidate = 1, name
            while candidate.casefold() in occupied:
                number += 1
                candidate = f"{name} (Imported {number})"
            if len(candidate) > 255:
                raise LibraryError("rekordbox playlist name exceeds the native 255-character limit.")
            return candidate

        for crate in crates:
            parent = None
            prefix = []
            for part in crate["path"][:-1]:
                prefix.append(part.casefold())
                key = tuple(prefix)
                if key not in folders:
                    matching = [row for row in children(parent)
                                if row.Attribute == 1 and row.Name.casefold() == part.casefold()]
                    if len(matching) > 1:
                        raise LibraryError("Ambiguous existing rekordbox playlist folders.")
                    folder = matching[0] if matching else db.create_playlist_folder(unique(part, parent), parent=parent)
                    if not matching:
                        nodes.append(folder)
                    folders[key] = folder
                parent = folders[key]
            name = unique(crate["path"][-1], parent)
            playlist = db.create_playlist(name, parent=parent)
            nodes.append(playlist)
            members = [track_ids[key] for key in crate["tracks"]]
            for position, cid in enumerate(members, 1):
                db.add_to_playlist(playlist, cid, track_no=position)
            expected.append((str(playlist.ID), name, str(playlist.ParentID), members))
        return expected
