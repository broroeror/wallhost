# wallhost

Animated wallpapers from your Wallpaper Engine workshop subscriptions, behind the
KDE Plasma 6 desktop on Wayland.

The rendering is done by [linux-wallpaperengine](https://github.com/Almamu/linux-wallpaperengine).
wallhost is everything around it: it runs one renderer per screen, keeps it
alive, pauses it when you can't see it, and gives you a picker and a tray icon.

## Why another one?

The usual way to get these wallpapers on Plasma is a wallpaper plugin, which
renders *inside* plasmashell. When a wallpaper misbehaves, your panels and
desktop go down with it.

wallhost keeps the renderer out of plasmashell. Each screen's wallpaper is a
separate process on the Wayland background layer, with Plasma's own wallpaper
still underneath. If a renderer crashes, you see your normal wallpaper until
wallhost restarts it.

## Features

- **Per-screen wallpapers**, set from a thumbnail picker or the command line
- **Crash recovery**: restarts with backoff, and gives up gracefully on a
  wallpaper that keeps crashing
- **Hang watchdog**: a renderer that stops making progress is replaced, so a
  frozen frame doesn't sit on screen for hours (useful on OLED monitors)
- **Pauses when covered**: fullscreen windows, and also borderless games and
  apps that fill the screen without being "fullscreen". The renderer is frozen,
  not killed, so it resumes instantly and costs nothing while paused
- **Recovers after suspend**: renderers are restarted on wake, since on some GPUs
  they survive sleep but stop drawing
- **Tray icon** with pause/resume and per-screen quick switching
- **Pinstripe fixes**: optional KWin settings for a known NVIDIA issue (below)

## Requirements

- KDE Plasma 6 on **Wayland** (X11 isn't supported)
- [Wallpaper Engine](https://store.steampowered.com/app/431960/) **owned on
  Steam and installed**. It's a Windows app; it doesn't need to run, but its
  files and your workshop subscriptions have to be downloaded. Any Steam library
  works, including the Flatpak version of Steam.
- `linux-wallpaperengine` (pulled in by the AUR package)

## Install

```
paru -S wallhost        # or: yay -S wallhost
```

Then open **Wallpapers** from your app launcher, pick a screen, and
double-click a wallpaper. Tick **Start at login** to have wallpapers come back
after a restart.

For the tray icon, run `wallhost-tray`. To start it with your desktop:

```
wallhost-tray --install-autostart
```

## Command line

```
wallhost list --type scene        # your downloaded wallpapers and their ids
wallhost set DP-1 1234567890      # set a screen's wallpaper (see: kscreen-doctor -o)
wallhost unset DP-1               # give a screen back to Plasma's wallpaper
wallhost status                   # what each screen is doing
wallhost start | stop             # the background service
wallhost autostart on | off       # start at login
```

Settings live in `~/.config/wallhost.json`; renderer logs in
`~/.local/state/wallhost/`.

## Pinstripe fixes (NVIDIA)

On NVIDIA, a moving wallpaper can show through windows and panels in thin
horizontal strips. It never appears in screenshots, because it happens after
KWin has finished the frame. It isn't specific to wallhost: in-plasmashell
wallpaper plugins show it too.

**Pinstripe fixes…** in the picker lists KWin settings that address it, each
with an explanation, the exact line it adds and where. Apply one, restart, and
revert it if nothing changes. The two that have helped so far:

| Setting | What it does |
|---|---|
| `KWIN_DRM_FORCE_GL_FINISH_PRESENT=1` | waits for each frame to finish before showing it |
| `KWIN_USE_BUFFER_AGE=0` | redraws the whole screen every frame (needed from KWin 6.8.1) |

Each fix is one file in `~/.config/environment.d/`, so you can also remove
them by hand.

## Known limitations

- **Presets** (workshop items that are only settings for another wallpaper)
  aren't supported yet; the picker shows them greyed out.
- What renders, and how well, is up to linux-wallpaperengine. Most scene
  wallpapers tested so far work; a few (some 3D/scripted ones) don't load.
  Video and web wallpapers go through the same renderer but have had less
  testing.
- When a renderer starts, its audio capture can cause a brief click in what's
  playing.

## How it works

`wallhost run` is a small supervisor (Python, GLib, D-Bus). A KWin script
reports which outputs are covered by a window, and wallhost freezes those
screens' renderers with `SIGSTOP`/`SIGCONT`. Renderers are started with
`PR_SET_PDEATHSIG`, so they never outlive the supervisor. The picker and the
tray only edit the config file and talk to the service over D-Bus
(`org.wallhost.Host`).

## Credits and disclaimer

Rendering by [linux-wallpaperengine](https://github.com/Almamu/linux-wallpaperengine)
by Almamu and contributors.

wallhost is an independent project. It isn't affiliated with or endorsed by
Wallpaper Engine or its developers, KDE, or Valve. Wallpapers belong to their
creators; wallhost only displays what you've subscribed to on your own machine.

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).
