import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crate_transfer.catalog import (default_destination_path, default_source_paths, find_source,
                                    OUTPUT_NAMES, source_definition, source_exists, source_guidance)


class CatalogTests(unittest.TestCase):
    def test_traktor_and_rekordbox_are_not_labeled_preview(self):
        for format in ("traktor", "rekordbox-native"):
            self.assertNotIn("preview", source_definition(format).name.lower())
            self.assertNotIn("preview", OUTPUT_NAMES[format].lower())
            self.assertNotIn("preview", source_guidance(format).lower())
        self.assertIn("preview", source_definition("engine").name.lower())
        self.assertIn("preview", OUTPUT_NAMES["engine"].lower())
        self.assertIn("preview", source_guidance("engine").lower())

    def test_rekordbox_destination_default_is_user_specific_and_macos_only(self):
        home = Path("/synthetic-home")
        self.assertEqual(default_destination_path("rekordbox-native", home, "darwin"),
                         str(home / "Library/Pioneer/rekordbox"))
        self.assertEqual(default_destination_path("rekordbox-native", home, "win32"), "")

    def test_default_paths_are_platform_specific_without_accessing_any_libraries(self):
        home = Path("/synthetic-home")
        self.assertEqual(default_source_paths("serato", home, "darwin"),
                         [home / "Library/Application Support/Serato/Library"])
        self.assertEqual(default_source_paths("rekordbox-native", home, "win32"),
                         [home / "AppData/Roaming/Pioneer/rekordbox/master.db"])
        self.assertEqual(default_source_paths("engine", home, "darwin"),
                         [home / "Music/Engine Library/Database2"])
        self.assertEqual(default_source_paths("rekordbox-xml", home, "darwin"), [])
        self.assertEqual(default_source_paths("serato3", home, "darwin"), [home / "Music/_Serato_"])
        self.assertIn(home / "Library/Application Support/Mixxx/mixxxdb.sqlite",
                      default_source_paths("mixxx", home, "darwin"))

    def test_source_detection_only_checks_explicit_candidate_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            found = directory / "database.xml"
            found.write_text("not parsed by discovery", encoding="utf-8")
            with patch("crate_transfer.catalog.default_source_paths", return_value=[directory / "missing", found]):
                self.assertEqual(find_source("virtualdj-native"), found)
            self.assertTrue(source_exists("virtualdj-native", str(found)))
            self.assertFalse(source_exists("virtualdj-native", str(directory / "missing")))

    def test_serato_path_check_accepts_library_or_parent_without_opening_databases(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            library = parent / "Library"
            library.mkdir()
            self.assertFalse(source_exists("serato", str(parent)))
            for name in ("root.sqlite", "master.sqlite"):
                (library / name).write_bytes(b"contents are not inspected")
            self.assertTrue(source_exists("serato", str(parent)))
            self.assertTrue(source_exists("serato", str(library)))

    def test_serato3_detection_checks_legacy_folder_not_serato4_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "_Serato_"
            (legacy / "Subcrates").mkdir(parents=True)
            self.assertTrue(source_exists("serato3", str(legacy)))
            self.assertTrue(source_exists("serato3", str(legacy / "Subcrates")))
            self.assertFalse(source_exists("serato", str(legacy)))
            self.assertIn("not DJ 4", source_guidance("serato3"))

    def test_close_app_guidance_is_source_specific(self):
        self.assertIn("Close Traktor", source_guidance("traktor-nml"))
        self.assertIn("need not be closed", source_guidance("rekordbox-xml"))
        self.assertIn("snapshot", source_guidance("serato"))

    def test_traktor_discovery_uses_numeric_version_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            native = home / "Documents/Native Instruments"
            for version in ("4.9", "4.10"):
                folder = native / ("Traktor " + version)
                folder.mkdir(parents=True)
                (folder / "collection.nml").write_text("not opened during detection", encoding="utf-8")
            with patch("crate_transfer.catalog.Path.home", return_value=home):
                self.assertEqual(find_source("traktor-nml"), native / "Traktor 4.10/collection.nml")

    def test_traktor_destination_suggests_newest_existing_collection(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            native = home / "Documents/Native Instruments"
            self.assertEqual(default_destination_path("traktor", home, "darwin"), "")
            for version in ("4.4.1", "4.5.0"):
                folder = native / ("Traktor " + version)
                folder.mkdir(parents=True)
                (folder / "collection.nml").write_text("not parsed", encoding="utf-8")
            (native / "Traktor 5.0/collection.nml").mkdir(parents=True)
            expected = native / "Traktor 4.5.0/collection.nml"
            self.assertEqual(default_destination_path("traktor", home, "darwin"), str(expected))
            self.assertEqual(default_destination_path("traktor", home, "win32"), str(expected))
            expected.unlink()
            self.assertEqual(default_destination_path("traktor", home, "darwin"),
                             str(native / "Traktor 4.4.1/collection.nml"))
