"""System-level awareness -- host metrics and service logs for the Raspberry Pi."""

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# Fixed locale for the tools we still parse, so decimal separators stay ASCII.
_C_LOCALE_ENV = {**os.environ, "LC_ALL": "C", "LANG": "C"}


_TOP_CPU_LINE = re.compile(r"^%Cpu(?:\(s\)|\d+)\s*:")


def _top_idle_pct(line: str) -> Optional[float]:
    """Return the idle percentage from a top CPU line, or None.

    Reads the "id" field rather than "us": user time alone reports a host
    pinned at 90% system time as 0% busy.
    """
    if not _TOP_CPU_LINE.match(line.strip()):
        return None
    for field in line.split(":", 1)[1].split(","):
        parts = field.split()
        if len(parts) == 2 and parts[1] == "id":
            return float(parts[0])
    return None


@dataclass
class SystemHealth:
    cpu_usage_pct: float
    ram_usage_pct: float
    disk_free_gb: float
    temperature_c: Optional[float] = None
    load_avg: List[float] = None


def get_system_stats() -> SystemHealth:
    """Gather host metrics using standard linux tools."""
    # CPU Usage
    #
    # 100 - idle, not the "us" field alone. Taking user time reported a host
    # pinned at 90% system time as 0% busy, which is the opposite of what a
    # health metric is for.
    cpu = 0.0
    try:
        out = subprocess.check_output(
            ["top", "-bn1"], text=True, env=_C_LOCALE_ENV,
            stderr=subprocess.DEVNULL,
        )
        # Both the aggregate "%Cpu(s):" line and the per-core "%Cpu0:" lines
        # that top emits when separate-CPU display is persisted in the service
        # user's toprc. Matching only the aggregate reported a busy host as
        # idle on any machine with that setting.
        idles = [
            idle for line in out.splitlines()
            if (idle := _top_idle_pct(line)) is not None
        ]
        if idles:
            cpu = round(100.0 - (sum(idles) / len(idles)), 1)
    except Exception: pass

    # RAM Usage
    ram = 0.0
    try:
        out = subprocess.check_output(
            ["free", "-m"], text=True, env=_C_LOCALE_ENV,
            stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            if "Mem:" in line:
                parts = line.split()
                total = int(parts[1])
                used = int(parts[2])
                ram = (used / total) * 100
    except Exception: pass

    # Disk Free
    #
    # shutil.disk_usage rather than parsing df -h. That output is
    # locale-dependent (a German host prints "4,0Gi"), platform-dependent
    # (macOS prints "93Gi", which the old suffix parsing read as 0), uses
    # binary powers while the field is named GB, and wraps onto two lines for
    # long device names. None of that applies to a syscall returning bytes.
    disk = 0.0
    try:
        disk = shutil.disk_usage("/").free / (1024 ** 3)
    except Exception: pass

    # Temperature (Pi specific)
    temp = None
    try:
        if os.path.exists("/usr/bin/vcgencmd"):
            out = subprocess.check_output(
                ["vcgencmd", "measure_temp"], text=True, stderr=subprocess.DEVNULL
            )
            # temp=45.0'C
            temp = float(out.split("=")[1].split("'")[0])
    except Exception: pass

    # Inside a try like every other metric: it raises OSError where it is
    # unavailable, and get_system_summary is called from the improvement cycle
    # without a guard, so an escape here would abort the cycle rather than lose
    # one number.
    load: List[float] = []
    try:
        load = list(os.getloadavg())
    except (OSError, AttributeError): pass

    return SystemHealth(
        cpu_usage_pct=cpu,
        ram_usage_pct=ram,
        disk_free_gb=disk,
        temperature_c=temp,
        load_avg=load,
    )


def get_service_logs(lines: int = 50) -> str:
    """Retrieve recent logs from the systemd service."""
    try:
        out = subprocess.check_output(
            ["journalctl", "-u", "ouroboros.service", "-n", str(lines), "--no-pager"],
            text=True, stderr=subprocess.STDOUT
        )
        return out
    except Exception as e:
        return f"Could not retrieve logs: {e}"


def get_system_summary() -> str:
    """Human-readable summary for LLM context."""
    health = get_system_stats()
    temp_str = f"{health.temperature_c}°C" if health.temperature_c else "N/A"
    return (
        f"System Health: CPU {health.cpu_usage_pct}% | "
        f"RAM {health.ram_usage_pct:.1f}% | "
        f"Disk {health.disk_free_gb:.1f}GB free | "
        f"Temp {temp_str}\n"
        f"Load: {health.load_avg}"
    )
