#!/usr/bin/env python3
"""
wallhost fixes — opt-in KWin workarounds for "pinstripes": an animated wallpaper
showing through windows and panels in scan-line strips on NVIDIA.

The strips never appear in screenshots, so they happen between KWin finishing
a frame and the monitor showing it. Each fix is one KWin environment variable,
written as its own file in ~/.config/environment.d so it can be applied and
reverted independently. KWin only reads them when it starts, so every change
needs a restart; status() tells "active" apart from "waiting for a restart" by
comparing when the file changed with when KWin started.

These are desktop-wide KWin settings, not wallhost settings: they stay in effect
whether or not wallhost is running.

  wallhost_fixes.py list
  wallhost_fixes.py apply <id>
  wallhost_fixes.py revert <id>
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ENV_DIR = Path.home() / ".config/environment.d"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "wallhost"
REVERTS = STATE_DIR / "fix-reverts.json"


@dataclass(frozen=True)
class Fix:
    id: str
    order: int
    var: str
    value: str
    title: str
    summary: str
    details: str
    cost: str
    legacy: tuple[str, ...] = ()       # earlier hand-written files for this fix

    @property
    def path(self) -> Path:
        return ENV_DIR / f"{self.order}-wallhost-{self.id}.conf"

    @property
    def line(self) -> str:
        return f"{self.var}={self.value}"


FIXES = [
    Fix("finish-present", 90, "KWIN_DRM_FORCE_GL_FINISH_PRESENT", "1",
        "Wait for each frame to finish",
        "Makes KWin wait until the graphics card has completely finished drawing "
        "a frame before sending it to the monitor.",
        "KWin draws back to front: wallpaper first, windows on top. If a frame is "
        "sent out before the card is done, the monitor briefly shows the wallpaper "
        "where windows haven't been painted yet. On KWin 6.8.0 this alone removed "
        "the strips.",
        "Very small. Rarely noticeable.",
        legacy=("90-kwin-finish-present.conf",)),
    Fix("no-buffer-age", 91, "KWIN_USE_BUFFER_AGE", "0",
        "Redraw the whole screen every frame",
        "Turns off KWin's partial redraws, so every frame is painted from scratch.",
        "KWin normally repaints only what changed and reuses older frames for the "
        "rest. KWin 6.8.1 reworked that bookkeeping (including for blurred panels "
        "like the dock), and stale strips came back. Without partial redraws there "
        "is nothing stale to show.",
        "Small with an animated wallpaper, which already changes the whole screen "
        "each frame. More GPU work on a still desktop.",
        legacy=("91-kwin-no-buffer-age.conf",)),
    Fix("no-triple-buffering", 92, "KWIN_DRM_DISABLE_TRIPLE_BUFFERING", "1",
        "Turn off triple buffering",
        "Stops KWin from starting the next frame before the previous one has "
        "reached the screen.",
        "During animations (the dock sliding in, windows opening) KWin may switch "
        "to triple buffering to keep up, which lets two frames overlap. Strips "
        "that show up only while something is moving point here.",
        "Animations can drop a frame now and then under heavy load.",
        ),
    Fix("no-overlays", 93, "KWIN_USE_OVERLAYS", "0",
        "Don't use hardware overlay planes",
        "Keeps KWin from handing individual windows or the wallpaper to the "
        "graphics card as separate layers.",
        "The card can combine separate layers itself at the last moment, after "
        "KWin's frame. If that goes wrong, lower layers show through. Overlays are "
        "only used now and then, so this is a long shot.",
        "Slightly more power use for fullscreen video.",
        ),
    Fix("no-direct-scanout", 94, "KWIN_DRM_NO_DIRECT_SCANOUT", "1",
        "Don't let fullscreen apps skip KWin",
        "Always composite through KWin, even for fullscreen windows.",
        "Fullscreen apps can normally send frames straight to the monitor, "
        "bypassing KWin. Only relevant if strips show up around fullscreen "
        "transitions; they haven't so far.",
        "Slightly more latency and GPU work in fullscreen games and video.",
        ),
]
BY_ID = {f.id: f for f in FIXES}


# ---------------------------------------------------------------- state

def kwin_started() -> float | None:
    """Wall-clock start time of the running kwin_wayland, or None."""
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if (proc / "comm").read_text().strip() != "kwin_wayland":
                continue
            start_ticks = int((proc / "stat").read_text().rsplit(")", 1)[1].split()[19])
        except (OSError, ValueError, IndexError):
            continue
        btime = next(int(l.split()[1]) for l in Path("/proc/stat").read_text().splitlines()
                     if l.startswith("btime"))
        return btime + start_ticks / os.sysconf("SC_CLK_TCK")
    return None


def _reverts() -> dict[str, float]:
    try:
        return json.loads(REVERTS.read_text())
    except (OSError, ValueError):
        return {}


def _configured_file(fix: Fix) -> Path | None:
    for p in (fix.path, *(ENV_DIR / name for name in fix.legacy)):
        if p.exists() and any(l.strip() == fix.line for l in p.read_text().splitlines()):
            return p
    return None


def status(fix: Fix) -> str:
    """'active' | 'pending-on' | 'pending-off' | 'off'"""
    started = kwin_started() or time.time()
    f = _configured_file(fix)
    if f is not None:
        return "active" if f.stat().st_mtime < started else "pending-on"
    reverted = _reverts().get(fix.id)
    return "pending-off" if reverted and reverted >= started else "off"


def migrate_legacy() -> None:
    """Move the hand-written fix files from before this module to its naming."""
    for fix in FIXES:
        for name in fix.legacy:
            old = ENV_DIR / name
            if old.exists() and not fix.path.exists():
                fix.path.write_text(old.read_text())
                os.utime(fix.path, (old.stat().st_atime, old.stat().st_mtime))  # keep "active"
                old.unlink()


def apply(fix: Fix) -> None:
    undoing_revert = status(fix) == "pending-off"     # KWin still has it
    ENV_DIR.mkdir(parents=True, exist_ok=True)
    fix.path.write_text(
        f"# Applied by wallhost on {time.strftime('%Y-%m-%d %H:%M')}: {fix.title}.\n"
        f"# {fix.summary}\n"
        f"# Revert from the wallhost picker, or delete this file and restart.\n"
        f"{fix.line}\n")
    started = kwin_started()
    if undoing_revert and started:
        os.utime(fix.path, (started - 1, started - 1))   # reads as "active" again
    rev = _reverts()
    if rev.pop(fix.id, None) is not None:
        REVERTS.write_text(json.dumps(rev))


def revert(fix: Fix) -> None:
    was_active = status(fix) == "active"
    for p in (fix.path, *(ENV_DIR / name for name in fix.legacy)):
        p.unlink(missing_ok=True)
    rev = _reverts()
    if was_active:
        rev[fix.id] = time.time()
    else:
        rev.pop(fix.id, None)       # never took effect, nothing to wait for
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    REVERTS.write_text(json.dumps(rev))


STATUS_TEXT = {
    "active": "On",
    "pending-on": "On after restart",
    "pending-off": "Off after restart",
    "off": "Off",
}


def main() -> int:
    migrate_legacy()
    args = sys.argv[1:]
    if not args or args[0] == "list":
        for fix in FIXES:
            print(f"  {fix.id:22} {STATUS_TEXT[status(fix)]:18} {fix.title}")
        return 0
    if len(args) == 2 and args[0] in ("apply", "revert") and args[1] in BY_ID:
        (apply if args[0] == "apply" else revert)(BY_ID[args[1]])
        print(f"{args[1]}: {STATUS_TEXT[status(BY_ID[args[1]])]}")
        return 0
    print("\n".join(__doc__.strip().splitlines()[-3:]), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
