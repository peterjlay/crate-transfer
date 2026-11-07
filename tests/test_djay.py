"""Synthetic native djay fixtures only; no user library or application access."""
import hashlib
import tempfile
import sqlite3
import struct
import unittest
import uuid
import wave
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import psutil

from crate_transfer.djay import (
    ARRAY_TAGS, CAVEAT, CLASSES, INDEXES, LINKS, PLAYLISTS, ROOT, DjayLibrary,
    Object, Value, _bookmark_path, _file_uri, _get, _ids, array,
    assert_djay_closed, decode, encode, native, text,
)
from crate_transfer.library import LibraryError
from crate_transfer.protocol import PROTOCOL, validate_manifest

SENDER = "11111111-1111-4111-8111-111111111111"


def make_djay_database(path):
    """Create an empty, synthetic supported native DB; return its final DB Path."""
    path = Path(path)
    if path.name != "MediaLibrary.db":
        path = path / "MediaLibrary.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        db.executescript(
            "CREATE TABLE database2(rowid INTEGER PRIMARY KEY,collection TEXT NOT NULL,"
            "key TEXT NOT NULL,data BLOB,metadata BLOB);"
            "CREATE UNIQUE INDEX database2_ck ON database2(collection,key);"
            "CREATE TABLE yap2(extension TEXT NOT NULL,key TEXT NOT NULL,data);"
            "CREATE UNIQUE INDEX yap2_ek ON yap2(extension,key);"
            "CREATE TABLE secondaryIndex_mediaItemLocationIndex(rowid INTEGER PRIMARY KEY,fileName TEXT);"
        )
        db.executemany("INSERT INTO yap2 VALUES(?,'versionTag',?)", [(name, b"synthetic-v1") for name in sorted(INDEXES)])
        db.execute("INSERT INTO yap2 VALUES('unrelated','opaque',?)", (b"untouched",))
        db.commit()
    return path


def _insert(db, collection, key, obj, metadata=None):
    return db.execute("INSERT INTO database2(collection,key,data,metadata) VALUES(?,?,?,?)",
                      (collection, key, encode(obj), metadata)).lastrowid


def _record(path, **metadata):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"id": hashlib.sha256((digest + "\0" + path.name).encode()).hexdigest(),
            "sha256": digest, "name": path.name, "size": path.stat().st_size,
            "metadata": metadata}


def _manifest(records, crates=None):
    return {"protocol": PROTOCOL, "destination": {"format": "serato"},
            "sender_id": SENDER, "sender_name": "Synthetic Sender", "files": records,
            "crates": crates or [{"path": ["Incoming"], "tracks": [r["id"] for r in records]}]}


def _bookmark(path):
    """Synthetic bounded CF bookmark with only verified component-array records."""
    body = bytearray(bytes(4))
    offsets = []
    for component in Path(path).parts[1:]:
        offsets.append(len(body))
        data = component.encode()
        body.extend(struct.pack("<II", len(data), 0x101) + data)
    array_offset = len(body)
    body.extend(struct.pack("<II", 4 * len(offsets), 0x601))
    body.extend(struct.pack("<" + "I" * len(offsets), *offsets))
    toc = len(body)
    body.extend(struct.pack("<IIIII", 32, 0xfffffffe, 1, 0, 1))
    body.extend(struct.pack("<III", 0x1004, array_offset, 0))
    body[:4] = struct.pack("<I", toc)
    return b"book" + struct.pack("<III", 16 + len(body), 0, 16) + body


class DjayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.library = DjayLibrary(make_djay_database(self.base / "Library.djayMediaLibrary"))
        self.audio = self.base / "Received" / "one # ü.mp3"
        self.audio.parent.mkdir()
        self.audio.write_bytes(b"ID3-synthetic-no-real-audio")
        self.record = _record(self.audio, title="One", artist="Artist", album="Album", bpm=123,
                              key="Am", cues=[{"kind": "loop", "position": 2, "end": 6, "slot": 0, "name": "Loop"}])
        self.manifest = _manifest([self.record])
        self.installed = {self.record["id"]: self.audio}

    def import_library(self, manifest=None, installed=None, guard=lambda: None, name="backup"):
        return self.library.import_crates(manifest or self.manifest, installed or self.installed,
                                          self.base / name, "synthetic-transfer", guard=guard)

    def rows(self):
        with closing(sqlite3.connect(self.library.path)) as db:
            return list(db.execute("SELECT rowid,collection,key,data,metadata FROM database2 ORDER BY rowid"))

    def objects(self, collection):
        return {key: decode(data) for _, c, key, data, _ in self.rows() if c == collection}

    def test_path_resolution_and_destination_no_same_volume_constraint(self):
        self.assertEqual(self.library.path, self.library.directory / "MediaLibrary.db")
        self.assertEqual(DjayLibrary(self.library.path).path, self.library.path)
        self.library.validate_destination(self.audio.parent)
        for path in (self.library.directory, self.library.directory / "Music", self.base):
            with self.subTest(path=path), self.assertRaisesRegex(LibraryError, "separate"):
                self.library.validate_destination(path.resolve())
        with self.assertRaisesRegex(LibraryError, "folder"):
            self.library.validate_destination(self.audio)

    def test_missing_database_is_not_created(self):
        path = self.base / "missing"
        with self.assertRaisesRegex(LibraryError, "not found"):
            DjayLibrary(path).validate_destination(self.audio.parent)
        self.assertFalse(path.exists())

    def test_registers_new_media_graph_final_paths_index_and_honest_analysis(self):
        guard = Mock()
        result = self.import_library(guard=guard)
        self.assertEqual(guard.call_count, 2)
        self.assertEqual({k: result[k] for k in ("crates", "files", "memberships")},
                         {"crates": 1, "files": 1, "memberships": 1})
        media = self.objects("mediaItems")
        self.assertEqual(len(media), 1)
        key, item = next(iter(media.items()))
        for collection in ("mediaItemTitleIDs", "mediaItemUserData", "mediaItemAnalyzedData", "localMediaItemLocations"):
            self.assertEqual(set(self.objects(collection)), {key})
        self.assertEqual(_get(item, "title"), "One")
        self.assertEqual(_get(item, "bpm"), 123)
        self.assertNotIn("duration", item.fields)
        analysis = self.objects("mediaItemAnalyzedData")[key]
        self.assertNotIn("isStraightGrid", analysis.fields)
        self.assertNotIn("waveform", analysis.fields)
        location = self.objects("localMediaItemLocations")[key]
        self.assertEqual(_get(location, "sourceURIs")[0], Value(33, self.audio.resolve().as_uri()))
        self.assertNotIn("urlBookmarkData", location.fields)
        self.assertIn(CAVEAT, result["warnings"])
        self.assertTrue(any("Unknown durations" in w for w in result["warnings"]))
        with closing(sqlite3.connect(self.library.path)) as db:
            location_row = db.execute("SELECT rowid FROM database2 WHERE collection='localMediaItemLocations'").fetchone()[0]
            self.assertEqual(db.execute("SELECT * FROM secondaryIndex_mediaItemLocationIndex").fetchall(),
                             [(location_row, self.audio.name)])
            self.assertEqual({r[0] for r in db.execute("SELECT data FROM yap2 WHERE key='versionTag'")}, {0})
            self.assertEqual(db.execute("SELECT data FROM yap2 WHERE extension='unrelated'").fetchone()[0], b"untouched")
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        with closing(sqlite3.connect(Path(result["backup"]) / "MediaLibrary.db")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM database2").fetchone()[0], 0)
            self.assertEqual({r[0] for r in db.execute("SELECT data FROM yap2 WHERE key='versionTag'")}, {b"synthetic-v1"})

    def test_export_real_native_graph_roundtrip_metadata_uri_and_order(self):
        self.import_library()
        crates = self.library.crates()
        self.assertEqual(len(crates), 1)
        manifest, paths = self.library.export([crates[0]["id"]], SENDER, "Sender")
        validate_manifest(manifest)
        self.assertEqual(manifest["files"][0]["metadata"], self.record["metadata"])
        self.assertEqual(paths, {self.record["id"]: self.audio.resolve()})
        self.assertEqual(manifest["crates"], self.manifest["crates"])
        self.assertEqual(manifest["destination"], {"format": "serato"})
        with self.assertRaisesRegex(LibraryError, "valid"):
            self.library.export([999999], SENDER, "Sender")

    def test_additive_collision_preserves_existing_records_and_root_unknown_fields(self):
        self.import_library()
        with closing(sqlite3.connect(self.library.path)) as db:
            root = decode(db.execute("SELECT data FROM database2 WHERE key=?", (ROOT,)).fetchone()[0])
            root.fields["unknownBinary"] = Value(21, b"\x00\xffbookmark-opaque")
            root.fields["unknownNested"] = Value(43, native("FutureClass", number=Value(19, 1.25)))
            db.execute("UPDATE database2 SET data=?,metadata=? WHERE key=?", (encode(root), b"keep-root-metadata", ROOT))
            db.execute("CREATE TABLE unrelated(payload BLOB)")
            db.execute("INSERT INTO unrelated VALUES(?)", (b"unknown-table",))
            _insert(db, "futureObjects", "future", native("UnknownClass", value=text("opaque")), b"metadata")
            db.commit()
        old = self.rows()
        self.import_library(name="backup-2")
        after = self.rows()
        self.assertEqual([r for r in old if r[2] != ROOT], [r for r in after if r[0] in {r[0] for r in old} and r[2] != ROOT])
        root = self.objects(PLAYLISTS)[ROOT]
        self.assertEqual(root.fields["unknownBinary"], Value(21, b"\x00\xffbookmark-opaque"))
        self.assertEqual(_get(root, "unknownNested").class_name, "FutureClass")
        self.assertEqual(next(r[4] for r in after if r[2] == ROOT), b"keep-root-metadata")
        self.assertEqual({tuple(c["path"]) for c in self.library.crates()}, {("Incoming",), ("Incoming (2)",)})
        self.assertEqual(len(self.objects("mediaItems")), 1)
        with closing(sqlite3.connect(self.library.path)) as db:
            self.assertEqual(db.execute("SELECT payload FROM unrelated").fetchone()[0], b"unknown-table")

    def test_nested_folders_duplicate_track_positions_and_parent_selection(self):
        key = self.record["id"]
        manifest = _manifest([self.record], [{"path": ["Folder", "Playlist"], "tracks": [key, key]},
                                             {"path": ["Folder", "Empty"], "tracks": []}])
        self.import_library(manifest=manifest)
        crates = self.library.crates()
        folder = next(c for c in crates if c["path"] == ["Folder"])
        self.assertEqual(folder["tracks"], 0)
        exported, _ = self.library.export([folder["id"]], SENDER, "Sender")
        self.assertEqual(len(exported["crates"]), 3)
        playlist = next(c for c in exported["crates"] if c["path"] == ["Folder", "Playlist"])
        self.assertEqual(playlist["tracks"], [key, key])
        self.assertEqual(len(self.objects(LINKS)), 2)
        self.import_library(manifest=manifest, name="backup-2")
        self.assertIn(["Folder (2)", "Playlist"], [c["path"] for c in self.library.crates()])

    def test_pcm_wav_duration_is_measured_not_fabricated(self):
        audio = self.audio.with_suffix(".wav")
        with wave.open(str(audio), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(bytes(16000))
        record = _record(audio)
        result = self.import_library(manifest=_manifest([record]), installed={record["id"]: audio})
        item = next(iter(self.objects("mediaItems").values()))
        self.assertEqual(_get(item, "duration"), 1)
        self.assertEqual(_get(item, "sampleRate"), 8000)
        self.assertFalse(any("Unknown durations" in w for w in result["warnings"]))

    def test_guard_rollback_before_commit_and_verified_backup(self):
        before = self.rows()
        guard = Mock(side_effect=[None, LibraryError("djay restarted")])
        with self.assertRaisesRegex(LibraryError, "restarted"):
            self.import_library(guard=guard)
        self.assertEqual(self.rows(), before)
        self.assertTrue((self.base / "backup" / "MediaLibrary.db").is_file())
        with closing(sqlite3.connect(self.library.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM secondaryIndex_mediaItemLocationIndex").fetchone()[0], 0)
            self.assertEqual({r[0] for r in db.execute("SELECT data FROM yap2 WHERE key='versionTag'")}, {b"synthetic-v1"})

    def test_guard_before_any_backup(self):
        with self.assertRaisesRegex(LibraryError, "running"):
            self.import_library(guard=Mock(side_effect=LibraryError("running")))
        self.assertFalse((self.base / "backup").exists())

    def test_received_audio_changes_at_commit_roll_back(self):
        calls = []

        def guard():
            calls.append(True)
            if len(calls) == 2:
                self.audio.write_bytes(b"changed")
        with self.assertRaisesRegex(LibraryError, "changed during"):
            self.import_library(guard=guard)
        self.assertEqual(self.rows(), [])

    def test_unsupported_schema_and_index_policies_fail_closed(self):
        for sql, message in (
            ("ALTER TABLE database2 ADD COLUMN unsupported", "schema"),
            ("INSERT INTO yap2 VALUES('futureIndex','versionTag',1)", "index policy"),
            ("CREATE TABLE secondaryIndex_futureIndex(rowid INTEGER)", "index table"),
            ("ALTER TABLE secondaryIndex_mediaItemLocationIndex ADD COLUMN unsupported", "index schema"),
            ("CREATE TRIGGER unsupported AFTER INSERT ON database2 BEGIN SELECT 1; END", "trigger"),
        ):
            with self.subTest(sql=sql):
                path = make_djay_database(self.base / uuid.uuid4().hex)
                library = DjayLibrary(path)
                with closing(sqlite3.connect(path)) as db:
                    db.execute(sql)
                    db.commit()
                with self.assertRaisesRegex(LibraryError, message):
                    library.import_crates(self.manifest, self.installed, self.base / uuid.uuid4().hex, "transfer")
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM database2").fetchone()[0], 0)

    def test_unknown_serialization_stops_before_writes(self):
        with closing(sqlite3.connect(self.library.path)) as db:
            db.execute("INSERT INTO database2(collection,key,data) VALUES('mediaItems','bad',?)", (b"not-TSAF",))
            db.commit()
        before = self.rows()
        with self.assertRaisesRegex(LibraryError, "TSAF"):
            self.import_library()
        self.assertEqual(self.rows(), before)

    def test_ambiguous_uri_bookmark_and_entity_identity_roll_back(self):
        self.import_library()
        key, location = next(iter(self.objects("localMediaItemLocations").items()))
        location.fields["urlBookmarkData"] = Value(21, _bookmark(self.audio.with_name("different.mp3").resolve()))
        with closing(sqlite3.connect(self.library.path)) as db:
            db.execute("UPDATE database2 SET data=? WHERE collection='localMediaItemLocations'", (encode(location),))
            db.commit()
        before = self.rows()
        with self.assertRaisesRegex(LibraryError, "Conflicting"):
            self.import_library(name="backup-2")
        self.assertEqual(self.rows(), before)

    def test_missing_streaming_and_ambiguous_locations_do_not_fallback_to_filenames(self):
        self.import_library()
        key, location = next(iter(self.objects("localMediaItemLocations").items()))
        location.fields["sourceURIs"] = array([Value(33, "https://example.invalid/one.mp3")])
        with closing(sqlite3.connect(self.library.path)) as db:
            db.execute("UPDATE database2 SET data=? WHERE collection='localMediaItemLocations'", (encode(location),))
            db.commit()
        crate = self.library.crates()[0]
        with self.assertRaisesRegex(LibraryError, "Missing local"):
            self.library.export([crate["id"]], SENDER, "Sender")

    def test_hierarchy_cycle_and_smart_playlist_rejected(self):
        self.import_library()
        key, obj = next((k, v) for k, v in self.objects(PLAYLISTS).items() if k != ROOT)
        obj.fields["type"] = Value(15, 2)
        with closing(sqlite3.connect(self.library.path)) as db:
            db.execute("UPDATE database2 SET data=? WHERE key=?", (encode(obj), key))
            db.commit()
        with self.assertRaisesRegex(LibraryError, "smart playlist"):
            self.library.crates()

    def test_hierarchy_with_parent_tracks_is_not_silently_dropped(self):
        key = self.record["id"]
        manifest = _manifest([self.record], [{"path": ["Parent"], "tracks": [key]},
                                             {"path": ["Parent", "Child"], "tracks": [key]}])
        with self.assertRaisesRegex(LibraryError, "both tracks and children"):
            self.import_library(manifest=manifest)
        self.assertEqual(self.rows(), [])

    def test_file_checksum_mismatch_fails_before_backup(self):
        self.audio.write_bytes(b"corrupt")
        with self.assertRaisesRegex(LibraryError, "checksum"):
            self.import_library()
        self.assertFalse((self.base / "backup").exists())

    def test_file_uri_percent_unicode_and_rejects_remote_or_ambiguous(self):
        self.assertEqual(_file_uri(self.audio.resolve().as_uri()), self.audio.resolve())
        for uri in ("file:///a/%ZZ.mp3", "file:///a/%00.mp3", "file://remote/a.mp3",
                    "file:///a/../b.mp3", "file:///a.mp3?other=1", "https://example.invalid/a.mp3"):
            with self.subTest(uri=uri), self.assertRaises(LibraryError):
                _file_uri(uri)

    def test_bookmark_parser_embedded_valid_conflicts_and_malformed(self):
        bookmark = _bookmark(self.audio.resolve())
        self.assertEqual(_bookmark_path(b"opaque-prefix" + bookmark), self.audio.resolve())
        self.assertIsNone(_bookmark_path(b"opaque-no-signature"))
        with self.assertRaisesRegex(LibraryError, "Conflicting"):
            _bookmark_path(bookmark + _bookmark(self.audio.with_name("other.mp3").resolve()))
        for malformed in (b"book", bookmark[:-1], b"book" + bytes(100)):
            with self.subTest(malformed=malformed[:20]), self.assertRaises(LibraryError):
                _bookmark_path(malformed)

    def test_application_guard_djay_denied_and_race(self):
        with patch("crate_transfer.djay.psutil.process_iter", return_value=[SimpleNamespace(info={"name": "djay Pro"})]):
            with self.assertRaisesRegex(LibraryError, "Quit djay"):
                assert_djay_closed()
        with patch("crate_transfer.djay.psutil.process_iter", side_effect=psutil.AccessDenied(1)):
            with self.assertRaisesRegex(LibraryError, "Cannot check"):
                assert_djay_closed()
        with patch("crate_transfer.djay.psutil.process_iter", return_value=[SimpleNamespace(info={"name": "unrelated"})]):
            assert_djay_closed()


class ArchiveTests(unittest.TestCase):
    def test_independently_encoded_native_string_reference(self):
        # A hand-built native class, literal value and pointer field-name.
        payload = b"\x2b\x08Class\0\x08literal\0\x05\x01\0"
        header = b"TSAF\x03\x00\x03\x00" + bytes(8) + struct.pack("<I", 2)
        obj = decode(header + payload)
        self.assertEqual(obj, Object("Class", {"literal": text("literal")}))
        self.assertEqual(decode(encode(obj)), obj)

    def test_all_verified_value_types_and_unknown_fields_survive(self):
        values = {"text": text("ü"), "uri": Value(33, "file:///one.mp3"),
                  "u16": Value(16, 65535), "u32": Value(17, 123456),
                  "u64": Value(18, 2**50), "float": Value(19, 1.25),
                  "double": Value(20, 12.5), "date": Value(48, 55.5),
                  "binary": Value(21, b"\0\xff"), "small": Value(15, 9),
                  "zero": Value(46, 0), "one": Value(45, 1),
                  "true": Value(13, True), "false": Value(14, False),
                  "dictionary": Value(24, {"nested": Value(43, native("UnknownClass", value=text("future"))),
                                           "null": Value(0)})}
        for tag in ARRAY_TAGS:
            values[f"array-{tag}"] = Value(tag, [text("same"), text("same")])
        obj = Object("ADCMediaItemPlaylist", values)
        self.assertEqual(decode(encode(obj)), obj)
        self.assertEqual(encode(decode(encode(obj))), encode(obj))

    def test_malformed_archive_fails_closed_and_limits(self):
        good = encode(native("Class", value=text("text")))
        for blob in (b"", good[:-1], good + b"trailing", b"NSKA" + good[4:],
                     good[:20] + b"\xff", good[:20] + b"\x05\xff",
                     good[:20] + b"\x2b\x08Class\0\x0a" + bytes(1) + struct.pack("<I", 0xffffffff)):
            with self.subTest(blob=blob[:24]), self.assertRaises(LibraryError):
                decode(blob)
        nested = text("bottom")
        for _ in range(70):
            nested = array([nested])
        with self.assertRaisesRegex(LibraryError, "nesting"):
            encode(native("Class", nested=nested))
        with self.assertRaisesRegex(LibraryError, "Non-finite"):
            encode(native("Class", number=Value(20, float("nan"))))


if __name__ == "__main__":
    unittest.main()
