from __future__ import annotations

import hashlib
import logging
import ntpath
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote, urlparse

import psutil

from .protocol import VIDEO

LOG = logging.getLogger(__name__)


class LibraryError(ValueError):
    pass


ROOT_COLUMNS = {
    "serato": {"revision"},
    "master": {"revision"},
    "space": {"id", "name", "revision"},
    "container": {"id", "revision", "parent_id", "name", "type", "list_order", "space_id",
                  "time_added", "expanded", "portable_id", "color"},
    "asset": {"id", "revision", "portable_id", "file_name", "file_size", "type", "format",
              "name", "time_added", "time_modified"},
    "space_asset": {"id", "asset_id", "space_id"},
    "container_asset": {"id", "revision", "container_id", "space_asset_id", "list_order",
                        "time_added"},
}


class SnapshotConnection(sqlite3.Connection):
    snapshot = None

    def close(self):
        try:
            super().close()
        finally:
            if self.snapshot is not None:
                self.snapshot.cleanup()
                self.snapshot = None


def _file_state(path: Path):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _snapshot_connection(path: Path) -> sqlite3.Connection:
    assert_serato_closed()
    paths = [path, Path(str(path) + "-wal"), Path(str(path) + "-journal")]
    before = [_file_state(item) for item in paths]
    temporary = tempfile.TemporaryDirectory(prefix="crate-transfer-library-")
    db = None
    try:
        copied = Path(temporary.name) / path.name
        # A private copy may create/recover its own journals. Never copy -shm:
        # it is transient coordination state, not committed database content.
        for item, state in zip(paths, before):
            if state is not None:
                shutil.copyfile(item, Path(temporary.name) / item.name)
        assert_serato_closed()
        if before != [_file_state(item) for item in paths]:
            raise LibraryError("Serato database changed while taking a read snapshot. Quit Serato and refresh again.")
        db = sqlite3.connect(copied.as_uri() + "?mode=rw", uri=True, timeout=5,
                             factory=SnapshotConnection)
        db.snapshot = temporary
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        db.execute("PRAGMA query_only=ON")
        LOG.info("Reading private Serato database snapshot after read-only open failed: %s", path)
        return db
    except BaseException:
        if db is not None:
            db.close()
        else:
            temporary.cleanup()
        raise


def connect(path: Path, writable: bool = False) -> sqlite3.Connection:
    if not path.is_file():
        raise LibraryError(
            f"Library not found: {path}.\n\nChoose the Serato folder or its Library subfolder "
            "in Settings. The Library folder must contain root.sqlite and master.sqlite. "
            "Open Serato DJ 4 once if the library has not been created yet."
        )
    db = None
    try:
        db = sqlite3.connect(path.resolve().as_uri() + ("?mode=rw" if writable else "?mode=ro"),
                             uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        # WAL sidecar access can fail on the first read, not at connect().
        db.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.Error as error:
        if db is not None:
            db.close()
        code = getattr(error, "sqlite_errorcode", None)
        cannot_open = (code is not None and code & 0xFF == sqlite3.SQLITE_CANTOPEN
                       or code is None and str(error) == "unable to open database file")
        if not writable and cannot_open:
            LOG.warning("Read-only Serato database open failed; attempting a closed-library snapshot: %s", path)
            try:
                return _snapshot_connection(path)
            except (sqlite3.Error, OSError, LibraryError) as snapshot_error:
                raise LibraryError(
                    f"Cannot read Serato database: {path}\n\nRead-only open: {error}\n"
                    f"Private snapshot: {snapshot_error}\n\nQuit Serato and refresh. "
                    "If this persists, check access to the complete Library folder and its journal files."
                ) from snapshot_error
        raise LibraryError(
            f"Cannot open Serato database: {path}\n\nSQLite: {error}\n\n"
            "Check that this is the Library folder on this computer and that your account "
            "can access the folder, database and any -wal/-shm files. If the library was copied, "
            "quit Serato before copying the complete Library folder, including its journal files. "
            "Do not delete journal files or change database permissions to bypass this error."
        ) from error
    return db


def columns(db: sqlite3.Connection, table: str) -> set[str]:
    # Table names come only from the fixed adapter schema.
    return {row["name"] for row in db.execute(f'PRAGMA table_info("{table}")')}


def aggregate_memberships(db: sqlite3.Connection) -> str:
    fields = columns(db, "container_asset")
    required = {"asset_id", "list_order"}
    if not required <= fields:
        raise LibraryError(
            "Unsupported Serato 4 aggregate schema: container_asset. "
            f"Missing columns: {', '.join(sorted(required - fields))}. "
            f"Found: {', '.join(sorted(fields))}."
        )
    order_id = "ca.id" if "id" in fields else "ca.asset_id"
    if "location_container_id" in fields:
        expected = {"id", "container_id"}
        location_fields = columns(db, "location_container")
        if not expected <= location_fields:
            raise LibraryError(
                "Unsupported Serato 4 aggregate schema: location_container. "
                f"Missing columns: {', '.join(sorted(expected - location_fields))}."
            )
        return (
            f"SELECT lc.container_id,ca.asset_id,ca.list_order,{order_id} AS membership_id "
            "FROM container_asset ca JOIN location_container lc ON lc.id=ca.location_container_id"
        )
    if "container_id" in fields:
        return (f"SELECT ca.container_id,ca.asset_id,ca.list_order,{order_id} AS membership_id "
                "FROM container_asset ca")
    raise LibraryError(
        "Unsupported Serato 4 aggregate schema: container_asset. Expected "
        "location_container_id or container_id; "
        f"found: {', '.join(sorted(fields))}."
    )


def check_root(db: sqlite3.Connection) -> tuple[int, int]:
    for table, required in ROOT_COLUMNS.items():
        missing = required - columns(db, table)
        if missing:
            raise LibraryError(f"Unsupported Serato 4 schema: {table} is missing {', '.join(sorted(missing))}. No changes made.")
    for table in ("serato", "master"):
        if db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] != 1:
            raise LibraryError(f"Unsupported Serato 4 schema: expected one {table} revision.")
    spaces = db.execute("SELECT id FROM space WHERE name='Serato Library'").fetchall()
    if len(spaces) != 1:
        raise LibraryError("Expected exactly one 'Serato Library' space.")
    space = spaces[0][0]
    roots = db.execute(
        "SELECT id FROM container WHERE space_id=? AND type=0 AND (parent_id IS NULL OR parent_id=0)",
        (space,),
    ).fetchall()
    if len(roots) != 1:
        raise LibraryError("Cannot identify the Serato Library root container.")
    return space, roots[0][0]


def assert_serato_closed() -> None:
    for process in psutil.process_iter(["name"]):
        try:
            name = (process.info["name"] or "").lower()
            if name.startswith(("serato dj pro", "serato dj lite")):
                raise LibraryError("Quit Serato DJ on the computer getting the crates before importing.")
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as error:
            raise LibraryError("Cannot check running applications; import refused for safety.") from error


def portable_path(path: str, windows: bool = False) -> str:
    if windows:
        value = path.replace("\\", "/").removeprefix("//?/")
        drive, tail = ntpath.splitdrive(value)
        if not drive or drive.startswith("//") or not tail.startswith("/"):
            raise LibraryError("Windows imports require a local drive, not a network share.")
        return tail.lstrip("/")
    return path.lstrip("/")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _volume_from_uri(uri: str) -> Path:
    value = unquote(urlparse(uri).path) if uri.startswith("file:") else uri
    value = value.replace("\\", "/")
    if sys.platform == "win32" and value.startswith("/") and len(value) > 2 and value[2] == ":":
        value = value[1:]
    lower = value.lower()
    suffix = "/_serato_/library/location.sqlite"
    if lower.endswith(suffix):
        return Path(value[:-len(suffix)] + "/")
    if lower.endswith("/library/root.sqlite"):
        return Path(Path(value).anchor)
    raise LibraryError(f"Unrecognized Serato location: {uri}. External/network location cannot be resolved safely.")


class Library:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory).expanduser().resolve()
        if self.directory.name.lower() in ("serato", "_serato_"):
            nested = self.directory / "Library"
            direct_database = any((self.directory / name).is_file()
                                  for name in ("master.sqlite", "root.sqlite"))
            nested_database = any((nested / name).is_file()
                                  for name in ("master.sqlite", "root.sqlite"))
            if direct_database and nested_database:
                raise LibraryError(
                    f"Multiple Serato library locations found under {self.directory}. "
                    "Choose the Library subfolder explicitly in Settings."
                )
            if nested.is_dir() and not direct_database:
                self.directory = nested

    def crates(self) -> list[dict]:
        with closing(connect(self.directory / "master.sqlite")) as db:
            return self._crates(db)

    def _crates(self, db: sqlite3.Connection) -> list[dict]:
        required = {
            "container": {"id", "parent_id", "name", "type", "list_order"},
            "asset": {"id", "location_id", "portable_id"},
        }
        for table, expected in required.items():
            if not expected <= columns(db, table):
                raise LibraryError(f"Unsupported Serato 4 aggregate schema: {table}.")
        memberships = aggregate_memberships(db)
        rows = db.execute(
            "SELECT c.id,c.parent_id,c.name,c.type,c.list_order,count(ca.asset_id) AS tracks "
            f"FROM container c LEFT JOIN ({memberships}) ca ON ca.container_id=c.id "
            "GROUP BY c.id ORDER BY c.list_order,c.id"
        ).fetchall()
        by_id = {row["id"]: dict(row) for row in rows}

        def names(row):
            result, seen = [], set()
            while row and row["type"] == 1:
                if row["id"] in seen:
                    raise LibraryError("Library contains a cyclic crate hierarchy.")
                seen.add(row["id"])
                result.insert(0, row["name"])
                row = by_id.get(row["parent_id"])
            return result

        return [{"id": row["id"], "path": names(row), "tracks": row["tracks"]}
                for row in rows if row["type"] == 1]

    def export(self, selected: list[int], machine_id: str, name: str, progress=lambda text: None) -> tuple[dict, dict[str, Path]]:
        files, paths, output, path_cache = {}, {}, [], {}
        with closing(connect(self.directory / "master.sqlite")) as db:
            db.execute("BEGIN")
            available = self._crates(db)
            memberships = aggregate_memberships(db)
            chosen = set(selected)
            if not chosen or not chosen <= {item["id"] for item in available}:
                raise LibraryError("Choose at least one valid crate. Refresh if the library changed.")
            prefixes = [item["path"] for item in available if item["id"] in chosen]
            # Selecting a parent includes its subcrates, without duplicates.
            crates = [item for item in available if any(item["path"][:len(p)] == p for p in prefixes)]
            locations = {}
            if {"location_id", "database_uri"} <= columns(db, "connection"):
                for row in db.execute("SELECT location_id,database_uri FROM connection"):
                    locations[row["location_id"]] = row["database_uri"]
            for crate in crates:
                members = []
                rows = db.execute(
                    f"SELECT a.id,a.location_id,a.portable_id FROM ({memberships}) ca "
                    "JOIN asset a ON a.id=ca.asset_id WHERE ca.container_id=? "
                    "ORDER BY ca.list_order,ca.membership_id",
                    (crate["id"],),
                )
                for row in rows:
                    if row["id"] not in path_cache:
                        raw = row["portable_id"].replace("\\", "/")
                        if raw.startswith(("http:", "https:", "soundcloud:", "tidal:", "beatport:")):
                            raise LibraryError("Streaming-service tracks cannot be transferred. Choose local-file crates.")
                        if row["location_id"] not in locations:
                            raise LibraryError(f"No volume mapping for track {raw}. No transfer started.")
                        volume = _volume_from_uri(locations[row["location_id"]])
                        path = (volume / raw.lstrip("/")).resolve()
                        if not path.is_relative_to(volume.resolve()) or not path.is_file():
                            raise LibraryError(f"Missing or invalid track: {path}. No transfer started.")
                        progress(f"Checking {path.name}")
                        digest = sha256_file(path)
                        key = hashlib.sha256((digest + "\0" + path.name).encode()).hexdigest()
                        files[key] = {"id": key, "sha256": digest, "name": path.name, "size": path.stat().st_size}
                        paths[key] = path
                        path_cache[row["id"]] = key
                    members.append(path_cache[row["id"]])
                output.append({"path": crate["path"], "tracks": members})
        return {"version": 1, "sender_id": machine_id, "sender_name": name,
                "crates": output, "files": list(files.values())}, paths

    def validate_destination(self, music: Path) -> None:
        music = music.resolve()
        if music.is_relative_to(self.directory) or self.directory.is_relative_to(music):
            raise LibraryError("Keep the received music folder separate from Serato's Library folder.")
        if music.exists() and not music.is_dir():
            raise LibraryError("The received music path must be a folder, not a file.")
        with closing(connect(self.directory / "root.sqlite")) as db:
            check_root(db)
        ancestor = music.resolve()
        while not ancestor.exists():
            ancestor = ancestor.parent
        if os.stat(ancestor).st_dev != os.stat(self.directory).st_dev:
            raise LibraryError("For this release, received music must be on the same local volume as Serato's Library.")
        if sys.platform == "win32" and (music.drive.startswith("\\\\") or music.drive.lower() != self.directory.drive.lower()):
            raise LibraryError("Choose a music folder on the same local Windows drive as Serato's Library.")

    def import_crates(self, manifest: dict, installed: dict[str, Path], backup_dir: Path,
                      transfer_id: str, guard=assert_serato_closed) -> dict:
        guard()
        root_path = self.directory / "root.sqlite"
        with closing(connect(root_path)) as source:
            check_root(source)
        backup_dir.mkdir(parents=True, exist_ok=False)
        for filename in ("root.sqlite", "master.sqlite"):
            with closing(connect(self.directory / filename)) as source, closing(sqlite3.connect(backup_dir / filename)) as target:
                source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise LibraryError("Database backup failed its integrity check. Import stopped.")

        with closing(connect(root_path, writable=True)) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                space, root = check_root(db)
                guard()
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
                    raise LibraryError("The existing Serato library failed integrity checks; import refused.")
                revision = db.execute("SELECT revision FROM serato").fetchone()[0] + 1
                db.execute("UPDATE serato SET revision=?", (revision,))
                db.execute("UPDATE master SET revision=?", (revision,))
                now = int(time.time())
                tables = ("asset", "space_asset", "container", "container_asset")
                sequence_exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone()
                sequence = dict(db.execute("SELECT name,seq FROM sqlite_sequence")) if sequence_exists else {}
                ids = {table: max(db.execute(f"SELECT coalesce(max(id),0) FROM {table}").fetchone()[0],
                                  sequence.get(table, 0)) for table in tables}

                def insert(table, values):
                    ids[table] += 1
                    values = {"id": ids[table], **values}
                    fields = ",".join(values)
                    db.execute(f"INSERT INTO {table} ({fields}) VALUES ({','.join('?' for _ in values)})",
                               tuple(values.values()))
                    return ids[table]

                def container(parent, name):
                    existing = {row[0].casefold() for row in db.execute(
                        "SELECT name FROM container WHERE parent_id=?", (parent,))}
                    original, number = name, 2
                    while name.casefold() in existing:
                        name = f"{original} ({number})"
                        number += 1
                    order = db.execute("SELECT coalesce(max(list_order),0)+1 FROM container WHERE parent_id=?",
                                       (parent,)).fetchone()[0]
                    return insert("container", {"revision": revision, "parent_id": parent, "name": name,
                                                "type": 1, "list_order": order, "space_id": space,
                                                "time_added": now, "expanded": 0, "portable_id": "", "color": None})

                containers, assets = {(): root}, {}
                for item in manifest["files"]:
                    path = installed[item["id"]].resolve()
                    pid = portable_path(str(path), sys.platform == "win32")
                    existing = db.execute("SELECT id FROM asset WHERE portable_id=? COLLATE NOCASE", (pid,)).fetchall()
                    if len(existing) > 1:
                        raise LibraryError(f"Ambiguous asset path in destination library: {path}")
                    asset_id = existing[0][0] if existing else insert("asset", {
                        "revision": revision, "portable_id": pid, "file_name": path.name,
                        "file_size": item["size"], "type": "video" if path.suffix.lower() in VIDEO else "audio",
                        "format": path.suffix[1:].lower(),
                        "name": path.stem, "time_added": now, "time_modified": now,
                    })
                    sa = db.execute("SELECT id FROM space_asset WHERE asset_id=? AND space_id=?",
                                    (asset_id, space)).fetchone()
                    assets[item["id"]] = sa[0] if sa else insert("space_asset", {"asset_id": asset_id, "space_id": space})
                memberships = 0
                for crate in manifest["crates"]:
                    key = ()
                    for name in crate["path"]:
                        child = key + (name,)
                        if child not in containers:
                            containers[child] = container(containers[key], name)
                        key = child
                    seen = set()
                    for index, file_id in enumerate(crate["tracks"]):
                        if file_id in seen:
                            continue
                        seen.add(file_id)
                        insert("container_asset", {"revision": revision, "container_id": containers[key],
                                                   "space_asset_id": assets[file_id], "list_order": index + 1,
                                                   "time_added": now})
                        memberships += 1
                db.execute("UPDATE space SET revision=? WHERE id=?", (revision, space))
                if db.execute("PRAGMA foreign_key_check").fetchone() or db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise LibraryError("Imported library failed integrity checks. Transaction rolled back.")
                guard()
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return {"crates": len(manifest["crates"]), "files": len(installed),
                "memberships": memberships, "backup": str(backup_dir)}
