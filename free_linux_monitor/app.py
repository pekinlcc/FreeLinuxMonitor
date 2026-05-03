"""
Free Linux Monitor — main application.

Mirrors the macOS StatusBarController + AppDelegate design:
- AyatanaAppIndicator3 in the system tray (shows ">>" + optional rotating
  metric text via the indicator label).
- Right-click (or just clicking the tray on most GNOME shells) opens the
  context menu with theme / breakdown / auto-release toggles.
- "Open Dashboard" menu item shows a borderless WebKit2 panel anchored
  near the indicator. Loses focus → hides itself, matching the "click
  outside to dismiss" UX of the Mac NSPanel.

Polling is a single GLib 1Hz timer that drives both the tray label and
the WebView; identical contract (`window.updateMetrics(data, opts)` and
`window.showReleaseToast(amount, hhmmss)`) to FreeMacMonitor's app.js.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# Force the GTK backend to X11 when running on Wayland. The Wayland protocol
# does not let clients place their own windows at absolute screen coordinates
# (xdg_toplevel.move is "best-effort" and Mutter / GNOME Shell ignores it
# entirely), so the dashboard ends up wherever the compositor decides — far
# from the tray icon. Routing through XWayland costs us native blur on the
# Liquid Glass theme but lets gtk_window_move() actually work, putting the
# panel at the calculated top-right anchor. Set FREE_LINUX_MONITOR_NATIVE=1
# in the environment to opt out (e.g. on wlroots-based compositors that
# support layer-shell, where you'd want to write a different anchor path).
if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("FREE_LINUX_MONITOR_NATIVE"):
    os.environ["GDK_BACKEND"] = "x11"

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
gi.require_version("AyatanaAppIndicator3", "0.1")
gi.require_version("Notify", "0.7")

from gi.repository import GLib, Gtk, Gdk, WebKit2  # noqa: E402
from gi.repository import AyatanaAppIndicator3 as AppIndicator3  # noqa: E402
from gi.repository import Notify  # noqa: E402

from . import metrics
from .memory_releaser import (
    AutoReleaseMode,
    ReleaseResult,
    release as run_release,
)


# ── Paths ───────────────────────────────────────────────────────────────────

PKG_DIR = Path(__file__).resolve().parent
RESOURCES_DIR = PKG_DIR / "resources"
ICONS_DIR = PKG_DIR / "icons"
INDEX_HTML = RESOURCES_DIR / "index.html"
ICON_NORMAL = ICONS_DIR / "free-linux-monitor.svg"
ICON_ATTENTION = ICONS_DIR / "free-linux-monitor-attention.svg"

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "free-linux-monitor"
CONFIG_FILE = CONFIG_DIR / "config.json"


# ── Constants (mirrored from Mac StatusBarController) ───────────────────────

CPU_ALERT = 80.0
MEM_ALERT = 80.0
GPU_ALERT = 80.0
DISK_ALERT = 85.0

AUTO_RELEASE_TRIGGER_PCT = 98.0
AUTO_RELEASE_HOLD_TICKS = 3
AUTO_RELEASE_COOLDOWN_SEC = 60.0

ROTATION_SECONDS = 3
PANEL_WIDTH = 320
PANEL_HEIGHT = 460


class Theme:
    LIQUID = "liquid-glass"
    FALLOUT = "fallout"

    @staticmethod
    def menu_title(t: str) -> str:
        return {Theme.LIQUID: "Liquid Glass", Theme.FALLOUT: "Fallout Terminal"}.get(t, t)


# ── Persistent prefs ────────────────────────────────────────────────────────

class Prefs:
    """Tiny JSON-backed config — equivalent of UserDefaults on macOS."""

    DEFAULTS = {
        "showLiveMetrics": True,
        "showMemoryBreakdown": False,
        "theme": Theme.LIQUID,
        "autoReleaseMode": AutoReleaseMode.NOTIFY.value,
    }

    def __init__(self) -> None:
        self._data = dict(self.DEFAULTS)
        self.load()

    def load(self) -> None:
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                disk = json.load(f)
                if isinstance(disk, dict):
                    for k, v in self.DEFAULTS.items():
                        self._data[k] = disk.get(k, v)
        except (OSError, json.JSONDecodeError):
            pass

    def save(self) -> None:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            tmp = CONFIG_FILE.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, CONFIG_FILE)
        except OSError:
            pass

    def get(self, key: str):
        return self._data.get(key, self.DEFAULTS[key])

    def set(self, key: str, value) -> None:
        self._data[key] = value
        self.save()


# ── Dashboard panel ─────────────────────────────────────────────────────────

class Panel:
    """Borderless RGBA Gtk.Window hosting the WebKit2 dashboard."""

    def __init__(self) -> None:
        self.window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.window.set_title("Free Linux Monitor")
        self.window.set_default_size(PANEL_WIDTH, PANEL_HEIGHT)
        self.window.set_resizable(False)
        self.window.set_decorated(False)
        self.window.set_skip_taskbar_hint(True)
        self.window.set_skip_pager_hint(True)
        self.window.set_keep_above(True)
        self.window.stick()
        self.window.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.window.set_app_paintable(True)

        screen = self.window.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None and screen.is_composited():
            self.window.set_visual(visual)

        # Transparent root; CSS owns the background.
        self.window.connect("draw", self._on_draw)
        # focus-out auto-hide is wired up after the first show, otherwise the
        # very act of opening (which steals focus from the menu) fires it
        # before the user even sees the panel.
        self._focus_handler_id: Optional[int] = None
        self.window.connect("delete-event", self._on_delete)

        self.webview = WebKit2.WebView()
        settings = self.webview.get_settings()
        settings.set_property("enable-developer-extras", False)
        settings.set_property("enable-javascript", True)
        settings.set_property("enable-write-console-messages-to-stdout", True)
        # Make the WebView background transparent so the GTK window's RGBA
        # visual carries through to the desktop / compositor blur.
        rgba = Gdk.RGBA()
        rgba.parse("rgba(0,0,0,0)")
        self.webview.set_background_color(rgba)

        self.window.add(self.webview)
        self._loaded = False
        self._pending_js: list[str] = []
        self.webview.connect("load-changed", self._on_load_changed)
        self.webview.load_uri(INDEX_HTML.as_uri())

    @staticmethod
    def _on_draw(widget, cr) -> bool:
        # We rely on the WebKit page itself to paint the chrome; keep this
        # transparent so glass shows the desktop through.
        cr.set_source_rgba(0, 0, 0, 0)
        cr.set_operator(1)  # CAIRO_OPERATOR_SOURCE
        cr.paint()
        return False

    def _on_focus_out(self, widget, event) -> bool:
        # Mac uses NSEvent.addGlobalMonitor; on GTK we hide on focus-out
        # (matches "click outside to dismiss" from the Mac UX).
        self.hide()
        return False

    def _on_delete(self, widget, event) -> bool:
        # Close button shouldn't destroy the window — just hide it. The
        # tray icon owns the lifecycle.
        self.hide()
        return True

    def _on_load_changed(self, view, event) -> None:
        if event == WebKit2.LoadEvent.FINISHED:
            self._loaded = True
            for js in self._pending_js:
                self.webview.run_javascript(js, None, None, None)
            self._pending_js.clear()

    def run_js(self, js: str) -> None:
        if self._loaded:
            self.webview.run_javascript(js, None, None, None)
        else:
            self._pending_js.append(js)

    def is_visible(self) -> bool:
        return self.window.is_visible()

    def show_at(self, x: int, y: int) -> None:
        self.window.move(x, y)
        self.window.show_all()
        self.window.present()
        # Arm focus-out only after the window has actually been shown and
        # claimed focus. A short defer keeps the dbusmenu close → focus
        # transfer from immediately re-hiding the panel.
        if self._focus_handler_id is None:
            def arm() -> bool:
                self._focus_handler_id = self.window.connect("focus-out-event", self._on_focus_out)
                return False
            GLib.timeout_add(400, arm)

    def hide(self) -> None:
        if self._focus_handler_id is not None:
            self.window.disconnect(self._focus_handler_id)
            self._focus_handler_id = None
        self.window.hide()


# ── Main application ────────────────────────────────────────────────────────

class App:
    def __init__(self) -> None:
        self.prefs = Prefs()
        Notify.init("Free Linux Monitor")

        self.panel = Panel()
        # AppIndicator looks the icon up in icon themes by name unless we
        # tell it to also search a local directory. Pointing at our bundled
        # icons folder lets us ship the >> glyph without polluting the
        # user's hicolor theme.
        self.indicator = AppIndicator3.Indicator.new_with_path(
            "free-linux-monitor",
            "free-linux-monitor",
            AppIndicator3.IndicatorCategory.SYSTEM_SERVICES,
            str(ICONS_DIR),
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_attention_icon_full("free-linux-monitor-attention", "alert")
        self.indicator.set_title("Free Linux Monitor")
        # Don't seed the label with ("", "") here. On GNOME's AppIndicator
        # extension that empty pair can win the registration race against
        # the first real set_label() — the host caches the zero-width label
        # and the proper text only appears once the user toggles the menu
        # item, which forces a fresh property-change signal.

        # Polling state
        self._cached_snap: Optional[metrics.MetricsSnapshot] = None
        self._tick_count = 0
        self._metric_index = 0

        # Auto-release runtime
        self._pressure_high_ticks = 0
        self._last_release_at: Optional[float] = None
        self._is_releasing = False
        self._animation_frames: list[tuple[str, str]] = []
        self._animation_index = 0
        self._animation_timer_id: Optional[int] = None
        self._last_release_result: Optional[tuple[int, float, datetime]] = None

        self._build_menu()

        # 1Hz polling. The first tick is deferred to an idle callback so
        # the indicator has time to register with the StatusNotifierWatcher
        # before we push a label — calling set_label() synchronously in
        # __init__ races the registration on GNOME and the host can keep
        # the original empty label until something forces a re-render.
        GLib.idle_add(self._first_tick)
        GLib.timeout_add(1000, self._tick_wrapper)

    def _first_tick(self) -> bool:
        self._tick_wrapper()
        return False    # one-shot

    # ── Menu ────────────────────────────────────────────────────────────────

    def _build_menu(self) -> None:
        menu = Gtk.Menu()

        # Open Dashboard — this is the Linux replacement for "left-click =
        # toggle panel" since AppIndicator on GNOME captures the click for
        # the menu itself.
        self.item_dashboard = Gtk.MenuItem(label="Open Dashboard")
        self.item_dashboard.connect("activate", self._on_toggle_panel)
        menu.append(self.item_dashboard)

        menu.append(Gtk.SeparatorMenuItem())

        self.item_live = Gtk.CheckMenuItem(label="Show Live Metrics")
        self.item_live.set_active(self.prefs.get("showLiveMetrics"))
        self.item_live.connect("toggled", self._on_toggle_live)
        menu.append(self.item_live)

        self.item_break = Gtk.CheckMenuItem(label="Show Memory Breakdown")
        self.item_break.set_active(self.prefs.get("showMemoryBreakdown"))
        self.item_break.connect("toggled", self._on_toggle_break)
        menu.append(self.item_break)

        # Theme submenu
        theme_item = Gtk.MenuItem(label="Theme")
        theme_sub = Gtk.Menu()
        theme_group: list[Gtk.RadioMenuItem] = []
        cur_theme = self.prefs.get("theme")
        for t in (Theme.LIQUID, Theme.FALLOUT):
            r = Gtk.RadioMenuItem.new_with_label(theme_group, Theme.menu_title(t))
            theme_group = r.get_group()
            if t == cur_theme:
                r.set_active(True)
            r.connect("toggled", self._on_pick_theme, t)
            theme_sub.append(r)
        theme_item.set_submenu(theme_sub)
        menu.append(theme_item)

        # Auto-release submenu
        ar_item = Gtk.MenuItem(label="Auto-Release Memory")
        ar_sub = Gtk.Menu()
        ar_group: list[Gtk.RadioMenuItem] = []
        cur_mode = AutoReleaseMode(self.prefs.get("autoReleaseMode"))
        for mode in (AutoReleaseMode.NOTIFY, AutoReleaseMode.AUTO_PASSWORD,
                     AutoReleaseMode.AUTO_SUDOERS, AutoReleaseMode.OFF):
            r = Gtk.RadioMenuItem.new_with_label(ar_group, mode.menu_title)
            ar_group = r.get_group()
            if mode == cur_mode:
                r.set_active(True)
            r.connect("toggled", self._on_pick_release_mode, mode)
            ar_sub.append(r)
        ar_item.set_submenu(ar_sub)
        menu.append(ar_item)

        self.item_release_now = Gtk.MenuItem(label="Release Memory Now…")
        self.item_release_now.connect("activate", self._on_release_now)
        menu.append(self.item_release_now)

        menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label="Quit Free Linux Monitor")
        quit_item.connect("activate", lambda *_: Gtk.main_quit())
        menu.append(quit_item)

        menu.show_all()
        self.indicator.set_menu(menu)
        self._menu = menu

    # ── Menu callbacks ──────────────────────────────────────────────────────

    def _on_toggle_panel(self, *_: object) -> None:
        if self.panel.is_visible():
            self.panel.hide()
            self.item_dashboard.set_label("Open Dashboard")
        else:
            x, y = self._anchor_position()
            self.panel.show_at(x, y)
            self.item_dashboard.set_label("Close Dashboard")
            if self._cached_snap is not None:
                self._push_metrics(self._cached_snap)

    def _on_toggle_live(self, item: Gtk.CheckMenuItem) -> None:
        self.prefs.set("showLiveMetrics", item.get_active())
        self._tick_count = 0
        self._metric_index = 0
        if self._cached_snap is not None:
            self._render_indicator(self._cached_snap)

    def _on_toggle_break(self, item: Gtk.CheckMenuItem) -> None:
        self.prefs.set("showMemoryBreakdown", item.get_active())
        if self._cached_snap is not None:
            self._push_metrics(self._cached_snap)

    def _on_pick_theme(self, item: Gtk.RadioMenuItem, theme: str) -> None:
        if not item.get_active():
            return
        self.prefs.set("theme", theme)
        if self._cached_snap is not None:
            self._push_metrics(self._cached_snap)

    def _on_pick_release_mode(self, item: Gtk.RadioMenuItem, mode: AutoReleaseMode) -> None:
        if not item.get_active():
            return
        self.prefs.set("autoReleaseMode", mode.value)
        self._pressure_high_ticks = 0     # reset hysteresis on mode change

    def _on_release_now(self, *_: object) -> None:
        self._trigger_release(manual=True)

    # ── Anchor / panel positioning ──────────────────────────────────────────

    def _anchor_position(self) -> tuple[int, int]:
        """
        AppIndicators don't expose their geometry to client code, so we
        approximate: pin the panel to the top-right of the primary display,
        just below the GNOME top bar. Close to where the indicator lives.
        """
        display = Gdk.Display.get_default()
        if display is None:
            return (40, 40)
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        if monitor is None:
            return (40, 40)
        geo = monitor.get_geometry()
        # GNOME top bar is ~32px on default scale; leave a small gap.
        margin = 8
        topbar = 36
        x = geo.x + geo.width - PANEL_WIDTH - margin
        y = geo.y + topbar
        return (x, y)

    # ── Tick ────────────────────────────────────────────────────────────────

    def _tick_wrapper(self) -> bool:
        try:
            self._tick()
        except Exception as e:               # noqa: BLE001
            print(f"[free-linux-monitor] tick error: {e}", file=sys.stderr)
        return True   # keep the GLib timeout alive

    def _tick(self) -> None:
        snap = metrics.snapshot()
        self._cached_snap = snap

        self._tick_count += 1
        if self._tick_count >= ROTATION_SECONDS:
            self._tick_count = 0
            self._metric_index += 1

        self._evaluate_auto_release(snap)
        self._render_indicator(snap)

        if self.panel.is_visible():
            self._push_metrics(snap)

    # ── Indicator rendering ─────────────────────────────────────────────────

    def _alerting(self, snap: metrics.MetricsSnapshot) -> list[str]:
        alerts: list[str] = []
        if snap.cpu > CPU_ALERT:
            alerts.append("cpu")
        if snap.memory > MEM_ALERT:
            alerts.append("mem")
        if snap.gpuUsage >= 0 and snap.gpuUsage > GPU_ALERT:
            alerts.append("gpu")
        if snap.diskPercent > DISK_ALERT:
            alerts.append("disk")
        return alerts

    def _available_metrics(self, snap: metrics.MetricsSnapshot) -> list[str]:
        m = ["cpu", "mem"]
        if snap.gpuUsage >= 0:
            m.append("gpu")
        m.append("disk")
        return m

    def _format_metric(self, kind: str, snap: metrics.MetricsSnapshot) -> str:
        v = {
            "cpu": snap.cpu,
            "mem": snap.memory,
            "gpu": snap.gpuUsage,
            "disk": snap.diskPercent,
        }[kind]
        label = {"cpu": "CPU", "mem": "MEM", "gpu": "GPU", "disk": "DSK"}[kind]
        return f"{label} {max(0, min(100, int(v))):2d}%"

    def _render_indicator(self, snap: metrics.MetricsSnapshot) -> None:
        # While the cleanup animation runs it owns the indicator label.
        if self._animation_timer_id is not None or self._animation_frames:
            return

        alerts = self._alerting(snap)
        is_alert = bool(alerts)

        if is_alert:
            self.indicator.set_status(AppIndicator3.IndicatorStatus.ATTENTION)
        else:
            self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)

        if not self.prefs.get("showLiveMetrics"):
            self.indicator.set_label("", "")
            return

        # Always rotate over every available metric, even during an alert.
        # The status switch to ATTENTION already swaps the icon to the red
        # variant, which is the visual alert. Restricting the label pool to
        # alerting metrics meant a near-full disk pinned the label to
        # "DSK 9X%" forever and CPU / MEM / GPU never got airtime.
        pool = self._available_metrics(snap)
        if not pool:
            self.indicator.set_label("", "")
            return
        kind = pool[self._metric_index % len(pool)]
        # The "guide" string keeps the slot from twitching width as the
        # text rotates between metrics — width is computed from this.
        self.indicator.set_label(self._format_metric(kind, snap), "DSK 100%")

    # ── WebView push ────────────────────────────────────────────────────────

    def _push_metrics(self, snap: metrics.MetricsSnapshot) -> None:
        body = snap.to_json()
        opts = json.dumps({
            "showBreakdown": bool(self.prefs.get("showMemoryBreakdown")),
            "theme": self.prefs.get("theme"),
        })
        js = (
            "if(typeof window.updateMetrics==='function'){"
            f"window.updateMetrics({body}, {opts});"
            "}"
        )
        self.panel.run_js(js)

    def _push_release_toast(self, bytes_freed: int, when: datetime) -> None:
        mb = bytes_freed / (1024 * 1024)
        if mb >= 1024:
            text = f"{mb / 1024:.1f} GB"
        else:
            text = f"{mb:.0f} MB"
        text = text.replace("'", "\\'")
        ts = when.strftime("%H:%M:%S")
        js = (
            "if(typeof window.showReleaseToast==='function'){"
            f"window.showReleaseToast('{text}', '{ts}');"
            "}"
        )
        self.panel.run_js(js)

    # ── Auto-release ────────────────────────────────────────────────────────

    def _evaluate_auto_release(self, snap: metrics.MetricsSnapshot) -> None:
        if self._is_releasing or self._animation_timer_id is not None:
            return
        if self._last_release_at is not None:
            if time.monotonic() - self._last_release_at < AUTO_RELEASE_COOLDOWN_SEC:
                return

        pressure = snap.memBreakdown.pressure
        if pressure >= AUTO_RELEASE_TRIGGER_PCT:
            self._pressure_high_ticks += 1
        else:
            self._pressure_high_ticks = 0
            return

        if self._pressure_high_ticks < AUTO_RELEASE_HOLD_TICKS:
            return

        mode = AutoReleaseMode(self.prefs.get("autoReleaseMode"))
        if mode == AutoReleaseMode.OFF:
            self._pressure_high_ticks = 0
        elif mode == AutoReleaseMode.NOTIFY:
            self._pressure_high_ticks = 0
            self._last_release_at = time.monotonic()
            self._notify(
                "Memory pressure high",
                f"Memory at {pressure:.0f}% — open the menu to release cache.",
            )
        else:
            self._pressure_high_ticks = 0
            self._trigger_release(manual=False)

    def _trigger_release(self, manual: bool) -> None:
        if self._is_releasing:
            return
        mode = AutoReleaseMode(self.prefs.get("autoReleaseMode"))
        if manual and mode in (AutoReleaseMode.OFF, AutoReleaseMode.NOTIFY):
            mode = AutoReleaseMode.AUTO_PASSWORD
        if mode not in (AutoReleaseMode.AUTO_PASSWORD, AutoReleaseMode.AUTO_SUDOERS):
            return

        self._is_releasing = True
        self._last_release_at = time.monotonic()
        self._start_cleanup_animation()

        running_mode = mode

        def on_done(result: ReleaseResult) -> None:
            # Releaser callback is on a worker thread; bounce to GLib main.
            GLib.idle_add(self._on_release_finished, result, running_mode)

        run_release(running_mode, on_done)

    def _on_release_finished(self, result: ReleaseResult, mode: AutoReleaseMode) -> bool:
        self._is_releasing = False
        snap = self._cached_snap
        if snap is None:
            self._finish_cleanup_animation(0.0, result.success)
            return False

        delta_pct = result.delta(snap.memBreakdown.total)
        self._last_release_result = (result.bytes_released, delta_pct, datetime.now())
        self._finish_cleanup_animation(delta_pct, result.success)

        if not result.success and result.error_message and result.error_message != "cancelled":
            body = (
                "sudoers-free mode needs a NOPASSWD rule for the purge helper. "
                "See README."
                if mode == AutoReleaseMode.AUTO_SUDOERS
                else result.error_message
            )
            self._notify("Memory release failed", body)
        return False

    # ── Cleanup animation (indicator label) ─────────────────────────────────

    def _start_cleanup_animation(self) -> None:
        pressure = int(self._cached_snap.memBreakdown.pressure) if self._cached_snap else 0
        # (text, severity) — severity drives the indicator status (ACTIVE = green,
        # ATTENTION = red). AppIndicator can't tint arbitrary label colour
        # directly, so we lean on status for the alert tone.
        self._animation_frames = [
            (f"MEM {pressure}%", "alert"),
            ("[FLUSH ····]", "warn"),
            ("[FLUSH ▓···]", "warn"),
            ("[FLUSH ▓▓··]", "warn"),
            ("[FLUSH ▓▓▓·]", "warn"),
            ("[FLUSH ▓▓▓▓]", "warn"),
        ]
        self._animation_index = 0
        if self._animation_timer_id is not None:
            GLib.source_remove(self._animation_timer_id)
        self._animation_timer_id = GLib.timeout_add(200, self._advance_animation)
        self._render_animation_frame()

    def _advance_animation(self) -> bool:
        self._animation_index += 1
        if self._animation_index >= len(self._animation_frames):
            self._animation_timer_id = None
            return False    # stop the timer; finish_cleanup_animation will continue
        self._render_animation_frame()
        return True

    def _finish_cleanup_animation(self, delta_pct: float, success: bool) -> None:
        pressure = int(self._cached_snap.memBreakdown.pressure) if self._cached_snap else 0
        if success:
            text = f"MEM {pressure}% ▼{max(0, int(round(delta_pct)))}"
            severity = "ok"
        else:
            text = "[FLUSH FAIL]"
            severity = "warn"
        self._animation_frames = [(text, severity)]
        self._animation_index = 0
        if self._animation_timer_id is not None:
            GLib.source_remove(self._animation_timer_id)
            self._animation_timer_id = None
        self._render_animation_frame()

        # Hold the result frame ~900ms, then return to normal rendering.
        GLib.timeout_add(900, self._end_animation)

    def _end_animation(self) -> bool:
        self._animation_frames = []
        self._animation_index = 0
        self._animation_timer_id = None
        if self._cached_snap is not None:
            self._render_indicator(self._cached_snap)
        # Push the result line into the panel toast as well.
        if self.panel.is_visible() and self._last_release_result is not None:
            bytes_freed, _delta, when = self._last_release_result
            self._push_release_toast(bytes_freed, when)
        return False

    def _render_animation_frame(self) -> None:
        if self._animation_index >= len(self._animation_frames):
            return
        text, severity = self._animation_frames[self._animation_index]
        if severity in ("alert", "warn"):
            self.indicator.set_status(AppIndicator3.IndicatorStatus.ATTENTION)
        else:
            self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_label(text, "[FLUSH ▓▓▓▓]")

    # ── Notifications ───────────────────────────────────────────────────────

    def _notify(self, summary: str, body: str) -> None:
        try:
            n = Notify.Notification.new(summary, body, "free-linux-monitor")
            n.show()
        except Exception:                    # noqa: BLE001
            pass


_singleton_lock_fd: Optional[int] = None


def _acquire_singleton_lock() -> bool:
    # Per-user lock so a second tray instance (e.g. GNOME re-firing the
    # XDG autostart entry after a long suspend/resume) exits silently
    # instead of fighting the running one for the indicator slot.
    global _singleton_lock_fd
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp"
    lock_path = os.path.join(runtime_dir, f"free-linux-monitor.{os.getuid()}.lock")
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    except OSError as e:
        print(f"[free-linux-monitor] lock open failed ({e}); skipping singleton check",
              file=sys.stderr)
        return True
    _singleton_lock_fd = fd
    return True


def main() -> int:
    if not INDEX_HTML.exists():
        print(f"Resource missing: {INDEX_HTML}", file=sys.stderr)
        return 1
    if not _acquire_singleton_lock():
        print("[free-linux-monitor] another instance is already running; exiting.",
              file=sys.stderr)
        return 0
    App()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
