from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from datetime import datetime, timezone

import psutil

from .exchange import ExchangeLibrary, _check_metadata
from .library import sha256_file
from .protocol import validate_manifest


def assert_mixxx_closed():
    for process in psutil.process_iter(["name"]):
        try:
            if (process.info["name"] or "").lower() in ("mixxx", "mixxx.exe"):
                raise ValueError("Quit Mixxx before accessing its native library.")
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as error:
            raise ValueError("Cannot check running applications; Mixxx access refused for safety.") from error


class MixxxLibrary(ExchangeLibrary):
    """Read native playlists/crates and add received playlists to existing databases."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        if self.path.is_dir():
            self.path /= "mixxxdb.sqlite"
        if not self.path.is_file():
            raise ValueError("No existing Mixxx library found. Choose mixxxdb.sqlite or its folder.")
        assert_mixxx_closed()
        self.directory = self.path.parent
        self.backup_paths = [self.path]
        self.format = "mixxx"
        self.warnings = [
            "Mixxx native source preserves playlist membership and basic tags; cue/loop and beatgrid blobs "
            "are not converted by this adapter. Crates have no stored track order and are sorted by artist/title."
        ]
        self._playlists = []
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            self._read(db)
        self._catalog = self._build_catalog()

    @staticmethod
    def _columns(db, table, required):
        columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
        if not required <= columns:
            raise ValueError(f"Unsupported Mixxx schema: {table} is missing {', '.join(sorted(required - columns))}.")
        return columns

    def _read(self, db):
        library = self._columns(db, "library", {"id", "location", "title", "artist", "album", "bpm"})
        self._columns(db, "track_locations", {"id", "location"})
        tracks = {}
        for row in db.execute("SELECT l.*, tl.location AS media_path FROM library l "
                              "JOIN track_locations tl ON tl.id=l.location"):
            if "mixxx_deleted" in library and row["mixxx_deleted"]:
                continue
            path = Path(row["media_path"]) if isinstance(row["media_path"], str) else None
            if path is None or not path.is_absolute():
                raise ValueError("Mixxx track has no absolute local media path.")
            metadata = {field: row[field] for field in ("title", "artist", "album", "bpm")
                        if row[field] not in (None, "", 0)}
            if "key" in library and row["key"]:
                metadata["key"] = row["key"]
            tracks[row["id"]] = {"path": path, "metadata": _check_metadata(metadata, self.warnings)}
        tables = {row[0].lower() for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, membership, folder, foreign, ordered in (
            ("Playlists", "PlaylistTracks", "Playlists", "playlist_id", True),
            ("crates", "crate_tracks", "Crates", "crate_id", False),
        ):
            if table.lower() not in tables and membership.lower() not in tables:
                continue
            columns = self._columns(db, table, {"id", "name"})
            self._columns(db, membership, {foreign, "track_id"} | ({"position", "id"} if ordered else set()))
            for container in db.execute(f'SELECT * FROM "{table}" ORDER BY id'):
                if ordered and "hidden" in columns and container["hidden"]:
                    continue
                query = f'SELECT track_id FROM "{membership}" WHERE "{foreign}"=?'
                if ordered:
                    query += " ORDER BY position,id"
                members = [row[0] for row in db.execute(query, (container["id"],))]
                if any(key not in tracks for key in members):
                    raise ValueError(f"Mixxx {folder} references a deleted or unknown track; refresh/repair it in Mixxx.")
                if not ordered:
                    members.sort(key=lambda key: (tracks[key]["metadata"].get("artist", "").casefold(),
                                                  tracks[key]["metadata"].get("title", "").casefold(), key))
                self._playlists.append({"path": [folder, container["name"]], "members": members, "tracks": tracks})

    def _write_schema(self, db):
        required = {
            "track_locations": {"id", "location", "filename", "directory", "filesize",
                                "fs_deleted", "needs_verification"},
            "library": {"id", "location", "title", "artist", "album", "bpm",
                        "mixxx_deleted", "played", "header_parsed", "filetype"},
            "Playlists": {"id", "name", "position", "hidden", "date_created", "date_modified"},
            "PlaylistTracks": {"id", "playlist_id", "track_id", "position"},
        }
        for table, columns in required.items():
            self._columns(db, table, columns)
            info = list(db.execute(f'PRAGMA table_info("{table}")'))
            if not any(row[1] == "id" and row[2].upper() == "INTEGER" and row[5] == 1 for row in info):
                raise ValueError(f"Unsupported Mixxx write schema: {table} needs an INTEGER primary key.")
            if any(row[3] and row[4] is None and row[1] not in columns and row[5] == 0 for row in info):
                raise ValueError(f"Unsupported Mixxx write schema: {table} has additional required columns.")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND lower(tbl_name)=lower(?)",
                          (table,)).fetchone():
                raise ValueError(f"Unsupported Mixxx write schema: unrecognized {table} triggers.")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Mixxx database failed integrity checks; import refused.")

    def validate_destination(self, music: Path):
        assert_mixxx_closed()
        if self.directory == music.resolve() or self.directory in music.resolve().parents:
            raise ValueError("Received music must be outside the Mixxx database directory.")
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
            self._write_schema(db)

    def import_crates(self, manifest, installed, backup_dir, transfer_id, guard=assert_mixxx_closed):
        validate_manifest(manifest)
        backup_dir = Path(backup_dir).expanduser().resolve()
        if backup_dir == self.directory or self.directory in backup_dir.parents:
            raise ValueError("Keep Mixxx backups outside the database directory.")
        if set(installed) != {item["id"] for item in manifest["files"]}:
            raise ValueError("Received Mixxx files do not match the manifest.")
        states = {}
        for item in manifest["files"]:
            path = installed[item["id"]].resolve(strict=True)
            if not path.is_file() or self.directory in path.parents or backup_dir in path.parents:
                raise ValueError("Received Mixxx media must be a local file outside the database and backup folders.")
            stat = path.stat()
            states[path] = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
            if stat.st_size != item["size"] or sha256_file(path) != item["sha256"]:
                raise ValueError("Received Mixxx media failed size/checksum verification.")
        guard()
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as source:
            self._write_schema(source)
            backup_dir.mkdir(parents=True, exist_ok=False)
            with closing(sqlite3.connect(backup_dir / self.path.name)) as target:
                source.backup(target)
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True)) as db:
            db.execute("ATTACH DATABASE ? AS backup", (str(backup_dir / self.path.name),))
            try:
                db.execute("BEGIN IMMEDIATE")
                self._write_schema(db)
                guard()
                schema = "SELECT type,name,tbl_name,sql FROM {}.sqlite_master ORDER BY type,name"
                if list(db.execute(schema.format("main"))) != list(db.execute(schema.format("backup"))):
                    raise ValueError("Mixxx library changed after backup; import refused.")
                for (table,) in db.execute("SELECT name FROM main.sqlite_master WHERE type='table'"):
                    quoted = table.replace('"', '""')
                    for first, second in (("main", "backup"), ("backup", "main")):
                        if db.execute(f'SELECT * FROM {first}."{quoted}" EXCEPT '
                                      f'SELECT * FROM {second}."{quoted}" LIMIT 1').fetchone():
                            raise ValueError("Mixxx library changed after backup; import refused.")
                tracks = {}
                for item in manifest["files"]:
                    path = installed[item["id"]].resolve(strict=True)
                    if not path.is_file():
                        raise ValueError("Received Mixxx media is not a regular file.")
                    existing = list(db.execute(
                        "SELECT l.id FROM library l JOIN track_locations t ON t.id=l.location "
                        "WHERE t.location=? AND coalesce(l.mixxx_deleted,0)=0", (str(path),)))
                    if len(existing) > 1:
                        raise ValueError("Mixxx has ambiguous registrations for the received media path.")
                    if existing:
                        tracks[item["id"]] = existing[0][0]
                        continue
                    location = db.execute("SELECT id FROM track_locations WHERE location=?", (str(path),)).fetchone()
                    if location:
                        location_id = location[0]
                        db.execute("UPDATE track_locations SET fs_deleted=0,needs_verification=0 WHERE id=?",
                                   (location_id,))
                    else:
                        location_id = db.execute(
                            "INSERT INTO track_locations(location,filename,directory,filesize,fs_deleted,needs_verification) "
                            "VALUES(?,?,?,?,0,0)", (str(path), path.name, str(path.parent), path.stat().st_size)).lastrowid
                    metadata = item.get("metadata", {})
                    tracks[item["id"]] = db.execute(
                        "INSERT INTO library(location,title,artist,album,bpm,filetype,mixxx_deleted,played,header_parsed) "
                        "VALUES(?,?,?,?,?,?,0,0,0)", (location_id, metadata.get("title", path.stem),
                                                     metadata.get("artist", ""), metadata.get("album", ""),
                                                     metadata.get("bpm"), path.suffix.lstrip(".").lower())).lastrowid
                names = {row[0].casefold() for row in db.execute("SELECT name FROM Playlists") if row[0]}
                position = db.execute("SELECT coalesce(max(position),0) FROM Playlists").fetchone()[0]
                now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                playlists, memberships = [], 0
                for crate in manifest["crates"]:
                    original = " / ".join(crate["path"])
                    name, number = original, 2
                    while name.casefold() in names:
                        name = f"{original} ({number})"
                        number += 1
                    names.add(name.casefold())
                    position += 1
                    playlist = db.execute(
                        "INSERT INTO Playlists(name,position,hidden,date_created,date_modified) VALUES(?,?,0,?,?)",
                        (name, position, now, now)).lastrowid
                    for index, key in enumerate(crate["tracks"], 1):
                        db.execute("INSERT INTO PlaylistTracks(playlist_id,track_id,position) VALUES(?,?,?)",
                                   (playlist, tracks[key], index))
                        memberships += 1
                    actual = [row[0] for row in db.execute(
                        "SELECT track_id FROM PlaylistTracks WHERE playlist_id=? ORDER BY position,id", (playlist,))]
                    if actual != [tracks[key] for key in crate["tracks"]]:
                        raise ValueError("Mixxx playlist membership readback failed; import rolled back.")
                    playlists.append(name)
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("Mixxx database failed post-import integrity checks.")
                guard()
                for path, expected in states.items():
                    stat = path.stat()
                    if expected != (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino):
                        raise ValueError("Received Mixxx media changed during import; transaction rolled back.")
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return {"crates": len(playlists), "files": len(tracks), "memberships": memberships,
                "backup": str(backup_dir), "warnings": [
                    "Mixxx native preview creates ordered playlists (nested names are flattened). Basic title/artist/"
                    "album/BPM are registered; key, cues, loops, grids and analysis are not written. Mixxx will "
                    "parse received audio headers and perform its own analysis. Native playback has not been validated."
                ]}
