#!/usr/bin/env python3
"""
wallhost — run Wallpaper Engine wallpapers on Plasma 6 without touching plasmashell.

The scene renderer is linux-wallpaperengine. The Plasma wallpaper plugin built on
it renders inside plasmashell, so a bad wallpaper takes the panels and desktop
down with it. wallhost runs one renderer process per screen instead, on the
Wayland "background" layer: Plasma's own wallpaper is still underneath, so a
renderer that dies simply uncovers it.

Model
-----
  one renderer    per configured output, started with --screen-root <output>
  crash           restarted with backoff (2s, 5s, 15s, 60s); after
                  GIVE_UP_CRASHES within GIVE_UP_WINDOW the screen is left on
                  Plasma's wallpaper until `wallhost reload`
  hang            a running, unpaused renderer whose CPU time stops advancing
                  for HANG_SECONDS is killed and restarted - a frozen frame is
                  exactly the static image an OLED must not show for hours
  wake            every renderer is restarted WAKE_DELAY seconds after a system
                  resume: on NVIDIA one survives suspend but draws nothing,
                  alive and using CPU, so neither crash nor hang handling sees it
  pause           SIGSTOP/SIGCONT. A stopped Wayland client keeps its last
                  frame on screen and costs nothing. linux-wallpaperengine's own
                  fullscreen pause doesn't work under KWin, so wallhost-kwin.js
                  reports which outputs have a fullscreen window and we pause
                  those (or every screen, with "pause": "any")

Config is ~/.config/wallhost.json; `wallhost set DP-1 <workshop id>` edits it.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = Path.home() / ".config" / "wallhost.json"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "wallhost"
KWIN_SCRIPT = HERE / "wallhost-kwin.js"
KWIN_SCRIPT_NAME = "wallhost"

WE_APP_ID = "431960"                 # Wallpaper Engine's Steam app id
STEAM_ROOTS = [Path.home() / ".local/share/Steam",
               Path.home() / ".steam/steam",
               Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam"]
RENDERER_DIRS = [Path("/opt/linux-wallpaperengine"),   # the linux-wallpaperengine-git package
                 HERE / "aur/linux-wallpaperengine-git/pkg/linux-wallpaperengine-git/opt/linux-wallpaperengine"]

UNIT_NAME = "wallhost.service"
UNIT_FILE = Path.home() / ".config/systemd/user" / UNIT_NAME
PACKAGED_UNIT = Path("/usr/lib/systemd/user") / UNIT_NAME
EXIT_ALREADY_RUNNING = 3            # the unit's RestartPreventExitStatus

BUS_NAME = "org.wallhost.Host"
OBJ_PATH = "/Host"

DEFAULTS = {
    "renderer": "",              # renderer directory; empty = find it (see RENDERER_DIRS)
    "pause": "covered",          # covered | any | never
    "fps": 30,
    "silent": True,
    "screens": {},               # {"DP-1": {"wallpaper": "1234567890", "fps": 30}}
    "recent": [],                # workshop ids, newest first, for the tray's quick switch
}
RECENT_MAX = 8

BACKOFF = [2, 5, 15, 60]
GIVE_UP_CRASHES = 5
GIVE_UP_WINDOW = 600
WATCHDOG_INTERVAL = 10
HANG_SECONDS = 30
WAKE_DELAY = 3          # let outputs come back before restarting renderers


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if CONFIG.exists():
        cfg.update(json.loads(CONFIG.read_text()))
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")


def steam_libraries() -> list[Path]:
    """Every Steam library folder, from each Steam install's libraryfolders.vdf."""
    libs: list[Path] = []
    for root in STEAM_ROOTS:
        vdf = root / "steamapps/libraryfolders.vdf"
        found = [root]
        try:
            for line in vdf.read_text(errors="replace").splitlines():
                parts = line.strip().split('"')
                if len(parts) >= 4 and parts[1] == "path":
                    found.append(Path(parts[3].replace("\\\\", "\\")))
        except OSError:
            pass
        for lib in found:
            lib = lib.resolve() if lib.exists() else lib
            if (lib / "steamapps").is_dir() and lib not in libs:
                libs.append(lib)
    return libs


def workshop_dirs() -> list[Path]:
    return [d for lib in steam_libraries()
            if (d := lib / "steamapps/workshop/content" / WE_APP_ID).is_dir()]


def wallpaper_dir(wid: str) -> Path | None:
    for d in workshop_dirs():
        if (d / wid).is_dir():
            return d / wid
    return None


def assets_dir() -> Path | None:
    for lib in steam_libraries():
        if (a := lib / "steamapps/common/wallpaper_engine/assets").is_dir():
            return a
    return None


def renderer_dir(cfg: dict) -> Path | None:
    for d in ([Path(cfg["renderer"])] if cfg.get("renderer") else []) + RENDERER_DIRS:
        if (d / "linux-wallpaperengine").is_file():
            return d
    return None


def renderer_env(renderer: Path) -> dict:
    return dict(os.environ, LD_LIBRARY_PATH=str(renderer / "lib"))


def assign(cfg: dict, output: str, wallpaper: str) -> None:
    """Set a screen's wallpaper and remember it for quick switching."""
    cfg["screens"].setdefault(output, {})["wallpaper"] = wallpaper
    cfg["recent"] = [wallpaper] + [w for w in cfg.get("recent", []) if w != wallpaper][:RECENT_MAX - 1]


def cpu_ticks(pid: int) -> int | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except OSError:
        return None
    return int(fields[11]) + int(fields[12])      # utime + stime


def _die_with_parent() -> None:
    # PR_SET_PDEATHSIG: if wallhost is killed outright, take the renderer with
    # it rather than leave an unsupervised wallpaper on screen.
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)


# ---------------------------------------------------------------- renderer

class Screen:
    def __init__(self, host: "Host", output: str, conf: dict):
        self.host = host
        self.output = output
        self.conf = conf
        self.proc: subprocess.Popen | None = None
        self.paused = False
        self.stopping = False
        self.given_up = False
        self.crashes: list[float] = []
        self.restart_timer = 0
        self.last_ticks: int | None = None
        self.still_since: float | None = None

    def command(self, wallpaper: Path) -> list[str]:
        cfg = self.host.cfg
        cmd = ["./linux-wallpaperengine", "--layer", "background",
               "--fps", str(self.conf.get("fps", cfg["fps"])),
               "--noautomute", "--no-fullscreen-pause"]
        if cfg["silent"]:
            cmd.append("--silent")
        # Paths rather than ids: the renderer only knows the default Steam library.
        if assets := assets_dir():
            cmd += ["--assets-dir", str(assets)]
        cmd += ["--screen-root", self.output, "--bg", str(wallpaper)]
        return cmd

    def start(self) -> None:
        from gi.repository import GLib
        self.restart_timer = 0
        renderer = renderer_dir(self.host.cfg)
        wallpaper = wallpaper_dir(str(self.conf["wallpaper"]))
        if renderer is None or wallpaper is None:
            self.given_up = True
            log(f"{self.output}: " + ("linux-wallpaperengine not found (install linux-wallpaperengine-git)"
                                      if renderer is None else
                                      f"wallpaper {self.conf['wallpaper']} not found in any Steam library"))
            return
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        logfile = open(STATE_DIR / f"{self.output}.log", "w")
        self.proc = subprocess.Popen(self.command(wallpaper), cwd=renderer, env=renderer_env(renderer),
                                     stdin=subprocess.DEVNULL, stdout=logfile,
                                     stderr=subprocess.STDOUT,
                                     preexec_fn=_die_with_parent)
        logfile.close()
        self.last_ticks, self.still_since = None, None
        log(f"{self.output}: started {self.conf['wallpaper']} (pid {self.proc.pid})")
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, self.proc.pid, self._exited, self.proc)
        if self.host.should_pause(self.output):
            self.set_paused(True)

    def _exited(self, pid: int, status: int, proc: subprocess.Popen) -> None:
        from gi.repository import GLib
        if proc is not self.proc:
            return
        self.proc = None
        self.paused = False
        if self.stopping:
            return
        now = time.monotonic()
        self.crashes = [t for t in self.crashes if now - t < GIVE_UP_WINDOW] + [now]
        if len(self.crashes) >= GIVE_UP_CRASHES:
            self.given_up = True
            log(f"{self.output}: {len(self.crashes)} crashes in {GIVE_UP_WINDOW}s, "
                f"leaving Plasma's wallpaper showing (see {STATE_DIR / (self.output + '.log')})")
            return
        delay = BACKOFF[min(len(self.crashes) - 1, len(BACKOFF) - 1)]
        log(f"{self.output}: renderer exited (status {status}), restarting in {delay}s")
        self.restart_timer = GLib.timeout_add_seconds(delay, self._restart)

    def _restart(self) -> bool:
        self.start()
        return False

    def set_paused(self, paused: bool) -> None:
        if self.proc is None or paused == self.paused:
            return
        self.proc.send_signal(signal.SIGSTOP if paused else signal.SIGCONT)
        self.paused = paused
        self.last_ticks, self.still_since = None, None
        log(f"{self.output}: {'paused' if paused else 'resumed'}")

    def check_hang(self) -> None:
        if self.proc is None or self.paused:
            return
        ticks = cpu_ticks(self.proc.pid)
        now = time.monotonic()
        if ticks is None or ticks != self.last_ticks:
            self.last_ticks, self.still_since = ticks, now
            return
        if now - self.still_since >= HANG_SECONDS:
            log(f"{self.output}: no progress for {HANG_SECONDS}s, killing hung renderer")
            self.proc.kill()            # _exited handles the restart

    def stop(self) -> None:
        from gi.repository import GLib
        self.stopping = True
        if self.restart_timer:
            GLib.source_remove(self.restart_timer)
            self.restart_timer = 0
        if self.proc is not None:
            proc = self.proc
            proc.send_signal(signal.SIGCONT)    # a stopped process can't act on SIGTERM
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            self.proc = None

    def status(self) -> dict:
        state = ("gave up" if self.given_up else "paused" if self.paused
                 else "running" if self.proc else "restarting")
        return {"wallpaper": str(self.conf["wallpaper"]), "state": state,
                "pid": self.proc.pid if self.proc else None,
                "recent_crashes": len(self.crashes)}


# ---------------------------------------------------------------- service

class Host:
    def __init__(self):
        self.cfg = load_config()
        self.covered: set[str] = set()
        self.screens: dict[str, Screen] = {}
        self.manual_pause = False       # from the tray; not saved, a restart clears it

    def should_pause(self, output: str) -> bool:
        if self.manual_pause:
            return True
        mode = self.cfg["pause"]
        if mode == "any":
            return bool(self.covered)
        if mode == "covered":
            return output in self.covered
        return False

    def apply_pause(self) -> None:
        for out, screen in self.screens.items():
            screen.set_paused(self.should_pause(out))

    def start_all(self) -> None:
        for out, conf in self.cfg["screens"].items():
            if conf.get("wallpaper"):
                self.screens[out] = Screen(self, out, conf)
                self.screens[out].start()
        if not self.screens:
            log(f"no screens configured; try: wallhost set DP-1 <workshop id>")

    def stop_all(self) -> None:
        for screen in self.screens.values():
            screen.stop()
        self.screens = {}

    def reload(self) -> None:
        log("reloading config")
        self.restart_all()

    def restart_all(self) -> None:
        self.stop_all()
        self.cfg = load_config()
        self.start_all()

    def on_sleep(self, going_to_sleep: bool) -> None:
        from gi.repository import GLib
        if going_to_sleep:
            log("system suspending")
            return
        log(f"system resumed, restarting renderers in {WAKE_DELAY}s")
        GLib.timeout_add_seconds(WAKE_DELAY, lambda: (self.restart_all(), False)[1])

    def watchdog(self) -> bool:
        for screen in self.screens.values():
            screen.check_hang()
        return True


def kwin_scripting(bus):
    import dbus
    return dbus.Interface(bus.get_object("org.kde.KWin", "/Scripting"), "org.kde.kwin.Scripting")


def load_kwin_script(bus) -> None:
    import dbus
    scripting = kwin_scripting(bus)
    if scripting.isScriptLoaded(KWIN_SCRIPT_NAME):
        scripting.unloadScript(KWIN_SCRIPT_NAME)
    sid = scripting.loadScript(str(KWIN_SCRIPT), KWIN_SCRIPT_NAME, signature="ss")
    if sid < 0:
        raise RuntimeError(f"KWin refused to load {KWIN_SCRIPT}")
    script = bus.get_object("org.kde.KWin", f"/Scripting/Script{sid}")
    dbus.Interface(script, "org.kde.kwin.Script").run()


def unload_kwin_script(bus) -> None:
    try:
        kwin_scripting(bus).unloadScript(KWIN_SCRIPT_NAME)
    except Exception as exc:          # KWin gone (logout) - nothing to unload
        log(f"could not unload KWin script: {exc}")


def cmd_run(args) -> int:
    import dbus
    import dbus.service
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    if bus.name_has_owner(BUS_NAME):
        print("wallhost is already running", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    host = Host()

    class Service(dbus.service.Object):
        @dbus.service.method(BUS_NAME, in_signature="s")
        def SetCovered(self, outputs):
            host.covered = {o for o in str(outputs).split(",") if o}
            host.apply_pause()

        @dbus.service.method(BUS_NAME, in_signature="b")
        def SetManualPause(self, paused):
            host.manual_pause = bool(paused)
            log("paused by user" if paused else "resumed by user")
            host.apply_pause()

        @dbus.service.method(BUS_NAME, out_signature="s")
        def Status(self):
            return json.dumps({"pause": host.cfg["pause"],
                               "manual_pause": host.manual_pause,
                               "covered": sorted(host.covered),
                               "screens": {o: s.status() for o, s in host.screens.items()}})

        @dbus.service.method(BUS_NAME)
        def Reload(self):
            host.reload()

    name = dbus.service.BusName(BUS_NAME, bus)     # noqa: F841 - must stay referenced
    Service(bus, OBJ_PATH)
    dbus.SystemBus().add_signal_receiver(
        lambda start: host.on_sleep(bool(start)), signal_name="PrepareForSleep",
        dbus_interface="org.freedesktop.login1.Manager",
        bus_name="org.freedesktop.login1", path="/org/freedesktop/login1")

    loop = GLib.MainLoop()
    try:
        from gi.repository import GLibUnix
        signal_add = GLibUnix.signal_add
    except ImportError:
        signal_add = GLib.unix_signal_add
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal_add(GLib.PRIORITY_HIGH, sig, lambda: (loop.quit(), False)[1])

    try:
        host.start_all()
        load_kwin_script(bus)
        GLib.timeout_add_seconds(WATCHDOG_INTERVAL, host.watchdog)
        log("wallhost running")
        loop.run()
    finally:
        log("shutting down")
        unload_kwin_script(bus)
        host.stop_all()
    return 0


# ---------------------------------------------------------------- client

def _remote(method: str):
    return _remote_call(method)


def _remote_call(method: str, *args):
    import dbus
    try:
        obj = dbus.SessionBus().get_object(BUS_NAME, OBJ_PATH)
    except dbus.exceptions.DBusException:
        return None
    return getattr(dbus.Interface(obj, BUS_NAME), method)(*args)


def cmd_status(args) -> int:
    reply = _remote("Status")
    if reply is None:
        print("wallhost is not running")
        return 1
    st = json.loads(reply)
    print(f"pause mode: {st['pause']}   fullscreen on: {', '.join(st['covered']) or 'nothing'}")
    titles = workshop_titles()
    for out, s in st["screens"].items():
        print(f"  {out:10} {s['state']:10} {s['wallpaper']}  {titles.get(s['wallpaper'], '')}"
              + (f"  ({s['recent_crashes']} recent crashes)" if s["recent_crashes"] else ""))
    return 0


def workshop_projects() -> list[Path]:
    """project.json of every downloaded wallpaper, across all Steam libraries."""
    seen, found = set(), []
    for d in workshop_dirs():
        for proj in sorted(d.glob("*/project.json")):
            if proj.parent.name not in seen:
                seen.add(proj.parent.name)
                found.append(proj)
    return found


def workshop_titles() -> dict[str, str]:
    titles = {}
    for proj in workshop_projects():
        try:
            titles[proj.parent.name] = json.loads(proj.read_text(encoding="utf-8-sig"))["title"]
        except (OSError, ValueError, KeyError):
            pass
    return titles


def cmd_list(args) -> int:
    rows = []
    for proj in workshop_projects():
        try:
            meta = json.loads(proj.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        kind = str(meta.get("type", "?")).lower()
        if args.type and kind != args.type:
            continue
        rows.append((kind, meta.get("title", "?"), proj.parent.name))
    for kind, title, wid in sorted(rows):
        print(f"{wid:>11}  {kind:6} {title}")
    return 0


def cmd_set(args) -> int:
    if wallpaper_dir(args.wallpaper) is None:
        print(f"no downloaded workshop item {args.wallpaper} in any Steam library", file=sys.stderr)
        return 1
    cfg = load_config()
    assign(cfg, args.output, args.wallpaper)
    save_config(cfg)
    print(f"{args.output} -> {args.wallpaper} {workshop_titles().get(args.wallpaper, '')}")
    _reload_if_running()
    return 0


def cmd_unset(args) -> int:
    cfg = load_config()
    if cfg["screens"].pop(args.output, None) is None:
        print(f"{args.output} has no wallpaper set")
        return 1
    save_config(cfg)
    _reload_if_running()
    return 0


def cmd_reload(args) -> int:
    if not _running():
        print("wallhost is not running")
        return 1
    _remote("Reload")
    return 0


# ---------------------------------------------------------------- systemd

def write_unit() -> None:
    """Make sure a wallhost.service exists. An installed package ships one; a
    copy run from source writes its own, pointing at itself."""
    if PACKAGED_UNIT.exists():
        if UNIT_FILE.exists() and "wallhost" in UNIT_FILE.read_text():
            # A from-source unit would shadow the packaged one; hand over to it.
            was_enabled = autostart_enabled()
            systemctl("disable", UNIT_NAME)
            UNIT_FILE.unlink()
            systemctl("daemon-reload")
            if was_enabled:
                systemctl("enable", UNIT_NAME)
        return
    UNIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    UNIT_FILE.write_text(f"""\
[Unit]
Description=wallhost - animated wallpapers behind the Plasma desktop
PartOf=graphical-session.target
After=graphical-session.target plasma-kwin_wayland.service

[Service]
ExecStart={sys.executable} "{Path(__file__).resolve()}" run
Restart=on-failure
RestartSec=5
RestartPreventExitStatus={EXIT_ALREADY_RUNNING}

[Install]
WantedBy=graphical-session.target
""")
    systemctl("daemon-reload")


def systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def autostart_enabled() -> bool:
    return systemctl("is-enabled", UNIT_NAME).stdout.strip() == "enabled"


def set_autostart(on: bool) -> None:
    write_unit()
    systemctl("enable" if on else "disable", UNIT_NAME)


def start_service() -> None:
    write_unit()
    systemctl("start", UNIT_NAME)


def stop_service() -> None:
    systemctl("stop", UNIT_NAME)


def cmd_start(args) -> int:
    start_service()
    return 0


def cmd_stop(args) -> int:
    stop_service()
    return 0


def cmd_autostart(args) -> int:
    set_autostart(args.state == "on")
    print(f"start at login: {'on' if autostart_enabled() else 'off'}")
    return 0


def _running() -> bool:
    import dbus
    return dbus.SessionBus().name_has_owner(BUS_NAME)


def _reload_if_running() -> None:
    if _running():
        _remote("Reload")
        print("reloaded running wallhost")


def main() -> int:
    parser = argparse.ArgumentParser(prog="wallhost", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the wallpaper host (foreground)").set_defaults(fn=cmd_run)
    sub.add_parser("status", help="show what each screen is doing").set_defaults(fn=cmd_status)
    sub.add_parser("reload", help="re-read config, restart renderers").set_defaults(fn=cmd_reload)
    sub.add_parser("start", help="start the background service").set_defaults(fn=cmd_start)
    sub.add_parser("stop", help="stop the background service").set_defaults(fn=cmd_stop)
    p = sub.add_parser("autostart", help="start wallhost at login")
    p.add_argument("state", choices=["on", "off"])
    p.set_defaults(fn=cmd_autostart)
    p = sub.add_parser("list", help="list workshop wallpapers")
    p.add_argument("--type", choices=["scene", "video", "web"])
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("set", help="set a screen's wallpaper")
    p.add_argument("output", help="output name, e.g. DP-1 (see kscreen-doctor -o)")
    p.add_argument("wallpaper", help="workshop id (see wallhost list)")
    p.set_defaults(fn=cmd_set)
    p = sub.add_parser("unset", help="stop managing a screen")
    p.add_argument("output")
    p.set_defaults(fn=cmd_unset)
    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
