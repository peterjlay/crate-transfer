import hashlib
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

from crate_transfer.exchange import ExchangeLibrary, write_exchange, _parse_m3u_line
from crate_transfer.protocol import PROTOCOL


def record(name, content, metadata=None):
    digest = hashlib.sha256(content).hexdigest()
    result = {"id": hashlib.sha256((digest + "\0" + name).encode()).hexdigest(),
              "sha256": digest, "name": name, "size": len(content)}
    if metadata:
        result["metadata"] = metadata
    return result


class ExchangeTests(unittest.TestCase):
    def test_windows_m3u_local_paths_and_file_uris_do_not_become_relative_or_remote(self):
        playlist = PureWindowsPath("D:/Playlists/Set.m3u")
        with patch("crate_transfer.exchange.os.name", "nt"), \
                patch("crate_transfer.exchange.Path", PureWindowsPath):
            for raw in ("C:\\Music\\Track #1.mp3", "C:/Music/Track #1.mp3",
                        "file:///C:/Music/Track%20%231.mp3", "file://localhost/C:/Music/Track%20%231.mp3"):
                self.assertEqual(_parse_m3u_line(raw, playlist), PureWindowsPath("C:/Music/Track #1.mp3"))
            self.assertEqual(_parse_m3u_line("../Music/song.mp3", playlist),
                             playlist.parent / "../Music/song.mp3")
            for raw in ("file:relative.mp3", "https://remote/song.mp3", "file://remote/C:/Music/song.mp3"):
                with self.subTest(raw=raw), self.assertRaises(ValueError):
                    _parse_m3u_line(raw, playlist)
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.media = self.work / "media"
        self.media.mkdir()
        self.first = self.media / "Mix Track.mp3"
        self.first.write_bytes(b"synthetic audio one")
        self.second_dir = self.media / "Other"
        self.second_dir.mkdir()
        self.second = self.second_dir / "Mix Track.mp3"
        self.second.write_bytes(b"synthetic audio two")

    def write(self, name, text):
        path = self.work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_m3u_relative_and_file_uri_lines_share_files_and_keep_order(self):
        playlist = self.write(
            "Set.m3u8",
            "#EXTM3U\nmedia/Mix%20Track.mp3\n" + self.second.as_uri() + "\n"
            "media/Mix%20Track.mp3\n",
        )
        library = ExchangeLibrary("m3u", playlist)
        self.assertEqual(library.crates(), [{"id": 1, "path": ["Set"], "tracks": 3}])
        manifest, paths = library.export([1], "11111111-1111-4111-8111-111111111111", "Sender")
        self.assertEqual(manifest["protocol"], PROTOCOL)
        self.assertEqual(manifest["destination"], {"format": "serato"})
        self.assertEqual(len(manifest["files"]), 2)
        self.assertEqual(manifest["crates"][0]["tracks"][0], manifest["crates"][0]["tracks"][2])
        self.assertEqual({path.resolve() for path in paths.values()}, {self.first.resolve(), self.second.resolve()})
        self.assertNotIn(str(self.work.resolve()), str(manifest))

    def test_recursive_m3u_directory_preserves_relative_hierarchy_and_selection_descendants(self):
        self.write("playlists/Live.m3u", "../media/Mix Track.mp3\n")
        self.write("playlists/Live/Opening.m3u8", "../../media/Other/Mix Track.mp3\n")
        library = ExchangeLibrary("m3u", self.work / "playlists")
        self.assertEqual([item["path"] for item in library.crates()],
                         [["Live"], ["Live", "Opening"]])
        manifest, _ = library.export([1], "11111111-1111-4111-8111-111111111111", "Sender")
        self.assertEqual([item["path"] for item in manifest["crates"]],
                         [["Live"], ["Live", "Opening"]])

    def test_rekordbox_reader_and_writer_roundtrip_locations_order_cues_loops_and_grid(self):
        xml = f"""<?xml version="1.0"?>
<DJ_PLAYLISTS Version="1.0.0">
 <COLLECTION Entries="2">
  <TRACK TrackID="7" Name="Mix" Artist="Artist" Album="Album" AverageBpm="128"
   Location="{self.first.as_uri()}">
   <TEMPO Inizio="0.5" Bpm="128" Metro="4/4" Battito="1"/>
   <TEMPO Inizio="1.0" Bpm="129" Metro="4/4" Battito="2"/>
   <POSITION_MARK Name="Hot cue" Type="0" Start="2.25" Num="3"/>
   <POSITION_MARK Name="Loop" Type="4" Start="8" End="10" Num="1"/>
  </TRACK>
  <TRACK TrackID="8" Name="Second" Location="{self.second.as_uri()}"/>
 </COLLECTION>
 <PLAYLISTS><NODE Type="0" Name="ROOT" Count="2">
  <NODE Type="0" Name="Folder" Count="2">
   <NODE Type="1" Name="First" Entries="2" KeyType="0"><TRACK Key="7"/><TRACK Key="8"/></NODE>
   <NODE Type="1" Name="Second" Entries="1" KeyType="0"><TRACK Key="7"/></NODE>
  </NODE>
 </NODE></PLAYLISTS>
</DJ_PLAYLISTS>"""
        source = self.write("source.xml", xml)
        library = ExchangeLibrary("rekordbox-xml", source)
        self.assertEqual([item["path"] for item in library.crates()],
                         [["Folder", "First"], ["Folder", "Second"]])
        manifest, paths = library.export([1], "11111111-1111-4111-8111-111111111111", "Sender")
        self.assertEqual([item["path"] for item in manifest["crates"]], [["Folder", "First"]])
        self.assertEqual(len(manifest["files"]), 2)
        first_record = next(item for item in manifest["files"] if item["name"] == self.first.name)
        metadata = first_record["metadata"]
        metadata["key"] = "8A"
        self.assertEqual(metadata["title"], "Mix")
        self.assertEqual(metadata["artist"], "Artist")
        self.assertEqual(metadata["cues"], [
            {"kind": "cue", "position": 2.25, "name": "Hot cue", "slot": 3},
            {"kind": "loop", "position": 8.0, "name": "Loop", "slot": 1, "end": 10.0},
        ])
        self.assertEqual(metadata["beatgrid"], [
            {"position": 0.5, "bpm": 128.0, "beat": 1},
            {"position": 1.0, "bpm": 129.0, "beat": 2},
        ])
        output_dir = self.work / "rb-out"
        output_dir.mkdir()
        final_paths = {file_id: self.work / "staged" / file_id / Path(paths[file_id]).name
                       for file_id in paths}
        output = write_exchange("rekordbox-xml", manifest, final_paths, output_dir)
        self.assertEqual(output.name, "rekordbox.xml")
        self.assertIn("Mix%20Track.mp3", output.read_text(encoding="utf-8"))
        parsed = ET.parse(output).getroot()
        playlist_node = parsed.find("./PLAYLISTS/NODE/NODE/NODE")
        self.assertEqual(playlist_node.get("Type"), "1")
        self.assertEqual(playlist_node.get("Entries"), "2")
        self.assertEqual(playlist_node.get("KeyType"), "0")
        self.assertEqual(parsed.find("./COLLECTION/TRACK/TEMPO").get("Inizio"), "0.500000")
        self.assertEqual(parsed.find("./COLLECTION/TRACK/POSITION_MARK").get("Start"), "2.250000")
        loop = next(mark for mark in parsed.findall("./COLLECTION/TRACK/POSITION_MARK")
                    if mark.get("Type") == "4")
        self.assertEqual(loop.get("End"), "10.000000")
        self.assertIn("omitted per-track musical key", " ".join(manifest["warnings"]))
        for file_id, path in final_paths.items():
            path.parent.mkdir(parents=True)
            path.write_bytes(paths[file_id].read_bytes())
        reread, _ = ExchangeLibrary("rekordbox-xml", output).export(
            [1], "11111111-1111-4111-8111-111111111111", "Sender")
        reread_track = reread["files"][0]
        self.assertEqual(reread_track["metadata"]["cues"], metadata["cues"])
        self.assertEqual(reread_track["metadata"]["beatgrid"], metadata["beatgrid"])
        self.assertNotIn("key", reread_track["metadata"])

    def test_traktor_nml_primary_keys_volume_paths_and_loop_grid_roundtrip(self):
        source = self.write("collection.nml", f"""<?xml version="1.0"?>
<NML VERSION="19">
 <COLLECTION ENTRIES="1"><ENTRY AUDIO_ID="42" TITLE="Mix" ARTIST="Artist"
  ALBUM="Album">
  <LOCATION DIR="/:{'/:'.join(self.second.absolute().parts[1:-1])}/:" FILE="Mix Track.mp3" VOLUME="{self.second.drive}"/>
  <INFO BPM="128" KEY="8A"/>
  <TEMPO BPM="128" BPM_QUALITY="100.000000"/>
  <CUE_V2 TYPE="0" NAME="Cue" START="2500" LEN="0" HOTCUE="2"/>
  <CUE_V2 TYPE="5" NAME="Loop" START="8000" LEN="2000" HOTCUE="1"/>
  <CUE_V2 TYPE="4" NAME="Grid" START="500" LEN="0" HOTCUE="-1"/>
 </ENTRY></COLLECTION>
 <SETS><NODE TYPE="FOLDER" NAME="Playlists"><SUBNODES COUNT="1">
  <NODE TYPE="FOLDER" NAME="Folder"><SUBNODES COUNT="1">
   <NODE TYPE="PLAYLIST" NAME="Set" UUID=""><PLAYLIST ENTRIES="1">
    <ENTRY><PRIMARYKEY TYPE="TRACK" KEY="{self.second.drive}/:{'/:'.join(self.second.absolute().parts[1:])}"/></ENTRY>
   </PLAYLIST></NODE>
  </SUBNODES></NODE>
 </SUBNODES></NODE></SETS>
</NML>""")
        original_dir = self.media / "Other"
        original_dir.mkdir(exist_ok=True)
        source_media = original_dir / self.first.name
        source_media.write_bytes(self.first.read_bytes())
        library = ExchangeLibrary("traktor-nml", source)
        self.assertEqual(library.crates(), [{"id": 1, "path": ["Folder", "Set"], "tracks": 1}])
        manifest, paths = library.export([1], "11111111-1111-4111-8111-111111111111", "Sender")
        metadata = manifest["files"][0]["metadata"]
        self.assertEqual(metadata["key"], "8A")
        self.assertEqual(metadata["cues"], [
            {"kind": "cue", "position": 2.5, "name": "Cue", "slot": 2},
            {"kind": "loop", "position": 8.0, "end": 10.0, "name": "Loop", "slot": 1},
        ])
        self.assertEqual(metadata["bpm"], 128.0)
        metadata["beatgrid"] = [{"position": 0.5, "bpm": 128.0, "beat": 1}]
        output_dir = self.work / "nml-out"
        output_dir.mkdir()
        final = self.work / "staged" / "Folder" / self.first.name
        output = write_exchange("traktor-nml", manifest, {next(iter(paths)): final}, output_dir)
        root = ET.parse(output).getroot()
        self.assertEqual(root.find("./COLLECTION/ENTRY/LOCATION").get("DIR"),
                         "/:" + "/:".join(final.absolute().parts[1:-1]) + "/:")
        self.assertEqual(root.find(".//PLAYLIST/ENTRY/PRIMARYKEY").get("KEY"),
                         final.drive + "/:" + "/:".join(final.absolute().parts[1:]))
        cue_nodes = root.findall("./COLLECTION/ENTRY/CUE_V2")
        self.assertEqual(cue_nodes[0].get("START"), "2500.000000")
        self.assertEqual(cue_nodes[1].get("TYPE"), "5")
        self.assertEqual(cue_nodes[1].get("LEN"), "2000.000000")
        self.assertIn("omitted beatgrid positions", " ".join(manifest["warnings"]))
        self.assertIn("omitted per-track musical key", " ".join(manifest["warnings"]))
        final.parent.mkdir(parents=True)
        final.write_bytes(self.first.read_bytes())
        reread, _ = ExchangeLibrary("traktor-nml", output).export(
            [1], "11111111-1111-4111-8111-111111111111", "Sender")
        self.assertEqual(reread["files"][0]["metadata"]["cues"], metadata["cues"])
        self.assertNotIn("beatgrid", reread["files"][0]["metadata"])

    def test_same_filename_different_contents_are_not_collapsed_by_writer(self):
        one = record("Mix Track.mp3", self.first.read_bytes())
        two = record("Mix Track.mp3", self.second.read_bytes())
        manifest = {
            "protocol": PROTOCOL, "destination": {"format": "serato"},
            "sender_id": "11111111-1111-4111-8111-111111111111", "sender_name": "Sender",
            "files": [one, two],
            "crates": [{"path": ["One"], "tracks": [one["id"]]},
                       {"path": ["Two"], "tracks": [two["id"]]}],
        }
        output_dir = self.work / "collision-out"
        output_dir.mkdir()
        first_target, second_target = self.work / "one" / one["name"], self.work / "two" / two["name"]
        output = write_exchange("rekordbox-xml", manifest,
                                {one["id"]: first_target, two["id"]: second_target}, output_dir)
        locations = [node.get("Location") for node in ET.parse(output).findall("./COLLECTION/TRACK")]
        self.assertEqual(locations, [first_target.absolute().as_uri(), second_target.absolute().as_uri()])
        self.assertFalse(first_target.exists())
        self.assertFalse(second_target.exists())

    def test_traktor_collection_without_sets_or_audio_ids_reads_direct_millisecond_cues(self):
        directory = "/:" + "/:".join(self.first.absolute().parts[1:-1]) + "/:"
        source = self.write("native.nml", f"""<NML VERSION="19"><COLLECTION ENTRIES="1">
        <ENTRY TITLE="Synthetic"><LOCATION DIR="{directory}" FILE="Mix Track.mp3" VOLUME="{self.first.drive}"/>
        <ALBUM TITLE="Synthetic album"/><TEMPO BPM="125"/>
        <CUE_V2 TYPE="0" NAME="Cue" START="1250" LEN="0" HOTCUE="0"/>
        <CUE_V2 TYPE="5" NAME="Loop" START="4000" LEN="2000" HOTCUE="1"/>
        <CUE_V2 TYPE="4" NAME="Grid" START="500" LEN="0" HOTCUE="-1"/>
        </ENTRY></COLLECTION></NML>""")
        library = ExchangeLibrary("traktor-nml", source)
        self.assertEqual(library.crates(), [{"id": 1, "path": ["native"], "tracks": 1}])
        manifest, _ = library.export([1], "11111111-1111-4111-8111-111111111111", "Sender")
        self.assertEqual(manifest["files"][0]["metadata"]["cues"], [
            {"kind": "cue", "position": 1.25, "name": "Cue", "slot": 0},
            {"kind": "loop", "position": 4.0, "end": 6.0, "name": "Loop", "slot": 1},
        ])
        self.assertEqual(manifest["files"][0]["metadata"]["album"], "Synthetic album")
        self.assertTrue(any("unsupported Traktor cue type '4'" in text for text in manifest["warnings"]))

    def test_unknown_references_duplicate_ids_and_unsafe_xml_are_rejected(self):
        unknown = self.write("unknown.xml", f"""<DJ_PLAYLISTS><COLLECTION Entries="1">
        <TRACK TrackID="1" Location="{self.first.as_uri()}"/></COLLECTION>
        <PLAYLISTS><NODE Type="0" Name="ROOT"><NODE Type="1" Name="Set" KeyType="0">
        <TRACK Key="missing"/></NODE></NODE></PLAYLISTS></DJ_PLAYLISTS>""")
        with self.assertRaisesRegex(ValueError, "unknown TrackID"):
            ExchangeLibrary("rekordbox-xml", unknown)
        duplicate = self.write("duplicate.xml", f"""<DJ_PLAYLISTS><COLLECTION Entries="2">
        <TRACK TrackID="1" Location="{self.first.as_uri()}"/>
        <TRACK TrackID="1" Location="{self.second.as_uri()}"/></COLLECTION>
        <PLAYLISTS><NODE Type="0" Name="ROOT"><NODE Type="0" Name="Set"/></NODE></PLAYLISTS>
        </DJ_PLAYLISTS>""")
        with self.assertRaisesRegex(ValueError, "duplicate TrackID"):
            ExchangeLibrary("rekordbox-xml", duplicate)
        unsafe = self.write("unsafe.xml", '<!DOCTYPE x [<!ENTITY e "boom">]><DJ_PLAYLISTS/>')
        with self.assertRaisesRegex(ValueError, "DTD"):
            ExchangeLibrary("rekordbox-xml", unsafe)
        malformed = self.write("malformed.xml", "<DJ_PLAYLISTS><")
        with self.assertRaisesRegex(ValueError, "Malformed"):
            ExchangeLibrary("rekordbox-xml", malformed)
        ambiguous = self.write("ambiguous.nml", """<NML><COLLECTION ENTRIES="1">
        <ENTRY AUDIO_ID="1"><LOCATION DIR="/:Music/:" FILE="one.mp3"
        VOLUME="unmounted-external-drive"/></ENTRY></COLLECTION><SETS>
        <NODE TYPE="FOLDER" NAME="Playlists"><SUBNODES COUNT="1">
        <NODE TYPE="PLAYLIST" NAME="Set"><PLAYLIST ENTRIES="1"><ENTRY>
        <PRIMARYKEY TYPE="TRACK" KEY="1"/></ENTRY></PLAYLIST></NODE>
        </SUBNODES></NODE></SETS></NML>""")
        with self.assertRaisesRegex(ValueError, "existing media"):
            ExchangeLibrary("traktor-nml", ambiguous)

    def test_m3u_rejects_remote_uri_missing_media_and_writer_is_not_supported(self):
        remote = self.write("remote.m3u", "https://example.invalid/track.mp3\n")
        with self.assertRaisesRegex(ValueError, "non-local"):
            ExchangeLibrary("m3u", remote)
        missing = self.write("missing.m3u", "not-here.mp3\n")
        with self.assertRaisesRegex(ValueError, "existing media"):
            ExchangeLibrary("m3u", missing)
        manifest = {
            "protocol": PROTOCOL, "destination": {"format": "serato"},
            "sender_id": "11111111-1111-4111-8111-111111111111", "sender_name": "Sender",
            "files": [], "crates": [],
        }
        with self.assertRaisesRegex(ValueError, "unsupported"):
            write_exchange("m3u", manifest, {}, self.work)


if __name__ == "__main__":
    unittest.main()
