from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crate_transfer.mixxx import MixxxLibrary, assert_mixxx_closed


def make_mixxx_library(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "mixxxdb.sqlite"
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            CREATE TABLE track_locations(id INTEGER PRIMARY KEY AUTOINCREMENT, location TEXT UNIQUE,
                filename TEXT, directory TEXT, filesize INTEGER, fs_deleted INTEGER, needs_verification INTEGER);
            CREATE TABLE library(id INTEGER PRIMARY KEY AUTOINCREMENT, location INTEGER,
                title TEXT, artist TEXT, album TEXT, bpm REAL, key TEXT,
                mixxx_deleted INTEGER, played INTEGER, header_parsed INTEGER DEFAULT 0, filetype TEXT DEFAULT '?');
            CREATE TABLE Playlists(id INTEGER PRIMARY KEY, name TEXT, position INTEGER,
                hidden INTEGER DEFAULT 0 NOT NULL, date_created DATETIME, date_modified DATETIME);
            CREATE TABLE PlaylistTracks(id INTEGER PRIMARY KEY, playlist_id INTEGER, track_id INTEGER, position INTEGER);
        """)
    return path


class MixxxTests(unittest.TestCase):
    def test_new_media_order_repeats_backup_and_additive_collision_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "source.m3u"
            media = directory / "received.mp3"
            media.write_bytes(b"synthetic audio")
            source.write_text(f"{media}\n{media}\n")
            from crate_transfer.exchange import ExchangeLibrary
            manifest, files = ExchangeLibrary("m3u", source).export(
                [1], "11111111-1111-4111-8111-111111111111", "Sender")
            path = make_mixxx_library(directory / "library")
            before = path.read_bytes()
            library = MixxxLibrary(path)
            library.validate_destination(directory / "incoming")
            result = library.import_crates(manifest, files, directory / "backup", "test")
            self.assertEqual((result["crates"], result["files"], result["memberships"]), (1, 1, 2))
            with closing(sqlite3.connect(directory / "backup" / path.name)) as db, db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM library").fetchone()[0], 0)
            self.assertNotEqual(path.read_bytes(), before)
            readback = MixxxLibrary(path)
            exported, sources = readback.export([1], manifest["sender_id"], "Forwarder")
            self.assertEqual(exported["crates"][0]["tracks"], manifest["crates"][0]["tracks"])
            self.assertEqual(list(sources.values()), [media.resolve()])
            library.import_crates(manifest, files, directory / "second-backup", "second")
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM library").fetchone()[0], 1)
                names = [row[0] for row in db.execute("SELECT name FROM Playlists ORDER BY id")]
                self.assertEqual(names, ["source", "source (2)"])

    def test_import_guard_failure_rolls_back_and_preserves_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = make_mixxx_library(directory / "library")
            calls = 0
            def guard():
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise ValueError("Mixxx opened during transfer")
            from support import valid_manifest
            manifest = valid_manifest([], [{"path": ["Empty"], "tracks": []}])
            with self.assertRaisesRegex(ValueError, "opened during"):
                MixxxLibrary(path).import_crates(manifest, {}, directory / "backup", "test", guard)
            for candidate in (path, directory / "backup" / path.name):
                with closing(sqlite3.connect(candidate)) as db, db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM Playlists").fetchone()[0], 0)

    def test_process_and_unknown_write_schema_fail_closed(self):
        process = unittest.mock.Mock(info={"name": "Mixxx.exe"})
        with patch("crate_transfer.mixxx.psutil.process_iter", return_value=[process]):
            with self.assertRaisesRegex(ValueError, "Quit Mixxx"):
                assert_mixxx_closed()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = make_mixxx_library(directory / "library")
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TRIGGER custom AFTER INSERT ON library BEGIN UPDATE library SET title='x'; END")
            with self.assertRaisesRegex(ValueError, "triggers"):
                MixxxLibrary(path).validate_destination(directory / "incoming")

    def test_native_playlists_crates_export_bytes_order_and_metadata_without_database_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            media = directory / "track.mp3"
            media.write_bytes(b"synthetic Mixxx audio")
            path = directory / "mixxxdb.sqlite"
            with closing(sqlite3.connect(path)) as db, db:
                db.executescript("""
                    CREATE TABLE library(id INTEGER, location INTEGER, title TEXT, artist TEXT, album TEXT, bpm REAL);
                    CREATE TABLE track_locations(id INTEGER, location TEXT);
                    CREATE TABLE Playlists(id INTEGER, name TEXT, hidden INTEGER);
                    CREATE TABLE PlaylistTracks(id INTEGER, playlist_id INTEGER, track_id INTEGER, position INTEGER);
                    CREATE TABLE crates(id INTEGER, name TEXT);
                    CREATE TABLE crate_tracks(crate_id INTEGER, track_id INTEGER);
                    INSERT INTO library VALUES(1,2,'Song','Artist','Album',125);
                    INSERT INTO Playlists VALUES(1,'Set',0);
                    INSERT INTO PlaylistTracks VALUES(1,1,1,1),(2,1,1,2);
                    INSERT INTO crates VALUES(2,'House');
                    INSERT INTO crate_tracks VALUES(2,1);
                """)
                db.execute("INSERT INTO track_locations VALUES(2,?)", (str(media),))
            before = path.read_bytes()
            reader = MixxxLibrary(directory)
            self.assertEqual([crate["path"] for crate in reader.crates()], [["Playlists", "Set"], ["Crates", "House"]])
            manifest, sources = reader.export([1], "11111111-1111-4111-8111-111111111111", "Sender")
            self.assertEqual(len(manifest["crates"][0]["tracks"]), 2)
            self.assertEqual(manifest["files"][0]["metadata"]["bpm"], 125)
            self.assertEqual(next(iter(sources.values())).read_bytes(), media.read_bytes())
            self.assertEqual(path.read_bytes(), before)
            self.assertTrue(manifest["warnings"])

    def test_missing_or_unknown_schema_fails_without_creating_a_library(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mixxxdb.sqlite"
            with self.assertRaisesRegex(ValueError, "No existing"):
                MixxxLibrary(path)
            self.assertFalse(path.exists())
            with closing(sqlite3.connect(path)) as db, db:
                pass
            with self.assertRaisesRegex(ValueError, "Unsupported Mixxx schema"):
                MixxxLibrary(path)
