"""Synthetic VirtualDJ XML only."""
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from crate_transfer.library import LibraryError
from crate_transfer.virtualdj import VirtualDJLibrary, assert_virtualdj_closed
from tests.test_traktor import SENDER, manifest, record


def make_virtualdj_library(path):
    path = Path(path)
    if path.name != "database.xml":
        path = path / "database.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('<?xml version="1.0" encoding="UTF-8"?>'
                    '<VirtualDJ_Database Version="8.5"><Unknown Value="preserve"/>'
                    '</VirtualDJ_Database>', encoding="utf-8")
    return path


class VirtualDJTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=".")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.path = make_virtualdj_library(self.base / "VirtualDJ")
        self.library = VirtualDJLibrary(self.path.parent)
        self.media = self.base / "Music" / "track.mp3"
        self.media.parent.mkdir()
        self.media.write_bytes(b"synthetic audio")
        self.item = record(self.media, title="Title", artist="Artist", bpm=120,
                           cues=[{"kind": "loop", "position": 1, "end": 3, "slot": 1},
                                 {"kind": "cue", "position": 0.5, "slot": 0}])
        self.manifest = manifest(self.item)
        self.patcher = patch("crate_transfer.virtualdj.assert_virtualdj_closed")
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def run_import(self, backup="backup", guard=lambda: None):
        return self.library.import_crates(self.manifest, {self.item["id"]: self.media},
                                          self.base / backup, "transfer", guard)

    def test_import_backup_and_readback(self):
        before = self.path.read_bytes()
        result = self.run_import()
        self.assertEqual((result["files"], result["crates"], result["memberships"]), (1, 1, 2))
        self.assertEqual((Path(result["backup"]) / "database.xml").read_bytes(), before)
        self.assertEqual(self.library.backup_paths, [self.path])
        root = ET.parse(self.path).getroot()
        self.assertEqual(root.find("Unknown").get("Value"), "preserve")
        song = root.find("Song")
        self.assertEqual(float(song.find("Tags").get("Bpm")), 0.5)
        self.assertEqual(float(song.find("Poi").get("Size")), 4)
        listing = self.path.parent / "MyLists" / "Sets" / "Night.vdjfolder"
        self.assertEqual(len(ET.parse(listing).getroot().findall("song")), 2)
        crates = self.library.crates()
        self.assertEqual(crates[0]["path"], ["MyLists", "Sets", "Night"])
        exported, _ = self.library.export([crates[0]["id"]], SENDER, "Sender")
        self.assertEqual(len(exported["crates"][0]["tracks"]), 2)
        self.assertEqual(exported["files"][0]["metadata"]["cues"][0]["end"], 3)
        self.run_import("backup2")
        self.assertTrue((self.path.parent / "MyLists" / "Sets (2)" / "Night.vdjfolder").exists())
        self.assertEqual(len(ET.parse(self.path).getroot().findall("Song")), 1)

    def test_older_folders_and_rollback(self):
        (self.path.parent / "Folders").mkdir()
        before = self.path.read_bytes()
        with patch("crate_transfer.traktor.os.replace", side_effect=OSError("publication failure")):
            with self.assertRaisesRegex(OSError, "publication failure"):
                self.run_import()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list((self.path.parent / "Folders").iterdir()), [])
        self.run_import("backup2")
        self.assertTrue((self.path.parent / "Folders" / "Sets" / "Night.vdjfolder").exists())

    def test_rejects_bad_schema_and_media(self):
        self.media.write_bytes(b"wrong")
        with self.assertRaisesRegex(LibraryError, "checksum"):
            self.run_import()
        self.path.write_text('<VirtualDJ_Database Version="7"/>')
        with self.assertRaisesRegex(LibraryError, "Unsupported"):
            self.library.validate_destination(self.media.parent)

    def test_running_virtualdj_is_refused(self):
        with patch("crate_transfer.traktor.psutil.process_iter",
                   return_value=[SimpleNamespace(info={"name": "VirtualDJ.exe"})]):
            with self.assertRaisesRegex(LibraryError, "Quit VirtualDJ"):
                assert_virtualdj_closed()
