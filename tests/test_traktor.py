"""Synthetic NML fixtures; no real user libraries."""
import hashlib
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from crate_transfer.library import LibraryError
from crate_transfer.protocol import PROTOCOL
from crate_transfer.traktor import TraktorLibrary, assert_traktor_closed

SENDER = "11111111-1111-4111-8111-111111111111"


def make_traktor_library(path):
    path = Path(path)
    if path.name != "collection.nml":
        path = path / "collection.nml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?><NML VERSION="20">'
        '<HEAD PROGRAM="Traktor Pro 4"/><COLLECTION ENTRIES="0" custom="keep"/>'
        '<PLAYLISTS><NODE TYPE="FOLDER" NAME="$ROOT"><SUBNODES COUNT="0"/>'
        '</NODE></PLAYLISTS><UNKNOWN value="preserve"/></NML>', encoding="utf-8")
    return path


def record(path, **metadata):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"id": hashlib.sha256((digest + "\0" + path.name).encode()).hexdigest(),
            "sha256": digest, "name": path.name, "size": path.stat().st_size, "metadata": metadata}


def manifest(item):
    return {"protocol": PROTOCOL, "destination": {"format": "serato"},
            "sender_id": SENDER, "sender_name": "Synthetic", "files": [item],
            "crates": [{"path": ["Sets", "Night"], "tracks": [item["id"], item["id"]]}]}


class TraktorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=".")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.path = make_traktor_library(self.base / "Traktor")
        self.library = TraktorLibrary(self.path.parent)
        self.media = self.base / "Music" / "track.mp3"
        self.media.parent.mkdir()
        self.media.write_bytes(b"synthetic audio")
        self.item = record(self.media, title="Title", artist="Artist", album="Album", bpm=120,
                           cues=[{"kind": "loop", "position": 1.5, "end": 3.5, "slot": 0}])
        self.manifest = manifest(self.item)
        self.patcher = patch("crate_transfer.traktor.assert_traktor_closed")
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def run_import(self, backup="backup", guard=lambda: None):
        return self.library.import_crates(self.manifest, {self.item["id"]: self.media},
                                          self.base / backup, "transfer", guard)

    def test_import_backup_roundtrip_and_collision(self):
        before = self.path.read_bytes()
        result = self.run_import()
        self.assertEqual((result["files"], result["crates"], result["memberships"]), (1, 1, 2))
        self.assertEqual((Path(result["backup"]) / "collection.nml").read_bytes(), before)
        self.assertEqual(self.library.backup_paths, [self.path])
        root = ET.parse(self.path).getroot()
        self.assertEqual(root.find("UNKNOWN").get("value"), "preserve")
        self.assertEqual(root.find("COLLECTION").get("ENTRIES"), "1")
        cue = root.find("./COLLECTION/ENTRY/CUE_V2")
        self.assertEqual((cue.get("TYPE"), float(cue.get("START")), float(cue.get("LEN"))), ("5", 1500, 2000))
        playlist = root.find(".//PLAYLIST")
        self.assertEqual(len(playlist.get("UUID")), 32)
        loc = root.find("./COLLECTION/ENTRY/LOCATION")
        self.assertEqual(playlist.find("./ENTRY/PRIMARYKEY").get("KEY"),
                         loc.get("VOLUME", "") + loc.get("DIR") + loc.get("FILE"))
        if __import__("sys").platform == "darwin":
            self.assertTrue(loc.get("VOLUME"))
        crates = self.library.crates()
        self.assertEqual(crates[0]["path"], ["Sets", "Night"])
        exported, sources = self.library.export([crates[0]["id"]], SENDER, "Sender")
        self.assertEqual(len(exported["crates"][0]["tracks"]), 2)
        self.assertEqual(exported["files"][0]["metadata"]["cues"][0]["end"], 3.5)
        self.assertEqual(next(iter(sources.values())), self.media)
        self.run_import("backup2")
        names = [c["path"] for c in self.library.crates()]
        self.assertIn(["Sets (2)", "Night"], names)
        self.assertEqual(len(ET.parse(self.path).getroot().findall("./COLLECTION/ENTRY")), 1)

    def test_guard_and_rollback_preserve_original(self):
        before = self.path.read_bytes()
        calls = []
        def guard():
            calls.append(1)
            if len(calls) == 2:
                raise LibraryError("cancelled")
        with self.assertRaisesRegex(LibraryError, "cancelled"):
            self.run_import(guard=guard)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual((self.base / "backup" / self.path.name).read_bytes(), before)

    def test_changed_library_and_schema_refused(self):
        def guard():
            if (self.base / "backup").exists():
                self.path.write_bytes(self.path.read_bytes() + b"\n")
        with self.assertRaisesRegex(LibraryError, "changed"):
            self.run_import(guard=guard)
        self.assertTrue(self.path.read_bytes().endswith(b"\n"))
        self.path.write_text('<NML VERSION="99"/>')
        with self.assertRaisesRegex(LibraryError, "Unsupported"):
            self.library.validate_destination(self.media.parent)

    def test_missing_and_checksum_refused(self):
        with self.assertRaises(LibraryError):
            TraktorLibrary(self.base / "missing.nml").validate_destination(self.media.parent)
        self.media.write_bytes(b"tampered")
        with self.assertRaisesRegex(LibraryError, "checksum"):
            self.run_import()
        self.assertFalse((self.base / "backup").exists())

    def test_smart_playlists_are_preserved_on_import_and_omitted_from_source(self):
        root = ET.parse(self.path).getroot()
        top = root.find("./PLAYLISTS/NODE/SUBNODES")
        folder = ET.SubElement(top, "NODE", TYPE="FOLDER", NAME="Existing")
        children = ET.SubElement(folder, "SUBNODES", COUNT="1")
        smart_node = ET.SubElement(children, "NODE", TYPE="SMARTLIST", NAME="Smart")
        smart = ET.SubElement(smart_node, "SMARTLIST", UUID="smart-uuid")
        ET.SubElement(smart, "SEARCH_EXPRESSION", VERSION="1", QUERY='$COMMENT % "EPIC"')
        top.set("COUNT", "1")
        ET.ElementTree(root).write(self.path, encoding="utf-8", xml_declaration=True)
        original_smart = ET.tostring(smart_node)

        self.assertEqual(self.library.crates(), [])
        self.assertTrue(any("smart playlists were omitted" in warning for warning in self.library.warnings))
        result = self.run_import()
        self.assertTrue(any("smart playlists were preserved" in warning for warning in result["warnings"]))
        updated = ET.parse(self.path).getroot()
        self.assertEqual(ET.tostring(updated.find(".//NODE[@TYPE='SMARTLIST']")), original_smart)
        self.assertEqual(self.library.crates()[0]["path"], ["Sets", "Night"])

    def test_unsupported_node_and_malformed_smart_playlist_are_refused(self):
        root = ET.parse(self.path).getroot()
        top = root.find("./PLAYLISTS/NODE/SUBNODES")
        node = ET.SubElement(top, "NODE", TYPE="UNKNOWN", NAME="Private name")
        ET.ElementTree(root).write(self.path, encoding="utf-8", xml_declaration=True)
        with self.assertRaisesRegex(LibraryError, "TYPE='UNKNOWN', NAME=present"):
            self.library.validate_destination(self.media.parent)
        node.set("TYPE", "SMARTLIST")
        ET.ElementTree(root).write(self.path, encoding="utf-8", xml_declaration=True)
        with self.assertRaisesRegex(LibraryError, "smart playlist has no SMARTLIST"):
            self.library.validate_destination(self.media.parent)

    def test_running_traktor_is_refused(self):
        with patch("crate_transfer.traktor.psutil.process_iter",
                   return_value=[SimpleNamespace(info={"name": "Traktor Pro"})]):
            with self.assertRaisesRegex(LibraryError, "Quit Traktor"):
                assert_traktor_closed()
