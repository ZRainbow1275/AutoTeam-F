"""Lightweight runtime resource probes for the AutoTeam service container."""

from __future__ import annotations

import gc
import os
import subprocess
from pathlib import Path
from typing import Any

PROC_SELF_STATUS = Path("/proc/self/status")
CGROUP_MEMORY_CURRENT = Path("/sys/fs/cgroup/memory.current")
CGROUP_MEMORY_MAX = Path("/sys/fs/cgroup/memory.max")
CGROUP_PIDS_CURRENT = Path("/sys/fs/cgroup/pids.current")
CGROUP_PIDS_MAX = Path("/sys/fs/cgroup/pids.max")


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _read_int(path: Path) -> int | None:
    text = _read_text(path)
    if not text or text == "max":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _read_proc_rss_bytes() -> int | None:
    text = _read_text(PROC_SELF_STATUS)
    if not text:
        return None
    for line in text.splitlines():
        if not line.startswith("VmRSS:"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            try:
                return int(parts[1]) * 1024
            except ValueError:
                return None
    return None


def _process_rows() -> list[str]:
    try:
        result = subprocess.run(
            ["ps", "-eo", "state=,comm=,args="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _count_browser_processes(rows: list[str]) -> dict[str, int]:
    total = 0
    live = 0
    zombie = 0
    for row in rows:
        lower = row.lower()
        if "chrome" not in lower and "chromium" not in lower and "playwright" not in lower:
            continue
        total += 1
        if row[:1] == "Z":
            zombie += 1
        else:
            live += 1
    return {
        "browser_process_total": total,
        "browser_process_live": live,
        "browser_process_zombie": zombie,
    }


def _bytes_to_mb(value: int | None) -> float | None:
    if value is None:
        return None
    return round(value / 1024 / 1024, 1)


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def collect_runtime_resource_snapshot() -> dict[str, Any]:
    """Collect memory and browser-process counters without external dependencies."""

    rss_bytes = _read_proc_rss_bytes()
    memory_current_bytes = _read_int(CGROUP_MEMORY_CURRENT)
    memory_max_bytes = _read_int(CGROUP_MEMORY_MAX)
    pids_current = _read_int(CGROUP_PIDS_CURRENT)
    pids_max = _read_int(CGROUP_PIDS_MAX)
    process_counts = _count_browser_processes(_process_rows())

    memory_usage_ratio = None
    if memory_current_bytes is not None and memory_max_bytes:
        memory_usage_ratio = memory_current_bytes / memory_max_bytes

    return {
        "rss_mb": _bytes_to_mb(rss_bytes),
        "cgroup_memory_mb": _bytes_to_mb(memory_current_bytes),
        "cgroup_memory_limit_mb": _bytes_to_mb(memory_max_bytes),
        "cgroup_memory_usage_ratio": memory_usage_ratio,
        "pids_current": pids_current,
        "pids_max": pids_max,
        **process_counts,
    }


def _fmt_mb(value: float | None) -> str:
    if value is None:
        return "unknown"
    return f"{value:.1f}MiB"


def _fmt_ratio(value: float | None) -> str:
    if value is None:
        return "unknown"
    return f"{value * 100:.1f}%"


def log_runtime_resource_snapshot(logger: Any, *, label: str = "runtime") -> dict[str, Any]:
    """Log a resource snapshot and run GC when memory is near the cgroup limit."""

    snapshot = collect_runtime_resource_snapshot()
    logger.info(
        "[资源] %s rss=%s cgroup=%s/%s usage=%s pids=%s/%s browser_live=%d browser_zombie=%d",
        label,
        _fmt_mb(snapshot["rss_mb"]),
        _fmt_mb(snapshot["cgroup_memory_mb"]),
        _fmt_mb(snapshot["cgroup_memory_limit_mb"]),
        _fmt_ratio(snapshot["cgroup_memory_usage_ratio"]),
        snapshot["pids_current"] if snapshot["pids_current"] is not None else "unknown",
        snapshot["pids_max"] if snapshot["pids_max"] is not None else "unknown",
        snapshot["browser_process_live"],
        snapshot["browser_process_zombie"],
    )

    ratio = snapshot["cgroup_memory_usage_ratio"]
    warn_ratio = _float_env("AUTOTEAM_MEMORY_WARN_RATIO", 0.85)
    if ratio is not None and ratio >= warn_ratio:
        collected = gc.collect()
        logger.warning(
            "[资源] %s cgroup memory usage %s >= %.1f%%, gc.collect reclaimed %d objects",
            label,
            _fmt_ratio(ratio),
            warn_ratio * 100,
            collected,
        )

    zombie_threshold = _int_env("AUTOTEAM_ZOMBIE_WARN_THRESHOLD", 20)
    if snapshot["browser_process_zombie"] >= zombie_threshold:
        logger.warning(
            "[资源] %s browser zombie processes=%d >= %d; container init/reaper should be enabled",
            label,
            snapshot["browser_process_zombie"],
            zombie_threshold,
        )

    return snapshot
