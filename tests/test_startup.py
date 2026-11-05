import plistlib
import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from crate_transfer import startup


class StartupTests(unittest.TestCase):
    def test_windows_run_entry_preserves_spaces_and_can_disable(self):
        registry = MagicMock()
        registry.REG_SZ = 1
        registry.HKEY_CURRENT_USER = 2
        command = [r"C:\Apps\Crate Transfer\Crate Transfer.exe", "--background",
                   "--data-dir", r"C:\Users\DJ Name\AppData\Local\CrateTransfer"]
        with patch("crate_transfer.startup.sys.platform", "win32"), \
                patch.dict("sys.modules", {"winreg": registry}), \
                patch("crate_transfer.startup.startup_command", return_value=command):
            startup.configure(True, Path("settings"))
            registry.SetValueEx.assert_called_once_with(
                registry.CreateKey.return_value.__enter__.return_value,
                "CrateTransfer", 0, 1, subprocess.list2cmdline(command))
            self.assertTrue(startup.enabled())
            startup.configure(False, Path("settings"))
            registry.DeleteValue.assert_called_once()

    def test_macos_opt_in_writes_quiet_login_command_and_can_disable(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            directory = home / "settings with spaces"
            with patch("crate_transfer.startup.sys.platform", "darwin"), \
                    patch("crate_transfer.startup.sys.frozen", True, create=True), \
                    patch("crate_transfer.startup.sys.executable",
                          "/Applications/Crate Transfer.app/Contents/MacOS/Crate Transfer"), \
                    patch("crate_transfer.startup.Path.home", return_value=home):
                self.assertFalse(startup.enabled())
                startup.configure(True, directory)
                self.assertTrue(startup.enabled())
                with (home / "Library/LaunchAgents/app.cratetransfer.desktop.plist").open("rb") as source:
                    value = plistlib.load(source)
                self.assertEqual(value["ProgramArguments"], [
                    "/Applications/Crate Transfer.app/Contents/MacOS/Crate Transfer",
                    "--background", "--data-dir", str(directory.resolve())])
                self.assertTrue(value["RunAtLoad"])
                self.assertNotIn("KeepAlive", value)
                startup.configure(False, directory)
                self.assertFalse(startup.enabled())
                startup.configure(False, directory)

    def test_startup_rejects_development_and_uninstalled_macos_apps(self):
        with patch("crate_transfer.startup.sys.frozen", False, create=True):
            with self.assertRaisesRegex(ValueError, "installed app"):
                startup.startup_command(Path("/tmp/settings"))
        with patch("crate_transfer.startup.sys.platform", "darwin"), \
                patch("crate_transfer.startup.sys.frozen", True, create=True), \
                patch("crate_transfer.startup.sys.executable", "/Volumes/Crate Transfer/Crate Transfer.app/executable"):
            with self.assertRaisesRegex(ValueError, "Applications"):
                startup.startup_command(Path("/tmp/settings"))
