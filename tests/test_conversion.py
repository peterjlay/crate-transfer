import tempfile
import sqlite3
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from crate_transfer.agent import Agent, TransferError, export_local, pair_computer, send_crates
from crate_transfer.catalog import source_library
from crate_transfer.config import Settings
from crate_transfer.library import Library, NativeImportUncertain
from crate_transfer.portable import export_bundle
from support import file_record, make_library, root_snapshot, valid_manifest


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.media = self.source / "track.mp3"
        self.media.write_bytes(b"synthetic conversion audio")
        self.playlist = self.source / "Set.m3u8"
        self.playlist.write_text("#EXTM3U\ntrack.mp3\ntrack.mp3\n", encoding="utf-8")
        self.output = self.base / "exports"
        self.output.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def agent(self, name):
        settings = Settings(self.base / name)
        library = self.base / (name + "-library")
        make_library(library, {}, [], {})
        settings.update(library=str(library), music=str(self.base / (name + "-music")),
                        source_format="m3u", source_path=str(self.playlist),
                        exports=str(self.output), portable_receive=True)
        return Agent(settings, guard=lambda: None)

    def selected(self):
        return [crate["id"] for crate in source_library("m3u", str(self.playlist)).crates()]

    def test_m3u_to_local_serato_reports_repeat_deduplication_and_keeps_sources_untouched(self):
        agent = self.agent("local")
        source_bytes = self.media.read_bytes()
        manifest_bytes = self.playlist.read_bytes()
        library = Path(agent.settings.snapshot()["library"])
        master_before = (library / "master.sqlite").read_bytes()
        result = export_local(agent, self.selected(), Path(agent.settings.snapshot()["music"]),
                              destination="serato")
        self.assertEqual(result["format"], "serato")
        self.assertEqual(result["crates"], 1)
        self.assertEqual(result["files"], 1)
        reader = Library(library)
        received = next(crate for crate in reader.crates() if crate["path"] == ["Set"])
        forwarded, paths = reader.export([received["id"]], agent.settings.snapshot()["id"], "Local")
        self.assertEqual(len(forwarded["crates"][0]["tracks"]), 1)
        self.assertEqual(result["memberships"], 1)
        self.assertTrue(any("1 repeated entries" in warning for warning in result["warnings"]))
        self.assertEqual(next(iter(paths.values())).read_bytes(), source_bytes)
        self.assertEqual(self.media.read_bytes(), source_bytes)
        self.assertEqual(self.playlist.read_bytes(), manifest_bytes)
        self.assertEqual((library / "master.sqlite").read_bytes(), master_before)
        self.assertTrue(Path(result["backup"]).is_dir())
        self.assertIsNone(agent.session)

    def test_paired_m3u_source_to_network_xml_uses_receivers_paths_without_native_changes(self):
        sender, receiver = self.agent("sender"), self.agent("receiver")
        sender.settings.update(exports=str(self.base / "unused"))
        sender_port = sender.start("127.0.0.1", 0)
        receiver_port = receiver.start("127.0.0.1", 0)
        try:
            sender.settings.update(port=sender_port)
            receiver.settings.update(port=receiver_port)
            pair_computer(sender.settings, "127.0.0.1", receiver_port, None, receiver.pairing())
            library = Path(receiver.settings.snapshot()["library"])
            before = root_snapshot(library / "root.sqlite")
            result = send_crates(sender, self.selected(), receiver.settings.snapshot()["id"],
                                 destination="rekordbox-xml")
            self.assertEqual(result["format"], "rekordbox-xml")
            self.assertEqual(result["files"], 1)
            self.assertEqual(result["memberships"], 2)
            bundle = Path(result["output"])
            exported_media = next((bundle / "Music").iterdir())
            self.assertEqual(exported_media.read_bytes(), self.media.read_bytes())
            xml = next(bundle.glob("*.xml")).read_text(encoding="utf-8")
            self.assertIn(exported_media.as_uri(), xml)
            self.assertNotIn(self.media.as_uri(), xml)
            self.assertTrue(any("manual import" in warning.lower() or "import it" in warning.lower()
                                for warning in result["warnings"]))
            self.assertEqual(root_snapshot(library / "root.sqlite"), before)
            self.assertIsNone(receiver.session)
        finally:
            sender.stop()
            receiver.stop()

    def test_exchange_bundles_roundtrip_metadata_and_point_to_published_media(self):
        record = file_record(self.media.name, self.media.read_bytes())
        record["metadata"] = {"title": "Track", "bpm": 120.0, "key": "Am",
                              "cues": [{"kind": "cue", "position": 1.25, "slot": 0, "name": "Intro"},
                                       {"kind": "loop", "position": 4.0, "end": 8.0, "slot": 1, "name": "Loop"}]}
        manifest = valid_manifest([record], [{"path": ["Set"], "tracks": [record["id"], record["id"]]}])
        for index, format in enumerate(("rekordbox-xml", "traktor-nml"), 1):
            manifest["destination"] = {"format": format}
            result = export_bundle(manifest, {record["id"]: self.media}, self.output,
                                   str(index) * 32, format=format)
            bundle = Path(result["output"])
            exchange = next(bundle.glob("*.xml" if format == "rekordbox-xml" else "*.nml"))
            reader = source_library(format, str(exchange))
            selected = [crate["id"] for crate in reader.crates() if crate["path"] == ["Set"]]
            restored, paths = reader.export(selected, manifest["sender_id"], "Restored")
            self.assertEqual(len(restored["crates"][0]["tracks"]), 2)
            metadata = restored["files"][0]["metadata"]
            self.assertEqual(metadata["bpm"], 120.0)
            self.assertEqual(metadata["title"], "Track")
            self.assertEqual(metadata["cues"], record["metadata"]["cues"])
            self.assertTrue(all(path.is_relative_to(bundle) for path in paths.values()))
            self.assertEqual(next(iter(paths.values())).read_bytes(), self.media.read_bytes())
            agent = self.agent("native-" + format)
            agent.settings.update(source_format=format, source_path=str(exchange))
            native = export_local(agent, selected, Path(agent.settings.snapshot()["music"]), destination="serato")
            self.assertTrue(any("does not convert exchange-file" in warning for warning in native["warnings"]))
            self.assertEqual(native["memberships"], 1)

    def test_exchange_writer_failure_does_not_publish_partial_bundle_or_remove_existing_output(self):
        sentinel = self.output / "existing.txt"
        sentinel.write_text("keep", encoding="utf-8")
        record = file_record(self.media.name, self.media.read_bytes())
        manifest = valid_manifest([record])
        manifest["destination"] = {"format": "rekordbox-xml"}
        with patch("crate_transfer.exchange.write_exchange", side_effect=ValueError("Unsupported metadata")), \
                self.assertRaisesRegex(ValueError, "Unsupported metadata"):
            export_bundle(manifest, {record["id"]: self.media}, self.output, "f" * 32, format="rekordbox-xml")
        self.assertEqual(list(self.output.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_text(), "keep")

    def djay_agent(self, name):
        from test_djay import make_djay_database
        agent = self.agent(name)
        path = make_djay_database(self.base / (name + "-djay"))
        agent.settings.update(djay_library=str(path), djay_receive=True)
        return agent, path

    def test_local_mixxx_registers_media_and_can_forward_to_portable_output(self):
        from test_mixxx import make_mixxx_library
        agent = self.agent("local-mixxx")
        path = make_mixxx_library(self.base / "mixxx-destination")
        agent.settings.update(native_libraries={"mixxx": str(path)}, native_receive={"mixxx": True})
        result = export_local(agent, self.selected(), Path(agent.settings.snapshot()["music"]), destination="mixxx")
        self.assertEqual((result["format"], result["files"], result["memberships"]), ("mixxx", 1, 2))
        agent.settings.update(source_format="mixxx", source_path=str(path))
        selected = [crate["id"] for crate in source_library("mixxx", str(path)).crates()]
        forwarded = export_local(agent, selected, self.output, destination="m3u")
        self.assertEqual(forwarded["memberships"], 2)
        self.assertEqual(next((Path(forwarded["output"]) / "Music").iterdir()).read_bytes(), self.media.read_bytes())

    def test_uncertain_native_restore_retains_media_and_marks_both_local_jobs_uncertain(self):
        from test_mixxx import make_mixxx_library
        agent = self.agent("uncertain-native")
        path = make_mixxx_library(self.base / "uncertain-destination")
        agent.settings.update(native_libraries={"mixxx": str(path)}, native_receive={"mixxx": True})
        with patch("crate_transfer.mixxx.MixxxLibrary.import_crates",
                   side_effect=NativeImportUncertain("Automatic restore failed; inspect the backup")), \
                self.assertRaisesRegex(NativeImportUncertain, "restore failed"):
            export_local(agent, self.selected(), Path(agent.settings.snapshot()["music"]), destination="mixxx")
        media = list(Path(agent.settings.snapshot()["music"]).rglob("*.mp3"))
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0].read_bytes(), self.media.read_bytes())
        self.assertTrue(all(job["status"] == "uncertain" for job in agent.history()))
        self.assertIsNone(agent.session)

    def test_paired_mixxx_uses_receiver_paths_and_requires_native_opt_in(self):
        from test_mixxx import make_mixxx_library
        sender, receiver = self.agent("mixxx-sender"), self.agent("mixxx-receiver")
        path = make_mixxx_library(self.base / "network-mixxx")
        receiver.settings.update(native_libraries={"mixxx": str(path)})
        sender_port, receiver_port = sender.start("127.0.0.1", 0), receiver.start("127.0.0.1", 0)
        try:
            sender.settings.update(port=sender_port)
            receiver.settings.update(port=receiver_port)
            pair_computer(sender.settings, "127.0.0.1", receiver_port, None, receiver.pairing())
            with self.assertRaisesRegex(TransferError, "not enabled"):
                send_crates(sender, self.selected(), receiver.settings.snapshot()["id"], destination="mixxx")
            self.assertIsNone(receiver.session)
            receiver.settings.update(native_receive={"mixxx": True})
            result = send_crates(sender, self.selected(), receiver.settings.snapshot()["id"], destination="mixxx")
            self.assertEqual((result["format"], result["memberships"]), ("mixxx", 2))
            with sqlite3.connect(path) as db:
                received = Path(db.execute("SELECT location FROM track_locations").fetchone()[0])
            self.assertTrue(received.is_relative_to(Path(receiver.settings.snapshot()["music"]).resolve()))
            self.assertEqual(received.read_bytes(), self.media.read_bytes())
            self.assertNotEqual(received, self.media)
        finally:
            sender.stop()
            receiver.stop()

    def native_destination(self, format, name):
        agent = self.agent(name)
        directory = self.base / (name + "-destination")
        if format == "traktor":
            from test_traktor import make_traktor_library
            path = make_traktor_library(directory)
        elif format == "virtualdj-native":
            from test_virtualdj import make_virtualdj_library
            path = make_virtualdj_library(directory)
        elif format == "engine":
            from test_engine import make_engine_database
            path = make_engine_database(directory / "Database2/m.db")
        else:
            from test_rekordbox import make_complete_rekordbox_library
            path = make_complete_rekordbox_library(directory)
        agent.settings.update(native_libraries={format: str(path)}, native_receive={format: True})
        return agent, path

    def test_serato4_to_local_serato3_and_back_emits_directional_warnings_and_native_paths(self):
        from test_serato3 import make_serato3_library
        agent = self.agent("serato-generations")
        library = Path(agent.settings.snapshot()["library"])
        export_local(agent, self.selected(), Path(agent.settings.snapshot()["music"]), destination="serato")
        legacy = make_serato3_library(self.base / "legacy")
        agent.settings.update(source_format="serato", source_path=str(library),
                              native_libraries={"serato3": str(legacy)}, native_receive={"serato3": True})
        selected = [crate["id"] for crate in Library(library).crates()]
        down = export_local(agent, selected, Path(agent.settings.snapshot()["music"]), destination="serato3")
        self.assertTrue(any("Version downgrade" in warning for warning in down["warnings"]))
        self.assertEqual((down["format"], down["memberships"]), ("serato3", 1))
        reader = source_library("serato3", str(legacy))
        selected = [crate["id"] for crate in reader.crates()]
        agent.settings.update(source_format="serato3", source_path=str(legacy))
        up = export_local(agent, selected, Path(agent.settings.snapshot()["music"]), destination="serato")
        self.assertTrue(any("Version upgrade" in warning for warning in up["warnings"]))
        self.assertEqual(up["memberships"], 1)
        forwarded, paths = reader.export(selected, agent.settings.snapshot()["id"], "Forward")
        self.assertEqual(list(paths.values())[0].read_bytes(), self.media.read_bytes())

    def test_serato3_network_opt_in_receiver_paths_and_forwarding_to_serato4(self):
        from test_serato3 import make_serato3_library
        sender, receiver = self.agent("legacy-sender"), self.agent("legacy-receiver")
        legacy = make_serato3_library(self.base / "legacy-network")
        receiver.settings.update(native_libraries={"serato3": str(legacy)}, native_receive={"serato3": True})
        sender_port, receiver_port = sender.start("127.0.0.1", 0), receiver.start("127.0.0.1", 0)
        try:
            sender.settings.update(port=sender_port)
            receiver.settings.update(port=receiver_port)
            pair_computer(sender.settings, "127.0.0.1", receiver_port, None, receiver.pairing())
            result = send_crates(sender, self.selected(), receiver.settings.snapshot()["id"], destination="serato3")
            self.assertEqual((result["format"], result["files"], result["memberships"]), ("serato3", 1, 1))
            reader = source_library("serato3", str(legacy))
            selected = [crate["id"] for crate in reader.crates()]
            manifest, files = reader.export(selected, receiver.settings.snapshot()["id"], "Legacy")
            self.assertTrue(all(path.is_relative_to(Path(receiver.settings.snapshot()["music"]).resolve())
                                for path in files.values()))
            receiver.settings.update(source_format="serato3", source_path=str(legacy))
            up = send_crates(receiver, selected, sender.settings.snapshot()["id"], destination="serato")
            self.assertTrue(any("Version upgrade" in warning for warning in up["warnings"]))
            self.assertEqual(up["format"], "serato")
            self.assertIsNone(receiver.session)
        finally:
            sender.stop()
            receiver.stop()

    def test_all_native_destinations_support_local_registration_and_source_forwarding(self):
        for format in ("traktor", "virtualdj-native", "engine", "rekordbox-native"):
            with self.subTest(format=format), ExitStack() as stack:
                if format == "rekordbox-native":
                    stack.enter_context(patch("pyrekordbox.db6.database.get_config", return_value={}))
                    stack.enter_context(patch("pyrekordbox.db6.database.get_rekordbox_pid", return_value=None))
                agent, path = self.native_destination(format, "local-" + format)
                before = path.read_bytes()
                result = export_local(agent, self.selected(), Path(agent.settings.snapshot()["music"]),
                                      destination=format)
                expected_memberships = 1 if format == "engine" else 2
                self.assertEqual((result["format"], result["files"], result["memberships"]),
                                 (format, 1, expected_memberships))
                self.assertTrue(Path(result["backup"]).is_dir())
                self.assertNotEqual(path.read_bytes(), before)
                agent.settings.update(source_format=format, source_path=str(path))
                reader = source_library(format, str(path))
                selected = [crate["id"] for crate in reader.crates() if crate["tracks"]]
                forwarded = export_local(agent, selected, self.output, destination="m3u")
                self.assertEqual(forwarded["memberships"], expected_memberships)
                copied = next((Path(forwarded["output"]) / "Music").iterdir())
                self.assertEqual(copied.read_bytes(), self.media.read_bytes())
                self.assertTrue(any("not" in warning or "unverified" in warning for warning in result["warnings"]))

    def test_all_native_network_destinations_use_receiver_music_and_preserve_unrelated_serato(self):
        sender = self.agent("matrix-sender")
        sender_port = sender.start("127.0.0.1", 0)
        sender.settings.update(port=sender_port)
        try:
            for format in ("traktor", "virtualdj-native", "engine", "rekordbox-native"):
                with self.subTest(format=format), ExitStack() as stack:
                    if format == "rekordbox-native":
                        stack.enter_context(patch("pyrekordbox.db6.database.get_config", return_value={}))
                        stack.enter_context(patch("pyrekordbox.db6.database.get_rekordbox_pid", return_value=None))
                    receiver, path = self.native_destination(format, "network-" + format)
                    receiver_port = receiver.start("127.0.0.1", 0)
                    stack.callback(receiver.stop)
                    receiver.settings.update(port=receiver_port)
                    serato = Path(receiver.settings.snapshot()["library"]) / "root.sqlite"
                    before = root_snapshot(serato)
                    pair_computer(sender.settings, "127.0.0.1", receiver_port, None, receiver.pairing())
                    result = send_crates(sender, self.selected(), receiver.settings.snapshot()["id"], destination=format)
                    self.assertEqual(result["format"], format)
                    reader = source_library(format, str(path))
                    selected = [crate["id"] for crate in reader.crates() if crate["tracks"]]
                    exported, media = reader.export(selected, sender.settings.snapshot()["id"], "Forwarder")
                    self.assertEqual(exported["files"][0]["sha256"], file_record(self.media.name, self.media.read_bytes())["sha256"])
                    self.assertTrue(all(value.is_relative_to(Path(receiver.settings.snapshot()["music"]).resolve())
                                        for value in media.values()))
                    self.assertEqual(root_snapshot(serato), before)
                    self.assertIsNone(receiver.session)
        finally:
            sender.stop()

    def test_local_djay_registers_new_media_and_forwards_native_playlists_to_portable_output(self):
        from crate_transfer.djay import DjayLibrary
        agent, database = self.djay_agent("local-djay")
        before = database.read_bytes()
        with sqlite3.connect(database) as source:
            before_rows = source.execute("SELECT * FROM database2 ORDER BY rowid").fetchall()
            before_indexes = source.execute("SELECT * FROM yap2 ORDER BY extension,key").fetchall()
        with patch("crate_transfer.djay.assert_djay_closed"):
            result = export_local(agent, self.selected(), Path(agent.settings.snapshot()["music"]), destination="djay")
            self.assertEqual(result["format"], "djay")
            self.assertEqual(result["memberships"], 2)
            self.assertNotEqual(database.read_bytes(), before)
            self.assertTrue(any("playback" in warning for warning in result["warnings"]))
            with sqlite3.connect(Path(result["backup"]) / "MediaLibrary.db") as backup:
                self.assertEqual(backup.execute("SELECT * FROM database2 ORDER BY rowid").fetchall(), before_rows)
                self.assertEqual(backup.execute("SELECT * FROM yap2 ORDER BY extension,key").fetchall(), before_indexes)
                self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            library = DjayLibrary(database)
            selected = [crate["id"] for crate in library.crates() if crate["path"] == ["Set"]]
            manifest, paths = library.export(selected, agent.settings.snapshot()["id"], "Native djay")
            self.assertEqual(len(manifest["files"]), 1)
            self.assertEqual(len(manifest["crates"][0]["tracks"]), 2)
            self.assertEqual(next(iter(paths.values())).read_bytes(), self.media.read_bytes())
            output = export_local(agent, selected, self.output, destination="m3u",
                                  source_format="djay", source_path=str(database))
            self.assertEqual(output["memberships"], 2)
        self.assertIsNone(agent.session)
        self.assertEqual(self.media.read_bytes(), b"synthetic conversion audio")

    def test_paired_network_djay_destination_is_opt_in_and_uses_receiver_paths(self):
        from crate_transfer.djay import DjayLibrary
        sender = self.agent("djay-sender")
        receiver, database = self.djay_agent("djay-receiver")
        sender_port = sender.start("127.0.0.1", 0)
        receiver_port = receiver.start("127.0.0.1", 0)
        try:
            sender.settings.update(port=sender_port)
            pair_computer(sender.settings, "127.0.0.1", receiver_port, None, receiver.pairing())
            with patch("crate_transfer.djay.assert_djay_closed"):
                result = send_crates(sender, self.selected(), receiver.settings.snapshot()["id"], destination="djay")
            self.assertEqual(result["format"], "djay")
            self.assertEqual(receiver.history()[0]["status"], "complete")
            library = DjayLibrary(database)
            selected = [crate["id"] for crate in library.crates() if crate["path"] == ["Set"]]
            _, paths = library.export(selected, sender.settings.snapshot()["id"], "Forward")
            self.assertTrue(all(path.is_relative_to(Path(receiver.settings.snapshot()["music"]).resolve()) for path in paths.values()))
            self.assertNotIn(self.media.resolve(), paths.values())
            self.assertEqual(next(iter(paths.values())).read_bytes(), self.media.read_bytes())
            receiver.settings.update(djay_receive=False)
            with self.assertRaisesRegex(TransferError, "has not enabled this destination"):
                send_crates(sender, self.selected(), receiver.settings.snapshot()["id"], destination="djay")
        finally:
            sender.stop()
            receiver.stop()
