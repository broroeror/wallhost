#!/usr/bin/env python3
"""
wallhost picker — choose a Wallpaper Engine wallpaper for each screen.

A thin Qt front end over wallhost.py: it reads the workshop folder, writes
~/.config/wallhost.json through wallhost's own helpers, and asks a running
wallhost to reload. It never talks to the renderer directly, except for the
Preview button, which runs one in an ordinary window.

Thumbnails are decoded off the UI thread and cached in ~/.cache/wallhost/thumbs,
since the workshop previews are full-size JPEGs and animated GIFs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QImage, QImageReader, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog,
                               QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMainWindow, QMessageBox, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

import wallhost
import wallhost_fixes

INSTANCE_KEY = "wallhost-picker"
ICON_FILE = Path(__file__).resolve().parent / "icons" / "wallhost.svg"


def app_icon() -> QIcon:
    """The installed theme icon, or the bundled one when running from source."""
    return QIcon.fromTheme("wallhost", QIcon(str(ICON_FILE)))
THUMB_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "wallhost" / "thumbs"
THUMB = QSize(256, 144)
TYPES = ["all", "scene", "video", "web", "preset"]


@dataclass
class Wallpaper:
    wid: str
    title: str
    kind: str          # scene | video | web | preset
    preview: Path | None

    @property
    def supported(self) -> bool:
        return self.kind != "preset"


def scan_workshop() -> list[Wallpaper]:
    found = []
    for proj in wallhost.workshop_projects():
        try:
            meta = json.loads(proj.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        # Presets carry no type of their own, only settings for their "dependency".
        kind = str(meta.get("type") or ("preset" if meta.get("dependency") else "?")).lower()
        prev = proj.parent / meta["preview"] if meta.get("preview") else None
        found.append(Wallpaper(proj.parent.name, str(meta.get("title") or proj.parent.name),
                               kind, prev if prev and prev.exists() else None))
    return sorted(found, key=lambda w: w.title.casefold())


# ---------------------------------------------------------------- thumbnails

class ThumbSignals(QObject):
    done = Signal(str, QImage)


class ThumbJob(QRunnable):
    def __init__(self, wp: Wallpaper, signals: ThumbSignals):
        super().__init__()
        self.wp, self.signals = wp, signals

    def run(self) -> None:
        cached = THUMB_DIR / f"{self.wp.wid}.jpg"
        img = QImage()
        if cached.exists() and self.wp.preview and cached.stat().st_mtime >= self.wp.preview.stat().st_mtime:
            img.load(str(cached))
        if img.isNull() and self.wp.preview:
            reader = QImageReader(str(self.wp.preview))
            src = reader.size()
            if src.isValid():
                reader.setScaledSize(src.scaled(THUMB, Qt.KeepAspectRatioByExpanding))
            img = reader.read()                     # first frame for GIFs
            if not img.isNull():
                img = img.copy((img.width() - THUMB.width()) // 2, (img.height() - THUMB.height()) // 2,
                               THUMB.width(), THUMB.height())
                THUMB_DIR.mkdir(parents=True, exist_ok=True)
                img.save(str(cached), "JPG", 85)
        self.signals.done.emit(self.wp.wid, img)


def dimmed(pix: QPixmap) -> QPixmap:
    out = QPixmap(pix.size())
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setOpacity(0.3)
    p.drawPixmap(0, 0, pix)
    p.end()
    return out


# ---------------------------------------------------------------- fixes

STATUS_STYLE = {
    "active": "#2e7d32",
    "pending-on": "#b26a00",
    "pending-off": "#b26a00",
    "off": "#757575",
}


class FixCard(QFrame):
    def __init__(self, fix: wallhost_fixes.Fix, changed):
        super().__init__()
        self.fix, self.changed = fix, changed
        self.setFrameShape(QFrame.StyledPanel)
        lay = QVBoxLayout(self)

        head = QHBoxLayout()
        title = QLabel(f"<b>{fix.title}</b>")
        self.chip = QLabel()
        head.addWidget(title, stretch=1)
        head.addWidget(self.chip)
        lay.addLayout(head)

        for text in (fix.summary,
                     f"<i>Why it might help:</i> {fix.details}",
                     f"<i>Cost:</i> {fix.cost}"):
            label = QLabel(text)
            label.setWordWrap(True)
            lay.addWidget(label)

        self.where = QLabel()
        self.where.setWordWrap(True)
        self.where.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.where)

        row = QHBoxLayout()
        row.addStretch(1)
        self.button = QPushButton()
        self.button.clicked.connect(self.toggle)
        row.addWidget(self.button)
        lay.addLayout(row)
        self.refresh()

    def refresh(self) -> None:
        st = wallhost_fixes.status(self.fix)
        self.chip.setText(wallhost_fixes.STATUS_TEXT[st])
        self.chip.setStyleSheet(f"color: white; background: {STATUS_STYLE[st]}; "
                                "border-radius: 6px; padding: 2px 8px;")
        on = st in ("active", "pending-on")
        self.button.setText("Revert" if on else "Apply")
        what = (f"Adds <code>{self.fix.line}</code> in <code>{self.fix.path}</code>"
                if on else f"Would add <code>{self.fix.line}</code> in <code>{self.fix.path}</code>")
        self.where.setText(f"<small>{what}</small>")

    def toggle(self) -> None:
        if wallhost_fixes.status(self.fix) in ("active", "pending-on"):
            wallhost_fixes.revert(self.fix)
        else:
            wallhost_fixes.apply(self.fix)
        self.changed()


class FixesDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pinstripe fixes")
        self.resize(760, 820)
        wallhost_fixes.migrate_legacy()
        lay = QVBoxLayout(self)

        intro = QLabel(
            "If the moving wallpaper shows through windows or the dock in thin strips, "
            "these KWin settings can stop it. Try them one at a time: apply one, restart, "
            "and see if the strips are gone. If nothing changes or something feels worse, "
            "revert it.<br><br>They affect the whole desktop, not just wallhost, and KWin "
            "only reads them when it starts, so each change needs a <b>restart</b> "
            "of your computer (logging out and back in also works on most setups).")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        self.banner = QFrame()
        self.banner.setStyleSheet("QFrame { background: #fff3e0; border-radius: 6px; } "
                                  "QLabel { color: #5d4037; }")
        bl = QHBoxLayout(self.banner)
        bl.addWidget(QLabel("Changes are waiting for a restart."), stretch=1)
        restart = QPushButton("Restart now…")
        restart.clicked.connect(self.restart)
        bl.addWidget(restart)
        lay.addWidget(self.banner)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        cards = QVBoxLayout(inner)
        self.cards = [FixCard(fix, self.refresh) for fix in wallhost_fixes.FIXES]
        for card in self.cards:
            cards.addWidget(card)
        cards.addStretch(1)
        scroll.setWidget(inner)
        lay.addWidget(scroll, stretch=1)
        self.refresh()

    def refresh(self) -> None:
        for card in self.cards:
            card.refresh()
        self.banner.setVisible(any(wallhost_fixes.status(f).startswith("pending")
                                   for f in wallhost_fixes.FIXES))

    def restart(self) -> None:
        answer = QMessageBox.question(
            self, "Restart now?",
            "Restart the computer now so KWin picks up the changes?\n\n"
            "Save any open work first; open apps will be closed.")
        if answer == QMessageBox.Yes:
            subprocess.Popen(["systemctl", "reboot"])


# ---------------------------------------------------------------- window

class Picker(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Wallpapers")
        self.resize(1320, 860)
        self.wallpapers = {w.wid: w for w in scan_workshop()}
        self.items: dict[str, QListWidgetItem] = {}
        self.preview_proc: subprocess.Popen | None = None

        central = QWidget()
        root = QVBoxLayout(central)
        self.setCentralWidget(central)

        # Screens, laid out left-to-right in their real arrangement.
        root.addWidget(QLabel("Screen:"))
        screen_row = QHBoxLayout()
        self.screen_group = QButtonGroup(self)
        self.screen_buttons: dict[str, QPushButton] = {}
        screens = sorted(QGuiApplication.screens(), key=lambda s: s.geometry().x())
        primary = QGuiApplication.primaryScreen()
        positions = {1: ["Screen"], 2: ["Left", "Right"], 3: ["Left", "Middle", "Right"]}
        names = positions.get(len(screens), [f"Screen {i + 1}" for i in range(len(screens))])
        for scr, position in zip(screens, names):
            btn = QPushButton()
            btn.setCheckable(True)
            btn.setMinimumHeight(56)
            btn.setProperty("output", scr.name())
            btn.setProperty("position", position)
            self.screen_group.addButton(btn)
            self.screen_buttons[scr.name()] = btn
            screen_row.addWidget(btn, stretch=scr.geometry().width())
            if scr is primary:
                btn.setChecked(True)
        self.screen_group.buttonToggled.connect(lambda *_: self.refresh_selection())
        root.addLayout(screen_row)

        # Filters.
        filters = QHBoxLayout()
        self.search = QLineEdit(placeholderText="Search titles…")
        self.search.textChanged.connect(self.apply_filter)
        self.type_box = QComboBox()
        for t in TYPES:
            n = sum(1 for w in self.wallpapers.values() if t == "all" or w.kind == t)
            self.type_box.addItem(f"{t.capitalize()} ({n})", t)
        self.type_box.currentIndexChanged.connect(self.apply_filter)
        filters.addWidget(self.search, stretch=1)
        filters.addWidget(self.type_box)
        root.addLayout(filters)

        # Grid.
        self.grid = QListWidget()
        self.grid.setViewMode(QListWidget.IconMode)
        self.grid.setIconSize(THUMB)
        self.grid.setGridSize(QSize(THUMB.width() + 20, THUMB.height() + 56))
        self.grid.setResizeMode(QListWidget.Adjust)
        self.grid.setMovement(QListWidget.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setWordWrap(True)
        self.grid.itemDoubleClicked.connect(lambda _: self.apply_selected())
        self.grid.currentItemChanged.connect(lambda *_: self.update_buttons())
        root.addWidget(self.grid, stretch=1)

        placeholder = QPixmap(THUMB)
        placeholder.fill(QColor(40, 40, 40))
        for wp in self.wallpapers.values():
            item = QListWidgetItem(QIcon(placeholder), "")
            item.setData(Qt.UserRole, wp.wid)
            item.setSizeHint(self.grid.gridSize())
            if not wp.supported:
                item.setToolTip("Presets aren't supported yet - they're settings for "
                                "another wallpaper, and the renderer can't apply them.")
            self.grid.addItem(item)
            self.items[wp.wid] = item

        # Actions.
        actions = QHBoxLayout()
        self.apply_btn = QPushButton("Set wallpaper")
        self.apply_btn.clicked.connect(self.apply_selected)
        self.preview_btn = QPushButton("Preview in a window")
        self.preview_btn.clicked.connect(self.preview_selected)
        self.clear_btn = QPushButton("Use Plasma's wallpaper here")
        self.clear_btn.clicked.connect(self.clear_screen)
        actions.addWidget(self.apply_btn)
        actions.addWidget(self.preview_btn)
        actions.addStretch(1)
        actions.addWidget(self.clear_btn)
        root.addLayout(actions)

        # Service.
        service = QHBoxLayout()
        self.status_label = QLabel()
        self.run_btn = QPushButton()
        self.run_btn.clicked.connect(self.toggle_running)
        self.autostart = QCheckBox("Start at login")
        self.autostart.setChecked(wallhost.autostart_enabled())
        self.autostart.toggled.connect(wallhost.set_autostart)
        self.pause_box = QComboBox()
        self.pause_box.addItem("Pause the screen a window covers", "covered")
        self.pause_box.addItem("Pause every screen when any is covered", "any")
        self.pause_box.addItem("Never pause", "never")
        self.pause_box.setCurrentIndex(max(0, self.pause_box.findData(wallhost.load_config()["pause"])))
        self.pause_box.currentIndexChanged.connect(self.set_pause_mode)
        fixes_btn = QPushButton("Pinstripe fixes…")
        fixes_btn.clicked.connect(lambda: FixesDialog(self).exec())
        service.addWidget(self.status_label, stretch=1)
        service.addWidget(fixes_btn)
        service.addWidget(self.pause_box)
        service.addWidget(self.autostart)
        service.addWidget(self.run_btn)
        root.addLayout(service)

        self.signals = ThumbSignals()
        self.signals.done.connect(self.set_thumb)
        pool = QThreadPool.globalInstance()
        for wp in self.wallpapers.values():
            pool.start(ThumbJob(wp, self.signals))

        self.status: dict = {}
        self.timer = QTimer(self, interval=2000, timeout=self.poll)
        self.timer.start()
        self.poll()

    # ------------------------------------------------------------ state

    def current_output(self) -> str:
        btn = self.screen_group.checkedButton()
        return btn.property("output") if btn else ""

    def selected(self) -> Wallpaper | None:
        item = self.grid.currentItem()
        return self.wallpapers.get(item.data(Qt.UserRole)) if item else None

    def assignments(self) -> dict[str, str]:
        return {out: str(conf.get("wallpaper"))
                for out, conf in wallhost.load_config()["screens"].items() if conf.get("wallpaper")}

    def poll(self) -> None:
        try:
            reply = wallhost._remote("Status") if wallhost._running() else None
        except Exception:
            reply = None
        self.status = json.loads(reply) if reply else {}
        running = bool(reply)
        self.status_label.setText("wallhost is running" if running else
                                  "wallhost is stopped - wallpapers won't show until it's started")
        self.run_btn.setText("Stop" if running else "Start")
        self.refresh_labels()

    def refresh_labels(self) -> None:
        assigned = self.assignments()
        states = self.status.get("screens", {})
        for out, btn in self.screen_buttons.items():
            wid = assigned.get(out)
            title = self.wallpapers[wid].title if wid in self.wallpapers else "Plasma's wallpaper"
            state = states.get(out, {}).get("state")
            btn.setText(f"{btn.property('position')}  ·  {out}\n{title}"
                        + (f"  ({state})" if state and state != "running" else ""))
        on = {}
        for out, wid in assigned.items():
            on.setdefault(wid, []).append(out)
        for wid, item in self.items.items():
            wp = self.wallpapers[wid]
            label = f"{wp.title}\n{wp.kind}"
            if wid in on:
                label += "  ·  on " + ", ".join(on[wid])
            item.setText(label)
        self.update_buttons()

    def refresh_selection(self) -> None:
        wid = self.assignments().get(self.current_output())
        if wid in self.items and not self.items[wid].isHidden():
            self.grid.setCurrentItem(self.items[wid])
            self.grid.scrollToItem(self.items[wid])
        self.update_buttons()

    def update_buttons(self) -> None:
        wp = self.selected()
        out = self.current_output()
        ok = bool(wp and wp.supported)
        self.apply_btn.setEnabled(ok)
        self.preview_btn.setEnabled(ok)
        self.apply_btn.setText(f"Set on {out}" if out else "Set wallpaper")
        self.clear_btn.setEnabled(out in self.assignments())

    def apply_filter(self) -> None:
        text = self.search.text().casefold()
        kind = self.type_box.currentData()
        for wid, item in self.items.items():
            wp = self.wallpapers[wid]
            item.setHidden(not ((kind == "all" or wp.kind == kind) and text in wp.title.casefold()))

    def set_thumb(self, wid: str, img: QImage) -> None:
        if img.isNull() or wid not in self.items:
            return
        pix = QPixmap.fromImage(img)
        self.items[wid].setIcon(QIcon(pix if self.wallpapers[wid].supported else dimmed(pix)))

    # ------------------------------------------------------------ actions

    def save(self, cfg: dict) -> None:
        wallhost.save_config(cfg)
        if wallhost._running():
            wallhost._remote("Reload")
        else:
            wallhost.start_service()
        QTimer.singleShot(1500, self.poll)
        self.refresh_labels()

    def apply_selected(self) -> None:
        wp, out = self.selected(), self.current_output()
        if not (wp and wp.supported and out):
            return
        cfg = wallhost.load_config()
        wallhost.assign(cfg, out, wp.wid)
        self.save(cfg)

    def clear_screen(self) -> None:
        cfg = wallhost.load_config()
        cfg["screens"].pop(self.current_output(), None)
        self.save(cfg)

    def set_pause_mode(self) -> None:
        cfg = wallhost.load_config()
        cfg["pause"] = self.pause_box.currentData()
        wallhost.save_config(cfg)
        if wallhost._running():
            wallhost._remote("Reload")

    def preview_selected(self) -> None:
        wp = self.selected()
        if not (wp and wp.supported):
            return
        if self.preview_proc and self.preview_proc.poll() is None:
            self.preview_proc.terminate()
        renderer = wallhost.renderer_dir(wallhost.load_config())
        folder = wallhost.wallpaper_dir(wp.wid)
        if renderer is None or folder is None:
            QMessageBox.warning(self, "Preview", "linux-wallpaperengine isn't installed."
                                if renderer is None else "That wallpaper's files are missing.")
            return
        assets = wallhost.assets_dir()
        self.preview_proc = subprocess.Popen(
            ["./linux-wallpaperengine", "--silent", "--window", "0x0x1280x720",
             *(["--assets-dir", str(assets)] if assets else []), str(folder)],
            cwd=renderer, env=wallhost.renderer_env(renderer),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def toggle_running(self) -> None:
        if wallhost._running():
            wallhost.stop_service()
        else:
            wallhost.start_service()
        QTimer.singleShot(1500, self.poll)

    def closeEvent(self, event) -> None:
        if self.preview_proc and self.preview_proc.poll() is None:
            self.preview_proc.terminate()
        super().closeEvent(event)


def already_running(message: str) -> bool:
    """Hand `message` to a picker that's already open, if there is one."""
    sock = QLocalSocket()
    sock.connectToServer(INSTANCE_KEY)
    if not sock.waitForConnected(300):
        return False
    sock.write(message.encode())
    sock.waitForBytesWritten(300)
    sock.disconnectFromServer()
    return True


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("wallhost")
    app.setDesktopFileName("wallhost-picker")
    app.setWindowIcon(app_icon())
    page = "fixes" if "--fixes" in sys.argv else "show"
    if already_running(page):
        return 0
    if not wallhost.workshop_dirs():
        QMessageBox.critical(None, "Wallpapers",
                             "No Wallpaper Engine workshop downloads were found in any Steam library.\n\n"
                             "Install Wallpaper Engine in Steam (it's a Windows app; it only needs to "
                             "be installed, not run) and subscribe to some wallpapers.")
        return 1
    win = Picker()

    def bring_forward(msg: str) -> None:
        win.showNormal()
        win.raise_()
        win.activateWindow()
        if msg == "fixes":
            FixesDialog(win).exec()

    server = QLocalServer(app)
    QLocalServer.removeServer(INSTANCE_KEY)         # stale socket from a crash
    server.listen(INSTANCE_KEY)

    def on_connection() -> None:
        conn = server.nextPendingConnection()
        conn.readyRead.connect(lambda: bring_forward(bytes(conn.readAll()).decode()))

    server.newConnection.connect(on_connection)
    win.show()
    win.refresh_selection()
    if page == "fixes":
        QTimer.singleShot(0, lambda: FixesDialog(win).exec())
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
