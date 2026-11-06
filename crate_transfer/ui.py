from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMenu, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSpinBox,
    QStackedWidget, QSystemTrayIcon, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from .agent import Agent, check_transfer, pair_computer, send_crates
from .config import Settings
from .discovery import Discovery
from .library import Library
from .protocol import label
from . import startup

LOG = logging.getLogger(__name__)

STYLE = """
QWidget { background: #101318; color: #e8ecf1; font-family: ".AppleSystemUIFont", "Segoe UI";
          font-size: 13px; }
QMainWindow, QDialog { background: #101318; }
QLabel { background: transparent; }
QLabel#title { font-size: 27px; font-weight: 700; }
QLabel#subtitle, QLabel#muted { color: #929eaf; }
QLabel#eyebrow { color: #7de2c4; font-weight: 700; font-size: 11px; }
QLabel[state="success"], QLabel#muted[state="success"] { color: #62e89f; font-weight: 700; }
QLabel[state="error"], QLabel#muted[state="error"] { color: #ff737d; font-weight: 700; }
QLabel[state="neutral"], QLabel#muted[state="neutral"] { color: #929eaf; font-weight: 400; }
QFrame#card { background: #191e26; border: 1px solid #2b3340; border-radius: 12px; }
QFrame#card QLabel, QWidget#pathField { background: transparent; }
QMenu { background: #1e2631; color: #e8ecf1; border: 1px solid #53677f; padding: 5px; }
QMenu::item { background: transparent; padding: 7px 24px; }
QMenu::item:selected { background: #387c68; color: #ffffff; }
QMenu::item:disabled { color: #929eaf; }
QMenu::separator { height: 1px; background: #354153; margin: 4px 8px; }
QPushButton { background: #242d39; border: 1px solid #354153; padding: 9px 16px;
              border-radius: 7px; font-weight: 600; min-height: 18px; }
QPushButton:hover { background: #313e50; }
QPushButton:disabled { color: #66717f; background: #1b222c; border-color: #29313b; }
QPushButton#primary { background: #7de2c4; color: #10251e; border-color: #7de2c4; }
QPushButton#primary:hover { background: #a0eed8; }
QPushButton#primary:disabled { background: #263c35; color: #70877f; border-color: #304b42; }
QPushButton#nav { border: none; background: transparent; text-align: left; padding: 13px 15px; }
QPushButton#nav:checked { background: #24352f; color: #8ce8ce; }
QLineEdit, QComboBox, QSpinBox { background: #191f29; border: 1px solid #354153;
                              padding: 9px; min-height: 18px; border-radius: 6px; selection-background-color: #387c68; }
QTreeWidget { background: #151a22; border: 1px solid #2b3340; border-radius: 9px;
              alternate-background-color: #191f28; outline: none; }
QTreeWidget::item { padding: 8px; border-bottom: 1px solid #222b36; }
QTreeWidget::item:selected { background: #273f38; color: #b7ffe9; }
QHeaderView::section { background: #1e2631; color: #9dacbf; border: none; padding: 11px; font-weight: 600; }
QProgressBar { border: none; background: #2a3440; height: 6px; border-radius: 3px; }
QProgressBar::chunk { background: #7de2c4; border-radius: 3px; }
QScrollArea { border: none; }
QToolTip { color: #e8ecf1; background: #283444; border: 1px solid #53677f; padding: 5px; }
"""


def app_icon(size=64) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 64, size / 64)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#7de2c4"))
    painter.drawRoundedRect(4, 4, 56, 56, 14, 14)
    painter.setBrush(QColor("#10251e"))
    for x, height in ((17, 15), (27, 31), (37, 23), (47, 11)):
        painter.drawRoundedRect(x, 32 - height // 2, 5, height, 2, 2)
    painter.end()
    return QIcon(pixmap)


def text(value, object_name=None, wrap=False):
    widget = QLabel(value)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if object_name:
        widget.setObjectName(object_name)
    widget.setWordWrap(wrap)
    return widget


def status_style(widget, state):
    widget.setProperty("state", state)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def button(value, callback, primary=False):
    widget = QPushButton(value)
    if primary:
        widget.setObjectName("primary")
    widget.clicked.connect(callback)
    return widget


def card():
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(20, 18, 20, 18)
    layout.setSpacing(12)
    return frame, layout


class Bridge(QObject):
    changed = Signal()
    discovered = Signal()
    completed = Signal(object, object)


class PairDialog(QDialog):
    def __init__(self, parent, candidate=None):
        super().__init__(parent)
        self.setWindowTitle("Pair a computer")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.addWidget(text("One-time pairing", "title"))
        layout.addWidget(text("On the other computer, click Allow pairing, then enter its 6-digit PIN here. "
                              "Use a trusted private network for this first connection. Pairing grants this computer "
                              "permission to send and receive crates in both directions while the apps are running.",
                              "muted", True))
        form = QFormLayout()
        candidate = candidate or {}
        self.host = QLineEdit(candidate.get("host", ""))
        self.host.setPlaceholderText("192.168.1.25 or computer.local")
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(candidate.get("port", 49783))
        self.code = QLineEdit()
        self.code.setMaxLength(6)
        self.code.setPlaceholderText("123456")
        for caption, widget in (("Host / IP address", self.host), ("Port", self.port),
                                ("Pairing PIN", self.code)):
            form.addRow(caption, widget)
        layout.addLayout(form)
        self.error = text("", "muted", True)
        layout.addWidget(self.error)
        controls = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.pair_button = controls.addButton("Pair computer", QDialogButtonBox.ButtonRole.AcceptRole)
        self.pair_button.setObjectName("primary")
        self.pair_button.clicked.connect(self.accept)
        controls.rejected.connect(self.reject)
        layout.addWidget(controls)

    def values(self):
        host = self.host.text().strip()
        if not host or "/" in host or len(host) > 253:
            raise ValueError("Enter the receiving computer's hostname or IPv4 address, not a URL.")
        code = self.code.text().strip()
        if not code.isascii() or not code.isdigit() or len(code) != 6:
            raise ValueError("Enter the other computer's 6-digit pairing code (PIN).")
        return host, self.port.value(), None, code


class Window(QMainWindow):
    def __init__(self, settings: Settings, start_services=True):
        super().__init__()
        self.settings = settings
        self.bridge = Bridge()
        self.executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="crate-work")
        self.bridge.completed.connect(self.task_completed)
        self.bridge.changed.connect(self.refresh_activity)
        self.bridge.discovered.connect(self.refresh_computers)
        self.agent = Agent(settings, notify=self.bridge.changed.emit)
        self.discovery = None
        self.port = settings.snapshot()["port"]
        self.active_tasks = 0
        self.sending = False
        self.cancel_send = threading.Event()
        self.loading = False
        self.crates_loaded = False
        self.exiting = False
        self.last_history = None
        self.setWindowTitle("Crate Transfer")
        self.setWindowIcon(app_icon())
        self.resize(1140, 770)
        self.setMinimumSize(980, 650)
        self.build()
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip("Crate Transfer - paired LAN agent")
        menu = QMenu()
        menu.addAction("Open Crate Transfer", self.show_window)
        menu.addAction("Quit", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.show_window()
                                    if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        if start_services:
            try:
                self.port = self.agent.start()
                self.receiver_status.setText(f"Agent online  /  TLS :{self.port}")
                status_style(self.receiver_status, "success")
            except OSError as error:
                self.receiver_status.setText("Agent offline")
                status_style(self.receiver_status, "error")
                QTimer.singleShot(0, lambda message=str(error): self.show_error("Agent could not start", message))
            else:
                try:
                    self.discovery = Discovery(settings.snapshot(), self.port, self.agent.fingerprint,
                                               self.bridge.discovered.emit)
                except Exception as error:
                    LOG.exception("Network discovery failed")
                    self.network_status.setText(f"Discovery unavailable: {error}. Use Pair by address.")
                    status_style(self.network_status, "error")
        else:
            self.receiver_status.setText("UI test mode / agent not started")
        self.refresh_computers()
        self.refresh_activity()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)
        if self.agent.startup_warning:
            QTimer.singleShot(0, lambda: QMessageBox.warning(
                self, "Transfer history recovered", self.agent.startup_warning,
            ))
        if start_services:
            QTimer.singleShot(0, self.load_crates)

    def run_task(self, action, callback=lambda result: None):
        self.active_tasks += 1

        def execute():
            try:
                result = action()
            except Exception as error:
                LOG.exception("Desktop operation failed")
                self.bridge.completed.emit((callback, None), error)
            else:
                self.bridge.completed.emit((callback, result), None)

        self.executor.submit(execute)

    def task_completed(self, payload, error):
        self.active_tasks -= 1
        callback, result = payload
        callback(result)
        if error is not None:
            self.show_error("Operation could not complete", str(error))

    def build(self):
        shell = QWidget()
        self.setCentralWidget(shell)
        layout = QHBoxLayout(shell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        sidebar = QWidget()
        sidebar.setFixedWidth(205)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 28, 18, 22)
        side.setSpacing(8)
        side.addWidget(text("CRATE / TRANSFER", "eyebrow"))
        side.addWidget(text("Your music.\nYour machines.", "muted"))
        side.addSpacing(28)
        self.pages = QStackedWidget()
        self.navigation = []
        for index, name in enumerate(("Send crates", "Computers", "Activity", "Settings")):
            nav = QPushButton(name)
            nav.setObjectName("nav")
            nav.setCheckable(True)
            nav.clicked.connect(lambda checked=False, i=index: self.page(i))
            self.navigation.append(nav)
            side.addWidget(nav)
        side.addStretch()
        side.addWidget(text("LOCAL NETWORK ONLY", "eyebrow"))
        self.receiver_status = text("Starting agent...", "muted", True)
        side.addWidget(self.receiver_status)
        side.addWidget(text("Manual transfers. Never sync.", "muted", True))
        layout.addWidget(sidebar)
        layout.addWidget(self.pages, 1)
        self.pages.addWidget(self.send_page())
        self.pages.addWidget(self.computers_page())
        self.pages.addWidget(self.activity_page())
        self.pages.addWidget(self.settings_page())
        self.page(0)

    def page(self, index):
        self.pages.setCurrentIndex(index)
        for i, nav in enumerate(self.navigation):
            nav.setChecked(i == index)

    def page_layout(self, title, subtitle):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(30, 28, 30, 26)
        layout.setSpacing(18)
        layout.addWidget(text(title, "title"))
        layout.addWidget(text(subtitle, "subtitle", True))
        return widget, layout

    def send_page(self):
        widget, layout = self.page_layout(
            "Send your next set.",
            "Choose crates, pick a paired computer, and send. Music and crate paths travel together.",
        )
        banner, content = card()
        content.addWidget(text("SERATO DJ 4 LIBRARY", "eyebrow"))
        content.addWidget(text("Original files. Local paths. Nothing overwritten.", wrap=True))
        content.addWidget(text("Subcrates are included when you select a parent. The other computer preserves "
                               "original crate names. Quit Serato on that computer first.", "muted", True))
        layout.addWidget(banner)
        toolbar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter crates...")
        self.search.textChanged.connect(self.filter_crates)
        toolbar.addWidget(self.search, 1)
        self.refresh_button = button("Refresh library", self.load_crates)
        toolbar.addWidget(self.refresh_button)
        layout.addLayout(toolbar)
        self.crate_tree = QTreeWidget()
        self.crate_tree.setHeaderLabels(["Crate", "Tracks"])
        self.crate_tree.setColumnWidth(0, 500)
        self.crate_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.crate_tree.setAlternatingRowColors(True)
        self.crate_tree.itemChanged.connect(self.selection_changed)
        layout.addWidget(self.crate_tree, 1)
        self.library_status = text("Library not loaded. Configure a Serato 4 Library folder in Settings.", "muted", True)
        layout.addWidget(self.library_status)
        footer = QHBoxLayout()
        self.selection_status = text("No crates selected", "muted")
        footer.addWidget(self.selection_status, 1)
        self.target = QComboBox()
        self.target.setMinimumWidth(200)
        footer.addWidget(self.target)
        self.send_button = button("Send selected crates", self.send, True)
        self.send_button.setEnabled(False)
        self.target.currentIndexChanged.connect(self.selection_changed)
        footer.addWidget(self.send_button)
        layout.addLayout(footer)
        return widget

    def computers_page(self):
        widget, layout = self.page_layout("Your computers.", "Run Crate Transfer on each machine. Pair once; send only when you choose.")
        own, content = card()
        self.own_name = text(self.settings.snapshot()["name"])
        content.addWidget(self.own_name)
        self.addresses = text(f"Port {self.port} / default receive folder: {self.settings.snapshot()['music']}", "muted", True)
        content.addWidget(self.addresses)
        content.addWidget(text("CERTIFICATE IDENTITY / MANAGED AUTOMATICALLY", "eyebrow"))
        self.fingerprint_label = text(self.agent.fingerprint)
        self.fingerprint_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.fingerprint_label.setWordWrap(True)
        content.addWidget(self.fingerprint_label)
        pairing = QHBoxLayout()
        pairing.addWidget(button("Allow pairing for 2 minutes", self.allow_pairing, True))
        self.pairing_status = text("Pairing is closed", "muted")
        pairing.addWidget(self.pairing_status, 1)
        content.addLayout(pairing)
        layout.addWidget(own)
        toolbar = QHBoxLayout()
        toolbar.addWidget(text("DISCOVERED & PAIRED", "eyebrow"), 1)
        toolbar.addWidget(button("Pair by address", lambda: self.pair_dialog()))
        layout.addLayout(toolbar)
        self.computers = QTreeWidget()
        self.computers.setHeaderLabels(["Computer", "Address", "Access", "Connection"])
        self.computers.setColumnWidth(0, 230)
        self.computers.setColumnWidth(1, 200)
        self.computers.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.computers, 1)
        controls = QHBoxLayout()
        controls.addWidget(button("Pair selected computer", self.pair_selected))
        controls.addWidget(button("Forget / revoke selected", self.revoke))
        controls.addStretch()
        layout.addLayout(controls)
        self.network_status = text("Bonjour / mDNS discovers agents on the same subnet. Pair by address if multicast is blocked.",
                                   "muted", True)
        layout.addWidget(self.network_status)
        layout.addWidget(text("Pair once to enable both sending and receiving. Transfers are always manual.", "muted", True))
        return widget

    def activity_page(self):
        widget, layout = self.page_layout("Transfer activity.", "Verified files first. Backed-up library import second.")
        self.activities = QTreeWidget()
        self.activities.setHeaderLabels(["Transfer", "Computer", "Status"])
        self.activities.setColumnWidth(0, 250)
        self.activities.setColumnWidth(1, 190)
        self.activities.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.activities.currentItemChanged.connect(self.activity_selected)
        layout.addWidget(self.activities, 1)
        self.job_progress = QProgressBar()
        self.job_progress.setRange(0, 1000)
        self.job_progress.setTextVisible(False)
        layout.addWidget(self.job_progress)
        self.job_detail = text("No transfers yet. Nothing is sent automatically.", "muted", True)
        self.job_detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.job_detail)
        controls = QHBoxLayout()
        controls.addWidget(button("Open backups", lambda: self.open_folder(self.settings.directory / "backups")))
        controls.addWidget(button("Open received music", lambda: self.open_folder(Path(self.settings.snapshot()["music"]))))
        self.cancel_send_button = button("Cancel send", self.cancel_send.set)
        self.cancel_send_button.setEnabled(False)
        controls.addWidget(self.cancel_send_button)
        controls.addWidget(button("Cancel pending receive", self.cancel_receive))
        controls.addWidget(button("Check uncertain transfer", self.check_selected_transfer))
        controls.addStretch()
        layout.addLayout(controls)
        return widget

    def settings_page(self):
        widget, layout = self.page_layout("Make it local.", "Each computer controls where incoming music is stored.")
        box, content = card()
        form = QFormLayout()
        form.setSpacing(18)
        self.name_input = QLineEdit(self.settings.snapshot()["name"])
        form.addRow("Computer name", self.name_input)
        self.library_input = QLineEdit(self.settings.snapshot()["library"])
        self.library_input.setObjectName("libraryPath")
        self.library_input.setPlaceholderText("Serato folder or its Library subfolder")
        form.addRow("Serato 4 Library folder", self.path_field(self.library_input))
        self.music_input = QLineEdit(self.settings.snapshot()["music"])
        self.music_input.setObjectName("musicPath")
        form.addRow("Received music folder", self.path_field(self.music_input))
        self.port_input = QSpinBox()
        self.port_input.setRange(1024, 65535)
        self.port_input.setValue(self.settings.snapshot()["port"])
        form.addRow("Agent TCP port", self.port_input)
        form.addRow("", button("Save connection port", self.save_connection_port))
        self.startup_input = QCheckBox("Launch at login (quietly in the menu bar / system tray)")
        try:
            self.startup_input.setChecked(startup.enabled())
        except OSError as error:
            LOG.exception("Could not read login startup setting")
            QTimer.singleShot(0, lambda message=str(error): self.show_error("Could not read startup setting", message))
        form.addRow("Startup", self.startup_input)
        content.addLayout(form)
        content.addWidget(text("Choose the Serato folder or its Library subfolder, which contains root.sqlite and master.sqlite. Defaults are detected for macOS "
                               "and Windows. Received music must be on the same local volume as this Library.", "muted", True))
        content.addWidget(text("Files use readable crate folders. Shared tracks are reused "
                               "only after checksum verification. Existing music and crates are never replaced.", "muted", True))
        self.save_button = button("Save settings", self.save_settings, True)
        content.addWidget(self.save_button)
        layout.addWidget(box)
        layout.addWidget(text("AGENT BEHAVIOR", "eyebrow"))
        layout.addWidget(text("Closing this window leaves the agent online in the menu bar / system tray. "
                              "Use Quit to stop it. Launch at login is optional and takes effect at your next login. "
                              "On macOS, install the app in Applications first.", "muted", True))
        layout.addWidget(text("This app does not synchronize, watch music folders, delete remote files, or use a cloud service. "
                              "Normal local-file crates are supported; smart-crate rules and streaming tracks are not transferred.",
                              "muted", True))
        layout.addStretch()
        layout.addWidget(button("Open application log", lambda: self.open_folder(self.settings.directory)))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(widget)
        return scroll

    def path_field(self, line_edit):
        widget = QWidget()
        widget.setObjectName("pathField")
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(line_edit, 1)

        def choose():
            chosen = QFileDialog.getExistingDirectory(self, "Choose folder", line_edit.text())
            if chosen:
                line_edit.setText(chosen)

        def copy_path():
            QApplication.clipboard().setText(line_edit.text())
            self.statusBar().showMessage("Folder path copied to clipboard", 3000)

        copy_button = button("Copy", copy_path)
        copy_button.setObjectName(f"copy-{line_edit.objectName()}")
        copy_button.setToolTip("Copy the full folder path to the clipboard")
        copy_button.setAccessibleName(f"Copy {line_edit.objectName()} folder path")
        layout.addWidget(copy_button)
        layout.addWidget(button("Browse...", choose))
        return widget

    def load_crates(self):
        if self.loading or self.sending:
            return
        self.loading = True
        self.refresh_button.setEnabled(False)
        self.library_status.setText("Reading Serato 4 crate list...")
        library = Library(self.settings.snapshot()["library"])

        def done(crates):
            self.loading = False
            self.refresh_button.setEnabled(True)
            self.crate_tree.clear()
            self.crates_loaded = crates is not None
            if crates is None:
                self.library_status.setText("Library unavailable. Check Settings, then refresh.")
                status_style(self.library_status, "error")
                self.selection_changed()
                return
            nodes = {}
            for crate in sorted(crates, key=lambda value: [part.casefold() for part in value["path"]]):
                path = ()
                for part in crate["path"]:
                    key = path + (part,)
                    if key not in nodes:
                        item = QTreeWidgetItem([part, ""])
                        (nodes[path].addChild(item) if path else self.crate_tree.addTopLevelItem(item))
                        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                        item.setCheckState(0, Qt.CheckState.Unchecked)
                        nodes[key] = item
                    path = key
                nodes[path].setText(1, str(crate["tracks"]))
                nodes[path].setData(0, Qt.ItemDataRole.UserRole, crate["id"])
                nodes[path].setToolTip(0, " / ".join(crate["path"]))
            self.crate_tree.expandToDepth(0)
            self.library_status.setText(f"{len(crates)} normal crates / Serato 4 SQLite library / source files stay untouched")
            status_style(self.library_status, "neutral")
            self.filter_crates()
            self.selection_changed()

        self.run_task(library.crates, done)

    def all_items(self):
        def descend(parent):
            for i in range(parent.childCount()):
                item = parent.child(i)
                yield item
                yield from descend(item)
        return list(descend(self.crate_tree.invisibleRootItem()))

    def selected_crates(self):
        return [item.data(0, Qt.ItemDataRole.UserRole) for item in self.all_items()
                if item.checkState(0) == Qt.CheckState.Checked
                and item.data(0, Qt.ItemDataRole.UserRole) is not None]

    def selection_changed(self, *args):
        selected = self.selected_crates()
        self.selection_status.setText(f"{len(selected)} selected / includes subcrates" if selected else "No crates selected")
        self.send_button.setEnabled(bool(selected) and self.target.currentData() is not None and not self.sending)

    def filter_crates(self):
        query = self.search.text().casefold().strip()
        for item in reversed(self.all_items()):
            match = query in item.toolTip(0).casefold() or query in item.text(0).casefold()
            child_visible = any(not item.child(i).isHidden() for i in range(item.childCount()))
            item.setHidden(not (match or child_visible))
            if query and child_visible:
                item.setExpanded(True)

    def send(self):
        self.refresh_computers()
        selected, peer_id = self.selected_crates(), self.target.currentData()
        if not selected or peer_id is None or self.sending:
            return
        peer = self.settings.snapshot()["peers"][peer_id]
        answer = QMessageBox.question(
            self, "Send crates and music?",
            f"Send the selected crates, their subcrates and referenced music files to {peer['name']}?\n\n"
            "Original crate names are preserved, with a number added only for name collisions. "
            "Existing crates will not be merged or replaced. "
            "Serato must be closed on that computer.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.sending = True
        self.cancel_send.clear()
        self.cancel_send_button.setEnabled(True)
        self.refresh_button.setEnabled(False)
        self.selection_changed()
        self.page(2)

        def done(result):
            self.sending = False
            self.cancel_send_button.setEnabled(False)
            self.refresh_button.setEnabled(True)
            self.selection_changed()
            self.refresh_activity()

        self.run_task(lambda: send_crates(self.agent, selected, peer_id, self.cancel_send.is_set), done)

    def allow_pairing(self):
        code = self.agent.pairing()
        self.pairing_status.setText(f"PIN {code} / 120 seconds")
        status_style(self.pairing_status, "success")

    def tick(self):
        remaining = max(0, int(self.agent.pair_until - time.monotonic()))
        self.pairing_status.setText(f"PIN {self.agent.pair_code} / {remaining}s" if remaining else "Pairing is closed")
        status_style(self.pairing_status, "success" if remaining else "neutral")
        own = self.settings.snapshot()
        state = (own["peers"], own["trusted"],
                 self.discovery.snapshot() if self.discovery else [])
        if state != getattr(self, "last_pairing_state", None):
            self.last_pairing_state = state
            self.refresh_computers()

    def refresh_computers(self):
        own = self.settings.snapshot()
        discovered = {peer["id"]: peer for peer in self.discovery.snapshot()} if self.discovery else {}
        peers = {key: dict(value) for key, value in own["peers"].items()}
        changed = False
        for key, peer in peers.items():
            candidate = discovered.get(key)
            if candidate and candidate["fingerprint"] == peer["fingerprint"]:
                if (peer["host"], peer["port"]) != (candidate["host"], candidate["port"]):
                    peer.update(host=candidate["host"], port=candidate["port"])
                    changed = True
        if changed:
            self.settings.update(peers=peers)
        selected_id = self.target.currentData()
        self.target.clear()
        self.target.addItem("Choose computer...", None)
        self.computers.clear()
        for key in sorted(set(discovered) | set(peers) | set(own["trusted"]),
                          key=lambda value: (peers.get(value) or discovered.get(value) or own["trusted"][value])["name"].casefold()):
            peer = peers.get(key) or discovered.get(key) or own["trusted"][key]
            access = []
            if key in peers:
                self.target.addItem(peer["name"], key)
                access.append("Can send")
            if key in own["trusted"]:
                access.append("Can receive from")
            if not access:
                access.append("Not paired")
            candidate = discovered.get(key)
            online = candidate is not None and (key not in peers or candidate["fingerprint"] == peers[key]["fingerprint"])
            connection = "Online" if online else "Not discovered"
            if candidate and key in peers and not online:
                connection = "Identity changed - pair again"
            address = f"{peer.get('host', 'Address not discovered')}:{peer.get('port', '')}"
            item = QTreeWidgetItem([peer["name"], address, " / ".join(access), connection])
            if online or connection.startswith("Identity changed"):
                item.setForeground(3, QColor("#62e89f" if online else "#ff737d"))
                font = item.font(3)
                font.setBold(True)
                item.setFont(3, font)
            item.setToolTip(3, "LAN discovery presence; certificate verification is enforced when connecting. "
                              "Not discovered can mean offline or multicast blocked; manual addressing still works.")
            item.setData(0, Qt.ItemDataRole.UserRole, {**peer, "id": key})
            self.computers.addTopLevelItem(item)
        index = self.target.findData(selected_id)
        if index >= 0:
            self.target.setCurrentIndex(index)
        self.selection_changed()

    def pair_selected(self):
        item = self.computers.currentItem()
        if item is None:
            self.show_error("Choose a computer", "Select a discovered computer, or use Pair by address.")
            return
        self.pair_dialog(item.data(0, Qt.ItemDataRole.UserRole))

    def pair_dialog(self, candidate=None):
        dialog = PairDialog(self, candidate)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            values = dialog.values()
        except ValueError as error:
            self.show_error("Pairing details incomplete", str(error))
            return
        self.run_task(lambda: pair_computer(self.settings, *values), lambda result: self.refresh_computers())

    def revoke(self):
        item = self.computers.currentItem()
        if item is None:
            return
        peer = item.data(0, Qt.ItemDataRole.UserRole)
        answer = QMessageBox.question(self, "Remove pairing?",
                                      f"Forget {peer['name']} and revoke its incoming access on this computer?\n\n"
                                      "To revoke this computer's access on the other machine, remove it there too.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        own = self.settings.snapshot()
        own["peers"].pop(peer["id"], None)
        own["trusted"].pop(peer["id"], None)
        self.settings.update(peers=own["peers"], trusted=own["trusted"])
        self.refresh_computers()

    def refresh_activity(self):
        history = self.agent.history()
        if history == self.last_history:
            return
        self.last_history = history
        selected = self.activities.currentItem()
        selected_id = selected.data(0, Qt.ItemDataRole.UserRole)["id"] if selected else None
        self.activities.clear()
        for job in history:
            timestamp = time.strftime("%d %b %H:%M", time.localtime(job["created"]))
            item = QTreeWidgetItem([f"{job.get('direction', 'transfer').capitalize()} / {timestamp}",
                                    job.get("peer", ""), job["status"].capitalize()])
            item.setData(0, Qt.ItemDataRole.UserRole, job)
            if job["status"] in ("failed", "uncertain", "complete"):
                item.setForeground(2, QColor("#62e89f" if job["status"] == "complete" else "#ff737d"))
                font = item.font(2)
                font.setBold(True)
                item.setFont(2, font)
            self.activities.addTopLevelItem(item)
            if selected_id == job["id"] or (selected_id is None and self.activities.topLevelItemCount() == 1):
                self.activities.setCurrentItem(item)
        self.activity_selected(self.activities.currentItem(), None)

    def activity_selected(self, current, previous):
        if current is None:
            return
        job = current.data(0, Qt.ItemDataRole.UserRole)
        self.job_progress.setValue(int(job.get("progress", 0) * 1000))
        detail = job.get("detail", "")
        if job.get("result", {}).get("backup"):
            detail += "\nBackup on destination computer: " + job["result"]["backup"]
        self.job_detail.setText(detail)
        status_style(self.job_detail, "error" if job["status"] in ("failed", "uncertain") else
                     "success" if job["status"] == "complete" else "neutral")

    def check_selected_transfer(self):
        current = self.activities.currentItem()
        if current is None:
            self.show_error("Select a transfer", "Select an uncertain outgoing transfer to check its outcome.")
            return
        job = current.data(0, Qt.ItemDataRole.UserRole)
        self.run_task(lambda: check_transfer(self.agent, job["id"]))

    def cancel_receive(self):
        # A session only changes under the agent lock. Cancellation is queued
        # behind the current file upload and never interrupts a DB transaction.
        session = self.agent.session
        if session is None:
            self.show_error("No pending receive", "There is no incoming transfer to cancel.")
            return
        session_id, peer = session["id"], session["peer"]
        self.run_task(lambda: self.agent.cancel(session_id, peer))

    def save_connection_port(self):
        if self.sending or self.agent.session:
            self.show_error("Transfer in progress", "Finish or cancel transfers before changing the connection port.")
            return
        try:
            self.settings.update(port=self.port_input.value())
        except OSError as error:
            self.show_error("Connection port was not saved", str(error))
            return
        QMessageBox.information(
            self, "Connection port saved",
            f"TCP port {self.port_input.value()} saved. Quit the agent from its tray menu and reopen it to apply. "
            "Library folders and startup settings were not changed.",
        )

    def save_settings(self):
        if self.active_tasks or self.agent.session:
            self.show_error("Transfer in progress", "Finish or cancel transfers before changing folders.")
            return
        try:
            name = label(self.name_input.text().strip(), 100)
            library = Path(self.library_input.text().strip()).expanduser()
            music = Path(self.music_input.text().strip()).expanduser()
            if not self.library_input.text().strip() or not self.music_input.text().strip():
                raise ValueError("Choose both a Library folder and a received music folder.")
            if not library.is_absolute() or not music.is_absolute():
                raise ValueError("Folder paths must be absolute.")
            resolved_library = Library(library)
            resolved_library.validate_destination(music)
            restart = name != self.settings.snapshot()["name"] or self.port_input.value() != self.settings.snapshot()["port"]
            if self.startup_input.isChecked() != startup.enabled():
                startup.configure(self.startup_input.isChecked(), self.settings.directory)
            self.settings.update(name=name, library=str(resolved_library.directory), music=str(music.resolve()), port=self.port_input.value())
        except (ValueError, OSError) as error:
            self.show_error("Settings were not saved", str(error))
            return
        self.own_name.setText(name)
        self.library_input.setText(str(resolved_library.directory))
        self.addresses.setText(f"Port {self.port} / default receive folder: {music}")
        self.crates_loaded = False
        self.load_crates()
        QMessageBox.information(self, "Settings saved",
                                "Folders saved. Restart the app to apply the computer name or port change."
                                if restart else "Settings saved. Incoming transfers will use your chosen music folder.")

    def show_error(self, title, message):
        dialog = QMessageBox(QMessageBox.Icon.Warning, title, message, QMessageBox.StandardButton.Ok, self)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setStyleSheet("QLabel { color: #ff737d; font-weight: 700; }")
        dialog.exec()

    def open_folder(self, path):
        path = path.expanduser()
        if not path.exists():
            self.show_error("Folder does not exist yet", str(path))
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            self.show_error("Could not open folder", str(path))

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        if not self.exiting and self.tray.isVisible():
            self.hide()
            event.ignore()
        elif not self.exiting:
            event.ignore()
            self.quit()
        else:
            event.accept()

    def quit(self):
        if self.active_tasks or self.agent.session:
            QMessageBox.warning(self, "Agent is busy", "Finish or cancel the current transfer before quitting.")
            return
        self.exiting = True
        self.timer.stop()
        self.tray.hide()
        if self.discovery:
            self.discovery.stop()
        self.agent.stop()
        self.executor.shutdown(wait=True)
        QApplication.instance().quit()
