"""
Linux equivalent of Mac MemoryReleaser.swift.

`/usr/sbin/purge` on macOS → `sysctl -w vm.drop_caches=3 && sync` on Linux.
This drops the page cache, dentries, and inodes — the closest analog to
purge's user-visible effect ("free -h" shows the cached column collapse).

Three modes mirror the Mac app:

  notify        — don't run anything; the caller posts a desktop notification
  auto-password — pkexec /usr/local/sbin/free-linux-monitor-purge
                  (or fallback: pkexec sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches')
                  PolicyKit shows a graphical password dialog.
  auto-sudoers  — sudo -n /usr/local/sbin/free-linux-monitor-purge
                  Requires a one-time sudoers rule (see scripts/setup-sudoers.sh).
                  Falls back to failure if the rule is absent.
"""

from __future__ import annotations

import enum
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .metrics import memory_breakdown


PURGE_HELPER_PATH = "/usr/local/sbin/free-linux-monitor-purge"


class AutoReleaseMode(enum.Enum):
    OFF = "off"
    NOTIFY = "notify"
    AUTO_PASSWORD = "auto-password"
    AUTO_SUDOERS = "auto-sudoers"

    @property
    def menu_title(self) -> str:
        return {
            AutoReleaseMode.OFF: "Off",
            AutoReleaseMode.NOTIFY: "Notify only (recommended)",
            AutoReleaseMode.AUTO_PASSWORD: "Auto-run — prompt password",
            AutoReleaseMode.AUTO_SUDOERS: "Auto-run — sudoers-free",
        }[self]


@dataclass
class ReleaseResult:
    before_bytes: int
    after_bytes: int
    success: bool
    error_message: Optional[str]

    def delta(self, total: int) -> float:
        if total <= 0 or self.before_bytes < self.after_bytes:
            return 0.0
        return (self.before_bytes - self.after_bytes) / total * 100.0

    @property
    def bytes_released(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


# ── helper-script command builders ──────────────────────────────────────────

def _drop_cache_argv() -> list[str]:
    """
    Prefer the dedicated helper script if it's been installed (which means
    /etc/sudoers.d entry refers to that one path — far safer than allowing
    arbitrary shell). Fall back to inline `sh -c` for first-run / dev mode.
    """
    if os.path.isfile(PURGE_HELPER_PATH) and os.access(PURGE_HELPER_PATH, os.X_OK):
        return [PURGE_HELPER_PATH]
    return ["sh", "-c", "sync && echo 3 > /proc/sys/vm/drop_caches"]


# ── backends ────────────────────────────────────────────────────────────────

def _run_via_pkexec() -> tuple[bool, Optional[str]]:
    if not shutil.which("pkexec"):
        return False, "pkexec not installed (try: sudo apt install policykit-1)"
    argv = ["pkexec"] + _drop_cache_argv()
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
    except subprocess.TimeoutExpired:
        return False, "pkexec timed out"
    except OSError as e:
        return False, f"pkexec failed: {e}"
    if r.returncode == 0:
        return True, None
    # 126/127 = user dismissed the auth dialog or pkexec couldn't authenticate.
    if r.returncode in (126, 127):
        return False, "cancelled"
    err = (r.stderr or r.stdout).strip()
    return False, err or f"pkexec exited {r.returncode}"


def _run_via_sudo() -> tuple[bool, Optional[str]]:
    if not shutil.which("sudo"):
        return False, "sudo not installed"
    argv = ["sudo", "-n"] + _drop_cache_argv()
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=15, check=False)
    except subprocess.TimeoutExpired:
        return False, "sudo timed out"
    except OSError as e:
        return False, f"sudo failed: {e}"
    if r.returncode == 0:
        return True, None
    err = (r.stderr or "").strip() or (r.stdout or "").strip()
    return False, err or f"sudo exited {r.returncode}"


# ── public API ──────────────────────────────────────────────────────────────

def release(
    mode: AutoReleaseMode,
    completion: Callable[[ReleaseResult], None],
) -> None:
    """
    Run the release in a background thread; deliver `completion` on the same
    thread it was called from (the GTK side wraps with GLib.idle_add to
    re-enter the main loop safely).
    """
    import threading

    def worker() -> None:
        before = memory_breakdown()
        before_used = before.app + before.wired + before.compressed

        ok, err = False, None
        if mode == AutoReleaseMode.AUTO_PASSWORD:
            ok, err = _run_via_pkexec()
        elif mode == AutoReleaseMode.AUTO_SUDOERS:
            ok, err = _run_via_sudo()
        else:
            err = f"release suppressed by mode {mode.value}"

        # drop_caches takes effect synchronously on Linux, but the next
        # /proc/meminfo read can still show stale numbers for a tick or two
        # depending on per-CPU stat aggregation. A short wait makes the
        # delta the user sees match what `free -h` would show.
        time.sleep(0.3)
        after = memory_breakdown()
        after_used = after.app + after.wired + after.compressed

        completion(ReleaseResult(
            before_bytes=before_used,
            after_bytes=after_used,
            success=ok,
            error_message=err,
        ))

    threading.Thread(target=worker, daemon=True).start()
