import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crate_transfer.config import Settings
from crate_transfer.identity import client_context, identity


class SettingsTests(unittest.TestCase):
    def test_settings_accept_powershell_utf8_bom_and_preserve_pairings(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            settings = Settings(directory)
            settings.update(port=48783, peers={"peer": {"token": "synthetic-token"}})
            expected = settings.snapshot()
            path = directory / "settings.json"
            path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
            self.assertEqual(Settings(directory).snapshot(), expected)
            self.assertFalse(path.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_platform_default_directories_follow_platform_and_localappdata(self):
        from crate_transfer.config import data_directory, library_directory

        home = Path("/synthetic-home")
        with patch("crate_transfer.config.Path.home", return_value=home), \
                patch("crate_transfer.config.sys.platform", "darwin"):
            self.assertEqual(data_directory(), home / "Library/Application Support/CrateTransfer")
            self.assertEqual(library_directory(), home / "Library/Application Support/Serato/Library")
        with patch("crate_transfer.config.sys.platform", "win32"), \
                patch.dict(os.environ, {"LOCALAPPDATA": "/synthetic-local"}):
            self.assertEqual(data_directory(), Path("/synthetic-local/CrateTransfer"))
            self.assertEqual(library_directory(), Path("/synthetic-local/Serato/Library"))

    def test_settings_updates_are_durable_and_snapshots_are_isolated(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "settings"
            settings = Settings(directory)
            original_id = settings.snapshot()["id"]
            snapshot = settings.snapshot()
            snapshot["peers"]["mutated"] = {"token": "not persisted"}
            self.assertNotIn("mutated", settings.snapshot()["peers"])

            settings.update(name="Synthetic DJ", peers={"peer-id": {"name": "Peer"}})
            reloaded = Settings(directory)
            self.assertEqual(reloaded.snapshot()["id"], original_id)
            self.assertEqual(reloaded.snapshot()["name"], "Synthetic DJ")
            self.assertEqual(reloaded.snapshot()["peers"], {"peer-id": {"name": "Peer"}})
            self.assertFalse((directory / "settings.json.tmp").exists())
            if os.name == "posix":
                self.assertEqual((directory / "settings.json").stat().st_mode & 0o777, 0o600)


class IdentityTests(unittest.TestCase):
    def test_identity_generates_persistent_tls_pair_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            first_context, first_fingerprint = identity(directory)
            second_context, second_fingerprint = identity(directory)
            self.assertEqual(first_fingerprint, second_fingerprint)
            self.assertEqual(first_context.minimum_version, second_context.minimum_version)
            self.assertEqual(first_context.minimum_version.name, "TLSv1_2")
            self.assertTrue((directory / "identity.pem").is_file())
            self.assertTrue((directory / "identity-key.pem").is_file())
            if os.name == "posix":
                self.assertEqual((directory / "identity-key.pem").stat().st_mode & 0o777, 0o600)

    def test_identity_refuses_incomplete_key_pair_and_client_uses_pinned_peer_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "identity.pem").write_text("incomplete", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Incomplete TLS identity"):
                identity(directory)

        context = client_context()
        self.assertFalse(context.check_hostname)
        self.assertEqual(context.verify_mode, 0)
        self.assertEqual(context.minimum_version.name, "TLSv1_2")


if __name__ == "__main__":
    unittest.main()
