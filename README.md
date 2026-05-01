# Free Linux Monitor

A minimal Ubuntu / GNOME system-tray monitor with a retro CRT / Pip-Boy aesthetic — a Linux port of [FreeMacMonitor](https://github.com/pekinlcc/FreeMacMonitor).

Pure Python 3 + GTK 3 + WebKit2 + AyatanaAppIndicator3. Reuses the Mac dashboard's HTML/CSS/JS verbatim so the two themes (Liquid Glass + Fallout Terminal) look pixel-equivalent.

---

## Requirements

- Ubuntu 22.04+ / any GNOME 42+ desktop with the AppIndicator extension
- Python 3.10+
- System packages:
  ```bash
  sudo apt install \
      python3-gi python3-psutil \
      gir1.2-gtk-3.0 gir1.2-webkit2-4.1 gir1.2-ayatanaappindicator3-0.1 \
      libnotify-bin policykit-1
  ```
- GNOME's AppIndicator extension enabled (Ubuntu's default ships with it):
  ```bash
  gnome-extensions enable ubuntu-appindicators@ubuntu.com
  ```

## Install

### Option A — Prebuilt `.deb` (Ubuntu / Debian)

Grab the latest from [Releases](https://github.com/pekinlcc/FreeLinuxMonitor/releases/latest), then:

```bash
sudo apt install ./free-linux-monitor_*.deb
```

`apt` resolves all the `gir1.2-*` runtime deps automatically. The package installs to:

```
/usr/bin/free-linux-monitor
/usr/lib/python3/dist-packages/free_linux_monitor/
/usr/share/applications/free-linux-monitor.desktop
/usr/share/icons/hicolor/scalable/apps/free-linux-monitor.svg
/usr/sbin/free-linux-monitor-purge
```

Uninstall with `sudo apt remove free-linux-monitor`.

### Option B — User-prefix install from source

If you want to hack on the code or don't have apt, run:

```bash
./scripts/install.sh
```

This installs into your user prefix only — no root needed:

```
~/.local/share/free-linux-monitor/   ← package
~/.local/bin/free-linux-monitor      ← launcher
~/.local/share/applications/free-linux-monitor.desktop
~/.local/share/icons/hicolor/scalable/apps/free-linux-monitor.svg
```

### Run

From the application grid, or from a terminal:

```bash
free-linux-monitor &
```

### Build the `.deb` yourself

```bash
./scripts/build-deb.sh
# → dist/free-linux-monitor_<version>_all.deb
```

## Features

- **System-tray indicator** — a small `>>` glyph that turns red (via `IndicatorStatus.ATTENTION`) when any metric breaches its alert threshold.
- **Live-metrics rotation** — toggle from the menu; the indicator label cycles every 3 seconds: `CPU 23%` → `MEM 64%` → `GPU 12%` → `DSK 11%`. When a metric breaches its threshold, the rotation locks onto the offending metric.
- **Dashboard panel** — `Open Dashboard` from the menu shows a 320 × 460 borderless WebKit2 popover with live bar charts. Click outside (any window loses focus → it auto-hides).
- **Two themes** — *Liquid Glass* (translucent dark glass with `backdrop-filter` blur, gradient pill bars) and *Fallout Terminal* (VT323 phosphor green, ASCII bars, scanlines + CRT vignette). Switch via **Theme ▸**.
- **Memory breakdown** — toggle **Show Memory Breakdown** to swap the single bar for a 5-colour stacked bar (App / Wired / Compressed / Cached / Free) + a 5-row legend with GB values per category. Buckets are derived from `/proc/meminfo` to mirror Activity Monitor's split as closely as Linux allows.
- **Auto-release at high pressure** — when (App + Wired + Compressed) / Total ≥ 98 % for 3 consecutive seconds, the app can run `sync && echo 3 > /proc/sys/vm/drop_caches` (the Linux analog of macOS `purge`). Three modes:
  1. **Notify only** *(default)* — posts a desktop notification, never runs `drop_caches`.
  2. **Auto-run — prompt password** — runs the helper via **pkexec**, which shows the standard PolicyKit graphical password dialog.
  3. **Auto-run — sudoers-free** — runs the helper via `sudo -n`. Requires a one-time sudoers rule (see below).
- **Always-on polling** at 1 Hz, whether the dashboard is open or not.
- **Persistent preferences** — theme, live metrics, breakdown, and auto-release mode persist in `~/.config/free-linux-monitor/config.json`.

## Sudoers-free auto-release

Run once, after the regular install:

```bash
sudo ./scripts/setup-sudoers.sh
```

This installs the helper at `/usr/local/sbin/free-linux-monitor-purge` and writes:

```
/etc/sudoers.d/free-linux-monitor-purge:
  $USER ALL=(root) NOPASSWD: /usr/local/sbin/free-linux-monitor-purge
```

The setup script validates the sudoers rule with `visudo -cf` before installing — a malformed rule would lock you out of `sudo`. To uninstall:

```bash
sudo rm /usr/local/sbin/free-linux-monitor-purge \
        /etc/sudoers.d/free-linux-monitor-purge
```

## Tray menu

| Item | Action |
|---|---|
| Open Dashboard | Toggles the WebKit2 dashboard panel. |
| Show Live Metrics | Toggles the rotating readout in the indicator label. |
| Show Memory Breakdown | Switches MEMORY to a 5-colour stacked chart + legend. |
| Theme ▸ | Submenu: Liquid Glass · Fallout Terminal. |
| Auto-Release Memory ▸ | Submenu: Notify only · Auto-run prompt password · Auto-run sudoers-free · Off. |
| Release Memory Now… | One-shot manual purge (prompts via pkexec unless sudoers is set up). |
| Quit Free Linux Monitor | Exits the app. |

## Alert thresholds

Compiled-in constants in [`free_linux_monitor/app.py`](free_linux_monitor/app.py):

| Metric | Default |
|---|---|
| CPU | 80% |
| Memory | 80% |
| GPU | 80% |
| Disk | 85% |

## Implementation notes

- **CPU** — delta of `/proc/stat` aggregate ticks between samples (matches `top`'s instant view).
- **Memory** — `/proc/meminfo` mapped onto Activity Monitor's 5 buckets:
  - **App** = `Active(anon) + Inactive(anon) − Shmem`
  - **Wired** = `SUnreclaim + KernelStack + PageTables + Shmem`
  - **Compressed** = `Zswap`
  - **Cached** = `Buffers + max(Cached, Active(file)+Inactive(file)) + SReclaimable`
  - **Free** = `MemFree`
  - **Pressure** (used for status-bar % and auto-release trigger) = `(App + Wired + Compressed) / Total`, identical to FreeMacMonitor's metric.
- **GPU** — best-effort. Tries:
  1. `nvidia-smi --query-gpu=utilization.gpu`
  2. AMD: `/sys/class/drm/cardN/device/gpu_busy_percent`
  3. Intel Xe driver: derives usage from `…/tile0/gt0/gtidle/idle_residency_ms` (a monotonic GT-C6 idle counter), so GPU % = 100 − (idle_delta / wall_delta) * 100. The first sample primes the baseline; usage shows from tick 2.
  4. Falls back to `-1` (N/A) — the JS handles this and renders `N/A — NO GPU DATA`.
- **Disk** — `statvfs("/")`.
- **Dashboard** — a borderless RGBA `Gtk.Window` with `WebKit2.WebView` loaded from `file://…/index.html`. Metric updates are pushed via `webview.run_javascript("window.updateMetrics(...)")`. Same contract as the Mac app, so the bundled `app.js` is byte-for-byte identical.
- **Click-outside-to-close** — `focus-out-event` on the panel window.

## Project layout

```
free_linux_monitor/
  app.py             — GTK app, indicator, panel, polling, animations
  metrics.py         — CPU / memory / GPU / disk sampling
  memory_releaser.py — drop_caches via pkexec / sudo -n
  resources/
    index.html / app.js / style.css   (dashboard UI)
    VT323.ttf                         (bundled CRT font)
  icons/
    free-linux-monitor.svg            (>> green)
    free-linux-monitor-attention.svg  (>> red)
scripts/
  install.sh                   user-prefix installer
  setup-sudoers.sh             optional: sudoers-free purge
  free-linux-monitor-purge     /proc/sys/vm/drop_caches helper
free-linux-monitor.desktop     desktop entry
```
