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
from crate_transfer.ui import PairDialog, STYLE, Window, app_icon
from support import file_record, make_library, valid_manifest


class DesktopTests(unittest.TestCase):
    def test_vinyl_icon_has_orange_label_and_no_transfer_arrow_or_mint_branding(self):
        image = app_icon(256).pixmap(256, 256).toImage()
        self.assertEqual(image.pixelColor(0, 0).alpha(), 0)
        self.assertEqual(image.pixelColor(8 * 4, 32 * 4).name(), "#101114")
        self.assertEqual(image.pixelColor(32 * 4, 26 * 4).name(), "#d98a47")
        self.assertEqual(image.pixelColor(32 * 4, 32 * 4).name(), "#101114")
        self.assertEqual(image.pixelColor(56 * 4, 49 * 4).name(), "#242835")

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
        discovery = patch("crate_transfer.ui.find_source", return_value=None)
        discovery.start()
        self.addCleanup(discovery.stop)

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

    def test_damaged_history_shows_visible_recovery_warning_after_window_initialization(self):
        self.window.quit()
        self.window.deleteLater()
        self.app.processEvents()
        path = self.settings.directory / "transfers.json"
        path.write_bytes(b"[invalid")
        with patch("crate_transfer.ui.QMessageBox.warning") as warning, \
                self.assertLogs("crate_transfer.agent", level="WARNING"):
            self.window = Window(self.settings, start_services=False)
            self.app.processEvents()
        warning.assert_called_once_with(
            self.window, "Transfer history recovered", self.window.agent.startup_warning,
        )
        self.assertIn("transfers-damaged-", warning.call_args.args[2])
        self.assertIn("before resending", warning.call_args.args[2])
        self.assertEqual(self.window.activities.topLevelItemCount(), 0)

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

    def test_received_crate_is_selectable_in_send_view_without_aggregate_rebuild(self):
        from crate_transfer.library import Library
        track = self.base / "received.mp3"
        track.write_bytes(b"received audio")
        record = file_record(track.name, track.read_bytes())
        manifest = valid_manifest([record], [
            {"path": ["West Coast Hip Hop 04"], "tracks": [record["id"]]},
        ])
        Library(self.settings.snapshot()["library"]).import_crates(
            manifest, {record["id"]: track}, self.base / "received-backup", "received",
            guard=lambda: None,
        )
        self.load()
        item = next(i for i in self.window.all_items() if i.text(0) == "West Coast Hip Hop 04")
        self.assertEqual(item.text(1), "1")
        item.setCheckState(0, Qt.CheckState.Checked)
        selected = self.window.selected_crates()
        self.assertEqual(selected, [item.data(0, Qt.ItemDataRole.UserRole)])
        self.assertLess(selected[0], 0)
        self.assertEqual(self.window.selection_status.text(), "1 selected / includes subcrates")

    def test_local_portable_export_is_available_without_a_paired_computer(self):
        self.load()
        item = next(i for i in self.window.all_items() if i.text(0) == "House")
        item.setCheckState(0, Qt.CheckState.Checked)
        self.assertTrue(self.window.export_button.isEnabled())
        self.assertFalse(self.window.send_button.isEnabled())
        output = self.base / "USB"
        output.mkdir()
        with patch("crate_transfer.ui.QFileDialog.getExistingDirectory", return_value=str(output)), \
                patch("crate_transfer.ui.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            self.window.export_portable()
            self.wait_for_tasks()
        self.assertEqual(self.window.agent.history()[0]["status"], "complete")
        self.assertTrue(self.window.export_button.isEnabled())
        self.assertEqual(self.errors, [])
        self.assertTrue(list(output.glob("Crates-*")))

    def test_source_changes_clear_stale_selection_and_native_adapters_explain_their_status(self):
        self.load()
        self.window.crate_tree.topLevelItem(0).setCheckState(0, Qt.CheckState.Checked)
        self.assertTrue(self.window.export_button.isEnabled())
        self.window.source_format.setCurrentIndex(self.window.source_format.findData("rekordbox-xml"))
        self.window.source_path.setText(str(self.base / "collection.xml"))
        self.assertEqual(self.window.crate_tree.topLevelItemCount(), 0)
        self.assertFalse(self.window.export_button.isEnabled())
        self.assertEqual(self.window.current_source(), ("rekordbox-xml", str(self.base / "collection.xml")))
        for key in ("rekordbox-native", "engine", "virtualdj-native"):
            item = self.window.source_format.model().item(self.window.source_format.findData(key))
            self.assertTrue(item.isEnabled())
            self.assertTrue(item.toolTip())
        for index in range(self.window.destination_format.count()):
            item = self.window.destination_format.model().item(index)
            if self.window.destination_format.itemData(index) is None:
                self.assertFalse(item.isEnabled())
                self.assertTrue(item.toolTip())

    def test_m3u_source_refresh_persists_source_and_exposes_playlists_without_serato(self):
        media = self.base / "song.mp3"
        media.write_bytes(b"synthetic media")
        playlist = self.base / "Selected.m3u8"
        playlist.write_text("#EXTM3U\nsong.mp3\n", encoding="utf-8")
        self.window.source_format.setCurrentIndex(self.window.source_format.findData("m3u"))
        self.window.source_path.setText(str(playlist))
        self.load()
        self.assertEqual(self.errors, [])
        self.assertEqual(self.window.crate_tree.topLevelItem(0).text(0), "Selected")
        self.assertEqual(self.window.crate_tree.topLevelItem(0).text(1), "1")
        self.assertEqual(self.settings.snapshot()["source_format"], "m3u")
        self.assertEqual(self.settings.snapshot()["source_path"], str(playlist))
        self.assertTrue(self.window.source_format.isEnabled())
        playlist.unlink()
        self.load()
        self.assertEqual(self.window.crate_tree.topLevelItemCount(), 0)
        self.assertEqual(self.errors, [])
        self.assertIn("No existing library", self.window.library_status.text())
        self.assertTrue(self.window.source_format.isEnabled())

    def test_local_serato_conversion_routes_to_configured_native_library_without_export_dialog(self):
        self.load()
        self.window.crate_tree.topLevelItem(0).setCheckState(0, Qt.CheckState.Checked)
        selected = self.window.selected_crates()
        self.window.destination_format.setCurrentIndex(self.window.destination_format.findData("serato"))
        with patch("crate_transfer.ui.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
                patch("crate_transfer.ui.QFileDialog.getExistingDirectory") as picker, \
                patch("crate_transfer.ui.export_local", return_value={}) as convert:
            self.window.export_portable()
            self.wait_for_tasks()
            picker.assert_not_called()
            convert.assert_called_once_with(
                self.window.agent, selected, Path(self.settings.snapshot()["music"]),
                self.window.cancel_send.is_set, destination="serato", source_format="serato",
                source_path=self.settings.snapshot()["library"],
            )
        self.assertFalse(self.window.sending)
        self.assertTrue(self.window.source_format.isEnabled())
        self.assertEqual(self.errors, [])

    def test_portable_receive_settings_save_independently_of_serato_and_validate_folder(self):
        before = self.settings.snapshot()
        self.window.library_input.setText(str(self.base / "no-library"))
        exports = self.base / "exports"
        exports.mkdir()
        self.window.exports_input.setText(str(exports))
        self.window.portable_receive_input.setChecked(True)
        with patch("crate_transfer.ui.QMessageBox.information"):
            self.window.save_portable_settings()
        self.assertEqual(Settings(self.settings.directory).snapshot(),
                         dict(before, exports=str(exports.resolve()), portable_receive=True))
        self.assertEqual(self.errors, [])
        self.window.exports_input.setText("")
        self.window.save_portable_settings()
        self.assertIn("existing portable export folder", self.errors[-1][1])
        self.assertEqual(self.settings.snapshot()["exports"], str(exports.resolve()))

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
        self.load()
        self.assertEqual([item.text(0) for item in self.window.all_items()], ["Parent-folder set"])

    def test_missing_library_is_nonmodal_and_clears_stale_selection(self):
        self.load()
        self.window.all_items()[0].setCheckState(0, Qt.CheckState.Checked)
        self.window.source_path.setText(str(self.base / "missing-library"))
        self.window.load_crates()
        self.wait_for_tasks()
        self.assertEqual(self.errors, [])
        self.assertEqual(self.window.selected_crates(), [])
        self.assertFalse(self.window.send_button.isEnabled())
        self.assertIn("No existing library", self.window.library_status.text())

    def test_source_pivot_remembers_independent_paths_and_keeps_destination_separate(self):
        original = self.window.source_path.text()
        xml_path = str(self.base / "rekordbox.xml")
        nml_path = str(self.base / "collection.nml")
        for format, path in (("rekordbox-xml", xml_path), ("traktor-nml", nml_path)):
            self.window.source_format.setCurrentIndex(self.window.source_format.findData(format))
            self.window.source_path.setText(path)
        self.window.source_format.setCurrentIndex(self.window.source_format.findData("rekordbox-xml"))
        self.assertEqual(self.window.source_path.text(), xml_path)
        self.window.source_format.setCurrentIndex(self.window.source_format.findData("serato"))
        self.assertEqual(self.window.source_path.text(), original)
        self.assertEqual(self.settings.snapshot()["library"], original)
        reloaded = Settings(self.settings.directory)
        self.assertEqual(reloaded.snapshot()["source_paths"]["traktor-nml"], nml_path)
        self.assertEqual(reloaded.snapshot()["source_paths"]["rekordbox-xml"], xml_path)

    def test_absent_native_source_is_selectable_but_does_not_open_a_library(self):
        with patch("crate_transfer.ui.source_library") as read:
            for format in ("engine", "rekordbox-native", "virtualdj-native"):
                self.window.source_format.setCurrentIndex(self.window.source_format.findData(format))
                self.window.load_crates()
                self.assertIn("No existing library", self.window.library_status.text())
                self.assertTrue(self.window.refresh_button.isEnabled())
            read.assert_not_called()
        self.assertEqual(self.errors, [])

    def test_normal_startup_does_not_probe_or_open_any_dj_library(self):
        self.window.quit()
        self.window.deleteLater()
        self.app.processEvents()
        with patch("crate_transfer.ui.source_library") as library, \
                patch("crate_transfer.ui.source_exists") as exists, \
                patch("crate_transfer.ui.Agent.start", return_value=49783), \
                patch("crate_transfer.ui.Discovery") as discovery, \
                patch("crate_transfer.ui.Window.show_error") as error:
            discovery.return_value.snapshot.return_value = []
            self.settings.update(library=str(self.base / "no-app-installed"), source_paths={})
            self.window = Window(self.settings, start_services=True)
            self.app.processEvents()
            library.assert_not_called()
            exists.assert_not_called()
            error.assert_not_called()
        self.assertIn("No source library opened", self.window.library_status.text())

    def test_agent_preferences_save_without_any_dj_library(self):
        self.window.library_input.setText(str(self.base / "missing-library"))
        self.window.name_input.setText("All DJ Apps")
        with patch("crate_transfer.ui.QMessageBox.information"), \
                patch("crate_transfer.ui.Library") as library, \
                patch("crate_transfer.ui.startup.enabled", return_value=False):
            self.window.save_agent_preferences()
            library.assert_not_called()
        self.assertEqual(self.settings.snapshot()["name"], "All DJ Apps")
        self.assertEqual(self.errors, [])

    def test_shared_music_folder_saves_without_serato4_and_does_not_move_old_media(self):
        existing = self.base / "old-media"
        existing.mkdir()
        (existing / "keep.mp3").write_bytes(b"keep")
        self.settings.update(library=str(self.base / "missing-dj4"), music=str(existing))
        self.window.music_input.setText(str(self.base / "new-music"))
        with patch("crate_transfer.ui.Library") as serato, \
                patch("crate_transfer.ui.native_library") as native, \
                patch("crate_transfer.ui.QMessageBox.information"):
            self.window.save_music_settings()
            serato.assert_not_called()
            native.assert_not_called()
        self.assertEqual(self.settings.snapshot()["music"], str((self.base / "new-music").resolve()))
        self.assertEqual((existing / "keep.mp3").read_bytes(), b"keep")
        self.assertFalse((self.base / "new-music").exists())
        self.assertEqual(self.errors, [])

    def test_native_djay_receive_defaults_off_and_can_be_disabled_without_opening_library(self):
        self.assertFalse(self.window.djay_receive_input.isChecked())
        self.window.djay_library_input.setText(str(self.base / "missing-djay"))
        with patch("crate_transfer.ui.QMessageBox.information"):
            self.window.save_djay_settings()
        self.assertFalse(self.settings.snapshot()["djay_receive"])
        self.assertEqual(self.settings.snapshot()["djay_library"], str(self.base / "missing-djay"))
        self.assertEqual(self.errors, [])

    def test_all_native_destination_settings_default_off_and_save_without_opening_if_disabled(self):
        with patch("crate_transfer.ui.native_library") as library, \
                patch("crate_transfer.ui.QMessageBox.information"):
            for format in ("serato3", "mixxx", "traktor", "virtualdj-native", "engine", "rekordbox-native"):
                self.assertFalse(self.window.native_receive_inputs[format].isChecked())
                path = str(self.base / ("missing-" + format))
                self.window.native_library_inputs[format].setText(path)
                self.window.save_native_settings(format)
                self.assertEqual(self.settings.snapshot()["native_libraries"][format], path)
                self.assertFalse(self.settings.snapshot()["native_receive"][format])
                self.assertGreaterEqual(self.window.destination_format.findData(format), 0)
            library.assert_not_called()
        self.assertEqual(self.errors, [])

    def test_serato_version_warning_is_shown_before_local_conversion_and_network_send(self):
        self.load()
        self.window.all_items()[0].setCheckState(0, Qt.CheckState.Checked)
        self.window.destination_format.setCurrentIndex(self.window.destination_format.findData("serato3"))
        with patch("crate_transfer.ui.QMessageBox.question", return_value=QMessageBox.StandardButton.No) as question, \
                patch("crate_transfer.ui.export_local") as convert:
            self.window.export_portable()
            self.assertIn("Version downgrade", question.call_args.args[2])
            self.assertIn("database-only metadata", question.call_args.args[2])
            convert.assert_not_called()
        peer = "22222222-2222-4222-8222-222222222222"
        self.settings.update(peers={peer: {"id": peer, "name": "DJ 3 Laptop", "host": "127.0.0.1",
                                           "port": 49783, "fingerprint": "a" * 64, "token": "test-token"}})
        self.window.refresh_computers()
        self.window.target.setCurrentIndex(1)
        with patch("crate_transfer.ui.QMessageBox.question", return_value=QMessageBox.StandardButton.No) as question, \
                patch("crate_transfer.ui.send_crates") as send:
            self.window.send()
            self.assertIn("Version downgrade", question.call_args.args[2])
            send.assert_not_called()

    def test_legacy_source_missing_library_is_inline_and_upgrade_warning_is_confirmed(self):
        from test_serato3 import make_serato3_library
        self.window.source_format.setCurrentIndex(self.window.source_format.findData("serato3"))
        self.assertIn("No existing library", self.window.library_status.text())
        self.assertEqual(self.errors, [])
        legacy = make_serato3_library(self.base / "legacy")
        from crate_transfer.serato3 import Serato3Library, crate_bytes
        media = self.base / "legacy.mp3"
        media.write_bytes(b"synthetic legacy source")
        library = Serato3Library(legacy)
        (legacy / "Subcrates/Test.crate").write_bytes(crate_bytes([media.relative_to(library.volume).as_posix()]))
        self.window.source_path.setText(str(legacy))
        self.load()
        self.window.all_items()[0].setCheckState(0, Qt.CheckState.Checked)
        self.window.destination_format.setCurrentIndex(self.window.destination_format.findData("serato"))
        with patch("crate_transfer.ui.QMessageBox.question", return_value=QMessageBox.StandardButton.No) as question:
            self.window.export_portable()
            self.assertIn("Version upgrade", question.call_args.args[2])
            self.assertIn("not an in-place library upgrade", question.call_args.args[2])
        self.assertEqual(self.errors, [])

    def test_mixxx_source_can_be_selected_without_probing_it_on_startup(self):
        index = self.window.source_format.findData("mixxx")
        self.assertGreaterEqual(index, 0)
        self.window.source_format.setCurrentIndex(index)
        self.assertIn("No existing library", self.window.library_status.text())
        self.assertIn("Close Mixxx", self.window.source_hint.text())
        self.assertEqual(self.errors, [])
        for index in range(self.window.destination_format.count()):
            if "Dual-format USB" in self.window.destination_format.itemText(index):
                self.assertFalse(self.window.destination_format.model().item(index).isEnabled())
                break
        else:
            self.fail("Dual-format USB must be explicitly represented as unavailable")

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

    def test_pairing_endpoint_follows_discovery_on_tick_and_before_send_without_changing_trust(self):
        from types import SimpleNamespace
        peer_id = "22222222-2222-4222-8222-222222222222"
        peer = {"id": peer_id, "name": "Windows", "host": "192.168.1.20",
                "port": 49783, "fingerprint": "a" * 64, "token": "b" * 64}
        self.settings.update(peers={peer_id: peer})
        discovered = {**peer, "host": "192.168.1.30", "port": 48783}
        self.window.discovery = SimpleNamespace(snapshot=lambda: [dict(discovered)])
        try:
            self.window.tick()
            expected = {**peer, "host": "192.168.1.30", "port": 48783}
            self.assertEqual(self.settings.snapshot()["peers"][peer_id], expected)
            discovered.update(host="192.168.1.40", port=48784)
            self.window.tick()
            expected.update(host="192.168.1.40", port=48784)
            self.assertEqual(self.settings.snapshot()["peers"][peer_id], expected)
            self.window.target.setCurrentIndex(self.window.target.findData(peer_id))
            discovered.update(port=48785)
            with patch.object(self.window, "selected_crates", return_value=[10]), \
                    patch("crate_transfer.ui.QMessageBox.question",
                          return_value=QMessageBox.StandardButton.Yes), \
                    patch("crate_transfer.ui.send_crates") as send:
                self.window.send()
                self.wait_for_tasks()
            send.assert_called_once()
            expected["port"] = 48785
            self.assertEqual(Settings(self.settings.directory).snapshot()["peers"][peer_id], expected)
            discovered.update(port=49999, fingerprint="c" * 64)
            self.window.tick()
            self.assertEqual(self.settings.snapshot()["peers"][peer_id], expected)
            self.assertEqual(self.window.computers.topLevelItem(0).text(3),
                             "Identity changed - pair again")
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
