"""Synthetic Engine-created schema fixtures; no real DJ library is included."""
from contextlib import closing
import hashlib
import sqlite3
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import psutil

from crate_transfer.engine import (
    EngineLibrary, LibraryError, TABLE_COLUMNS, TRACK_COLUMNS,
    assert_engine_closed, make_engine_library,
)
from crate_transfer.protocol import PROTOCOL


UUID = "11111111-1111-4111-8111-111111111111"


def make_engine_database(path, patch_version=1, bpm_type="REAL"):
    """Create a synthetic Database2/m.db using independently described schema facts."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = set("""path filename title artist album genre comment label composer remixer
albumArt fileType streamingSource uri originDatabaseUuid uuid databaseUuid""".split())
    boolean = set("""isPlayed isAnalyzed isAvailable isMetadataOfPackedTrackChanged
isPerfomanceDataOfPackedTrackChanged isMetadataImported isBeatGridLocked
explicitLyrics isPersisted isExplicitlyExported""".split())
    datetime = set("timeLastPlayed dateCreated dateAdded lastEditTime".split())
    blobs = set("trackData overviewWaveFormData beatData quickCues loops".split())
    definitions = dict(TABLE_COLUMNS)
    definitions["Track"] = TRACK_COLUMNS + (["albumArtSourceHash"] if patch_version == 2 else [])
    constraints = {
        "Track": ["UNIQUE(path)", "UNIQUE(originDatabaseUuid,originTrackId)",
                  "FOREIGN KEY(albumArtId) REFERENCES AlbumArt(id) ON DELETE RESTRICT"],
        "Playlist": ["UNIQUE(title,parentListId)", "UNIQUE(parentListId,nextListId)"],
        "PlaylistEntity": ["UNIQUE(listId,databaseUuid,trackId)",
                           "FOREIGN KEY(listId) REFERENCES Playlist(id) ON DELETE CASCADE"],
        "PerformanceData": ["FOREIGN KEY(trackId) REFERENCES Track(id) ON DELETE CASCADE ON UPDATE CASCADE"],
    }
    with closing(sqlite3.connect(path)) as db, db:
        for table, columns in definitions.items():
            fields = []
            for index, column in enumerate(columns):
                kind = ("TEXT" if column in text else "BOOLEAN" if column in boolean else
                        "DATETIME" if column in datetime else "BLOB" if column in blobs else
                        bpm_type if column == "bpmAnalyzed" else
                        "CHAR(40)" if column == "albumArtSourceHash" else "INTEGER")
                field = f'"{column}" {kind}'
                if index == 0:
                    field += " PRIMARY KEY"
                    if table != "PerformanceData":
                        field += " AUTOINCREMENT"
                fields.append(field)
            db.execute(f'CREATE TABLE "{table}" ({",".join(fields + constraints.get(table, []))})')
        db.execute("CREATE TABLE AlbumArt(id INTEGER PRIMARY KEY AUTOINCREMENT, hash TEXT, albumArt BLOB)")
        db.execute("CREATE TABLE Unrelated(id INTEGER PRIMARY KEY, payload BLOB)")
        db.execute("INSERT INTO Unrelated VALUES(5,?)", (b"preserve this opaque data",))
        db.execute("INSERT INTO Information(uuid,schemaVersionMajor,schemaVersionMinor,schemaVersionPatch)"
                   " VALUES(?,3,0,?)", (UUID, patch_version))
        db.executescript("""
            CREATE TRIGGER trigger_after_insert_Track_fix_origin AFTER INSERT ON Track
            WHEN NEW.originDatabaseUuid IS NULL OR NEW.originTrackId IS NULL BEGIN
              UPDATE Track SET originDatabaseUuid=(SELECT uuid FROM Information),
                               originTrackId=NEW.id WHERE id=NEW.id;
            END;
            CREATE TRIGGER trigger_after_insert_Track_insert_performance_data AFTER INSERT ON Track BEGIN
              INSERT INTO PerformanceData(trackId) VALUES(NEW.id);
            END;
            CREATE TRIGGER trigger_before_insert_List BEFORE INSERT ON Playlist BEGIN
              UPDATE Playlist SET nextListId=-nextListId-1
              WHERE parentListId=NEW.parentListId AND nextListId=NEW.nextListId;
            END;
            CREATE TRIGGER trigger_after_insert_List AFTER INSERT ON Playlist BEGIN
              UPDATE Playlist SET nextListId=NEW.id
              WHERE parentListId=NEW.parentListId AND nextListId=-NEW.nextListId-1;
            END;
        """)
    return path


def transfer(media, crates=None, metadata=None):
    records, installed = [], {}
    for path in media:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        key = hashlib.sha256((digest + "\0" + path.name).encode()).hexdigest()
        records.append({"id": key, "sha256": digest, "name": path.name, "size": path.stat().st_size,
                        "metadata": metadata or {"title": "Song", "artist": "Artist", "album": "Album", "bpm": 123.5}})
        installed[key] = path
    keys = [row["id"] for row in records]
    return {"protocol": PROTOCOL, "sender_id": UUID, "sender_name": "Sender",
            "destination": {"format": "serato"}, "files": records,
            "crates": crates or [{"path": ["Received", "Set"], "tracks": keys}]}, installed


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(".engine-test-work") / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.process_patch = patch("crate_transfer.engine.psutil.process_iter", return_value=[])
        self.process_patch.start()
        self.addCleanup(self.process_patch.stop)
        self.addCleanup(self.cleanup)
        self.database = make_engine_database(self.root / "Engine Library" / "Database2" / "m.db")
        self.library = EngineLibrary(self.database)

    def cleanup(self):
        import shutil
        shutil.rmtree(self.root)
        try:
            self.root.parent.rmdir()
        except OSError:
            pass

    def audio(self, name="received.mp3", data=b"synthetic received music"):
        path = self.root / "Music" / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        return path.resolve()

    def seed(self):
        media = self.audio("old.mp3", b"existing audio")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO Track(path,filename,title,bpmAnalyzed,fileBytes,fileType,isAnalyzed,isAvailable)"
                       " VALUES('../Music/old.mp3','old.mp3','Old',120,?,'mp3',1,1)", (media.stat().st_size,))
            db.execute("UPDATE PerformanceData SET quickCues=?,loops=?,beatData=? WHERE trackId=1",
                       (b"opaque cues", b"opaque loops", b"opaque grid"))
            db.execute("INSERT INTO Playlist(title,parentListId,isPersisted,nextListId) VALUES('Old',0,1,0)")
            db.execute("INSERT INTO PlaylistEntity(listId,trackId,databaseUuid,nextEntityId,membershipReference)"
                       " VALUES(1,1,?,0,0)", (UUID,))
        return media

    def import_media(self, manifest, installed, name="backup", guard=lambda: None):
        return self.library.import_crates(manifest, installed, self.root / name, "transfer", guard)

    def test_constructor_resolves_native_library_and_never_creates_missing_database(self):
        for path in (self.database, self.database.parent, self.database.parent.parent, self.root):
            library = make_engine_library(path)
            self.assertEqual(library.path, self.database.resolve())
            self.assertEqual(library.directory, self.database.parent.parent.resolve())
            self.assertEqual(library.backup_paths, [self.database.resolve()])
        missing = self.root / "Missing" / "Database2" / "m.db"
        with self.assertRaisesRegex(LibraryError, "existing Engine-created"):
            EngineLibrary(missing)
        self.assertFalse(missing.exists())

    def test_both_supported_schemas_register_local_music_and_ordered_nested_playlists(self):
        self.seed()
        first, second = self.audio("one.mp3"), self.audio("two.mp3", b"second music")
        manifest, installed = transfer([first, second])
        keys = [record["id"] for record in manifest["files"]]
        manifest["crates"].append({"path": ["Received", "Empty"], "tracks": []})
        manifest["crates"][0]["tracks"] = [keys[1], keys[0], keys[1]]
        with closing(sqlite3.connect(self.database)) as db, db:
            before_track = db.execute("SELECT * FROM Track WHERE id=1").fetchone()
            before_perf = db.execute("SELECT * FROM PerformanceData WHERE trackId=1").fetchone()
        receipt = self.import_media(manifest, installed)
        self.assertEqual((receipt["crates"], receipt["files"], receipt["memberships"]), (2, 2, 2))
        self.assertTrue(any("duplicate" in warning for warning in receipt["warnings"]))
        self.assertTrue(any("not written" in warning for warning in receipt["warnings"]))
        with closing(sqlite3.connect(self.database)) as db, db:
            self.assertEqual(db.execute("SELECT * FROM Track WHERE id=1").fetchone(), before_track)
            self.assertEqual(db.execute("SELECT * FROM PerformanceData WHERE trackId=1").fetchone(), before_perf)
            self.assertEqual(db.execute("SELECT nextListId FROM Playlist WHERE id=1").fetchone(), (2,))
            self.assertEqual(db.execute("SELECT isAnalyzed,bpmAnalyzed,key FROM Track WHERE id=2").fetchone(), (0, 123.5, None))
            self.assertEqual(db.execute("SELECT quickCues,loops,beatData FROM PerformanceData WHERE trackId=2").fetchone(),
                             (None, None, None))
            self.assertEqual(db.execute("SELECT payload FROM Unrelated").fetchone(), (b"preserve this opaque data",))
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        with closing(sqlite3.connect(self.root / "backup" / "m.db")) as backup, backup:
            self.assertEqual(backup.execute("SELECT count(*) FROM Track").fetchone(), (1,))
            self.assertEqual(backup.execute("SELECT nextListId FROM Playlist").fetchone(), (0,))
        catalog = self.library.crates()
        self.assertEqual([row["path"] for row in catalog], [["Old"], ["Received"], ["Received", "Set"], ["Received", "Empty"]])
        exported, sources = self.library.export([catalog[2]["id"]], UUID, "Sender")
        self.assertEqual([sources[key] for key in exported["crates"][0]["tracks"]], [second, first])
        self.assertEqual(exported["destination"], {"format": "serato"})
        self.assertEqual(exported["files"][0]["metadata"]["bpm"], 123.5)
        device = make_engine_database(self.root / "device" / "Engine Library" / "Database2" / "m.db", 2)
        receipt = EngineLibrary(device).import_crates(manifest, installed, self.root / "device-backup", "device")
        self.assertEqual(receipt["memberships"], 2)

    def test_source_export_is_readonly_and_unknown_performance_is_warned_not_decoded(self):
        media = self.seed()
        before = self.database.read_bytes()
        exported, sources = self.library.export([1], UUID, "Sender")
        self.assertEqual(exported["files"][0]["metadata"], {"title": "Old", "bpm": 120.0})
        self.assertEqual(list(sources.values()), [media])
        self.assertTrue(any("blobs" in warning for warning in exported["warnings"]))
        self.assertEqual(self.database.read_bytes(), before)

    def test_existing_track_reuse_is_additive_and_metadata_untouched(self):
        media = self.seed()
        manifest, installed = transfer([media], metadata={"title": "Do not overwrite", "key": "8A",
                                                        "cues": [{"kind": "cue", "position": 1}]})
        receipt = self.import_media(manifest, installed)
        self.assertTrue(any("reused" in warning for warning in receipt["warnings"]))
        with closing(sqlite3.connect(self.database)) as db, db:
            self.assertEqual(db.execute("SELECT title FROM Track").fetchall(), [("Old",)])
            self.assertEqual(db.execute("SELECT count(*) FROM PlaylistEntity").fetchone(), (2,))

    def test_existing_playlists_are_not_merged_or_overwritten(self):
        self.seed()
        manifest, installed = transfer([self.audio()])
        manifest["crates"][0]["path"] = ["old", "New"]
        before = self.database.read_bytes()
        with self.assertRaisesRegex(LibraryError, "already exists"):
            self.import_media(manifest, installed)
        self.assertEqual(self.database.read_bytes(), before)
        self.assertFalse((self.root / "backup").exists())

    def test_unknown_version_columns_and_registration_triggers_fail_closed(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE Information SET schemaVersionPatch=99")
        with self.assertRaisesRegex(LibraryError, "Unsupported Engine schema"):
            self.library.crates()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE Information SET schemaVersionPatch=1")
            db.execute("DROP TRIGGER trigger_after_insert_Track_insert_performance_data")
        with self.assertRaisesRegex(LibraryError, "triggers"):
            self.library.validate_destination(self.root / "Music")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("ALTER TABLE Track ADD COLUMN unknown INTEGER")
        with self.assertRaisesRegex(LibraryError, "Track schema"):
            self.library.crates()

    def test_broken_or_external_playlist_chains_are_refused(self):
        self.seed()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE PlaylistEntity SET nextEntityId=id")
        with self.assertRaisesRegex(LibraryError, "ordering"):
            self.library.crates()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE PlaylistEntity SET nextEntityId=0,databaseUuid=?", (str(uuid.uuid4()),))
        with self.assertRaisesRegex(LibraryError, "another database"):
            self.library.crates()

    def test_tampered_audio_or_guard_failure_rolls_back_native_changes(self):
        manifest, installed = transfer([self.audio()])
        next(iter(installed.values())).write_bytes(b"tampered")
        with self.assertRaisesRegex(LibraryError, "checksum"):
            self.import_media(manifest, installed)
        self.assertFalse((self.root / "backup").exists())
        manifest, installed = transfer([self.audio()])
        count = 0

        def guard():
            nonlocal count
            count += 1
            if count == 3:
                raise LibraryError("guard refused")

        with self.assertRaisesRegex(LibraryError, "guard refused"):
            self.import_media(manifest, installed, guard=guard)
        with closing(sqlite3.connect(self.database)) as db, db:
            self.assertEqual(db.execute("SELECT count(*) FROM Track").fetchone(), (0,))
            self.assertEqual(db.execute("SELECT count(*) FROM Playlist").fetchone(), (0,))
        self.assertTrue((self.root / "backup" / "m.db").is_file())

    def test_backup_is_compared_to_pinned_prewrite_and_preservation_detects_side_effects(self):
        manifest, installed = transfer([self.audio()])
        from crate_transfer.engine import backup_database

        def tamper(source, target, name):
            backup_database(source, target, name)
            target.execute("UPDATE Unrelated SET payload=?", (b"changed backup",))
            target.commit()

        with patch("crate_transfer.engine.backup_database", side_effect=tamper):
            with self.assertRaisesRegex(LibraryError, "backup contents differ"):
                self.import_media(manifest, installed)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TRIGGER unexpected AFTER INSERT ON Track BEGIN DELETE FROM Unrelated; END")
        with self.assertRaisesRegex(LibraryError, "unexpectedly modified"):
            self.import_media(manifest, installed, name="second-backup")
        with closing(sqlite3.connect(self.database)) as db, db:
            self.assertEqual(db.execute("SELECT count(*) FROM Track").fetchone(), (0,))
            self.assertEqual(db.execute("SELECT count(*) FROM Unrelated").fetchone(), (1,))

    def test_running_engine_and_uninspectable_processes_refuse_access(self):
        process = type("Process", (), {"info": {"name": "Engine DJ.exe"}})()
        with patch("crate_transfer.engine.psutil.process_iter", return_value=[process]):
            with self.assertRaisesRegex(LibraryError, "Quit Engine"):
                self.library.crates()
        with patch("crate_transfer.engine.psutil.process_iter", side_effect=psutil.AccessDenied()):
            with self.assertRaisesRegex(LibraryError, "refused"):
                assert_engine_closed()

    def test_audio_modified_after_registration_rolls_back_and_keeps_backup(self):
        media = self.audio()
        manifest, installed = transfer([media])
        count = 0

        def guard():
            nonlocal count
            count += 1
            if count == 3:
                media.write_bytes(b"audio modified after registration")

        with self.assertRaisesRegex(LibraryError, "changed during import"):
            self.import_media(manifest, installed, guard=guard)
        with closing(sqlite3.connect(self.database)) as db, db:
            self.assertEqual(db.execute("SELECT count(*) FROM Track").fetchone(), (0,))
        self.assertTrue((self.root / "backup" / "m.db").is_file())

    def test_unexpected_trigger_cannot_insert_additional_native_records(self):
        manifest, installed = transfer([self.audio()])
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("""
                CREATE TRIGGER surprise AFTER INSERT ON Track
                WHEN NEW.filename='received.mp3' BEGIN
                  INSERT INTO Track(path,filename) VALUES('../Music/rogue.mp3','rogue.mp3');
                END
            """)
        with self.assertRaisesRegex(LibraryError, "unexpectedly inserted"):
            self.import_media(manifest, installed)
        with closing(sqlite3.connect(self.database)) as db, db:
            self.assertEqual(db.execute("SELECT count(*) FROM Track").fetchone(), (0,))

    def test_unknown_column_types_are_refused(self):
        database = make_engine_database(self.root / "wrong" / "Database2" / "m.db", bpm_type="TEXT")
        with self.assertRaisesRegex(LibraryError, "declaration"):
            EngineLibrary(database)

    def test_database_music_overlap_and_nonlocal_source_fail(self):
        with self.assertRaisesRegex(LibraryError, "separate"):
            self.library.validate_destination(self.database.parent)
        self.seed()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE Track SET path='https://example.invalid/track.mp3'")
        with self.assertRaisesRegex(LibraryError, "local media"):
            self.library.export([1], UUID, "Sender")
