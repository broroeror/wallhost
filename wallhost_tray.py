#!/usr/bin/env python3
"""
wallhost tray — a small system-tray icon for wallhost, started at login.

Left-click opens the picker. The menu shows what each screen is doing, pauses or
resumes everything by hand, quick-switches each screen between recently used
wallpapers, and starts or stops the wallhost service. Like the picker, it only
talks to wallhost (config file + D-Bus); it never touches a renderer.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QActionGroup, QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

import wallhost
from wallhost_picker import app_icon

HERE = Path(__file__).resolve().parent
PICKER = HERE / "wallhost_picker.py"
AUTOSTART = Path.home() / ".config/autostart/wallhost-tray.desktop"
POSITIONS = {1: ["Screen"], 2: ["Left", "Right"], 3: ["Left", "Middle", "Right"]}


def open_picker(*args: str) -> None:
    subprocess.Popen([sys.executable, str(PICKER), *args], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def screen_names() -> list[tuple[str, str]]:
    """(output, position) left to right, e.g. ("DP-1", "Middle")."""
    screens = sorted(QGuiApplication.screens(), key=lambda s: s.geometry().x())
    names = POSITIONS.get(len(screens), [f"Screen {i + 1}" for i in range(len(screens))])
    return [(s.name(), pos) for s, pos in zip(screens, names)]


class Tray(QSystemTrayIcon):
    def __init__(self):
        self.icon_on = app_icon()
        super().__init__(self.icon_on)
        self.icon_off = QIcon(self.icon_on.pixmap(64, QIcon.Disabled))
        self.titles = {}
        self.status: dict = {}

        self.menu = QMenu()
        self.menu.aboutToShow.connect(self.build_menu)
        self.setContextMenu(self.menu)
        self.activated.connect(self.on_activated)

        self.timer = QTimer(self, interval=3000, timeout=self.poll)
        self.timer.start()
        self.poll()
        self.build_menu()

    # ------------------------------------------------------------ state

    def poll(self) -> None:
        try:
            reply = wallhost._remote("Status") if wallhost._running() else None
        except Exception:
            reply = None
        self.status = json.loads(reply) if reply else {}
        self.setIcon(self.icon_on if self.status and not self.paused_by_user() else self.icon_off)
        self.setToolTip("Wallpapers\n" + self.summary())

    def paused_by_user(self) -> bool:
        return bool(self.status.get("manual_pause"))

    def title(self, wid: str) -> str:
        if wid not in self.titles:
            self.titles = wallhost.workshop_titles()
        return self.titles.get(wid, wid)

    def summary(self) -> str:
        if not self.status:
            return "Stopped"
        if self.paused_by_user():
            return "Paused"
        lines = []
        for out, pos in screen_names():
            s = self.status.get("screens", {}).get(out)
            if s:
                lines.append(f"{pos}: {self.title(s['wallpaper'])} ({s['state']})")
        return "\n".join(lines) or "Running, no screens set"

    # ------------------------------------------------------------ menu

    def build_menu(self) -> None:
        self.poll()
        m = self.menu
        m.clear()
        head = m.addAction(self.summary().replace("\n", "  ·  "))
        head.setEnabled(False)
        m.addSeparator()

        running = bool(self.status)
        pause = m.addAction("Pause wallpapers")
        pause.setCheckable(True)
        pause.setChecked(self.paused_by_user())
        pause.setEnabled(running)
        pause.toggled.connect(lambda on: (wallhost._remote_call("SetManualPause", on), self.poll()))
        m.addSeparator()

        cfg = wallhost.load_config()
        recent = list(cfg.get("recent", []))
        for out, pos in screen_names():
            current = str(cfg["screens"].get(out, {}).get("wallpaper") or "")
            for wid in ([current] if current else []):
                if wid not in recent:
                    recent.append(wid)
            sub = m.addMenu(f"{pos}: {self.title(current) if current else 'Plasma wallpaper'}")
            group = QActionGroup(sub)
            for wid in recent:
                act = sub.addAction(self.title(wid))
                act.setCheckable(True)
                act.setChecked(wid == current)
                group.addAction(act)
                act.triggered.connect(lambda _=False, o=out, w=wid: self.switch(o, w))
            if recent:
                sub.addSeparator()
            sub.addAction("More wallpapers…").triggered.connect(lambda: open_picker())
            plain = sub.addAction("Use Plasma's wallpaper")
            plain.setEnabled(bool(current))
            plain.triggered.connect(lambda _=False, o=out: self.clear(o))
        m.addSeparator()

        m.addAction("Open Wallpapers…").triggered.connect(lambda: open_picker())
        m.addAction("Pinstripe fixes…").triggered.connect(lambda: open_picker("--fixes"))
        if running:
            m.addAction("Stop wallhost").triggered.connect(wallhost.stop_service)
        else:
            m.addAction("Start wallhost").triggered.connect(wallhost.start_service)
        m.addSeparator()
        m.addAction("Hide tray icon").triggered.connect(QApplication.quit)

    # ------------------------------------------------------------ actions

    def apply(self, cfg: dict) -> None:
        wallhost.save_config(cfg)
        if wallhost._running():
            wallhost._remote("Reload")
        else:
            wallhost.start_service()
        QTimer.singleShot(1500, self.poll)

    def switch(self, output: str, wid: str) -> None:
        cfg = wallhost.load_config()
        wallhost.assign(cfg, output, wid)
        self.apply(cfg)

    def clear(self, output: str) -> None:
        cfg = wallhost.load_config()
        cfg["screens"].pop(output, None)
        self.apply(cfg)

    def on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            open_picker()


def install_autostart() -> None:
    installed = shutil.which("wallhost-tray")
    exec_line = installed or f'{sys.executable} "{Path(__file__).resolve()}"'
    AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART.write_text(f"""\
[Desktop Entry]
Type=Application
Name=Wallpapers tray
Comment=Tray icon for animated wallpapers
Exec={exec_line}
Icon=wallhost
NoDisplay=true
X-KDE-autostart-phase=2
""")


def main() -> int:
    if "--install-autostart" in sys.argv:
        install_autostart()
        print(f"wrote {AUTOSTART}")
        return 0
    app = QApplication(sys.argv)
    app.setApplicationName("wallhost-tray")
    app.setDesktopFileName("wallhost-picker")
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("no system tray available", file=sys.stderr)
        return 1
    tray = Tray()
    tray.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
