import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import psutil

from crate_transfer.library import (
    Library,
    LibraryError,
    assert_serato_closed,
    backup_database,
    check_root,
    connect,
    portable_path,
    verify_backup_contents,
)
from support import (
    create_aggregate_database,
    create_root_database,
    file_record,
    make_library,
    root_snapshot,
)


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def make_source(self):
        contents = {
            "Music/one.mp3": b"ID3\x00tag-one\x00synthetic-audio",
            "Music/two.mp3": b"ID3\x03tag-two\x00synthetic-audio",
            "A/collision.mp3": b"first-tagged-audio",
            "B/collision.mp3": b"second-tagged-audio",
            "Other/unrelated.mp3": b"unrelated",
        }
        crates = [
            {"id": 10, "name": "Selected", "tracks": [2, 1, 3, 4]},
            {"id": 11, "parent_id": 10, "name": "Child", "tracks": [1, 2]},
            {"id": 12, "name": "Unrelated", "tracks": [5]},
        ]
        assets = {
            1: "Music/one.mp3",
            2: "Music/two.mp3",
            3: "A/collision.mp3",
            4: "B/collision.mp3",
            5: "Other/unrelated.mp3",
        }
        directory = self.base / "source-library"
        volume = make_library(directory, contents, crates, assets)
        return Library(directory), volume, contents, assets

    def test_direct_and_location_linked_aggregate_layouts_preserve_same_crate_memberships(self):
        source, volume, _, assets = self.make_source()
        direct = self.base / "direct-library"
        create_aggregate_database(direct / "master.sqlite", volume, [
            {"id": 10, "name": "Selected", "tracks": [2, 1, 3, 4]},
            {"id": 11, "parent_id": 10, "name": "Child", "tracks": [1, 2]},
            {"id": 12, "name": "Unrelated", "tracks": [5]},
        ], assets, linked=False)
        self.assertEqual(Library(direct).crates(), source.crates())
        arguments = ([10], "11111111-1111-4111-8111-111111111111", "Sender")
        self.assertEqual(Library(direct).export(*arguments), source.export(*arguments))

    def test_location_linked_aggregate_without_membership_id_keeps_track_order(self):
        library, _, _, _ = self.make_source()
        with closing(sqlite3.connect(library.directory / "master.sqlite")) as db:
            db.executescript(
                "ALTER TABLE container_asset RENAME TO old_membership;"
                "CREATE TABLE container_asset AS SELECT asset_id,location_container_id,list_order FROM old_membership;"
                "DROP TABLE old_membership;"
            )
        self.assertEqual(library.crates()[0]["tracks"], 4)
        manifest, _ = library.export([11], "11111111-1111-4111-8111-111111111111", "Sender")
        records = {record["name"]: record["id"] for record in manifest["files"]}
        self.assertEqual(manifest["crates"], [
            {"path": ["Selected", "Child"], "tracks": [records["one.mp3"], records["two.mp3"]]},
        ])

    def test_location_linked_aggregate_rejects_missing_bridge_with_useful_columns(self):
        library, _, _, _ = self.make_source()
        with closing(sqlite3.connect(library.directory / "master.sqlite")) as db:
            db.execute("DROP TABLE location_container")
        with self.assertRaisesRegex(LibraryError, "location_container.*Missing columns"):
            library.crates()

    def test_serato_parent_resolves_library_subfolder_and_lists_crates(self):
        parent = self.base / "Application Support" / "Serato"
        nested = parent / "Library"
        make_library(nested, {"Music/a.mp3": b"audio"}, [
            {"id": 10, "name": "Set", "tracks": [1]},
        ], {1: "Music/a.mp3"})
        library = Library(parent)
        self.assertEqual(library.directory, nested.resolve())
        self.assertEqual(library.crates(), [{"id": 10, "path": ["Set"], "tracks": 1}])
        self.assertEqual(Library(nested).directory, nested.resolve())

    def test_serato_parent_rejects_ambiguous_databases_and_preserves_direct_layout(self):
        parent = self.base / "Serato"
        make_library(parent, {}, [], {})
        self.assertEqual(Library(parent).directory, parent.resolve())
        make_library(parent / "Library", {}, [], {})
        with self.assertRaisesRegex(LibraryError, "Multiple Serato library locations"):
            Library(parent)
        self.assertEqual(Library(parent / "Library").directory, (parent / "Library").resolve())

    def test_database_open_error_reports_path_and_preserves_original_cause(self):
        path = self.base / "Library with spaces" / "master.sqlite"
        path.parent.mkdir()
        path.write_bytes(b"placeholder")
        with patch("crate_transfer.library.sqlite3.connect",
                   side_effect=sqlite3.OperationalError("access denied")) as open_db:
            with self.assertRaises(LibraryError) as caught:
                connect(path)
        self.assertIn(str(path), str(caught.exception))
        self.assertIn("access denied", str(caught.exception))
        self.assertIn("-wal/-shm", str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, sqlite3.OperationalError)
        self.assertEqual(open_db.call_count, 1)
        uri = open_db.call_args.args[0]
        self.assertIn("%20", uri)
        self.assertTrue(uri.endswith("?mode=ro"))
        self.assertNotIn("immutable", uri)

    def test_first_read_error_closes_connection_and_reports_journal_guidance(self):
        path = self.base / "master.sqlite"
        path.write_bytes(b"placeholder")
        with patch("crate_transfer.library.sqlite3.connect") as open_db:
            db = open_db.return_value
            db.execute.side_effect = [None, sqlite3.OperationalError("journal access denied")]
            with self.assertRaisesRegex(LibraryError, "journal files"):
                connect(path)
            db.close.assert_called_once()

    def test_cannot_open_reads_private_snapshot_and_removes_it_on_close(self):
        path = self.base / "master.sqlite"
        with closing(sqlite3.connect(path)) as writer:
            writer.execute("CREATE TABLE sample(name)")
            writer.execute("INSERT INTO sample VALUES ('snapshot row')")
            writer.commit()
        original = path.read_bytes()
        real_connect = sqlite3.connect

        def open_database(database, **kwargs):
            if database == path.resolve().as_uri() + "?mode=ro":
                raise sqlite3.OperationalError("unable to open database file")
            return real_connect(database, **kwargs)

        with patch("crate_transfer.library.sqlite3.connect", side_effect=open_database), \
                patch("crate_transfer.library.assert_serato_closed") as guard:
            with closing(connect(path)) as reader:
                self.assertEqual(reader.execute("SELECT name FROM sample").fetchone()[0], "snapshot row")
                snapshot_path = Path(reader.execute("PRAGMA database_list").fetchone()[2])
                self.assertNotEqual(snapshot_path, path)
                self.assertTrue(snapshot_path.is_file())
                with self.assertRaises(sqlite3.OperationalError):
                    reader.execute("INSERT INTO sample VALUES ('forbidden')")
            self.assertEqual(guard.call_count, 2)
        self.assertFalse(snapshot_path.parent.exists())
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(sorted(item.name for item in self.base.iterdir()), ["master.sqlite"])

    def test_snapshot_replays_wal_without_changing_original_database_or_wal(self):
        path = self.base / "master.sqlite"
        real_connect = sqlite3.connect
        with closing(real_connect(path)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("CREATE TABLE sample(name)")
            writer.execute("INSERT INTO sample VALUES ('committed WAL row')")
            writer.commit()
            wal = Path(str(path) + "-wal")
            originals = (path.read_bytes(), wal.read_bytes())

            def open_database(database, **kwargs):
                if database == path.resolve().as_uri() + "?mode=ro":
                    raise sqlite3.OperationalError("unable to open database file")
                return real_connect(database, **kwargs)

            with patch("crate_transfer.library.sqlite3.connect", side_effect=open_database), \
                    patch("crate_transfer.library.assert_serato_closed"):
                with closing(connect(path)) as reader:
                    self.assertEqual(reader.execute("SELECT name FROM sample").fetchone()[0], "committed WAL row")
            self.assertEqual((path.read_bytes(), wal.read_bytes()), originals)

    def test_snapshot_refuses_running_serato_and_write_connections_never_fall_back(self):
        path = self.base / "root.sqlite"
        path.write_bytes(b"placeholder")
        with patch("crate_transfer.library.sqlite3.connect",
                   side_effect=sqlite3.OperationalError("unable to open database file")) as open_db, \
                patch("crate_transfer.library.assert_serato_closed",
                      side_effect=LibraryError("Quit Serato before importing")), \
                patch("crate_transfer.library.shutil.copyfile") as copy:
            with self.assertRaisesRegex(LibraryError, "Quit Serato"):
                connect(path)
            copy.assert_not_called()
            self.assertEqual(open_db.call_count, 1)
            with self.assertRaisesRegex(LibraryError, "Cannot open Serato database"):
                connect(path, writable=True)
            copy.assert_not_called()
            self.assertEqual(open_db.call_count, 2)

    def test_snapshot_rejects_source_changes_during_copy_and_cleans_temporary_files(self):
        path = self.base / "master.sqlite"
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE sample(name)")
        real_copy = __import__("shutil").copyfile
        copies = []

        def copy(source, destination):
            real_copy(source, destination)
            copies.append(destination)
            with path.open("ab") as output:
                output.write(b"changed")

        with patch("crate_transfer.library.sqlite3.connect",
                   side_effect=sqlite3.OperationalError("unable to open database file")), \
                patch("crate_transfer.library.assert_serato_closed"), \
                patch("crate_transfer.library.shutil.copyfile", side_effect=copy):
            with self.assertRaisesRegex(LibraryError, "changed while taking"):
                connect(path)
        self.assertEqual(len(copies), 1)
        self.assertFalse(copies[0].parent.exists())

    def test_read_only_wal_connection_includes_uncheckpointed_rows(self):
        path = self.base / "Library with spaces" / "master.sqlite"
        path.parent.mkdir()
        with closing(sqlite3.connect(path)) as writer:
            self.assertEqual(writer.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("CREATE TABLE sample(name TEXT)")
            writer.execute("INSERT INTO sample VALUES ('committed in WAL')")
            writer.commit()
            self.assertGreater(Path(str(path) + "-wal").stat().st_size, 0)
            with closing(connect(path)) as reader:
                self.assertEqual(reader.execute("SELECT name FROM sample").fetchone()[0], "committed in WAL")
                with self.assertRaises(sqlite3.OperationalError):
                    reader.execute("INSERT INTO sample VALUES ('must not write')")

    def test_export_parent_and_child_preserves_order_bytes_and_excludes_unrelated(self):
        library, volume, contents, assets = self.make_source()
        manifest, paths = library.export(
            [10, 11], "11111111-1111-4111-8111-111111111111", "Sender"
        )
        self.assertEqual(
            [crate["path"] for crate in manifest["crates"]],
            [["Selected"], ["Selected", "Child"]],
        )
        records = {record["name"]: record for record in manifest["files"]}
        self.assertEqual(
            manifest["crates"][0]["tracks"],
            [records["two.mp3"]["id"], records["one.mp3"]["id"],
             next(r["id"] for r in manifest["files"] if r["sha256"] == hashlib.sha256(
                 contents["A/collision.mp3"]).hexdigest()),
             next(r["id"] for r in manifest["files"] if r["sha256"] == hashlib.sha256(
                 contents["B/collision.mp3"]).hexdigest())],
        )
        self.assertEqual(
            manifest["crates"][1]["tracks"],
            [records["one.mp3"]["id"], records["two.mp3"]["id"]],
        )
        self.assertNotIn("Unrelated", [part for crate in manifest["crates"] for part in crate["path"]])
        self.assertEqual(len(paths), 4)
        self.assertEqual(paths[records["one.mp3"]["id"]].read_bytes(), contents["Music/one.mp3"])
        self.assertEqual(paths[records["two.mp3"]["id"]].read_bytes(), contents["Music/two.mp3"])
        self.assertEqual(records["one.mp3"]["size"], len(contents["Music/one.mp3"]))
        self.assertEqual(records["one.mp3"]["sha256"], hashlib.sha256(contents["Music/one.mp3"]).hexdigest())
        self.assertEqual(paths[records["one.mp3"]["id"]].parent.parent.resolve(), volume.resolve())

    def test_export_parent_plus_child_selection_does_not_duplicate_subcrate(self):
        library, _, _, _ = self.make_source()
        manifest, _ = library.export(
            [10, 11, 10], "11111111-1111-4111-8111-111111111111", "Sender"
        )
        self.assertEqual([crate["path"] for crate in manifest["crates"]],
                         [["Selected"], ["Selected", "Child"]])

    def test_export_rejects_empty_unknown_and_missing_media(self):
        library, volume, _, _ = self.make_source()
        for selected in ([], [999]):
            with self.subTest(case="invalid-selection"):
                with self.assertRaises(LibraryError):
                    library.export(selected, "11111111-1111-4111-8111-111111111111", "Sender")
        (volume / "Music/one.mp3").unlink()
        with self.assertRaisesRegex(LibraryError, "Missing or invalid track"):
            library.export([10], "11111111-1111-4111-8111-111111111111", "Sender")

    def test_crates_lists_nested_names_and_rejects_aggregate_schema_or_cycle(self):
        library, _, _, _ = self.make_source()
        self.assertEqual(
            [(crate["id"], crate["path"], crate["tracks"]) for crate in library.crates()],
            [(10, ["Selected"], 4), (11, ["Selected", "Child"], 2), (12, ["Unrelated"], 1)],
        )
        malformed = self.base / "malformed"
        malformed.mkdir()
        malformed_db = sqlite3.connect(malformed / "master.sqlite")
        malformed_db.execute("CREATE TABLE container(id)")
        malformed_db.commit()
        malformed_db.close()
        with self.assertRaisesRegex(LibraryError, "aggregate schema"):
            Library(malformed).crates()

        db = sqlite3.connect(library.directory / "master.sqlite")
        db.execute("UPDATE container SET parent_id=11 WHERE id=11")
        db.commit()
        db.close()
        with self.assertRaisesRegex(LibraryError, "cyclic crate hierarchy"):
            library.crates()

    def test_root_schema_requires_columns_singletons_space_and_root(self):
        bad_dir = self.base / "bad-root"
        bad_dir.mkdir()
        db = sqlite3.connect(bad_dir / "root.sqlite")
        db.execute("CREATE TABLE serato(revision INTEGER)")
        db.commit()
        db.close()
        with closing(connect(bad_dir / "root.sqlite")) as db:
            with self.assertRaisesRegex(LibraryError, "master is missing revision"):
                check_root(db)

        root_dir = self.base / "bad-root-cardinality"
        create_root_database(root_dir / "root.sqlite", keep_unrelated=False)
        db = sqlite3.connect(root_dir / "root.sqlite")
        db.execute("INSERT INTO serato VALUES (2)")
        db.commit()
        db.close()
        with closing(connect(root_dir / "root.sqlite")) as db:
            with self.assertRaisesRegex(LibraryError, "expected one serato"):
                check_root(db)

    def test_portable_paths_strip_platform_prefixes_and_reject_network_shares(self):
        self.assertEqual(portable_path("/Volumes/Audio/Music/a.mp3"), "Volumes/Audio/Music/a.mp3")
        self.assertEqual(portable_path(r"C:\Users\DJ\Music\a.mp3", windows=True),
                         "Users/DJ/Music/a.mp3")
        self.assertEqual(portable_path(r"\\?\C:\Music\a.mp3", windows=True), "Music/a.mp3")
        for network_path in (r"\\server\share\a.mp3", r"//server/share/a.mp3"):
            with self.subTest(case="network-path"):
                with self.assertRaisesRegex(LibraryError, "network share"):
                    portable_path(network_path, windows=True)

    def make_receiver(self):
        library_dir = self.base / "receiver-library"
        create_root_database(library_dir / "root.sqlite")
        # This aggregate is a pre-existing, unrelated cache and must not be rewritten.
        create_aggregate_database(library_dir / "master.sqlite", self.base, [], {})
        music = self.base / "receiver-music"
        music.mkdir()
        return Library(library_dir), library_dir, music

    def test_destination_rejects_cross_volume_overlap_and_file_paths(self):
        library, library_dir, music = self.make_receiver()
        library.validate_destination(music)
        with self.assertRaisesRegex(LibraryError, "separate"):
            library.validate_destination(library_dir / "music")
        file_path = self.base / "not-a-folder"
        file_path.write_bytes(b"existing file")
        with self.assertRaisesRegex(LibraryError, "not a file"):
            library.validate_destination(file_path)
        real_stat = os.stat
        resolved_music = music.resolve()

        def stat(path, **kwargs):
            if Path(path) == resolved_music:
                return SimpleNamespace(st_dev=-1, st_mode=real_stat(path, **kwargs).st_mode)
            return real_stat(path, **kwargs)

        with patch("crate_transfer.library.os.stat", side_effect=stat):
            with self.assertRaisesRegex(LibraryError, "same local volume"):
                library.validate_destination(music)

    def test_running_serato_guard_blocks_pro_lite_and_unverifiable_processes(self):
        for name in ("Serato DJ Pro", "Serato DJ Lite.exe"):
            with self.subTest(application=name):
                with patch("crate_transfer.library.psutil.process_iter", return_value=[
                    SimpleNamespace(info={"name": name}),
                ]):
                    with self.assertRaisesRegex(LibraryError, "Quit Serato"):
                        assert_serato_closed()
        with patch("crate_transfer.library.psutil.process_iter", return_value=[
            SimpleNamespace(info={"name": "Crate Transfer"}),
        ]):
            assert_serato_closed()

        class Unreadable:
            @property
            def info(self):
                raise psutil.AccessDenied()

        with patch("crate_transfer.library.psutil.process_iter", return_value=[Unreadable()]):
            with self.assertRaisesRegex(LibraryError, "Cannot check"):
                assert_serato_closed()

    def make_import(self, music):
        first = b"ID3\x00incoming-one"
        second = b"ID3\x04incoming-two"
        records = [file_record("collision.mp3", first), file_record("collision.mp3", second)]
        installed = {}
        for record, content in zip(records, (first, second)):
            path = music / record["id"] / record["name"]
            path.parent.mkdir(parents=True)
            path.write_bytes(content)
            installed[record["id"]] = path
        manifest = {
            "files": records,
            "crates": [
                {"path": ["Incoming"], "tracks": [records[1]["id"], records[0]["id"]]},
                {"path": ["Incoming", "Child"], "tracks": [records[0]["id"], records[1]["id"]]},
            ],
        }
        return manifest, installed, records, (first, second)

    def test_import_preserves_serato_collated_aggregate_and_its_backup_indexes(self):
        library, library_dir, music = self.make_receiver()
        manifest, installed, _, _ = self.make_import(music)
        master = library_dir / "master.sqlite"
        collation = "serato_asset_string_comparator"

        # Deliberately nonstandard ordering proves no guessed comparator or
        # rebuilt index is used by the importer.
        def reverse_compare(left, right):
            return (right > left) - (right < left)

        with closing(sqlite3.connect(master)) as db:
            db.create_collation(collation, reverse_compare)
            db.executescript(
                'CREATE TABLE "sort""cache"(id INTEGER PRIMARY KEY, '
                f'label TEXT COLLATE {collation}, payload BLOB);'
                f'CREATE INDEX custom_sort ON "sort""cache"(label COLLATE {collation});'
            )
            rows = [(i, f"Track {i}", bytes([i % 256])) for i in range(257)]
            rows.append((257, None, b"\x00\xff"))
            db.executemany('INSERT INTO "sort""cache" VALUES (?,?,?)', rows)
            db.commit()
            expected_order = db.execute(
                'SELECT id FROM "sort""cache" ORDER BY label'
            ).fetchall()
        master_bytes = master.read_bytes()
        before = root_snapshot(library_dir / "root.sqlite")
        backup_dir = self.base / "collated-backup"
        with self.assertLogs("crate_transfer.library", level="WARNING") as logs:
            result = library.import_crates(manifest, installed, backup_dir, "collated",
                                           guard=lambda: None)
        self.assertIn("Serato-only index ordering cannot be checked", logs.output[0])
        self.assertEqual(result["memberships"], 4)
        self.assertEqual(root_snapshot(library_dir / "root.sqlite")["revision"], (2, 2))
        self.assertEqual(root_snapshot(backup_dir / "root.sqlite"), before)
        self.assertEqual(master.read_bytes(), master_bytes)
        with closing(sqlite3.connect(backup_dir / "master.sqlite")) as db:
            db.create_collation(collation, reverse_compare)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertEqual(db.execute('SELECT * FROM "sort""cache" NOT INDEXED').fetchall(), rows)
            self.assertEqual(db.execute(
                'SELECT id FROM "sort""cache" ORDER BY label'
            ).fetchall(), expected_order)

    def test_unknown_root_collation_stops_import_before_any_library_changes(self):
        library, library_dir, music = self.make_receiver()
        manifest, installed, _, _ = self.make_import(music)
        root = library_dir / "root.sqlite"
        with closing(sqlite3.connect(root)) as db:
            db.create_collation("serato_asset_string_comparator",
                                lambda left, right: (left > right) - (left < right))
            db.execute("CREATE INDEX root_custom_sort ON asset("
                       "file_name COLLATE serato_asset_string_comparator)")
            db.commit()
        before = root.read_bytes()
        master_before = (library_dir / "master.sqlite").read_bytes()
        with self.assertRaisesRegex(LibraryError, "Cannot validate root.sqlite backup"):
            library.import_crates(manifest, installed, self.base / "unsupported-backup",
                                  "unsupported", guard=lambda: None)
        self.assertEqual(root.read_bytes(), before)
        self.assertEqual((library_dir / "master.sqlite").read_bytes(), master_before)

    def test_backup_comparison_rejects_schema_changed_rows_and_missing_or_extra_rows(self):
        with closing(sqlite3.connect(":memory:")) as source:
            source.row_factory = sqlite3.Row
            source.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, data BLOB)")
            source.executemany("INSERT INTO sample VALUES (?,?)",
                               [(i, bytes([i % 256])) for i in range(257)])
            source.commit()
            for mutation in (
                "ALTER TABLE sample ADD COLUMN extra",
                "UPDATE sample SET data=X'ff00' WHERE id=200",
                "DELETE FROM sample WHERE id=256",
                "INSERT INTO sample VALUES (257, X'01')",
            ):
                with self.subTest(mutation=mutation), closing(sqlite3.connect(":memory:")) as target:
                    source.backup(target)
                    target.execute(mutation)
                    with self.assertRaisesRegex(LibraryError, "backup .* differs? from its source"):
                        verify_backup_contents(source, target)

    def test_master_backup_comparison_uses_the_same_wal_snapshot_as_backup(self):
        path = self.base / "master.sqlite"
        with closing(sqlite3.connect(path)) as writer:
            writer.create_collation("serato_asset_string_comparator",
                                    lambda left, right: (left > right) - (left < right))
            writer.execute("PRAGMA journal_mode=WAL")
            writer.executescript(
                "CREATE TABLE sample(label TEXT);"
                "CREATE INDEX custom_sort ON sample(label COLLATE serato_asset_string_comparator);"
                "INSERT INTO sample VALUES ('original');"
            )
            with closing(connect(path)) as source, closing(sqlite3.connect(":memory:")) as target:
                def change_original_then_compare(original, copied):
                    writer.execute("UPDATE sample SET label='later'")
                    writer.commit()
                    verify_backup_contents(original, copied)

                with patch("crate_transfer.library.verify_backup_contents",
                           side_effect=change_original_then_compare), \
                        self.assertLogs("crate_transfer.library", level="WARNING"):
                    backup_database(source, target, "master.sqlite")
                self.assertEqual(target.execute("SELECT label FROM sample").fetchall(), [("original",)])
                self.assertEqual(writer.execute("SELECT label FROM sample").fetchall(), [("later",)])

    def test_master_backup_does_not_ignore_other_integrity_errors(self):
        for error in ("database disk image is malformed", "disk I/O error",
                      "no such function: serato_str_norm"):
            with self.subTest(error=error):
                source, target = Mock(), Mock()
                target.execute.side_effect = sqlite3.OperationalError(error)
                with patch("crate_transfer.library.verify_backup_contents") as compare:
                    with self.assertRaisesRegex(LibraryError, "Cannot validate master.sqlite backup"):
                        backup_database(source, target, "master.sqlite")
                compare.assert_not_called()
        source, target = Mock(), Mock()
        target.execute.return_value.fetchall.return_value = [("index contains wrong entries",)]
        with self.assertRaisesRegex(LibraryError, "master.sqlite backup failed its integrity check"):
            backup_database(source, target, "master.sqlite")

    def test_import_backs_up_both_databases_bumps_revisions_and_preserves_unrelated_cache(self):
        library, library_dir, music = self.make_receiver()
        manifest, installed, records, contents = self.make_import(music)
        before = root_snapshot(library_dir / "root.sqlite")
        master_bytes = (library_dir / "master.sqlite").read_bytes()
        backup_dir = self.base / "backups" / "transfer-a"
        result = library.import_crates(manifest, installed, backup_dir, "transfer-a",
                                       guard=lambda: None)
        after = root_snapshot(library_dir / "root.sqlite")

        self.assertEqual(result["crates"], 2)
        self.assertEqual(result["files"], 2)
        self.assertEqual(result["memberships"], 4)
        self.assertEqual(after["revision"][0], before["revision"][0] + 1)
        self.assertEqual(after["revision"][1], before["revision"][1] + 1)
        self.assertIn((2, 1, "Keep Existing", 1, 1, 1), after["containers"])
        self.assertIn((1, "Existing/keep.mp3", "keep.mp3", 4), after["assets"])
        self.assertEqual((library_dir / "master.sqlite").read_bytes(), master_bytes)
        self.assertEqual(root_snapshot(backup_dir / "root.sqlite"), before)
        backup_master = sqlite3.connect(backup_dir / "master.sqlite")
        self.assertEqual(backup_master.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(backup_master.execute("SELECT count(*) FROM container").fetchone()[0], 1)
        self.assertEqual(backup_master.execute("SELECT count(*) FROM connection").fetchone()[0], 1)
        backup_master.close()

        received = [row for row in after["containers"] if row[2] in ("Incoming", "Child")]
        self.assertEqual([row[2] for row in received], ["Incoming", "Child"])
        db = sqlite3.connect(library_dir / "root.sqlite")
        imported_assets = db.execute(
            "SELECT id,portable_id,file_name FROM asset WHERE id>1 ORDER BY id"
        ).fetchall()
        imported_memberships = db.execute(
            "SELECT c.name,a.portable_id,ca.list_order FROM container_asset ca "
            "JOIN container c ON c.id=ca.container_id "
            "JOIN space_asset sa ON sa.id=ca.space_asset_id "
            "JOIN asset a ON a.id=sa.asset_id WHERE c.name IN ('Incoming','Child') "
            "ORDER BY c.name,ca.list_order"
        ).fetchall()
        db.close()
        self.assertEqual(len(imported_assets), 2)
        self.assertEqual({row[1] for row in imported_assets},
                         {portable_path(str(path.resolve()), sys.platform == "win32") for path in installed.values()})
        expected_paths = {record["id"]: portable_path(str(installed[record["id"]].resolve()), sys.platform == "win32")
                          for record in records}
        self.assertEqual(
            [(row[0], row[1]) for row in imported_memberships],
            [("Child", expected_paths[records[0]["id"]]),
             ("Child", expected_paths[records[1]["id"]]),
             ("Incoming", expected_paths[records[1]["id"]]),
             ("Incoming", expected_paths[records[0]["id"]])],
        )
        self.assertEqual([installed[r["id"]].read_bytes() for r in records], list(contents))
        self.assertNotEqual(records[0]["id"], records[1]["id"])

    def test_import_guard_and_transaction_failure_leave_database_unchanged(self):
        library, library_dir, music = self.make_receiver()
        manifest, installed, _, _ = self.make_import(music)
        before = root_snapshot(library_dir / "root.sqlite")
        first_backup = self.base / "backups" / "guarded"
        def refuse():
            raise LibraryError("Serato is running")
        with self.assertRaisesRegex(LibraryError, "Serato is running"):
            library.import_crates(manifest, installed, first_backup, "guarded", guard=refuse)
        self.assertFalse(first_backup.exists())
        self.assertEqual(root_snapshot(library_dir / "root.sqlite"), before)

        calls = []
        def refuse_before_commit():
            calls.append(None)
            if len(calls) == 3:
                raise LibraryError("Serato started during import")
        with self.assertRaisesRegex(LibraryError, "started during import"):
            library.import_crates(manifest, installed, self.base / "backups" / "rollback",
                                  "rollback", guard=refuse_before_commit)
        self.assertEqual(len(calls), 3)
        self.assertEqual(root_snapshot(library_dir / "root.sqlite"), before)

    def test_import_rejects_invalid_root_schema_before_creating_backup(self):
        library_dir = self.base / "invalid-library"
        library_dir.mkdir()
        db = sqlite3.connect(library_dir / "root.sqlite")
        db.execute("CREATE TABLE serato(revision INTEGER)")
        db.commit()
        db.close()
        (library_dir / "master.sqlite").write_bytes(b"untouched-cache")
        library = Library(library_dir)
        with self.assertRaises(LibraryError):
            library.import_crates({}, {}, self.base / "bad-backup", "bad", guard=lambda: None)
        self.assertFalse((self.base / "bad-backup").exists())
        self.assertEqual((library_dir / "master.sqlite").read_bytes(), b"untouched-cache")


if __name__ == "__main__":
    unittest.main()
