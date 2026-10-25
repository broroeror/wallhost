// wallhost KWin script: report which outputs are completely covered by a window.
//
// Loaded and unloaded by wallhost.py over KWin's /Scripting D-Bus interface.
// It only ever sends the comma-separated list of covered output names, and only
// when that list changes; every decision about pausing lives in wallhost.py.
//
// "Covered" is not just the fullscreen flag: Spotify's fullscreen and Proton's
// borderless-windowed games are plain windows sized to the screen, and never
// set it.

let lastSent = null;

function onCurrentDesktop(w) {
    if (w.onAllDesktops) return true;
    const cur = workspace.currentDesktop;
    return w.desktops.some(d => d.id === cur.id);
}

function coversOutput(w) {
    if (w.fullScreen) return true;
    // KWin lists the renderer's own background-layer surface as a normal window.
    if (!w.normalWindow || w.resourceClass === "linux-wallpaperengine") return false;
    const g = w.frameGeometry, o = w.output.geometry;
    return g.x <= o.x && g.y <= o.y &&
           g.x + g.width >= o.x + o.width && g.y + g.height >= o.y + o.height;
}

function report() {
    const covered = new Set();
    for (const w of workspace.windowList()) {
        if (!w.minimized && w.output && onCurrentDesktop(w) && coversOutput(w))
            covered.add(w.output.name);
    }
    const msg = Array.from(covered).sort().join(",");
    if (msg === lastSent) return;
    lastSent = msg;
    callDBus("org.wallhost.Host", "/Host", "org.wallhost.Host", "SetCovered", msg);
}

function watch(w) {
    w.fullScreenChanged.connect(report);
    w.frameGeometryChanged.connect(report);
    w.minimizedChanged.connect(report);
    w.outputChanged.connect(report);
    w.desktopsChanged.connect(report);
}

workspace.windowList().forEach(watch);
workspace.windowAdded.connect(w => { watch(w); report(); });
workspace.windowRemoved.connect(report);
workspace.currentDesktopChanged.connect(report);
report();
