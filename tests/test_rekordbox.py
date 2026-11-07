"""Synthetic native projections and mocked public SDK transaction boundary.

Public-fixture release plan: create a new empty library in licensed rekordbox 6
and 7 on each packaged macOS/Windows runtime, add rights-owned tiny WAV files,
normal/nested/empty/duplicate-name playlists and one smart playlist, quit the
app, and obtain consent to publish sanitized DB/XML/sidecars. Run imports into
copies, reopen the native app, verify exact file locations/playback/order/USNs
and analyze tracks. Never use an actual user's DJ library as a test fixture.
The mocked writer below is not evidence of real application acceptance.
"""
from __future__ import annotations

import hashlib
import shutil
import sqlite3
import unittest
import uuid
import xml.etree.ElementTree as ET
from contextlib import ExitStack
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from crate_transfer.library import LibraryError, NativeImportUncertain
from crate_transfer.protocol import PROTOCOL, validate_manifest
from crate_transfer.rekordbox import RekordboxLibrary, SCHEMA, _sdk_factory


def make_rekordbox_library(directory):
    """Return master.db for a synthetic six-table native read projection.

    It is deliberately not a writable complete SDK schema. Imports in these
    tests cross a mocked SDK boundary, never our own native SQL implementation.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    audio = directory.parent / (directory.name + "-source.mp3")
    audio.write_bytes(b"rights-owned synthetic audio bytes")
    path = directory / "master.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE djmdContent(ID TEXT PRIMARY KEY, FolderPath TEXT, Title TEXT,
                ArtistID TEXT, AlbumID TEXT, KeyID TEXT, BPM INTEGER);
            CREATE TABLE djmdArtist(ID TEXT PRIMARY KEY, Name TEXT);
            CREATE TABLE djmdAlbum(ID TEXT PRIMARY KEY, Name TEXT);
            CREATE TABLE djmdKey(ID TEXT PRIMARY KEY, ScaleName TEXT);
            CREATE TABLE djmdPlaylist(ID TEXT PRIMARY KEY, ParentID TEXT, Name TEXT,
                Attribute INTEGER, Seq INTEGER);
            CREATE TABLE djmdSongPlaylist(ID TEXT PRIMARY KEY, PlaylistID TEXT,
                ContentID TEXT, TrackNo INTEGER);
            INSERT INTO djmdArtist VALUES('2','Artist');
            INSERT INTO djmdAlbum VALUES('3','Album');
            INSERT INTO djmdKey VALUES('4','Am');
            INSERT INTO djmdPlaylist VALUES('10','root','Sets',1,1),
                ('11','10','House',0,1),('12','10','Empty',0,2),('13','root','Smart',4,2);
            INSERT INTO djmdSongPlaylist VALUES('a','11','1',1),('b','11','1',2);
        """)
        db.execute("INSERT INTO djmdContent VALUES('1',?,'Title','2','3','4',12550)", (str(audio.resolve()),))
    root = ET.Element("MASTER_PLAYLIST")
    ET.SubElement(root, "PRODUCT", Name="rekordbox", Version="6.8.5")
    playlists = ET.SubElement(root, "PLAYLISTS")
    for pid, parent, attribute in ((10, "0", 1), (11, "A", 0), (12, "A", 0), (13, "0", 4)):
        ET.SubElement(playlists, "NODE", Id=f"{pid:X}", ParentId=parent,
                      Attribute=str(attribute), Timestamp="1700000000000", Lib_Type="0", CheckType="0")
    ET.ElementTree(root).write(directory / "masterPlaylists6.xml", encoding="utf-8")
    return path


def make_complete_rekordbox_library(directory):
    """Generate every public SDK-mapped table for real SDK boundary tests.

    The SDK annotations infer NOT NULL for optional native fields; synthetic
    non-primary columns are nullable so absent analysis is not fabricated.
    This is a structural fixture, not a claim to reproduce rekordbox's DDL.
    """
    from sqlalchemy import MetaData, create_engine
    from pyrekordbox.db6.tables import Base, AgentRegistry, DjmdDevice, DjmdMenuItems
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "master.db"
    metadata = MetaData()
    for table in Base.metadata.tables.values():
        cloned = table.to_metadata(metadata)
        for column in cloned.columns:
            if not column.primary_key:
                column.nullable = True
    engine = create_engine("sqlite:///" + str(path))
    try:
        metadata.create_all(engine)
        with engine.begin() as db:
            db.execute(AgentRegistry.__table__.insert(), {"registry_id": "localUpdateCount", "int_1": 1})
            db.execute(DjmdDevice.__table__.insert(), {"ID": "1", "MasterDBID": "synthetic-master", "Name": "Synthetic"})
            db.execute(DjmdMenuItems.__table__.insert(), {"ID": "1", "Name": "TRACK", "rb_local_usn": 1})
    finally:
        engine.dispose()
    root = ET.Element("MASTER_PLAYLIST")
    ET.SubElement(root, "PRODUCT", Name="rekordbox", Version="7.0.0")
    ET.SubElement(root, "PLAYLISTS")
    ET.ElementTree(root).write(directory / "masterPlaylists6.xml", encoding="utf-8")
    return path


class Query(list):
    def first(self):
        return self[0] if self else None


class FakeSdk:
    """A public-method stand-in; commit intentionally simulates a multi-file commit."""
    def __init__(self, owner, **kwargs):
        self.owner = owner
        self.path = Path(kwargs["path"])
        owner.calls.append(kwargs)
        self.session = object()
        self.engine = SimpleNamespace(dispose=lambda: owner.events.append("dispose"))
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            self.tracks = [SimpleNamespace(**dict(row)) for row in db.execute("SELECT * FROM djmdContent")]
            self.playlists = [SimpleNamespace(**dict(row), Songs=[]) for row in db.execute("SELECT * FROM djmdPlaylist")]
            for row in db.execute("SELECT * FROM djmdSongPlaylist"):
                next(item for item in self.playlists if item.ID == row["PlaylistID"]).Songs.append(
                    SimpleNamespace(**dict(row)))
            self.tags = {table: [SimpleNamespace(**dict(row)) for row in db.execute(f"SELECT * FROM {table}")]
                         for table in ("djmdArtist", "djmdAlbum", "djmdKey")}
        for row in self.tracks:
            for attr, table, field, column in (
                ("ArtistName", "djmdArtist", "Name", "ArtistID"),
                ("AlbumName", "djmdAlbum", "Name", "AlbumID"),
                ("KeyName", "djmdKey", "ScaleName", "KeyID"),
            ):
                match = next((item for item in self.tags[table] if item.ID == getattr(row, column)), None)
                setattr(row, attr, getattr(match, field) if match else "")
        self.xml = ET.parse(self.path.parent / "masterPlaylists6.xml")
        self.playlist_xml = SimpleNamespace(get=self.xml_get)

    def xml_get(self, pid):
        row = self.xml.find(f".//NODE[@Id='{int(pid):X}']")
        return None if row is None else {**row.attrib, "Attribute": int(row.get("Attribute"))}

    def get_content(self, **kwargs):
        return next((item for item in self.tracks if item.ID == kwargs["ID"]), None) if kwargs else Query(self.tracks)

    def get_playlist(self, **kwargs):
        return next((item for item in self.playlists if item.ID == kwargs["ID"]), None) if kwargs else Query(self.playlists)

    def tag(self, table, **kwargs):
        return Query(item for item in self.tags[table] if all(getattr(item, key) == value for key, value in kwargs.items()))

    def get_artist(self, **kwargs):
        return self.tag("djmdArtist", **kwargs)

    def get_album(self, **kwargs):
        return self.tag("djmdAlbum", **kwargs)

    def get_key(self, **kwargs):
        return self.tag("djmdKey", **kwargs)

    def add_artist(self, name):
        row = SimpleNamespace(ID=str(uuid.uuid4()), Name=name)
        self.tags["djmdArtist"].append(row)
        return row

    def add_album(self, name, artist=None):
        row = SimpleNamespace(ID=str(uuid.uuid4()), Name=name)
        self.tags["djmdAlbum"].append(row)
        return row

    def add_content(self, path, **kwargs):
        self.owner.events.append(("add_content", str(path), kwargs))
        row = SimpleNamespace(ID=str(len(self.tracks) + 100), FolderPath=str(path), Title=None,
                              ArtistID=None, AlbumID=None, KeyID=None, BPM=None)
        for key, value in kwargs.items():
            setattr(row, key, value)
        self.tracks.append(row)
        return row

    def create(self, name, parent, attribute):
        row = SimpleNamespace(ID=str(len(self.playlists) + 100), Name=name, Attribute=attribute,
                              ParentID="root" if parent is None else parent.ID,
                              Seq=len(self.playlists) + 1, Songs=[])
        self.playlists.append(row)
        ET.SubElement(self.xml.find("PLAYLISTS"), "NODE", Id=f"{int(row.ID):X}",
                      ParentId="0" if parent is None else f"{int(parent.ID):X}", Attribute=str(attribute))
        return row

    def create_playlist(self, name, parent=None):
        return self.create(name, parent, 0)

    def create_playlist_folder(self, name, parent=None):
        return self.create(name, parent, 1)

    def add_to_playlist(self, playlist, content, track_no=None):
        playlist.Songs.append(SimpleNamespace(ID=str(uuid.uuid4()), PlaylistID=playlist.ID,
                                              ContentID=content, TrackNo=track_no))

    def flush(self):
        self.owner.events.append("flush")

    def commit(self):
        self.owner.events.append("commit")
        with sqlite3.connect(self.path) as db:
            for table, rows in (("djmdContent", self.tracks), ("djmdPlaylist", self.playlists),
                                *self.tags.items()):
                columns = [item[1] for item in db.execute(f"PRAGMA table_info({table})")]
                db.execute(f"DELETE FROM {table}")
                db.executemany(f"INSERT INTO {table} VALUES({','.join('?' for _ in columns)})",
                               [[getattr(row, column) for column in columns] for row in rows])
            db.execute("DELETE FROM djmdSongPlaylist")
            for row in self.playlists:
                db.executemany("INSERT INTO djmdSongPlaylist VALUES(?,?,?,?)",
                               [(song.ID, row.ID, song.ContentID, song.TrackNo) for song in row.Songs])
        if self.owner.failure == "xml":
            (self.path.parent / "masterPlaylists6.xml").write_bytes(b"partial XML")
            Path(str(self.path) + "-journal").write_bytes(b"partial sidecar")
            raise OSError("synthetic XML write failure after database commit")
        self.xml.write(self.path.parent / "masterPlaylists6.xml", encoding="utf-8")
        if self.owner.failure == "readback":
            with sqlite3.connect(self.path) as db:
                db.execute("UPDATE djmdContent SET FolderPath='broken' WHERE ID!='1'")
        if self.owner.failure == "missing-xml-node":
            self.xml.find("PLAYLISTS").clear()
            self.xml.write(self.path.parent / "masterPlaylists6.xml", encoding="utf-8")

    def close(self):
        self.owner.events.append("close")
        self.session = None

    def rollback(self):
        self.owner.events.append("rollback")


class SdkBoundary:
    def __init__(self, failure=None):
        self.failure, self.calls, self.events = failure, [], []

    def __call__(self, **kwargs):
        return FakeSdk(self, **kwargs)


class RekordboxTests(unittest.TestCase):
    def setUp(self):
        # Project-local scratch only; never use tempfile's system directories.
        self.root = Path(__file__).parent / (".rekordbox-test-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.path = make_rekordbox_library(self.root / "library")
        self.library = RekordboxLibrary(self.path)
        self.music = self.root / "received"
        self.music.mkdir()
        self.media = self.music / "new.mp3"
        self.media.write_bytes(b"synthetic receiver audio")
        digest = hashlib.sha256(self.media.read_bytes()).hexdigest()
        key = hashlib.sha256((digest + "\0" + self.media.name).encode()).hexdigest()
        self.key = key
        self.manifest = {
            "protocol": PROTOCOL, "destination": {"format": "rekordbox-native"},
            "sender_id": "11111111-1111-4111-8111-111111111111", "sender_name": "Sender",
            "files": [{"id": key, "name": self.media.name, "sha256": digest,
                       "size": self.media.stat().st_size,
                       "metadata": {"title": "New title", "artist": "New artist", "album": "New album",
                                    "bpm": 128.25, "key": "not-native",
                                    "cues": [{"kind": "cue", "position": 1.0}],
                                    "beatgrid": [{"position": 0, "bpm": 128.25, "beat": 1}]}}],
            "crates": [{"path": ["Sets", "House"], "tracks": [key, key]},
                       {"path": ["Sets", "New", "Empty"], "tracks": []}],
        }
        validate_manifest(self.manifest)
        self.backup = self.root / "backup"

    def boundary(self, sdk):
        stack = ExitStack()
        stack.enter_context(patch("crate_transfer.rekordbox.assert_rekordbox_closed"))
        stack.enter_context(patch("crate_transfer.rekordbox._sdk_factory", return_value=sdk))
        stack.enter_context(patch("crate_transfer.rekordbox._validate_sdk_schema"))
        return stack

    def snapshot(self):
        return {path.name: path.read_bytes() if path.exists() else None for path in self.library.backup_paths}

    def run_import(self, **kwargs):
        return self.library.import_crates(self.manifest, {self.key: self.media}, self.backup, "transfer", **kwargs)

    def test_native_read_export_hierarchy_empty_order_basic_tags_and_generic_destination(self):
        before = self.snapshot()
        with patch("crate_transfer.rekordbox.assert_rekordbox_closed"):
            crates = self.library.crates()
            self.assertEqual([row["path"] for row in crates], [["Sets", "House"], ["Sets", "Empty"]])
            manifest, sources = self.library.export([crates[0]["id"]], self.manifest["sender_id"], "Sender")
        self.assertEqual(manifest["destination"], {"format": "serato"})
        self.assertEqual(manifest["protocol"], PROTOCOL)
        self.assertEqual(len(manifest["crates"][0]["tracks"]), 2)
        self.assertEqual(manifest["crates"][0]["tracks"][0], manifest["crates"][0]["tracks"][1])
        self.assertEqual(manifest["files"][0]["metadata"],
                         {"title": "Title", "artist": "Artist", "album": "Album", "key": "Am", "bpm": 125.5})
        self.assertEqual(len(sources), 1)
        self.assertIn("Smart/unsupported rekordbox playlists were omitted.", manifest["warnings"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(RekordboxLibrary(self.path.parent).path, self.path)
        self.assertEqual(self.library.directory, self.path.parent)

    def test_public_sdk_import_registers_exact_paths_additive_hierarchy_and_readback(self):
        before = self.snapshot()
        sdk = SdkBoundary()
        with self.boundary(sdk):
            self.library.validate_destination(self.music)
            result = self.run_import()
        self.assertEqual((result["crates"], result["files"], result["memberships"]), (2, 1, 2))
        self.assertEqual(result["backup"], str(self.backup))
        self.assertTrue(any("Unmatched" in warning for warning in result["warnings"]))
        self.assertTrue(any("cues" in warning for warning in result["warnings"]))
        self.assertEqual(sum(item == "commit" for item in sdk.events), 1)
        added = next(item for item in sdk.events if isinstance(item, tuple))
        self.assertEqual(added[1], str(self.media))
        self.assertEqual(added[2]["BPM"], 12825)
        self.assertEqual(added[2]["Title"], "New title")
        self.assertTrue(all(not call["unlock"] and call["path"] == self.path
                            and call["db_dir"] == self.path.parent for call in sdk.calls))
        for name, data in before.items():
            if data is not None:
                self.assertEqual((self.backup / name).read_bytes(), data)
        with patch("crate_transfer.rekordbox.assert_rekordbox_closed"):
            exported = self.library.crates()
        self.assertIn(["Sets", "House"], [row["path"] for row in exported])
        self.assertIn(["Sets", "House (Imported 2)"], [row["path"] for row in exported])
        self.assertIn(["Sets", "New", "Empty"], [row["path"] for row in exported])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT Title FROM djmdContent WHERE ID='1'").fetchone()[0], "Title")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM djmdPlaylist WHERE Name='Sets'").fetchone()[0], 1)

    def test_existing_final_track_path_reuses_native_id_without_metadata_overwrite(self):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE djmdContent SET FolderPath=?", (str(self.media),))
        sdk = SdkBoundary()
        with self.boundary(sdk):
            result = self.run_import()
        self.assertFalse(any(isinstance(event, tuple) for event in sdk.events))
        self.assertTrue(any("reused" in warning for warning in result["warnings"]))
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT Title FROM djmdContent").fetchall(), [("Title",)])

    def test_partial_database_commit_xml_failure_readback_failure_restore_all_originals(self):
        for failure in ("xml", "readback", "missing-xml-node"):
            with self.subTest(failure=failure):
                before = self.snapshot()
                sdk = SdkBoundary(failure)
                with self.boundary(sdk), self.assertRaisesRegex(LibraryError, "originals restored"):
                    self.run_import()
                self.assertEqual(self.snapshot(), before)
                self.assertIn("commit", sdk.events)
                self.assertIn("dispose", sdk.events)
                shutil.rmtree(self.backup)

    def test_guard_cancellation_before_and_after_commit_restores_originals(self):
        for at in (2, 3):
            calls = []
            def guard():
                calls.append(1)
                if len(calls) == at:
                    raise RuntimeError("cancelled")
            before = self.snapshot()
            sdk = SdkBoundary()
            with self.boundary(sdk), self.assertRaisesRegex(LibraryError, "cancelled"):
                self.run_import(guard=guard)
            self.assertEqual(self.snapshot(), before)
            self.assertEqual("commit" in sdk.events, at == 3)
            shutil.rmtree(self.backup)

    def test_failed_partial_commit_restore_is_explicitly_uncertain(self):
        copy = shutil.copy2
        def fail_restore(source, destination, *args, **kwargs):
            if Path(source).parent == self.backup:
                raise OSError("synthetic restore failure")
            return copy(source, destination, *args, **kwargs)
        sdk = SdkBoundary("xml")
        with self.boundary(sdk), patch("crate_transfer.rekordbox.shutil.copy2", side_effect=fail_restore), \
                self.assertRaisesRegex(NativeImportUncertain, "automatic restore was unsafe/failed"):
            self.run_import()
        self.assertTrue((self.backup / "master.db").is_file())
        self.assertIn("commit", sdk.events)

    def test_tampering_unsupported_extensions_and_wrong_destination_never_start_sdk(self):
        sdk = SdkBoundary()
        self.media.write_bytes(b"tampered")
        with self.boundary(sdk), self.assertRaisesRegex(LibraryError, "checksum"):
            self.run_import()
        self.assertFalse(sdk.calls)
        self.assertFalse(self.backup.exists())
        self.manifest["destination"]["format"] = "serato"
        with self.boundary(sdk), self.assertRaisesRegex(LibraryError, "does not target"):
            self.run_import()

    def test_source_schema_dangling_memberships_cycles_and_ambiguous_order_rejected(self):
        statements = [
            "UPDATE djmdSongPlaylist SET ContentID='missing' WHERE ID='a'",
            "UPDATE djmdPlaylist SET ParentID='10' WHERE ID='10'",
            "UPDATE djmdSongPlaylist SET TrackNo=1 WHERE ID='b'",
            "ALTER TABLE djmdContent RENAME COLUMN FolderPath TO unknown",
        ]
        original = self.path.read_bytes()
        for statement in statements:
            self.path.write_bytes(original)
            with sqlite3.connect(self.path) as db:
                db.execute(statement)
            with patch("crate_transfer.rekordbox.assert_rekordbox_closed"), self.assertRaises(LibraryError):
                self.library.crates()

    def test_missing_sdk_and_encrypted_database_fail_actionably_without_creation(self):
        with patch("crate_transfer.rekordbox.version", side_effect=PackageNotFoundError), \
                self.assertRaisesRegex(LibraryError, "SQLCipher"):
            _sdk_factory()
        with patch("crate_transfer.rekordbox.version", return_value="0.5.0"), self.assertRaisesRegex(
                LibraryError, "pyrekordbox==0.4.4"):
            _sdk_factory()
        self.path.write_bytes(b"synthetic encrypted db bytes")
        with patch("crate_transfer.rekordbox.assert_rekordbox_closed"), patch(
                "crate_transfer.rekordbox._sdk_factory", side_effect=LibraryError("SQLCipher required")), \
                self.assertRaisesRegex(LibraryError, "SQLCipher required"):
            self.library.crates()
        missing = self.root / "missing" / "master.db"
        with self.assertRaisesRegex(LibraryError, "existing"):
            RekordboxLibrary(missing).crates()
        self.assertFalse(missing.exists())

    def test_running_app_and_inaccessible_process_list_refuse_native_access(self):
        process = SimpleNamespace(info={"name": "rekordbox.exe"})
        with patch("crate_transfer.rekordbox.psutil.process_iter", return_value=[process]), \
                self.assertRaisesRegex(LibraryError, "Quit rekordbox"):
            self.library.crates()
        import psutil
        with patch("crate_transfer.rekordbox.psutil.process_iter", side_effect=psutil.AccessDenied(1)), \
                self.assertRaisesRegex(LibraryError, "Cannot verify"):
            self.library.crates()

    def test_missing_master_playlist_xml_or_backup_inside_library_refuses_without_commit(self):
        sdk = SdkBoundary()
        self.backup = self.path.parent / "backup"
        with self.boundary(sdk), self.assertRaisesRegex(LibraryError, "outside"):
            self.run_import()
        (self.path.parent / "masterPlaylists6.xml").unlink()
        self.backup = self.root / "backup"
        with self.boundary(sdk), self.assertRaisesRegex(LibraryError, "masterPlaylists6.xml"):
            self.run_import()
        self.assertFalse(sdk.calls)

    def test_all_database_sidecars_are_backed_up_and_restored_if_sdk_open_fails(self):
        for suffix in ("-wal", "-shm", "-journal"):
            Path(str(self.path) + suffix).write_bytes(("synthetic " + suffix).encode())
        before = self.snapshot()
        def fail_open(factory):
            for path in self.library.backup_paths:
                path.write_bytes(b"synthetic partial mutation")
            raise LibraryError("synthetic SDK open failure")
        with self.boundary(SdkBoundary()), patch.object(self.library, "_open_sdk", side_effect=fail_open), \
                self.assertRaisesRegex(LibraryError, "originals restored"):
            self.run_import()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.library.backup_paths), 5)
        for name, data in before.items():
            self.assertEqual((self.backup / name).read_bytes(), data)

    def test_plain_read_never_requires_sdk_but_native_destination_does(self):
        with patch("crate_transfer.rekordbox.assert_rekordbox_closed"), patch(
                "crate_transfer.rekordbox._sdk_factory", side_effect=LibraryError("SQLCipher required")) as factory:
            self.assertEqual(len(self.library.crates()), 2)
            factory.assert_not_called()
            with self.assertRaisesRegex(LibraryError, "SQLCipher required"):
                self.library.validate_destination(self.music)
            factory.assert_called_once_with()

    def test_unsupported_native_registration_extension_refuses_before_backup_or_sdk_open(self):
        media = self.music / "unsupported.ogg"
        media.write_bytes(b"synthetic media")
        digest = hashlib.sha256(media.read_bytes()).hexdigest()
        key = hashlib.sha256((digest + "\0" + media.name).encode()).hexdigest()
        self.manifest["files"] = [{"id": key, "sha256": digest, "name": media.name,
                                  "size": media.stat().st_size}]
        self.manifest["crates"] = [{"path": ["Set"], "tracks": [key]}]
        sdk = SdkBoundary()
        with self.boundary(sdk), self.assertRaisesRegex(LibraryError, "cannot register .ogg"):
            self.library.import_crates(self.manifest, {key: media}, self.backup, "transfer")
        self.assertFalse(sdk.calls)
        self.assertFalse(self.backup.exists())

    def test_complete_public_sdk_schema_validation_when_optional_runtime_is_available(self):
        try:
            from sqlalchemy import create_engine
            from pyrekordbox.db6.tables import Base
        except ImportError:
            self.skipTest("Optional public pyrekordbox/SQLAlchemy runtime is not installed.")
        from crate_transfer.rekordbox import _validate_sdk_schema
        engine = create_engine("sqlite:///" + str(self.root / "complete-native.db"))
        try:
            Base.metadata.create_all(engine)
            _validate_sdk_schema(SimpleNamespace(engine=engine))
            with engine.begin() as connection:
                connection.exec_driver_sql("DROP TABLE djmdContent")
            with self.assertRaisesRegex(LibraryError, "djmdContent"):
                _validate_sdk_schema(SimpleNamespace(engine=engine))
        finally:
            engine.dispose()

    def test_real_public_sdk_registers_tracks_playlists_usns_and_xml_in_complete_synthetic_schema(self):
        try:
            factory = _sdk_factory()
        except LibraryError:
            self.skipTest("Pinned public SDK/SQLCipher runtime is not installed.")
        path = make_complete_rekordbox_library(self.root / "complete")
        library = RekordboxLibrary(path)
        with ExitStack() as stack:
            stack.enter_context(patch("crate_transfer.rekordbox.assert_rekordbox_closed"))
            stack.enter_context(patch("pyrekordbox.db6.database.get_config", return_value={}))
            stack.enter_context(patch("pyrekordbox.db6.database.get_rekordbox_pid", return_value=None))
            library.validate_destination(self.music)
            result = library.import_crates(self.manifest, {self.key: self.media}, self.backup, "transfer")
        self.assertEqual((result["files"], result["crates"], result["memberships"]), (1, 2, 2))
        with patch("pyrekordbox.db6.database.get_config", return_value={}), patch(
                "pyrekordbox.db6.database.get_rekordbox_pid", return_value=None):
            db = factory(path=path, db_dir=path.parent, unlock=False)
        try:
            row = db.get_content().one()
            self.assertEqual(row.FolderPath, str(self.media))
            self.assertEqual(row.Title, "New title")
            self.assertEqual(row.ArtistName, "New artist")
            self.assertEqual(row.AlbumName, "New album")
            self.assertEqual(row.BPM, 12825)
            self.assertEqual(row.FileSize, self.media.stat().st_size)
            self.assertEqual(row.FileType, 1)
            self.assertGreater(row.rb_local_usn, 1)
            self.assertGreater(db.registry.get_local_update_count(), row.rb_local_usn)
            playlist = db.get_playlist(Name="House").one()
            self.assertEqual([song.ContentID for song in sorted(playlist.Songs, key=lambda song: song.TrackNo)],
                             [row.ID, row.ID])
            self.assertEqual(db.playlist_xml.get(playlist.ID)["Attribute"], 0)
            self.assertEqual(db.get_playlist(Name="Empty").one().Songs, [])
        finally:
            db.close()
            db.engine.dispose()

    def test_real_sdk_xml_save_failure_after_sql_commit_restores_complete_database(self):
        try:
            _sdk_factory()
            from pyrekordbox.db6.aux_files import MasterPlaylistXml
        except (LibraryError, ImportError):
            self.skipTest("Pinned public SDK/SQLCipher runtime is not installed.")
        path = make_complete_rekordbox_library(self.root / "complete")
        library = RekordboxLibrary(path)
        before = {item.name: item.read_bytes() if item.exists() else None for item in library.backup_paths}
        with patch("crate_transfer.rekordbox.assert_rekordbox_closed"), patch.object(
                MasterPlaylistXml, "save", side_effect=OSError("synthetic XML save failure")), \
                patch("pyrekordbox.db6.database.get_config", return_value={}), \
                patch("pyrekordbox.db6.database.get_rekordbox_pid", return_value=None), \
                self.assertRaisesRegex(LibraryError, "originals restored"):
            library.import_crates(self.manifest, {self.key: self.media}, self.backup, "transfer")
        self.assertEqual({item.name: item.read_bytes() if item.exists() else None for item in library.backup_paths},
                         before)
