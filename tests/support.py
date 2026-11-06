"""Synthetic Serato-shaped SQLite and media fixtures; never accesses user data."""

import hashlib
import sqlite3
from pathlib import Path

def create_root_database(path, keep_unrelated=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(
        """
        CREATE TABLE serato(revision INTEGER NOT NULL);
        CREATE TABLE master(revision INTEGER NOT NULL);
        CREATE TABLE space(id INTEGER PRIMARY KEY, name TEXT NOT NULL, revision INTEGER NOT NULL);
        CREATE TABLE container(
            id INTEGER PRIMARY KEY AUTOINCREMENT, revision INTEGER NOT NULL, parent_id INTEGER REFERENCES container(id),
            name TEXT NOT NULL, type INTEGER NOT NULL, list_order INTEGER NOT NULL,
            space_id INTEGER NOT NULL REFERENCES space(id), time_added INTEGER NOT NULL,
            expanded INTEGER NOT NULL, portable_id TEXT NOT NULL, color TEXT,
            UNIQUE(parent_id, name COLLATE NOCASE, type)
        );
        CREATE TABLE asset(
            id INTEGER PRIMARY KEY AUTOINCREMENT, revision INTEGER NOT NULL, portable_id TEXT NOT NULL,
            file_name TEXT NOT NULL, file_size INTEGER NOT NULL, type TEXT NOT NULL,
            format TEXT NOT NULL, name TEXT NOT NULL, time_added INTEGER NOT NULL,
            time_modified INTEGER NOT NULL
        );
        CREATE TABLE space_asset(
            id INTEGER PRIMARY KEY AUTOINCREMENT, asset_id INTEGER NOT NULL REFERENCES asset(id),
            space_id INTEGER NOT NULL REFERENCES space(id)
        );
        CREATE TABLE container_asset(
            id INTEGER PRIMARY KEY AUTOINCREMENT, revision INTEGER NOT NULL,
            container_id INTEGER NOT NULL REFERENCES container(id),
            space_asset_id INTEGER NOT NULL REFERENCES space_asset(id),
            list_order INTEGER NOT NULL, time_added INTEGER NOT NULL
        );
        INSERT INTO serato VALUES (1);
        INSERT INTO master VALUES (1);
        INSERT INTO space VALUES (1, 'Serato Library', 1);
        INSERT INTO container VALUES (1, 1, NULL, 'Serato Library', 0, 0, 1, 1, 0, '', NULL);
        """
    )
    if keep_unrelated:
        db.executescript(
            """
            INSERT INTO container VALUES (2, 1, 1, 'Keep Existing', 1, 1, 1, 2, 0, '', NULL);
            INSERT INTO asset VALUES (1, 1, 'Existing/keep.mp3', 'keep.mp3', 4, 'audio',
                                      'mp3', 'keep', 1, 1);
            INSERT INTO space_asset VALUES (1, 1, 1);
            INSERT INTO container_asset VALUES (1, 1, 2, 1, 1, 1);
            """
        )
    for table in ("container", "space_asset"):
        for operation, event in (("INSERT", "NEW"), ("DELETE", "OLD")):
            db.execute(
                f"CREATE TRIGGER revision_{table}_{operation.lower()} AFTER {operation} ON {table} "
                f"BEGIN UPDATE space SET revision=(SELECT revision FROM serato) "
                f"WHERE id={event}.space_id AND revision < (SELECT revision FROM serato); END"
            )
    db.commit()
    db.close()


def create_aggregate_database(path, volume, crates, assets, location_id=1, linked=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE container(
            id INTEGER PRIMARY KEY, parent_id INTEGER, name TEXT NOT NULL,
            type INTEGER NOT NULL, list_order INTEGER NOT NULL
        );
        CREATE TABLE container_asset(
            id INTEGER PRIMARY KEY, location_container_id INTEGER NOT NULL,
            asset_id INTEGER NOT NULL, list_order INTEGER NOT NULL
        );
        CREATE TABLE location_container(
            id INTEGER PRIMARY KEY, container_id INTEGER NOT NULL, location_id INTEGER NOT NULL
        );
        CREATE TABLE asset(
            id INTEGER PRIMARY KEY, location_id INTEGER NOT NULL, portable_id TEXT NOT NULL
        );
        CREATE TABLE connection(location_id INTEGER PRIMARY KEY, database_uri TEXT NOT NULL);
        """
    )
    if not linked:
        db.execute("ALTER TABLE container_asset RENAME COLUMN location_container_id TO container_id")
    db.execute("INSERT INTO container VALUES (1, NULL, 'Serato Library', 0, 0)")
    db.execute(
        "INSERT INTO connection(location_id, database_uri) VALUES (?, ?)",
        (location_id, (Path(volume) / "_Serato_" / "Library" / "location.sqlite").as_uri()),
    )
    for asset_id, relative_path in assets.items():
        db.execute(
            "INSERT INTO asset(id, location_id, portable_id) VALUES (?, ?, ?)",
            (asset_id, location_id, relative_path),
        )
    membership_id = 1
    for crate in crates:
        db.execute(
            "INSERT INTO container VALUES (?, ?, ?, 1, ?)",
            (crate["id"], crate.get("parent_id", 1), crate["name"], crate.get("order", crate["id"])),
        )
        membership_container = crate["id"]
        if linked:
            membership_container = crate["id"] + 1000
            db.execute("INSERT INTO location_container VALUES (?, ?, ?)",
                       (membership_container, crate["id"], location_id))
        for index, asset_id in enumerate(crate.get("tracks", []), 1):
            db.execute(
                "INSERT INTO container_asset VALUES (?, ?, ?, ?)",
                (membership_id, membership_container, asset_id, index if crate.get("membership_order") is None
                 else crate["membership_order"][index - 1]),
            )
            membership_id += 1
    db.commit()
    db.close()


def make_library(directory, files, crates, assets):
    """Create root/master DBs plus synthetic files in an isolated source volume.

    `files` maps portable relative paths to exact byte strings. `assets` maps
    aggregate asset IDs to those portable paths.
    """
    directory = Path(directory)
    volume = directory / "source-volume"
    for relative, content in files.items():
        target = volume / relative.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    create_root_database(directory / "root.sqlite", keep_unrelated=False)
    create_aggregate_database(directory / "master.sqlite", volume, crates, assets)
    return volume


def file_record(name, content):
    digest = hashlib.sha256(content).hexdigest()
    key = hashlib.sha256((digest + "\0" + name).encode()).hexdigest()
    return {"id": key, "sha256": digest, "name": name, "size": len(content)}


def valid_manifest(items=None, crates=None, sender_id="11111111-1111-4111-8111-111111111111"):
    items = list(items or [])
    if crates is None:
        crates = [{"path": ["Set"], "tracks": [item["id"] for item in items]}]
    return {
        "version": 1,
        "sender_id": sender_id,
        "sender_name": "Synthetic Sender",
        "files": items,
        "crates": crates,
    }


def root_snapshot(path):
    db = sqlite3.connect(path)
    result = {
        "revision": (db.execute("SELECT revision FROM serato").fetchone()[0],
                     db.execute("SELECT revision FROM master").fetchone()[0]),
        "containers": db.execute(
            "SELECT id,parent_id,name,type,list_order,space_id FROM container ORDER BY id"
        ).fetchall(),
        "assets": db.execute(
            "SELECT id,portable_id,file_name,file_size FROM asset ORDER BY id"
        ).fetchall(),
        "memberships": db.execute(
            "SELECT id,container_id,space_asset_id,list_order FROM container_asset ORDER BY id"
        ).fetchall(),
    }
    db.close()
    return result
