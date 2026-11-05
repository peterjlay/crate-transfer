import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QContextMenuEvent, QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QPushButton, QScrollArea

from crate_transfer.config import Settings
from crate_transfer.ui import PairDialog, STYLE, Window
from support import make_library


class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        cls.app.setStyle("Fusion")
        cls.app.setStyleSheet(STYLE)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        library = self.base / "library"
        make_library(library, {"Music/a.mp3": b"synthetic audio"}, [
            {"id": 10, "name": "House", "tracks": [1]},
            {"id": 11, "name": "Deep", "parent_id": 10, "tracks": [1]},
            {"id": 12, "name": "Techno", "tracks": []},
        ], {1: "Music/a.mp3"})
        self.settings = Settings(self.base / "settings")
        self.settings.update(library=str(library), music=str(self.base / "music"))
        self.window = Window(self.settings, start_services=False)
        self.errors = []
        self.window.show_error = lambda title, message: self.errors.append((title, message))

    def tearDown(self):
        self.wait_for_tasks()
        self.window.quit()
        self.window.deleteLater()
        self.app.processEvents()
        self.temporary.cleanup()

    def wait_for_tasks(self):
        until = time.monotonic() + 5
        while self.window.active_tasks and time.monotonic() < until:
            QTest.qWait(10)
        self.assertEqual(self.window.active_tasks, 0, "Desktop worker did not finish")

    def load(self):
        self.window.load_crates()
        self.wait_for_tasks()

    def show_settings(self):
        self.window.page(3)
        self.window.show()
        QTest.qWait(30)

    def test_save_button_is_opaque_and_readable_without_hover(self):
        self.show_settings()
        QTest.mouseMove(self.window.navigation[0])
        self.app.processEvents()
        button = self.window.save_button
        image = button.grab().toImage()
        background = image.pixelColor(12, image.height() // 2)
        self.assertEqual(background, QColor("#7de2c4"))
        self.assertEqual(button.palette().buttonText().color(), QColor("#10251e"))
        self.assertTrue(button.isVisible())
        self.assertTrue(button.isEnabled())
        self.assertEqual(button.text(), "Save settings")

    def test_settings_scroll_at_minimum_window_size_without_clipping_controls(self):
        self.window.resize(980, 650)
        self.show_settings()
        scroll = self.window.pages.widget(3)
        self.assertIsInstance(scroll, QScrollArea)
        for field in (self.window.name_input, self.window.library_input,
                      self.window.music_input, self.window.port_input):
            with self.subTest(field=field.objectName()):
                self.assertGreaterEqual(field.height(), field.fontMetrics().height() + 18)
        scroll.ensureWidgetVisible(self.window.save_button)
        self.app.processEvents()
        location = self.window.save_button.mapTo(scroll.viewport(), QPoint(0, 0))
        self.assertGreaterEqual(location.y(), 0)
        self.assertLessEqual(location.y() + self.window.save_button.height(), scroll.viewport().height())

    def test_copy_buttons_copy_full_paths_not_only_the_selected_text(self):
        self.show_settings()
        for field in (self.window.library_input, self.window.music_input):
            with self.subTest(field=field.objectName()):
                path = "/Users/DJ/Music/Long folder with spaces/Caf\u00e9/Selected crate"
                field.setText(path)
                field.setSelection(0, 5)
                self.app.clipboard().setText("old clipboard")
                copy_button = self.window.findChild(QPushButton, f"copy-{field.objectName()}")
                QTest.mouseClick(copy_button, Qt.MouseButton.LeftButton)
                self.assertEqual(self.app.clipboard().text(), path)
                self.assertEqual(field.text(), path)
                self.assertEqual(self.window.statusBar().currentMessage(), "Folder path copied to clipboard")

    def test_path_context_menu_is_opaque_and_copies_selected_text(self):
        self.show_settings()
        field = self.window.library_input
        field.setText("/Users/DJ/Library/Application Support/Serato/Library")
        field.setFocus()
        field.setSelection(10, 7)
        selected = field.selectedText()
        self.app.clipboard().setText("old clipboard")
        observations = {}

        def click_copy():
            menu = QApplication.activePopupWidget()
            observations["menu"] = isinstance(menu, QMenu)
            if not isinstance(menu, QMenu):
                return
            image = menu.grab().toImage()
            observations["background"] = image.pixelColor(2, 2)
            copy_action = next(action for action in menu.actions()
                               if action.text().replace("&", "").split("\t")[0] == "Copy")
            observations["enabled"] = copy_action.isEnabled()
            QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(copy_action).center())

        QTimer.singleShot(100, click_copy)
        local = QPoint(15, field.height() // 2)
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, local, field.mapToGlobal(local))
        self.app.sendEvent(field, event)
        QTest.qWait(150)
        self.assertTrue(observations["menu"])
        self.assertTrue(observations["enabled"])
        self.assertEqual(observations["background"], QColor("#1e2631"))
        self.assertEqual(self.app.clipboard().text(), selected)

    def test_keyboard_copy_and_paste_keep_standard_text_editing_behavior(self):
        self.show_settings()
        field = self.window.library_input
        field.setFocus()
        field.setText("/Users/DJ/Library/Application Support/Serato/Library")
        field.selectAll()
        QTest.keySequence(field, QKeySequence(QKeySequence.StandardKey.Copy))
        self.assertEqual(self.app.clipboard().text(), field.text())
        self.app.clipboard().setText("/Volumes/Music/Serato/Library")
        QTest.keySequence(field, QKeySequence(QKeySequence.StandardKey.Paste))
        self.assertEqual(field.text(), "/Volumes/Music/Serato/Library")

    def test_selecting_crates_and_receiver_enables_only_manual_send(self):
        self.load()
        self.assertEqual(self.errors, [])
        self.assertEqual([item.text(0) for item in self.window.all_items()], ["House", "Deep", "Techno"])
        self.assertFalse(self.window.send_button.isEnabled())
        self.window.all_items()[0].setCheckState(0, Qt.CheckState.Checked)
        self.assertEqual(self.window.selected_crates(), [10])
        self.assertFalse(self.window.send_button.isEnabled())
        peer = "22222222-2222-4222-8222-222222222222"
        self.settings.update(peers={peer: {"id": peer, "name": "DJ Laptop", "host": "127.0.0.1",
                                           "port": 49783, "fingerprint": "a" * 64, "token": "test-token"}})
        self.window.refresh_computers()
        self.window.target.setCurrentIndex(1)
        self.assertTrue(self.window.send_button.isEnabled())
        self.assertEqual(self.window.agent.history(), [])
        self.window.search.setText("Deep")
        self.assertFalse(self.window.all_items()[0].isHidden())
        self.assertFalse(self.window.all_items()[1].isHidden())
        self.assertTrue(self.window.all_items()[2].isHidden())
        with patch("crate_transfer.ui.send_crates", return_value=None) as send:
            with patch("crate_transfer.ui.QMessageBox.question", return_value=QMessageBox.StandardButton.No):
                self.window.send()
                send.assert_not_called()
            with patch("crate_transfer.ui.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                self.window.send()
                self.assertTrue(self.window.sending)
                self.assertFalse(self.window.send_button.isEnabled())
                self.wait_for_tasks()
            send.assert_called_once()
            self.assertEqual(send.call_args.args[1:3], ([10], peer))
            self.assertFalse(self.window.sending)

    def test_pairing_needs_only_receiver_code_and_valid_address(self):
        dialog = PairDialog(self.window, {"host": "192.168.1.20", "port": 49783, "fingerprint": "b" * 64})
        self.assertTrue(dialog.pair_button.isEnabled())
        for invalid in ("", "123", "abcdef", "１２３４５６"):
            dialog.code.setText(invalid)
            with self.assertRaisesRegex(ValueError, "6-digit"):
                dialog.values()
        dialog.code.setText("123456")
        self.assertEqual(dialog.values(), ("192.168.1.20", 49783, None, "123456"))
        self.assertFalse(hasattr(dialog, "fingerprint"))
        self.assertFalse(hasattr(dialog, "verified"))
        dialog.host.setText("https://192.168.1.20")
        with self.assertRaisesRegex(ValueError, "not a URL"):
            dialog.values()
        dialog.deleteLater()

    def test_incoming_pairing_automatically_enables_send_target(self):
        peer_id = "22222222-2222-4222-8222-222222222222"
        self.window.tick()
        self.settings.update(
            peers={peer_id: {"id": peer_id, "name": "Other Mac", "host": "192.168.1.20",
                             "port": 49783, "fingerprint": "a" * 64, "token": "b" * 64}},
            trusted={peer_id: {"name": "Other Mac", "token_hash": "c" * 64}},
        )
        self.window.tick()
        self.assertGreaterEqual(self.window.target.findData(peer_id), 0)
        self.assertEqual(self.window.computers.topLevelItem(0).text(2), "Can send / Can receive from")

    def test_settings_persist_receive_folder_and_reject_library_overlap(self):
        destination = self.base / "new-local-music"
        self.window.music_input.setText(str(destination))
        with patch("crate_transfer.ui.QMessageBox.information") as information:
            self.window.save_settings()
            information.assert_called_once()
        self.wait_for_tasks()
        self.assertEqual(self.errors, [])
        self.assertEqual(Settings(self.settings.directory).snapshot()["music"], str(destination.resolve()))
        self.window.music_input.setText(str(self.base / "library" / "music"))
        self.window.save_settings()
        self.assertIn("separate", self.errors[-1][1])
        self.assertEqual(self.settings.snapshot()["music"], str(destination.resolve()))

    def test_connection_port_saves_without_validating_library_or_changing_other_settings(self):
        before = self.settings.snapshot()
        self.window.library_input.setText(str(self.base / "missing"))
        self.window.music_input.setText("")
        self.window.port_input.setValue(48783)
        with patch("crate_transfer.ui.QMessageBox.information") as information:
            self.window.save_connection_port()
        self.assertEqual(self.errors, [])
        self.assertEqual(Settings(self.settings.directory).snapshot(), dict(before, port=48783))
        information.assert_called_once()

    def test_settings_accept_serato_parent_and_persist_resolved_library_folder(self):
        parent = self.base / "Application Support" / "Serato"
        library = parent / "Library"
        make_library(library, {}, [{"id": 10, "name": "Parent-folder set", "tracks": []}], {})
        self.window.library_input.setText(str(parent))
        with patch("crate_transfer.ui.QMessageBox.information"):
            self.window.save_settings()
        self.wait_for_tasks()
        self.assertEqual(self.errors, [])
        self.assertEqual(self.settings.snapshot()["library"], str(library.resolve()))
        self.assertEqual(self.window.library_input.text(), str(library.resolve()))
        self.assertEqual([item.text(0) for item in self.window.all_items()], ["Parent-folder set"])

    def test_library_failure_is_visible_and_clears_stale_selection(self):
        self.load()
        self.window.all_items()[0].setCheckState(0, Qt.CheckState.Checked)
        self.settings.update(library=str(self.base / "missing-library"))
        self.window.load_crates()
        self.wait_for_tasks()
        self.assertIn("Library not found", self.errors[-1][1])
        self.assertEqual(self.window.selected_crates(), [])
        self.assertFalse(self.window.send_button.isEnabled())
        self.assertIn("Library unavailable", self.window.library_status.text())

    def test_activity_surfaces_completion_backup_and_failure_without_auto_retry(self):
        self.window.agent.job("a" * 32, direction="receive", peer="Sender", status="complete",
                              detail="Imported 1 crate", progress=1, result={"backup": str(self.base / "backup")})
        self.app.processEvents()
        self.assertEqual(self.window.activities.topLevelItem(0).text(2), "Complete")
        self.assertEqual(self.window.job_progress.value(), 1000)
        self.assertIn("Backup on destination computer:", self.window.job_detail.text())
        self.window.agent.job("b" * 32, direction="send", peer="Receiver", status="failed",
                              detail="Checksum failed", progress=0.5)
        self.app.processEvents()
        self.window.activities.setCurrentItem(self.window.activities.topLevelItem(0))
        self.assertEqual(self.window.job_detail.text(), "Checksum failed")
        self.assertEqual(self.window.job_progress.value(), 500)
        self.assertEqual(self.window.job_detail.property("state"), "error")
        self.assertEqual(self.window.activities.topLevelItem(0).foreground(2).color(), QColor("#ff737d"))
        self.assertTrue(self.window.activities.topLevelItem(0).font(2).bold())
        self.assertEqual([job["status"] for job in self.window.agent.history()], ["failed", "complete"])

    def test_pairing_pin_is_bold_green_and_expiry_returns_to_neutral(self):
        self.window.allow_pairing()
        self.app.processEvents()
        self.assertIn("PIN " + self.window.agent.pair_code, self.window.pairing_status.text())
        self.assertEqual(self.window.pairing_status.palette().color(self.window.pairing_status.foregroundRole()),
                         QColor("#62e89f"))
        self.assertTrue(self.window.pairing_status.font().bold())
        self.window.agent.pair_until = 0
        self.window.tick()
        self.assertEqual(self.window.pairing_status.property("state"), "neutral")

    def test_matching_discovered_peer_has_bold_green_connection_status(self):
        from types import SimpleNamespace
        peer_id = "22222222-2222-4222-8222-222222222222"
        peer = {"id": peer_id, "name": "Other Computer", "host": "192.168.1.20",
                "port": 49783, "fingerprint": "a" * 64, "token": "b" * 64}
        self.settings.update(peers={peer_id: peer})
        discovered = dict(peer)
        discovery = SimpleNamespace(snapshot=lambda: [discovered])
        self.window.discovery = discovery
        try:
            self.window.refresh_computers()
            item = self.window.computers.topLevelItem(0)
            self.assertEqual(item.text(3), "Online")
            self.assertEqual(item.foreground(3).color(), QColor("#62e89f"))
            self.assertTrue(item.font(3).bold())
            discovered["fingerprint"] = "c" * 64
            self.window.refresh_computers()
            item = self.window.computers.topLevelItem(0)
            self.assertEqual(item.text(3), "Identity changed - pair again")
            self.assertEqual(item.foreground(3).color(), QColor("#ff737d"))
        finally:
            self.window.discovery = None

    def test_startup_registration_error_is_visible_and_settings_are_not_saved(self):
        self.window.startup_input.setChecked(True)
        with patch("crate_transfer.ui.startup.enabled", return_value=False), \
                patch("crate_transfer.ui.startup.configure", side_effect=OSError("Startup access denied")):
            self.window.save_settings()
        self.assertIn("Startup access denied", self.errors[-1][1])


if __name__ == "__main__":
    unittest.main()
