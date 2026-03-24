"""System-level awareness -- host metrics and service logs for the Raspberry Pi."""

import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class SystemHealth:
    cpu_usage_pct: float
    ram_usage_pct: float
    disk_free_gb: float
    temperature_c: Optional[float] = None
    load_avg: List[float] = None


def get_system_stats() -> SystemHealth:
    """Gather host metrics using standard linux tools."""
    # CPU Usage (simplified)
    cpu = 0.0
    try:
        out = subprocess.check_output(["top", "-bn1"], text=True)
        for line in out.splitlines():
            if "%Cpu(s)" in line:
                # e.g. "%Cpu(s):  5.0 us,  2.0 sy..."
                cpu = float(line.split(":")[1].split()[0])
                break
    except Exception: pass

    # RAM Usage
    ram = 0.0
    try:
        out = subprocess.check_output(["free", "-m"], text=True)
        for line in out.splitlines():
            if "Mem:" in line:
                parts = line.split()
                total = int(parts[1])
                used = int(parts[2])
                ram = (used / total) * 100
    except Exception: pass

    # Disk Free
    disk = 0.0
    try:
        out = subprocess.check_output(["df", "-h", "/"], text=True)
        line = out.splitlines()[1]
        parts = line.split()
        disk = float(parts[3].replace("G", "").replace("M", "0.001"))
    except Exception: pass

    # Temperature (Pi specific)
    temp = None
    try:
        if os.path.exists("/usr/bin/vcgencmd"):
            out = subprocess.check_output(["vcgencmd", "measure_temp"], text=True)
            # temp=45.0'C
            temp = float(out.split("=")[1].split("'")[0])
    except Exception: pass

    return SystemHealth(
        cpu_usage_pct=cpu,
        ram_usage_pct=ram,
        disk_free_gb=disk,
        temperature_c=temp,
        load_avg=list(os.getloadavg())
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
        f"Disk {health.disk_free_gb}GB free | "
        f"Temp {temp_str}\n"
        f"Load: {health.load_avg}"
    )
