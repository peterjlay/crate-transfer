import os
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from crate_transfer.library import LibraryError, assert_serato_closed
from crate_transfer.serato3 import Serato3Library, crate_bytes, crate_paths, field, fields, volume_root
from support import file_record, valid_manifest


def make_serato3_library(directory):
    directory = Path(directory) / "_Serato_"
    (directory / "Subcrates").mkdir(parents=True)
    header = field(b"vrsn", "2.0/Serato Scratch LIVE Database".encode("utf-16-be"))
    (directory / "database V2").write_bytes(header)
    (directory / "neworder.pref").write_bytes(b"opaque existing crate ordering")
    return directory


class Serato3Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.directory = make_serato3_library(self.base / "library")
        self.media = self.base / "Music/song.mp3"
        self.media.parent.mkdir()
        self.media.write_bytes(b"synthetic legacy audio")
        self.record = file_record(self.media.name, self.media.read_bytes())
        self.manifest = valid_manifest([self.record], [
            {"path": ["Sets", "Night"], "tracks": [self.record["id"], self.record["id"]]}
        ])
        self.manifest["destination"] = {"format": "serato3"}
        self.library = Serato3Library(self.directory)

    def test_tlv_wire_shape_utf16_paths_and_bounded_malformed_input(self):
        data = crate_bytes(["Music/é🎵.mp3", "Music/two.wav"])
        self.assertEqual(struct.unpack_from(">4sI", data), (b"vrsn", 56))
        self.assertEqual(crate_paths(data), ["Music/é🎵.mp3", "Music/two.wav"])
        for value in (data[:-1], b"x", b"ptrk" + struct.pack(">I", 999), b"\0bad\0\0\0\0"):
            with self.subTest(value=value), self.assertRaises(LibraryError):
                fields(value)
        with self.assertRaisesRegex(LibraryError, "version"):
            crate_paths(field(b"vrsn", "unexpected".encode("utf-16-be")))
        with self.assertRaisesRegex(LibraryError, "exactly one"):
            crate_paths(crate_bytes([]) + field(b"otrk", field(b"ptrk", "a".encode("utf-16-be")) +
                                               field(b"ptrk", "b".encode("utf-16-be"))))

    def test_native_import_backup_order_hierarchy_dedup_and_collision(self):
        before = {path: path.read_bytes() for path in self.library.backup_paths}
        result = self.library.import_crates(self.manifest, {self.record["id"]: self.media},
                                           self.base / "backup", "test")
        self.assertEqual((result["crates"], result["files"], result["memberships"]), (1, 1, 1))
        crate = self.directory / "Subcrates/Sets%%Night.crate"
        native = self.media.relative_to(volume_root(self.directory)).as_posix()
        self.assertEqual(crate_paths(crate.read_bytes()), [native])
        self.assertEqual(crate_paths((self.directory / "Subcrates/Sets.crate").read_bytes()), [])
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual((self.base / "backup" / path.relative_to(self.directory)).read_bytes(), data)
        self.assertTrue((self.base / "backup/created-crates.json").is_file())
        received = self.library.crates()
        self.assertEqual([row["path"] for row in received], [["Sets"], ["Sets", "Night"]])
        manifest, paths = self.library.export([received[0]["id"]], self.manifest["sender_id"], "Forward")
        self.assertEqual(manifest["files"][0]["sha256"], self.record["sha256"])
        self.assertEqual(list(paths.values()), [self.media])
        self.library.import_crates(self.manifest, {self.record["id"]: self.media}, self.base / "backup2", "test2")
        self.assertTrue((self.directory / "Subcrates/Sets (2)%%Night.crate").is_file())

    def test_database_tags_and_crate_order_are_read_without_mutation(self):
        path = self.directory / "database V2"
        path.write_bytes(path.read_bytes() + field(b"otrk", b"".join(
            field(tag, value.encode("utf-16-be")) for tag, value in (
                (b"pfil", self.media.relative_to(self.library.volume).as_posix()),
                (b"tsng", "Legacy title"), (b"tart", "Artist"), (b"talb", "Album"), (b"tbpm", "123.5")))))
        crate = self.directory / "Subcrates/Test.crate"
        crate.write_bytes(crate_bytes([self.media.relative_to(self.library.volume).as_posix()] * 2))
        before = path.read_bytes()
        reader = Serato3Library(self.directory)
        manifest, _ = reader.export([1], self.manifest["sender_id"], "Source")
        self.assertEqual(manifest["files"][0]["metadata"],
                         {"title": "Legacy title", "artist": "Artist", "album": "Album", "bpm": 123.5})
        self.assertEqual(len(manifest["crates"][0]["tracks"]), 2)
        self.assertEqual(path.read_bytes(), before)

    def test_publication_failure_removes_new_crates_and_keeps_existing_artifacts(self):
        original = os.link
        calls = 0
        def fail_second(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic publication failure")
            original(source, destination)
        with patch("crate_transfer.serato3.os.link", side_effect=fail_second), \
                self.assertRaisesRegex(OSError, "publication failure"):
            self.library.import_crates(self.manifest, {self.record["id"]: self.media}, self.base / "backup", "test")
        self.assertEqual(list((self.directory / "Subcrates").iterdir()), [])
        self.assertTrue((self.base / "backup/database V2").is_file())

    def test_library_change_during_guard_is_not_overwritten(self):
        def guard():
            if (self.base / "backup").exists():
                (self.directory / "neworder.pref").write_bytes(b"new native changes")
        with self.assertRaisesRegex(LibraryError, "changed after backup"):
            self.library.import_crates(self.manifest, {self.record["id"]: self.media},
                                       self.base / "backup", "test", guard)
        self.assertEqual((self.directory / "neworder.pref").read_bytes(), b"new native changes")
        self.assertEqual(list((self.directory / "Subcrates").iterdir()), [])

    def test_missing_running_unknown_header_and_cross_volume_fail_closed(self):
        with self.assertRaisesRegex(LibraryError, "existing"):
            Serato3Library(self.base / "missing")
        with patch("crate_transfer.library.psutil.process_iter",
                   return_value=[SimpleNamespace(info={"name": "Serato DJ Pro"})]), \
                self.assertRaisesRegex(LibraryError, "Quit Serato"):
            Serato3Library(self.directory)
        with patch("crate_transfer.serato3.volume_root", return_value=Path("/other-volume")), \
                self.assertRaisesRegex(LibraryError, "volume root"):
            self.library.validate_destination(self.media.parent)
        (self.directory / "database V2").write_bytes(field(b"vrsn", "unknown".encode("utf-16-be")))
        with self.assertRaisesRegex(LibraryError, "header"):
            self.library.crates()

    def test_tampered_media_refused_before_backup_or_crate_publication(self):
        self.media.write_bytes(b"tampered")
        with self.assertRaisesRegex(LibraryError, "checksum"):
            self.library.import_crates(self.manifest, {self.record["id"]: self.media}, self.base / "backup", "test")
        self.assertFalse((self.base / "backup").exists())

    def test_unsafe_legacy_paths_and_backup_or_transfer_identity_are_rejected(self):
        for raw in ("", "../music.mp3", "C:/sender/music.mp3", "//server/music.mp3",
                    "file:///sender/song.mp3", "song\n.mp3"):
            with self.subTest(raw=raw), self.assertRaisesRegex(LibraryError, "volume-relative"):
                self.library._media(raw)
        with self.assertRaisesRegex(LibraryError, "transfer identity"):
            self.library.import_crates(self.manifest, {self.record["id"]: self.media},
                                       self.base / "backup", "../unsafe")
        with self.assertRaisesRegex(LibraryError, "backups outside"):
            self.library.import_crates(self.manifest, {self.record["id"]: self.media},
                                       self.directory / "backup", "test")
        self.assertEqual(list((self.directory / "Subcrates").iterdir()), [])

    def test_descendant_only_existing_crate_reserves_its_parent_and_crash_ledger_is_exact(self):
        import json
        existing = self.directory / "Subcrates/Sets%%Old.crate"
        data = crate_bytes([])
        existing.write_bytes(data)
        result = self.library.import_crates(self.manifest, {self.record["id"]: self.media},
                                           self.base / "backup", "test")
        self.assertEqual(existing.read_bytes(), data)
        ledger = json.loads((Path(result["backup"]) / "created-crates.json").read_text())
        self.assertEqual(ledger, ["Subcrates/Sets (2).crate", "Subcrates/Sets (2)%%Night.crate"])
        for relative in ledger:
            self.assertTrue((self.directory / relative).is_file())
