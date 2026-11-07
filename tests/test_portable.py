import shutil
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from crate_transfer.portable import ExportError, METADATA_WARNING, export_bundle
from support import file_record, valid_manifest


class PortableExportTests(unittest.TestCase):
    def test_exchange_file_is_opened_writable_for_windows_fsync_and_sync_errors_propagate(self):
        manifest, sources, _ = self.fixture()
        manifest["destination"] = {"format": "rekordbox-xml"}
        original_open = Path.open
        original_fsync = os.fsync
        exchange_descriptors = set()
        def track_open(path, mode="r", *args, **kwargs):
            handle = original_open(path, mode, *args, **kwargs)
            if path.name == "rekordbox.xml" and mode == "r+b":
                exchange_descriptors.add(handle.fileno())
            return handle
        def fsync(descriptor):
            if descriptor in exchange_descriptors:
                raise OSError("synthetic exchange sync failure")
            return original_fsync(descriptor)
        with patch("pathlib.Path.open", track_open), patch("crate_transfer.portable.os.fsync", fsync), \
                self.assertRaisesRegex(OSError, "exchange sync failure"):
            export_bundle(manifest, sources, self.output, self.export_id, format="rekordbox-xml")
        self.assertTrue(exchange_descriptors)
        self.assertEqual(list(self.output.iterdir()), [])
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.output = self.base / "USB"
        self.output.mkdir()
        self.source = self.base / "source"
        self.source.mkdir()
        self.export_id = "a" * 32

    def fixture(self):
        contents = [b"first media tags", b"second media", b"video bytes"]
        names = ["Same.mp3", "same.mp3", "caf\u00e9.mpg"]
        records, sources = [], {}
        for index, (name, content) in enumerate(zip(names, contents)):
            record = file_record(name, content)
            path = self.source / str(index)
            path.write_bytes(content)
            records.append(record)
            sources[record["id"]] = path
        a, b, c = (record["id"] for record in records)
        manifest = valid_manifest(records, [
            {"path": ["Road/Set"], "tracks": [b, a, b]},
            {"path": ["Road/Set", "Child"], "tracks": [a, c]},
            {"path": ["Road?Set"], "tracks": [c]},
            {"path": ["Empty"], "tracks": []},
        ])
        return manifest, sources, contents

    def test_export_preserves_hierarchy_track_order_shared_bytes_and_portable_relative_paths(self):
        manifest, sources, contents = self.fixture()
        sentinel = self.output / "existing-library"
        sentinel.write_bytes(b"existing USB content")
        result = export_bundle(manifest, sources, self.output, self.export_id)
        bundle = Path(result["output"])
        self.assertEqual(result["format"], "m3u")
        self.assertEqual((result["crates"], result["files"], result["memberships"]), (4, 3, 6))
        self.assertEqual(result["warnings"], [METADATA_WARNING])
        self.assertEqual(sentinel.read_bytes(), b"existing USB content")
        media = {path.name: path.read_bytes() for path in (bundle / "Music").iterdir()}
        self.assertEqual(media, {"Same.mp3": contents[0], "same (2).mp3": contents[1],
                                 "caf\u00e9.mpg": contents[2]})
        expected = {
            "Road_Set": [contents[1], contents[0], contents[1]],
            "Road_Set/Child": [contents[0], contents[2]],
            "Road_Set (2)": [contents[2]],
            "Empty": [],
        }
        for folder, ordered in expected.items():
            playlist = bundle / "Playlists" / folder / "playlist.m3u8"
            lines = playlist.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], "#EXTM3U")
            self.assertTrue(all(not line.startswith(("/", "\\")) and "\\" not in line for line in lines[1:]))
            self.assertEqual([(playlist.parent / line).read_bytes() for line in lines[1:]], ordered)
        moved = self.base / "moved-export"
        shutil.move(str(bundle), str(moved))
        playlist = moved / "Playlists/Road_Set/playlist.m3u8"
        self.assertEqual([(playlist.parent / line).read_bytes()
                          for line in playlist.read_text().splitlines()[1:]], expected["Road_Set"])
        self.assertEqual([path.read_bytes() for path in sources.values()], contents)
        self.assertEqual(list(self.output.glob(".crate-export-*")), [])

    def test_checksum_or_size_change_aborts_without_publishing_or_touching_existing_files(self):
        manifest, sources, _ = self.fixture()
        for content in (b"bad", b"changed same len", b"x" * 1024):
            with self.subTest(length=len(content)):
                sources[manifest["files"][0]["id"]].write_bytes(content)
                with self.assertRaisesRegex(ExportError, "changed|checksum"):
                    export_bundle(manifest, sources, self.output, self.export_id)
                self.assertEqual(list(self.output.iterdir()), [])

    def test_cancelled_copy_removes_only_its_private_staging(self):
        manifest, sources, _ = self.fixture()
        calls = 0

        def cancelled():
            nonlocal calls
            calls += 1
            return calls == 3

        sentinel = self.output / "keep"
        sentinel.write_text("keep")
        with self.assertRaisesRegex(ExportError, "cancelled"):
            export_bundle(manifest, sources, self.output, self.export_id, cancelled=cancelled)
        self.assertEqual(list(self.output.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_text(), "keep")

    def test_existing_export_and_symlink_destinations_are_never_overwritten(self):
        manifest, sources, _ = self.fixture()
        existing = self.output / f"Crates-{self.export_id}"
        existing.mkdir()
        with self.assertRaisesRegex(ExportError, "already exists"):
            export_bundle(manifest, sources, self.output, self.export_id)
        self.assertTrue(existing.is_dir())
        existing.rmdir()
        try:
            existing.symlink_to(self.source, target_is_directory=True)
        except OSError:
            self.skipTest("Directory symlink creation is unavailable on this host")
        with self.assertRaisesRegex(ExportError, "already exists"):
            export_bundle(manifest, sources, self.output, self.export_id)
        self.assertTrue(existing.is_symlink())

    def test_low_space_invalid_identity_or_wrong_files_fail_before_staging(self):
        manifest, sources, _ = self.fixture()
        for identity in ("", "../escape", "g" * 32):
            with self.subTest(identity=identity), self.assertRaisesRegex(ExportError, "identity"):
                export_bundle(manifest, sources, self.output, identity)
        with self.assertRaisesRegex(ExportError, "does not match"):
            export_bundle(manifest, {}, self.output, self.export_id)
        with patch("crate_transfer.portable.shutil.disk_usage", return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(ExportError, "free space"):
                export_bundle(manifest, sources, self.output, self.export_id)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_empty_playlist_exports_valid_header_without_media(self):
        result = export_bundle(valid_manifest(), {}, self.output, self.export_id)
        bundle = Path(result["output"])
        self.assertEqual((bundle / "Playlists/Set/playlist.m3u8").read_bytes(), b"#EXTM3U\n")
        self.assertEqual(list((bundle / "Music").iterdir()), [])

    def test_destination_created_concurrently_is_not_replaced(self):
        manifest, sources, _ = self.fixture()
        destination = self.output / f"Crates-{self.export_id}"

        def progress(message):
            if not destination.exists():
                destination.mkdir()

        with self.assertRaisesRegex(ExportError, "appeared during export"):
            export_bundle(manifest, sources, self.output, self.export_id, progress)
        self.assertTrue(destination.is_dir())
        self.assertEqual(list(destination.iterdir()), [])
        self.assertEqual(list(self.output.glob(".crate-export-*")), [])
