"""
Linux equivalent of Mac SystemMetrics.swift.

Mirrors the Activity-Monitor-style 5-segment memory breakdown so the WebKit
dashboard can be reused unchanged. CPU via /proc/stat (delta of jiffies),
memory from /proc/meminfo, GPU best-effort (Xe driver gtidle, then nvidia-smi,
then radeontop, then N/A), disk via statvfs of "/".
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import psutil


# ── Memory breakdown ────────────────────────────────────────────────────────

@dataclass
class MemoryBreakdown:
    total: int
    app: int
    wired: int
    compressed: int
    cached: int
    free: int

    @property
    def used(self) -> int:
        return self.app + self.wired + self.compressed + self.cached

    @property
    def pressure(self) -> float:
        # (App + Wired + Compressed) / Total — matches FreeMacMonitor's metric
        # so the auto-release threshold logic ports verbatim.
        if self.total <= 0:
            return 0.0
        return (self.app + self.wired + self.compressed) / self.total * 100.0


@dataclass
class MetricsSnapshot:
    cpu: float
    memory: float          # equals memBreakdown.pressure (kept for JS contract)
    memBreakdown: MemoryBreakdown
    gpuUsage: float        # -1.0 means N/A
    diskUsed: int
    diskTotal: int

    @property
    def diskPercent(self) -> float:
        return self.diskUsed / self.diskTotal * 100.0 if self.diskTotal > 0 else 0.0

    def to_json(self) -> str:
        d = asdict(self)
        d["diskPercent"] = self.diskPercent
        return json.dumps(d)


# ── /proc/meminfo parsing ────────────────────────────────────────────────────

def _read_meminfo() -> dict[str, int]:
    """Return /proc/meminfo as { key: bytes } (kB → bytes)."""
    out: dict[str, int] = {}
    try:
        with open("/proc/meminfo", "rb") as f:
            for raw in f:
                line = raw.decode("ascii", "replace")
                k, _, rest = line.partition(":")
                rest = rest.strip()
                # Most lines end with " kB"; a couple are bare numbers.
                parts = rest.split()
                try:
                    val_kb = int(parts[0])
                except (ValueError, IndexError):
                    continue
                out[k] = val_kb * 1024
    except OSError:
        pass
    return out


def memory_breakdown() -> MemoryBreakdown:
    """
    Map Linux /proc/meminfo onto the macOS Activity-Monitor-style 5 buckets:

      App        = anonymous user pages (Active+Inactive(anon) − Shmem)
      Wired      = non-reclaimable kernel + shared (SUnreclaim + KernelStack
                   + PageTables + Shmem)
      Compressed = Zswap (compressed swap pool, Zswap key in /proc/meminfo)
      Cached     = page cache (Buffers + Active+Inactive(file) + SReclaimable)
      Free       = MemFree

    These are picked so Used = MemTotal − Free closely matches the sum of the
    other four buckets. The split echoes Activity Monitor's semantics rather
    than `free -h`'s used/avail numbers, which the FMM dashboard expects.
    """
    m = _read_meminfo()
    total = m.get("MemTotal", 0)
    free = m.get("MemFree", 0)

    buffers = m.get("Buffers", 0)
    cached_kern = m.get("Cached", 0)
    active_file = m.get("Active(file)", 0)
    inactive_file = m.get("Inactive(file)", 0)
    active_anon = m.get("Active(anon)", 0)
    inactive_anon = m.get("Inactive(anon)", 0)
    shmem = m.get("Shmem", 0)
    sreclaimable = m.get("SReclaimable", 0)
    sunreclaim = m.get("SUnreclaim", 0)
    kernel_stack = m.get("KernelStack", 0)
    page_tables = m.get("PageTables", 0)
    zswap = m.get("Zswap", 0)

    cached = buffers + max(cached_kern, active_file + inactive_file) + sreclaimable
    wired = sunreclaim + kernel_stack + page_tables + shmem
    compressed = zswap
    app = max(0, (active_anon + inactive_anon) - shmem)

    # Sanity: if categories overshoot total (rounding / overlaps), trim
    # cached first since it has the most slack against page-cache reclaim.
    accounted = app + wired + compressed + cached + free
    if accounted > total > 0:
        cached = max(0, cached - (accounted - total))

    return MemoryBreakdown(
        total=total,
        app=app,
        wired=wired,
        compressed=compressed,
        cached=cached,
        free=free,
    )


# ── CPU ─────────────────────────────────────────────────────────────────────

_cpu_prev: Optional[tuple[int, int]] = None  # (idle_total, total_total)


def cpu_usage() -> float:
    """Whole-system CPU % via delta of /proc/stat aggregate line."""
    global _cpu_prev
    try:
        with open("/proc/stat", "rb") as f:
            first = f.readline().decode("ascii", "replace").split()
    except OSError:
        return 0.0
    if not first or first[0] != "cpu":
        return 0.0
    nums = [int(x) for x in first[1:]]
    # user nice system idle iowait irq softirq steal guest guest_nice
    while len(nums) < 8:
        nums.append(0)
    idle_all = nums[3] + nums[4]                # idle + iowait
    non_idle = nums[0] + nums[1] + nums[2] + nums[5] + nums[6] + nums[7]
    total = idle_all + non_idle

    if _cpu_prev is None:
        _cpu_prev = (idle_all, total)
        return 0.0

    d_idle = idle_all - _cpu_prev[0]
    d_total = total - _cpu_prev[1]
    _cpu_prev = (idle_all, total)
    if d_total <= 0:
        return 0.0
    return max(0.0, min(100.0, (d_total - d_idle) / d_total * 100.0))


# ── GPU ─────────────────────────────────────────────────────────────────────

_gpu_xe_state: dict[str, tuple[int, float]] = {}  # path → (idle_ms, mono_time)


def _gpu_xe() -> Optional[float]:
    """
    Intel Xe driver: /sys/class/drm/cardN/device/tile0/gt0/gtidle/idle_residency_ms
    is a monotonic counter of GT C6 (deep idle) residency. usage% = 100 - idle%.
    Returns the highest usage across discovered GTs, or None if unsupported.
    """
    candidates: list[Path] = []
    base = Path("/sys/class/drm")
    if not base.exists():
        return None
    for card in sorted(base.glob("card[0-9]*")):
        for tile in sorted(card.glob("device/tile*")):
            for gt in sorted(tile.glob("gt*")):
                p = gt / "gtidle" / "idle_residency_ms"
                if p.is_file():
                    candidates.append(p)
    if not candidates:
        return None

    now = time.monotonic()
    usages: list[float] = []
    for p in candidates:
        try:
            idle_ms = int(p.read_text().strip())
        except (OSError, ValueError):
            continue
        prev = _gpu_xe_state.get(str(p))
        _gpu_xe_state[str(p)] = (idle_ms, now)
        if prev is None:
            continue
        d_idle = idle_ms - prev[0]
        d_wall_ms = max(1.0, (now - prev[1]) * 1000.0)
        idle_pct = max(0.0, min(100.0, d_idle / d_wall_ms * 100.0))
        usages.append(100.0 - idle_pct)
    if not usages:
        # First sample established the baseline; report unknown until next tick.
        return None
    return max(usages)


def _gpu_nvidia() -> Optional[float]:
    """nvidia-smi --query-gpu=utilization.gpu — fast, no root needed."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=1.5, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    pcts: list[float] = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pcts.append(float(line))
        except ValueError:
            pass
    return max(pcts) if pcts else None


def _gpu_amd() -> Optional[float]:
    """amdgpu busy_percent sysfs file."""
    base = Path("/sys/class/drm")
    if not base.exists():
        return None
    pcts: list[float] = []
    for card in sorted(base.glob("card[0-9]*")):
        p = card / "device" / "gpu_busy_percent"
        if p.is_file():
            try:
                pcts.append(float(p.read_text().strip()))
            except (OSError, ValueError):
                pass
    return max(pcts) if pcts else None


def gpu_usage() -> float:
    """Returns 0..100 or -1.0 when no GPU data source is available."""
    for fn in (_gpu_nvidia, _gpu_amd, _gpu_xe):
        v = fn()
        if v is not None:
            return max(0.0, min(100.0, v))
    return -1.0


# ── Disk ────────────────────────────────────────────────────────────────────

def disk_root() -> tuple[int, int]:
    """(used_bytes, total_bytes) for the root filesystem."""
    try:
        st = os.statvfs("/")
    except OSError:
        return 0, 0
    total = st.f_blocks * st.f_frsize
    free = st.f_bfree * st.f_frsize
    return total - free, total


# ── Snapshot ────────────────────────────────────────────────────────────────

def snapshot() -> MetricsSnapshot:
    cpu = cpu_usage()
    mem = memory_breakdown()
    used, total_disk = disk_root()
    return MetricsSnapshot(
        cpu=cpu,
        memory=mem.pressure,
        memBreakdown=mem,
        gpuUsage=gpu_usage(),
        diskUsed=used,
        diskTotal=total_disk,
    )


# psutil is imported but currently unused; keep the import so the install
# script's dependency check fails loudly if it's missing. Future versions
# may switch to psutil for portability across kernels.
_ = psutil
