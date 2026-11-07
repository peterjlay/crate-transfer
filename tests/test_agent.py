import hashlib
from contextlib import closing
import http.client
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from crate_transfer.agent import Agent, Client, TransferError, check_transfer, export_local, pair_computer, recover_receipt, send_crates
from crate_transfer.config import Settings, atomic_json
from crate_transfer.identity import client_context
from crate_transfer.library import LibraryError, portable_path
from crate_transfer.protocol import PROTOCOL, ProtocolError
from support import create_aggregate_database, create_root_database, file_record, make_library, root_snapshot, valid_manifest


PEER_ID = "22222222-2222-4222-8222-222222222222"
OTHER_ID = "33333333-3333-4333-8333-333333333333"


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def make_receiver(self, guard=None, prefix="receiver"):
        settings = Settings(self.base / (prefix + "-settings"))
        library_dir = self.base / (prefix + "-library")
        create_root_database(library_dir / "root.sqlite")
        create_aggregate_database(library_dir / "master.sqlite", self.base, [], {})
        music = self.base / (prefix + "-music")
        settings.update(library=str(library_dir), music=str(music))
        return Agent(settings, guard=guard or (lambda: None)), settings, library_dir, music

    def make_manifest(self, content=b"ID3\x00synthetic-track"):
        record = file_record("track.mp3", content)
        return valid_manifest([record]), record, content

    def test_agent_runs_without_dj_libraries_and_advertises_only_configured_destinations(self):
        settings = Settings(self.base / "standalone-settings")
        settings.update(library=str(self.base / "no-serato"), portable_receive=False)
        agent = Agent(settings)
        port = agent.start("127.0.0.1", 0)
        self.addCleanup(agent.stop)
        client = Client("127.0.0.1", port, agent.fingerprint)
        self.assertEqual(client.request("GET", "/api/info")["destinations"], [])
        settings.update(portable_receive=True)
        self.assertEqual(client.request("GET", "/api/info")["destinations"],
                         ["m3u", "rekordbox-xml", "traktor-nml"])
        self.assertIsNone(agent.session)
        self.assertFalse(Path(settings.snapshot()["library"]).exists())

    def test_damaged_history_is_preserved_and_startup_recovers_without_touching_user_data(self):
        for index, damaged in enumerate((
            b"[invalid", b"", b"\xff", b"{}", b"[null]",
            b'[{"id":42,"status":"complete","created":1}]',
        )):
            with self.subTest(case=index):
                agent, settings, library, music = self.make_receiver(prefix=f"damaged-{index}")
                before_settings = settings.path.read_bytes()
                before_root = root_snapshot(library / "root.sqlite")
                before_identity = agent.fingerprint
                history = settings.directory / "transfers.json"
                history.write_bytes(damaged)
                staging = music / ".crate-transfer-incoming" / ("a" * 32)
                staging.mkdir(parents=True)
                (staging / "keep").write_bytes(b"partial-media")
                with self.assertLogs("crate_transfer.agent", level="WARNING"):
                    recovered = Agent(settings, guard=lambda: None)
                backups = list(settings.directory.glob("transfers-damaged-*.json"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_bytes(), damaged)
                self.assertIn(str(backups[0]), recovered.startup_warning)
                self.assertIn("before resending", recovered.startup_warning)
                self.assertEqual(recovered.history(), [])
                self.assertEqual(json.loads(history.read_text()), [])
                self.assertEqual(settings.path.read_bytes(), before_settings)
                self.assertEqual(recovered.fingerprint, before_identity)
                self.assertEqual(root_snapshot(library / "root.sqlite"), before_root)
                self.assertEqual((staging / "keep").read_bytes(), b"partial-media")
                restarted = Agent(settings, guard=lambda: None)
                self.assertIsNone(restarted.startup_warning)
                self.assertEqual(restarted.history(), [])
                self.assertEqual(backups[0].read_bytes(), damaged)

    def test_valid_bom_history_preserves_receipts_without_recovery(self):
        agent, settings, _, _ = self.make_receiver()
        job = {"id": "a" * 32, "created": 1, "status": "complete",
               "owner": PEER_ID, "result": {"crates": 1}}
        agent.jobs_path.write_bytes(b"\xef\xbb\xbf" + json.dumps([job]).encode())
        recovered = Agent(settings, guard=lambda: None)
        self.assertEqual(recovered.history(), [job])
        self.assertIsNone(recovered.startup_warning)
        self.assertEqual(list(settings.directory.glob("transfers-damaged-*.json")), [])

    def test_history_read_or_preservation_failure_stops_startup_without_overwriting(self):
        agent, settings, _, _ = self.make_receiver()
        damaged = b"[invalid"
        agent.jobs_path.write_bytes(damaged)
        for operation in ("open", "rename"):
            with self.subTest(operation=operation):
                with patch.object(Path, operation, side_effect=PermissionError("access denied")):
                    with self.assertRaises(PermissionError):
                        Agent(settings, guard=lambda: None)
                self.assertEqual(agent.jobs_path.read_bytes(), damaged)
                self.assertEqual(list(settings.directory.glob("transfers-damaged-*.json")), [])

    def test_history_save_failure_after_recovery_preserves_original_backup(self):
        agent, settings, _, _ = self.make_receiver()
        damaged = b"[invalid"
        agent.jobs_path.write_bytes(damaged)
        with patch("crate_transfer.agent.atomic_json", side_effect=OSError("disk full")), \
                self.assertLogs("crate_transfer.agent", level="WARNING"):
            with self.assertRaisesRegex(OSError, "disk full"):
                Agent(settings, guard=lambda: None)
        backups = list(settings.directory.glob("transfers-damaged-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), damaged)

    def test_windows_reserved_port_error_reports_action_without_changing_port(self):
        agent, settings, _, _ = self.make_receiver()
        denied = OSError("Socket access denied")
        denied.winerror = 10013
        with patch("crate_transfer.agent.Server", side_effect=denied):
            with self.assertRaisesRegex(OSError, "TCP port 49783.*WinError 10013") as raised:
                agent.start()
        self.assertIn("48783", str(raised.exception))
        self.assertIn("netsh interface ipv4", str(raised.exception))
        self.assertIs(raised.exception.__cause__, denied)
        self.assertIsNone(agent.server)
        self.assertEqual(settings.snapshot()["port"], 49783)

    def test_pairing_code_attempt_limit_expiry_and_successful_token_revocation(self):
        agent, settings, _, _ = self.make_receiver()
        code = agent.pairing()
        for _ in range(5):
            with self.assertRaisesRegex(ProtocolError, "Incorrect pairing code"):
                agent.pair({"protocol": PROTOCOL, "code": "wrong", "id": PEER_ID, "name": "Peer"})
        with self.assertRaisesRegex(ProtocolError, "Pairing is closed"):
            agent.pair({"protocol": PROTOCOL, "code": code, "id": PEER_ID, "name": "Peer"})

        code = agent.pairing()
        result = agent.pair({"protocol": PROTOCOL, "code": code, "id": PEER_ID, "name": "Peer"})
        saved = Settings(settings.directory)
        trusted = saved.snapshot()["trusted"][PEER_ID]
        self.assertEqual(trusted["name"], "Peer")
        self.assertEqual(trusted["token_hash"], hashlib.sha256(result["token"].encode()).hexdigest())
        self.assertEqual(agent.authorize(result["token"]), PEER_ID)
        agent.settings.update(trusted={})
        self.assertEqual(Settings(settings.directory).snapshot()["trusted"], {})
        with self.assertRaisesRegex(PermissionError, "not paired"):
            agent.authorize(result["token"])

        agent.pairing()
        with patch("crate_transfer.agent.time.monotonic", return_value=100.0):
            agent.pair_until = 99.0
            with self.assertRaisesRegex(ProtocolError, "Pairing is closed"):
                agent.pair({"protocol": PROTOCOL, "code": "000000", "id": PEER_ID, "name": "Peer"})

    def test_begin_rejects_sender_mismatch_running_guard_and_busy_receiver(self):
        manifest, _, _ = self.make_manifest()
        agent, _, _, _ = self.make_receiver()
        with self.assertRaisesRegex(PermissionError, "does not match"):
            agent.begin(manifest, OTHER_ID)
        self.assertIsNone(agent.session)

        guarded, _, _, _ = self.make_receiver(
            guard=lambda: (_ for _ in ()).throw(LibraryError("Quit Serato before importing")),
            prefix="guarded",
        )
        with self.assertRaisesRegex(LibraryError, "Quit Serato"):
            guarded.begin(manifest, manifest["sender_id"])
        self.assertIsNone(guarded.session)

        session_id = agent.begin(manifest, manifest["sender_id"])["id"]
        with self.assertRaisesRegex(TransferError, "computer is busy"):
            agent.begin(manifest, manifest["sender_id"])
        staging = agent.session["staging"]
        agent.cancel(session_id, manifest["sender_id"])
        self.assertFalse(staging.exists())
        self.assertIsNone(agent.session)

    def test_low_disk_space_rejects_transfer_before_staging_or_library_changes(self):
        agent, _, library, music = self.make_receiver()
        manifest, _, _ = self.make_manifest()
        before = root_snapshot(library / "root.sqlite")
        with patch("crate_transfer.agent.shutil.disk_usage", return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(TransferError, "Not enough free space"):
                agent.begin(manifest, manifest["sender_id"])
        self.assertIsNone(agent.session)
        self.assertFalse((music / ".crate-transfer-incoming").exists())
        self.assertEqual(root_snapshot(library / "root.sqlite"), before)

    def test_idle_receive_expiry_accepts_exact_limit_and_rejects_later_requests(self):
        for elapsed in (3599, 3600, 3601):
            with self.subTest(idle_seconds=elapsed):
                agent, _, library, _ = self.make_receiver(prefix=f"idle-{elapsed}")
                manifest, _, _ = self.make_manifest()
                before = root_snapshot(library / "root.sqlite")
                peer = manifest["sender_id"]
                session_id = agent.begin(manifest, peer)["id"]
                staging = agent.session["staging"]
                touched = 1000.0
                agent.session["touched"] = touched
                with patch("crate_transfer.agent.time.monotonic", return_value=touched + elapsed):
                    if elapsed <= 3600:
                        self.assertEqual(agent.require_session(session_id, peer)["id"], session_id)
                    else:
                        with self.assertRaisesRegex(TransferError, "expired"):
                            agent.require_session(session_id, peer)
                        self.assertIsNone(agent.session)
                        self.assertFalse(staging.exists())
                        self.assertEqual(agent.history()[0]["status"], "failed")
                if agent.session:
                    with patch("crate_transfer.agent.time.monotonic", return_value=touched + elapsed):
                        agent.cancel(session_id, peer)
                self.assertEqual(root_snapshot(library / "root.sqlite"), before)

    def test_http_write_actions_reject_unpaired_and_revoked_tokens_without_side_effects(self):
        agent, settings, library, _ = self.make_receiver()
        manifest, record, _ = self.make_manifest()
        peer = manifest["sender_id"]
        paired = agent.pair({"protocol": PROTOCOL, "id": peer, "name": "Sender", "code": agent.pairing()})
        session_id = agent.begin(manifest, peer)["id"]
        before = root_snapshot(library / "root.sqlite")
        port = agent.start("127.0.0.1", 0)
        self.addCleanup(agent.stop)
        client = Client("127.0.0.1", port, agent.fingerprint, "unpaired-token")
        for method, path in (
            ("POST", "/api/transfers"),
            ("PUT", f"/api/transfers/{session_id}/files/{record['id']}"),
            ("POST", f"/api/transfers/{session_id}/finish"),
            ("DELETE", f"/api/transfers/{session_id}"),
        ):
            with self.subTest(action=method, endpoint=path):
                with self.assertRaisesRegex(TransferError, "not paired"):
                    client.request(method, path, manifest)
                self.assertEqual(agent.session["id"], session_id)
                self.assertEqual(agent.session["received"], set())
                self.assertEqual(root_snapshot(library / "root.sqlite"), before)
        settings.update(trusted={})
        client.token = paired["token"]
        with self.assertRaisesRegex(TransferError, "revoked"):
            client.request("POST", f"/api/transfers/{session_id}/finish")
        self.assertEqual(root_snapshot(library / "root.sqlite"), before)
        agent.cancel(session_id, peer)

    def test_upload_rejects_length_checksum_and_incomplete_body_and_cancel_cleans_staging(self):
        manifest, record, content = self.make_manifest()
        agent, _, _, _ = self.make_receiver()
        peer = manifest["sender_id"]
        session_id = agent.begin(manifest, peer)["id"]
        staging = agent.session["staging"]

        with self.assertRaisesRegex(ProtocolError, "Content-Length"):
            agent.upload(session_id, record["id"], peer, len(content) + 1, io.BytesIO(content))
        with self.assertRaisesRegex(TransferError, "checksum failed"):
            agent.upload(session_id, record["id"], peer, len(content), io.BytesIO(b"x" * len(content)))
        self.assertFalse((staging / record["id"]).exists())
        with self.assertRaisesRegex(TransferError, "ended before"):
            agent.upload(session_id, record["id"], peer, len(content), io.BytesIO(content[:-1]))
        with self.assertRaisesRegex(TransferError, "Not all files"):
            agent.finish(session_id, peer)
        agent.cancel(session_id, peer)
        self.assertFalse(staging.exists())
        self.assertIsNone(agent.session)

    def test_finish_imports_once_and_repeated_finish_returns_recorded_result(self):
        manifest, record, content = self.make_manifest()
        agent, _, _, music = self.make_receiver()
        peer = manifest["sender_id"]
        session_id = agent.begin(manifest, peer)["id"]
        agent.upload(session_id, record["id"], peer, len(content), io.BytesIO(content))
        result = agent.finish(session_id, peer)
        self.assertEqual(result["crates"], 1)
        self.assertEqual(result["files"], 1)
        destination = music / "Set" / record["name"]
        self.assertEqual(destination.read_bytes(), content)
        second_result = agent.finish(session_id, peer)
        self.assertEqual(second_result, result)
        self.assertEqual(sum(job["status"] == "complete" for job in agent.history()), 1)

    def test_portable_receiver_requires_opt_in_and_exports_without_a_serato_library(self):
        settings = Settings(self.base / "portable-settings")
        exports = self.base / "USB"
        exports.mkdir()
        settings.update(exports=str(exports), library=str(self.base / "absent-library"))
        def guard():
            raise LibraryError("Serato is running")
        agent = Agent(settings, guard=guard)
        manifest, record, content = self.make_manifest()
        manifest.update(destination={"format": "m3u"})
        peer = manifest["sender_id"]
        with self.assertRaisesRegex(TransferError, "disabled"):
            agent.begin(manifest, peer)
        self.assertEqual(list(exports.iterdir()), [])
        settings.update(portable_receive=True)
        session_id = agent.begin(manifest, peer)["id"]
        staging = agent.session["staging"]
        agent.upload(session_id, record["id"], peer, len(content), io.BytesIO(content))
        result = agent.finish(session_id, peer)
        self.assertEqual(result["format"], "m3u")
        bundle = Path(result["output"])
        playlist = bundle / "Playlists/Set/playlist.m3u8"
        entry = playlist.read_text().splitlines()[1]
        self.assertEqual((playlist.parent / entry).read_bytes(), content)
        self.assertEqual(agent.finish(session_id, peer), result)
        self.assertFalse(staging.exists())
        self.assertIsNone(agent.session)
        self.assertFalse((settings.directory / "backups").exists())
        self.assertFalse((self.base / "absent-library").exists())

    def test_local_export_records_receipt_and_never_changes_source_library(self):
        folder = self.base / "local-source"
        make_library(folder, {"Music/a.mp3": b"audio"}, [
            {"id": 10, "name": "House", "tracks": [1]},
        ], {1: "Music/a.mp3"})
        settings = Settings(self.base / "local-settings")
        settings.update(library=str(folder))
        agent = Agent(settings, guard=lambda: None)
        output = self.base / "local-export"
        output.mkdir()
        before = {name: (folder / name).read_bytes() for name in ("root.sqlite", "master.sqlite")}
        result = export_local(agent, [10], output)
        self.assertEqual(agent.history()[0]["result"], result)
        self.assertEqual(agent.history()[0]["direction"], "export")
        self.assertEqual(agent.history()[0]["status"], "complete")
        self.assertIn("not OneLibrary", agent.history()[0]["detail"])
        for name, data in before.items():
            self.assertEqual((folder / name).read_bytes(), data)

    def test_paired_network_portable_export_negotiates_destination_and_verifies_files(self):
        receiver, receiver_settings, library, _ = self.make_receiver(prefix="portable-network")
        output = self.base / "remote-export"
        output.mkdir()
        receiver_settings.update(exports=str(output), portable_receive=True)
        before = root_snapshot(library / "root.sqlite")
        port = receiver.start("127.0.0.1", 0)
        self.addCleanup(receiver.stop)
        folder = self.base / "network-source"
        make_library(folder, {"Music/network.mp3": b"network media"}, [
            {"id": 10, "name": "Network", "tracks": [1]},
        ], {1: "Music/network.mp3"})
        settings = Settings(self.base / "network-settings")
        settings.update(library=str(folder))
        sender = Agent(settings, guard=lambda: None)
        sender_port = sender.start("127.0.0.1", 0)
        self.addCleanup(sender.stop)
        settings.update(port=sender_port)
        pair_computer(settings, "127.0.0.1", port, None, receiver.pairing())
        peer_id = receiver_settings.snapshot()["id"]
        result = send_crates(sender, [10], peer_id, destination="m3u")
        self.assertEqual(result["format"], "m3u")
        self.assertEqual(sender.history()[0]["status"], "complete")
        self.assertEqual(receiver.history()[0]["status"], "complete")
        self.assertEqual(receiver.history()[0]["result"], result)
        self.assertIn("Export created", sender.history()[0]["detail"])
        self.assertEqual(root_snapshot(library / "root.sqlite"), before)
        playlist = Path(result["output"]) / "Playlists/Network/playlist.m3u8"
        self.assertEqual((playlist.parent / playlist.read_text().splitlines()[1]).read_bytes(),
                         b"network media")

    def test_legacy_or_disabled_receiver_is_rejected_before_any_upload_for_portable_export(self):
        agent, settings, _, _ = self.make_receiver()
        settings.update(peers={PEER_ID: {"name": "Older", "host": "127.0.0.1", "port": 49783,
                                        "fingerprint": "a" * 64, "token": "token"}})
        for info in ({"version": 1}, {"protocol": PROTOCOL, "id": PEER_ID, "destinations": ["serato"]}):
            with self.subTest(info=info), patch.object(Client, "request", return_value=info) as request:
                with self.assertRaisesRegex(TransferError, "Incompatible receiver|enabled this destination"):
                    send_crates(agent, [1], PEER_ID, destination="m3u")
                request.assert_called_once_with("GET", "/api/info")
                self.assertEqual(agent.history()[0]["status"], "failed")

    def test_serato_send_also_requires_the_unified_receiver_before_reading_or_uploading(self):
        agent, settings, _, _ = self.make_receiver()
        settings.update(peers={PEER_ID: {"name": "Legacy", "host": "127.0.0.1", "port": 49783,
                                        "fingerprint": "a" * 64, "token": "token"}})
        with patch.object(Client, "request", return_value={"version": 1}) as request, \
                patch("crate_transfer.agent.source_library") as source:
            with self.assertRaisesRegex(TransferError, "Update Crate Transfer on both"):
                send_crates(agent, [10], PEER_ID, destination="serato")
            source.assert_not_called()
            request.assert_called_once_with("GET", "/api/info")
        with patch.object(Client, "request", return_value={
            "protocol": PROTOCOL, "id": OTHER_ID, "destinations": ["serato"],
        }) as request:
            with self.assertRaisesRegex(TransferError, "identity differs"):
                send_crates(agent, [10], PEER_ID)
            request.assert_called_once_with("GET", "/api/info")

    def test_legacy_receive_manifest_is_rejected_before_creating_staging(self):
        agent, _, library, music = self.make_receiver()
        before = root_snapshot(library / "root.sqlite")
        manifest = valid_manifest()
        manifest.pop("protocol")
        manifest["version"] = 1
        with self.assertRaisesRegex(ProtocolError, "Incompatible transfer"):
            agent.begin(manifest, manifest["sender_id"])
        self.assertIsNone(agent.session)
        self.assertFalse(music.exists())
        self.assertEqual(root_snapshot(library / "root.sqlite"), before)

    def test_interrupted_portable_staging_is_removed_on_restart_from_configured_export_folder(self):
        settings = Settings(self.base / "portable-restart")
        exports = self.base / "exports-restart"
        settings.update(exports=str(exports))
        job_id = "a" * 32
        staging = exports / ".crate-transfer-incoming" / job_id
        staging.mkdir(parents=True)
        (staging / "partial").write_bytes(b"partial")
        atomic_json(settings.directory / "transfers.json", [
            {"id": job_id, "status": "receiving", "format": "m3u", "created": 1},
        ])
        recovered = Agent(settings, guard=lambda: None)
        self.assertFalse(staging.exists())
        self.assertEqual(recovered.history()[0]["status"], "failed")

    def test_receipt_recovery_polls_transient_disconnect_without_resending_import(self):
        client = Client("127.0.0.1", 49783, "a" * 64, "token")
        receipt = {"crates": 1, "files": 25}
        with patch.object(client, "request", side_effect=[
                TimeoutError("network interrupted"), {"status": "importing"},
                {"status": "complete", "result": receipt}]) as request, \
                patch("crate_transfer.agent.time.sleep") as sleep:
            self.assertEqual(recover_receipt(client, "transfer"), receipt)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue(all(call.args == ("GET", "/api/transfers/transfer") for call in request.call_args_list))

    def test_receipt_recovery_timeout_and_remote_failure_are_not_success(self):
        client = Client("127.0.0.1", 49783, "a" * 64)
        with patch.object(client, "request", return_value={"status": "importing"}):
            with self.assertRaisesRegex(TransferError, "Could not confirm"):
                recover_receipt(client, "transfer", wait_seconds=0)
        with patch.object(client, "request", return_value={"status": "failed", "detail": "Disk full"}):
            with self.assertRaisesRegex(TransferError, "Disk full"):
                recover_receipt(client, "transfer", wait_seconds=0)

    def test_check_uncertain_transfer_queries_receipt_without_resending_files(self):
        agent, settings, _, _ = self.make_receiver()
        settings.update(peers={PEER_ID: {"id": PEER_ID, "name": "Other Computer", "host": "127.0.0.1",
                                        "port": 49783, "fingerprint": "a" * 64, "token": "token"}})
        agent.job("job", direction="send", status="uncertain", peer="Other Computer",
                  remote_id="remote", detail="Import outcome unknown")
        with patch("crate_transfer.agent.Client.request",
                   return_value={"status": "complete", "result": {"crates": 1}}) as request:
            self.assertEqual(check_transfer(agent, "job"), {"crates": 1})
        request.assert_called_once_with("GET", "/api/transfers/remote", timeout=10)
        self.assertEqual(agent.history()[0]["status"], "complete")
        self.assertIn("No files were resent", agent.history()[0]["detail"])

    def test_transfer_status_requires_owner_and_survives_restart(self):
        agent, settings, _, _ = self.make_receiver()
        first = agent.pair({"protocol": PROTOCOL, "code": agent.pairing(), "id": PEER_ID, "name": "First"})["token"]
        second = agent.pair({"protocol": PROTOCOL, "code": agent.pairing(), "id": OTHER_ID, "name": "Second"})["token"]
        manifest, record, content = self.make_manifest()
        manifest["sender_id"] = PEER_ID
        session = agent.begin(manifest, PEER_ID)["id"]
        agent.upload(session, record["id"], PEER_ID, len(content), io.BytesIO(content))
        receipt = agent.finish(session, PEER_ID)
        restarted = Agent(Settings(settings.directory), guard=lambda: None)
        port = restarted.start("127.0.0.1", 0)
        self.addCleanup(restarted.stop)
        self.assertEqual(Client("127.0.0.1", port, agent.fingerprint, first).request(
            "GET", f"/api/transfers/{session}")["result"], receipt)
        with self.assertRaisesRegex(TransferError, "Unknown transfer"):
            Client("127.0.0.1", port, agent.fingerprint, second).request("GET", f"/api/transfers/{session}")

    def test_empty_crate_imports_without_sending_any_files(self):
        agent, _, library, _ = self.make_receiver()
        manifest = valid_manifest()
        session_id = agent.begin(manifest, manifest["sender_id"])["id"]
        result = agent.finish(session_id, manifest["sender_id"])
        self.assertEqual((result["crates"], result["files"], result["memberships"]), (1, 0, 0))
        after = root_snapshot(library / "root.sqlite")
        self.assertIn("Set", [row[2] for row in after["containers"]])
        self.assertEqual(len(after["assets"]), 1)

    def test_repeated_manual_transfers_reuse_verified_music_not_crate_groups(self):
        agent, _, library, music = self.make_receiver()
        manifest, record, content = self.make_manifest()
        results = []
        for _ in range(2):
            session_id = agent.begin(manifest, manifest["sender_id"])["id"]
            agent.upload(session_id, record["id"], manifest["sender_id"], len(content), io.BytesIO(content))
            results.append(agent.finish(session_id, manifest["sender_id"]))
        snapshot = root_snapshot(library / "root.sqlite")
        self.assertEqual(len(snapshot["assets"]), 2)
        names = [row[2] for row in snapshot["containers"]]
        self.assertIn("Set", names)
        self.assertIn("Set (2)", names)
        self.assertFalse(any(name.startswith("Received ") for name in names))
        self.assertEqual((music / "Set" / record["name"]).read_bytes(), content)
        self.assertEqual([p.name for p in music.iterdir() if not p.name.startswith(".")], ["Set"])

    def test_failed_import_removes_new_music_and_preserves_existing_music(self):
        agent, _, library, music = self.make_receiver()
        first, second = file_record("existing.mp3", b"existing"), file_record("new.mp3", b"new")
        existing = music / first["id"] / first["name"]
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"existing")
        manifest = valid_manifest([first, second])
        before = root_snapshot(library / "root.sqlite")
        session_id = agent.begin(manifest, manifest["sender_id"])["id"]
        for item, content in ((first, b"existing"), (second, b"new")):
            agent.upload(session_id, item["id"], manifest["sender_id"], len(content), io.BytesIO(content))
        with patch.object(agent.session["library"], "import_crates", side_effect=LibraryError("schema rejected")):
            with self.assertRaisesRegex(LibraryError, "schema rejected"):
                agent.finish(session_id, manifest["sender_id"])
        self.assertEqual(existing.read_bytes(), b"existing")
        self.assertFalse((music / "Set" / second["name"]).exists())
        self.assertFalse((music / "Set").exists())
        self.assertEqual(root_snapshot(library / "root.sqlite"), before)
        self.assertEqual(agent.history()[0]["status"], "failed")
        self.assertIsNone(agent.session)

    def test_readable_crate_folders_preserve_shared_tracks_and_number_filename_collisions(self):
        agent, _, library, music = self.make_receiver()
        first, second = file_record("same.mp3", b"first"), file_record("same.mp3", b"second")
        manifest = valid_manifest([first, second], [
            {"path": ["Live Set", "Warmup"], "tracks": [first["id"], second["id"]]},
            {"path": ["Other Set"], "tracks": [first["id"]]},
        ])
        for _ in range(2):
            session = agent.begin(manifest, manifest["sender_id"])["id"]
            for record, content in ((first, b"first"), (second, b"second")):
                agent.upload(session, record["id"], manifest["sender_id"], len(content), io.BytesIO(content))
            agent.finish(session, manifest["sender_id"])
        self.assertEqual((music / "Live Set/Warmup/same.mp3").read_bytes(), b"first")
        self.assertEqual((music / "Live Set/Warmup/same (2).mp3").read_bytes(), b"second")
        self.assertEqual(len(list(music.rglob("*.mp3"))), 2)
        with closing(sqlite3.connect(library / "root.sqlite")) as db:
            names = [row[0] for row in db.execute("SELECT name FROM container WHERE parent_id=1")]
            children = db.execute(
                "SELECT p.name,c.name FROM container c JOIN container p ON c.parent_id=p.id "
                "WHERE c.name='Warmup' ORDER BY p.name").fetchall()
        self.assertIn("Live Set", names)
        self.assertIn("Live Set (2)", names)
        self.assertIn("Other Set", names)
        self.assertIn("Other Set (2)", names)
        self.assertEqual(children, [("Live Set", "Warmup"), ("Live Set (2)", "Warmup")])

    def test_crate_folder_names_cannot_escape_music_directory_or_follow_symlinks(self):
        agent, _, _, music = self.make_receiver()
        manifest, record, content = self.make_manifest()
        manifest["crates"][0]["path"] = ["../Outside"]
        session = agent.begin(manifest, manifest["sender_id"])["id"]
        agent.upload(session, record["id"], manifest["sender_id"], len(content), io.BytesIO(content))
        agent.finish(session, manifest["sender_id"])
        self.assertEqual((music / "_.._Outside" / record["name"]).read_bytes(), content)
        self.assertFalse((music.parent / "Outside").exists())
        external = self.base / "external"
        external.mkdir()
        (music / "Linked").symlink_to(external, target_is_directory=True)
        manifest["crates"][0]["path"] = ["Linked"]
        session = agent.begin(manifest, manifest["sender_id"])["id"]
        agent.upload(session, record["id"], manifest["sender_id"], len(content), io.BytesIO(content))
        with self.assertRaisesRegex(TransferError, "symbolic link"):
            agent.finish(session, manifest["sender_id"])
        self.assertEqual(list(external.iterdir()), [])

    def test_staging_cleanup_error_is_visible_but_committed_music_is_preserved(self):
        agent, _, _, music = self.make_receiver()
        manifest, record, content = self.make_manifest()
        session_id = agent.begin(manifest, manifest["sender_id"])["id"]
        agent.upload(session_id, record["id"], manifest["sender_id"], len(content), io.BytesIO(content))
        with self.assertLogs("crate_transfer.agent", level="ERROR"):
            with patch("crate_transfer.agent.shutil.rmtree", side_effect=OSError("cleanup denied")):
                result = agent.finish(session_id, manifest["sender_id"])
        self.assertEqual(result["crates"], 1)
        self.assertEqual((music / "Set" / record["name"]).read_bytes(), content)
        self.assertEqual(agent.history()[0]["status"], "complete")
        self.assertIn("Staging cleanup failed", agent.history()[0]["detail"])
        self.assertIsNone(agent.session)

    def test_cancelled_send_does_not_open_a_remote_transfer(self):
        settings = Settings(self.base / "cancelled-sender")
        source = self.base / "cancelled-source"
        make_library(source, {"Music/a.mp3": b"audio"}, [{"id": 10, "name": "Set", "tracks": [1]}],
                     {1: "Music/a.mp3"})
        settings.update(library=str(source), peers={PEER_ID: {
            "id": PEER_ID, "name": "Receiver", "host": "127.0.0.1",
            "port": 49783, "fingerprint": "a" * 64, "token": "test-token",
        }})
        sender = Agent(settings, guard=lambda: None)
        with self.assertLogs("crate_transfer.agent", level="ERROR"):
            with patch("crate_transfer.agent.Client.request") as request:
                with self.assertRaisesRegex(TransferError, "cancelled by this computer"):
                    send_crates(sender, [10], PEER_ID, cancelled=lambda: True)
                request.assert_not_called()
        self.assertEqual(sender.history()[0]["status"], "failed")
        self.assertIn("No crates imported", sender.history()[0]["detail"])

    def test_interrupted_jobs_are_classified_and_abandoned_staging_is_removed(self):
        agent_settings = Settings(self.base / "recovery-settings")
        music = self.base / "recovery-music"
        agent_settings.update(music=str(music))
        incoming = music / ".crate-transfer-incoming"
        statuses = [
            ("a" * 32, "receiving"),
            ("b" * 32, "importing"),
            ("c" * 32, "complete"),
        ]
        jobs = [{"id": job_id, "status": status, "detail": "previous run"} for job_id, status in statuses]
        atomic_json(agent_settings.directory / "transfers.json", jobs)
        abandoned = []
        for job_id, _ in statuses:
            path = incoming / job_id
            path.mkdir(parents=True)
            (path / "partial").write_bytes(b"partial")
            abandoned.append(path)

        recovered = Agent(agent_settings, guard=lambda: None)
        history = {job["id"]: job for job in recovered.history()}
        self.assertEqual(history["a" * 32]["status"], "failed")
        self.assertEqual(history["b" * 32]["status"], "uncertain")
        self.assertEqual(history["c" * 32]["status"], "complete")
        self.assertIn("Interrupted during import", history["b" * 32]["detail"])
        self.assertTrue(all(not path.exists() for path in abandoned))
        persisted = json.loads((agent_settings.directory / "transfers.json").read_text())
        self.assertEqual({job["id"]: job["status"] for job in persisted},
                         {"a" * 32: "failed", "b" * 32: "uncertain", "c" * 32: "complete"})

    def test_fingerprint_mismatch_rejects_before_pair_credentials_are_sent(self):
        receiver, _, _, _ = self.make_receiver()
        code = receiver.pairing()
        wrong = "0" * 64
        self.assertNotEqual(wrong, receiver.fingerprint)
        client = Client("127.0.0.1", receiver.settings.snapshot()["port"], wrong)
        with self.assertRaisesRegex(TransferError, "No credentials or music were sent"):
            # A local TLS endpoint proves that pinning is checked before HTTP payload transmission.
            port = receiver.start("127.0.0.1", 0)
            self.addCleanup(receiver.stop)
            client.port = port
            client.request("POST", "/api/pair", {"id": PEER_ID, "name": "Peer", "code": code})
        self.assertEqual(receiver.pair_attempts, 0)
        self.assertEqual(receiver.pair_code, code)

    def test_code_only_pairing_rejects_wrong_or_closed_code_without_saving_trust(self):
        receiver, receiver_settings, _, _ = self.make_receiver()
        sender_settings = Settings(self.base / "sender-settings")
        port = receiver.start("127.0.0.1", 0)
        self.addCleanup(receiver.stop)
        code = receiver.pairing()
        wrong_code = f"{(int(code) + 1) % 1000000:06d}"
        with self.assertRaisesRegex(TransferError, "Incorrect pairing code"):
            pair_computer(sender_settings, "127.0.0.1", port, None, wrong_code)
        receiver.pair_until = 0
        with self.assertRaisesRegex(TransferError, "Pairing is closed"):
            pair_computer(sender_settings, "127.0.0.1", port, None, code)
        self.assertEqual(Settings(sender_settings.directory).snapshot()["peers"], {})
        self.assertEqual(receiver_settings.snapshot()["trusted"], {})

    def test_code_only_pairing_rejects_certificate_change_before_sending_code(self):
        receiver, _, _, _ = self.make_receiver()
        replacement, _, _, _ = self.make_receiver(prefix="replacement")
        sender_settings = Settings(self.base / "sender-settings")
        port = receiver.start("127.0.0.1", 0)
        replacement_port = replacement.start("127.0.0.1", 0)
        self.addCleanup(receiver.stop)
        self.addCleanup(replacement.stop)
        code = receiver.pairing()
        replacement.pairing()
        connection = http.client.HTTPSConnection
        with patch("crate_transfer.agent.http.client.HTTPSConnection", side_effect=[
            connection("127.0.0.1", port, context=client_context(), timeout=5),
            connection("127.0.0.1", replacement_port, context=client_context(), timeout=5),
        ]):
            with self.assertRaisesRegex(TransferError, "No credentials or music were sent"):
                pair_computer(sender_settings, "127.0.0.1", port, None, code)
        self.assertEqual(receiver.pair_attempts, 0)
        self.assertEqual(replacement.pair_attempts, 0)
        self.assertEqual(Settings(sender_settings.directory).snapshot()["peers"], {})

    def test_one_code_pairing_enables_real_transfers_in_both_directions(self):
        agents = []
        for index in (1, 2):
            settings = Settings(self.base / f"settings-{index}")
            library = self.base / f"library-{index}"
            filename = "track1.mp3" if index == 1 else "DJ Qbert - Wave Twisters [2001].MPG"
            make_library(library, {f"Music/{filename}": f"ID3-track-{index}".encode()},
                         [{"id": 10, "name": f"Set {index}", "tracks": [1]}],
                         {1: f"Music/{filename}"})
            settings.update(library=str(library), music=str(self.base / f"music-{index}"))
            agent = Agent(settings, guard=lambda: None)
            settings.update(port=agent.start("127.0.0.1", 0))
            self.addCleanup(agent.stop)
            agents.append(agent)
        first, second = agents
        pair_computer(first.settings, "127.0.0.1", second.settings.snapshot()["port"], None, second.pairing())
        for sender, receiver in ((first, second), (second, first)):
            receiver_id = receiver.settings.snapshot()["id"]
            reloaded = Settings(sender.settings.directory).snapshot()
            self.assertIn(receiver_id, reloaded["peers"])
            self.assertIn(receiver_id, reloaded["trusted"])
            receipt = send_crates(sender, [10], receiver_id)
            self.assertEqual((receipt["crates"], receipt["files"]), (1, 1))
            self.assertEqual(receiver.history()[0]["status"], "complete")
        filename = "DJ Qbert - Wave Twisters [2001].MPG"
        received = [path for path in Path(first.settings.snapshot()["music"]).rglob("*") if path.name == filename]
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].read_bytes(), b"ID3-track-2")
        with closing(sqlite3.connect(Path(first.settings.snapshot()["library"]) / "root.sqlite")) as db:
            row = db.execute("SELECT type,format,portable_id FROM asset WHERE file_name=?", (filename,)).fetchone()
        self.assertEqual(row, ("video", "mpg", portable_path(str(received[0].resolve()), sys.platform == "win32")))
        for receiver in agents:
            receiver.settings.update(trusted={})
        for sender, receiver in ((first, second), (second, first)):
            receiver_id = receiver.settings.snapshot()["id"]
            peer = sender.settings.snapshot()["peers"][receiver_id]
            with self.assertRaisesRegex(TransferError, "not paired"):
                Client(peer["host"], peer["port"], peer["fingerprint"], peer["token"]).request(
                    "POST", "/api/transfers", {})

    def test_older_receiver_requires_update_before_pairing_code_is_sent(self):
        settings = Settings(self.base / "sender-settings")
        with patch("crate_transfer.agent.Client.request", return_value={"version": 1}) as request:
            with self.assertRaisesRegex(TransferError, "Update Crate Transfer"):
                pair_computer(settings, "127.0.0.1", 49783, "a" * 64, "123456")
        request.assert_called_once_with("GET", "/api/info")
        self.assertEqual(settings.snapshot()["peers"], {})
        self.assertEqual(settings.snapshot()["trusted"], {})

    def test_two_local_tls_agents_pair_send_import_and_finish_idempotently(self):
        payload = b"ID3\x00synthetic-title\x00tag-bytes"
        source_library = self.base / "sender-library"
        make_library(
            source_library,
            {"Music/live.mp3": payload},
            [{"id": 10, "name": "Live Set", "tracks": [1]}],
            {1: "Music/live.mp3"},
        )
        sender_settings = Settings(self.base / "sender-settings")
        sender_settings.update(library=str(source_library), music=str(self.base / "sender-music"))
        sender = Agent(sender_settings, guard=lambda: None)
        receiver, receiver_settings, receiver_library, receiver_music = self.make_receiver()
        receiver_code = receiver.pairing()
        port = receiver.start("127.0.0.1", 0)
        self.addCleanup(receiver.stop)

        pair_computer(sender_settings, "127.0.0.1", port, None, receiver_code)
        peer_id = receiver_settings.snapshot()["id"]
        peer = sender_settings.snapshot()["peers"][peer_id]
        self.assertEqual(peer["host"], "127.0.0.1")
        self.assertEqual(peer["port"], port)
        self.assertEqual(peer["fingerprint"], receiver.fingerprint)
        sender_id = sender_settings.snapshot()["id"]
        reverse_peer = receiver_settings.snapshot()["peers"][sender_id]
        self.assertEqual(reverse_peer["fingerprint"], sender.fingerprint)
        self.assertEqual(reverse_peer["host"], "127.0.0.1")
        self.assertEqual(sender.authorize(reverse_peer["token"]), peer_id)

        original_request = Client.request
        finish_calls = []

        def lose_finish_response(client, method, endpoint, *args, **kwargs):
            result = original_request(client, method, endpoint, *args, **kwargs)
            if endpoint.endswith("/finish"):
                finish_calls.append(endpoint)
                self.assertEqual(kwargs["timeout"], 600)
                raise TimeoutError("Simulated lost final response after successful import")
            return result

        with patch.object(Client, "request", new=lose_finish_response):
            receipt = send_crates(sender, [10], peer_id)
        self.assertEqual(len(finish_calls), 1)
        self.assertEqual(receipt["crates"], 1)
        self.assertEqual(receipt["files"], 1)
        self.assertEqual(receipt["memberships"], 1)
        sender_job = sender.history()[0]
        receiver_job = receiver.history()[0]
        self.assertEqual(sender_job["status"], "complete")
        self.assertEqual(receiver_job["status"], "complete")
        self.assertEqual(receiver_job["result"], receipt)

        remote_id = sender_job["remote_id"]
        repeat = Client("127.0.0.1", port, peer["fingerprint"], peer["token"]).request(
            "POST", "/api/transfers/%s/finish" % remote_id
        )
        self.assertEqual(repeat, receipt)

        receiver_db = sqlite3.connect(receiver_library / "root.sqlite")
        imported = receiver_db.execute(
            "SELECT a.portable_id,a.file_name FROM asset a WHERE a.file_name='live.mp3'"
        ).fetchone()
        crate = receiver_db.execute("SELECT name FROM container WHERE name='Live Set'").fetchone()
        receiver_db.close()
        self.assertIsNotNone(imported)
        self.assertEqual(imported[0], portable_path(str((receiver_music / "Live Set" / "live.mp3").resolve()),
                                                  sys.platform == "win32"))
        self.assertEqual(imported[1], "live.mp3")
        self.assertEqual(crate[0], "Live Set")
        imported_path = receiver_music / "Live Set" / "live.mp3"
        self.assertEqual(imported_path.read_bytes(), payload)
        self.assertTrue((Path(receipt["backup"]) / "root.sqlite").is_file())
        self.assertTrue((Path(receipt["backup"]) / "master.sqlite").is_file())


if __name__ == "__main__":
    unittest.main()
