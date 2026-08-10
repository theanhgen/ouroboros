"""Tests for host metrics and service log collection."""

import subprocess
from unittest import mock

import pytest

from ouroboros.system import (
    SystemHealth,
    get_service_logs,
    get_system_stats,
    get_system_summary,
)

TOP_OUTPUT = """top - 12:00:00 up 1 day,  2:03,  1 user,  load average: 0.10, 0.20, 0.30
Tasks: 120 total,   1 running, 119 sleeping,   0 stopped,   0 zombie
%Cpu(s):  7.3 us,  2.1 sy,  0.0 ni, 90.6 id,  0.0 wa,  0.0 hi,  0.0 si,  0.0 st
MiB Mem :   3792.0 total,    120.0 free,   1500.0 used,   2172.0 buff/cache
"""

FREE_OUTPUT = """              total        used        free      shared  buff/cache   available
Mem:           3792        1500         120          10        2172        2100
Swap:           100           0         100
"""

VCGENCMD_OUTPUT = "temp=45.7'C\n"


def _usage(free_gb):
    """Minimal shutil.disk_usage result."""
    from collections import namedtuple

    Usage = namedtuple("Usage", "total used free")
    return Usage(total=0, used=0, free=int(free_gb * 1024 ** 3))


def _fake_check_output(*, top=TOP_OUTPUT, free=FREE_OUTPUT,
                       vcgencmd=VCGENCMD_OUTPUT, journal="log line\n"):
    """Return a check_output stand-in dispatching on the command name."""

    def run(cmd, **kwargs):
        program = cmd[0]
        table = {
            "top": top,
            "free": free,
            "vcgencmd": vcgencmd,
            "journalctl": journal,
        }
        if program not in table:
            raise AssertionError(f"unexpected command: {cmd}")
        value = table[program]
        if isinstance(value, Exception):
            raise value
        return value

    return run


# -- get_system_stats --------------------------------------------------------

def test_get_system_stats_parses_every_metric():
    with mock.patch("ouroboros.system.subprocess.check_output", _fake_check_output()):
        with mock.patch("ouroboros.system.shutil.disk_usage",
                        return_value=_usage(free_gb=34.0)):
            with mock.patch("ouroboros.system.os.path.exists", return_value=True):
                with mock.patch("ouroboros.system.os.getloadavg", return_value=(0.1, 0.2, 0.3)):
                    health = get_system_stats()

    assert isinstance(health, SystemHealth)
    # 100 - 90.6 idle, not the 7.3 user field.
    assert health.cpu_usage_pct == 9.4
    assert health.ram_usage_pct == pytest.approx(1500 / 3792 * 100)
    assert health.disk_free_gb == pytest.approx(34.0)
    assert health.temperature_c == 45.7
    assert health.load_avg == [0.1, 0.2, 0.3]


def test_temperature_is_none_without_vcgencmd():
    """Only the Pi has vcgencmd; elsewhere temperature is simply unknown."""
    with mock.patch("ouroboros.system.subprocess.check_output", _fake_check_output()):
        with mock.patch("ouroboros.system.os.path.exists", return_value=False):
            with mock.patch("ouroboros.system.os.getloadavg", return_value=(0.0, 0.0, 0.0)):
                health = get_system_stats()

    assert health.temperature_c is None


@pytest.mark.parametrize(
    ("failing", "attribute", "expected"),
    [
        ("top", "cpu_usage_pct", 0.0),
        ("free", "ram_usage_pct", 0.0),
    ],
)
def test_a_missing_tool_degrades_that_metric_only(failing, attribute, expected):
    """One absent utility must not lose the metrics that did work.

    Asserting the full SystemHealth, not just the failed field: an
    implementation that zeroed everything whenever any one command failed
    would satisfy a check on the failing metric alone.
    """
    kwargs = {failing: FileNotFoundError(f"{failing} not found")}
    intact = {
        "cpu_usage_pct": 9.4,
        "ram_usage_pct": pytest.approx(1500 / 3792 * 100),
        attribute: expected,
    }

    with mock.patch("ouroboros.system.subprocess.check_output",
                    _fake_check_output(**kwargs)):
        with mock.patch("ouroboros.system.shutil.disk_usage",
                        return_value=_usage(free_gb=34.0)):
            with mock.patch("ouroboros.system.os.path.exists", return_value=True):
                with mock.patch("ouroboros.system.os.getloadavg", return_value=(1.0, 1.0, 1.0)):
                    health = get_system_stats()

    for field, value in intact.items():
        assert getattr(health, field) == value, field
    assert health.disk_free_gb == pytest.approx(34.0)
    assert health.load_avg == [1.0, 1.0, 1.0]
    assert health.temperature_c == 45.7


def test_all_tools_missing_yields_zeroes():
    def always_fail(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    with mock.patch("ouroboros.system.subprocess.check_output", always_fail):
        with mock.patch("ouroboros.system.os.path.exists", return_value=False):
            with mock.patch("ouroboros.system.os.getloadavg", return_value=(0.0, 0.0, 0.0)):
                health = get_system_stats()

    assert (health.cpu_usage_pct, health.ram_usage_pct) == (0.0, 0.0)
    assert health.temperature_c is None


def test_unparseable_tool_output_is_tolerated():
    """Garbage from a tool must not raise out of a metrics call."""
    garbage = "not the output we expected at all\n"

    with mock.patch("ouroboros.system.subprocess.check_output",
                    _fake_check_output(top=garbage, free=garbage,
                                       vcgencmd=garbage)):
        with mock.patch("ouroboros.system.os.path.exists", return_value=True):
            with mock.patch("ouroboros.system.os.getloadavg", return_value=(0.0, 0.0, 0.0)):
                health = get_system_stats()

    assert health.cpu_usage_pct == 0.0


def test_a_called_process_error_is_tolerated():
    error = subprocess.CalledProcessError(1, ["top"])

    with mock.patch("ouroboros.system.subprocess.check_output",
                    _fake_check_output(top=error)):
        with mock.patch("ouroboros.system.os.path.exists", return_value=False):
            with mock.patch("ouroboros.system.os.getloadavg", return_value=(0.0, 0.0, 0.0)):
                health = get_system_stats()

    assert health.cpu_usage_pct == 0.0


def test_ram_with_zero_total_does_not_raise():
    """A ZeroDivisionError here would take out every other metric too."""
    zero_total = "Mem:           0        0         0\n"

    with mock.patch("ouroboros.system.subprocess.check_output",
                    _fake_check_output(free=zero_total)):
        with mock.patch("ouroboros.system.os.path.exists", return_value=False):
            with mock.patch("ouroboros.system.os.getloadavg", return_value=(0.0, 0.0, 0.0)):
                health = get_system_stats()

    assert health.ram_usage_pct == 0.0


# -- get_service_logs --------------------------------------------------------

def test_get_service_logs_returns_output():
    with mock.patch("ouroboros.system.subprocess.check_output",
                    _fake_check_output(journal="unit started\n")):
        assert get_service_logs() == "unit started\n"


def test_get_service_logs_passes_the_line_count():
    captured = {}

    def capture(cmd, **kwargs):
        captured["cmd"] = cmd
        return ""

    with mock.patch("ouroboros.system.subprocess.check_output", capture):
        get_service_logs(lines=7)

    assert "-n" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("-n") + 1] == "7"
    assert "ouroboros.service" in captured["cmd"]


def test_get_service_logs_reports_failure_instead_of_raising():
    """This feeds LLM context; an exception here would abort the cycle."""
    def fail(cmd, **kwargs):
        raise FileNotFoundError("journalctl not found")

    with mock.patch("ouroboros.system.subprocess.check_output", fail):
        result = get_service_logs()

    assert "Could not retrieve logs" in result
    assert "journalctl" in result


# -- get_system_summary ------------------------------------------------------

def test_get_system_summary_includes_every_metric():
    health = SystemHealth(
        cpu_usage_pct=7.3,
        ram_usage_pct=39.5,
        disk_free_gb=34.0,
        temperature_c=45.7,
        load_avg=[0.1, 0.2, 0.3],
    )

    with mock.patch("ouroboros.system.get_system_stats", return_value=health):
        summary = get_system_summary()

    assert "CPU 7.3%" in summary
    assert "RAM 39.5%" in summary
    assert "Disk 34.0GB free" in summary
    assert "45.7" in summary
    assert "[0.1, 0.2, 0.3]" in summary


def test_get_system_summary_shows_na_without_a_temperature():
    health = SystemHealth(
        cpu_usage_pct=0.0, ram_usage_pct=0.0, disk_free_gb=0.0,
        temperature_c=None, load_avg=[0.0, 0.0, 0.0],
    )

    with mock.patch("ouroboros.system.get_system_stats", return_value=health):
        assert "Temp N/A" in get_system_summary()


def test_get_system_summary_is_a_single_report():
    with mock.patch("ouroboros.system.subprocess.check_output", _fake_check_output()):
        with mock.patch("ouroboros.system.os.path.exists", return_value=True):
            with mock.patch("ouroboros.system.os.getloadavg", return_value=(0.1, 0.2, 0.3)):
                summary = get_system_summary()

    assert summary.count("System Health") == 1
    assert "Load:" in summary


# -- disk, via shutil rather than parsing df ---------------------------------

def test_disk_free_comes_from_a_syscall_not_df():
    """df -h output is locale- and platform-dependent; bytes are not.

    macOS prints "93Gi" and a German host prints "4,0Gi"; both defeated the
    previous suffix parsing and reported 0 GB free.
    """
    with mock.patch("ouroboros.system.subprocess.check_output", _fake_check_output()):
        with mock.patch("ouroboros.system.shutil.disk_usage",
                        return_value=_usage(free_gb=0.5)) as disk_usage:
            with mock.patch("ouroboros.system.os.path.exists", return_value=False):
                with mock.patch("ouroboros.system.os.getloadavg", return_value=(0.0, 0.0, 0.0)):
                    health = get_system_stats()

    disk_usage.assert_called_once_with("/")
    assert health.disk_free_gb == pytest.approx(0.5)


def test_disk_failure_degrades_only_that_metric():
    with mock.patch("ouroboros.system.subprocess.check_output", _fake_check_output()):
        with mock.patch("ouroboros.system.shutil.disk_usage",
                        side_effect=OSError("no such mount")):
            with mock.patch("ouroboros.system.os.path.exists", return_value=True):
                with mock.patch("ouroboros.system.os.getloadavg", return_value=(1.0, 1.0, 1.0)):
                    health = get_system_stats()

    assert health.disk_free_gb == 0.0
    assert health.cpu_usage_pct == 9.4
    assert health.temperature_c == 45.7


# -- CPU semantics -----------------------------------------------------------

def test_cpu_counts_system_time_not_just_user():
    """A host pinned at 90% system time is busy, not idle."""
    system_heavy = (
        "%Cpu(s):  0.0 us, 90.0 sy,  0.0 ni, 10.0 id,  0.0 wa,  0.0 hi,  0.0 si\n"
    )

    with mock.patch("ouroboros.system.subprocess.check_output",
                    _fake_check_output(top=system_heavy)):
        with mock.patch("ouroboros.system.shutil.disk_usage",
                        return_value=_usage(free_gb=1.0)):
            with mock.patch("ouroboros.system.os.path.exists", return_value=False):
                with mock.patch("ouroboros.system.os.getloadavg", return_value=(0.0, 0.0, 0.0)):
                    health = get_system_stats()

    assert health.cpu_usage_pct == 90.0


def test_cpu_fully_idle_is_zero():
    idle = "%Cpu(s):  0.0 us,  0.0 sy,  0.0 ni,100.0 id,  0.0 wa\n"

    with mock.patch("ouroboros.system.subprocess.check_output",
                    _fake_check_output(top=idle)):
        with mock.patch("ouroboros.system.shutil.disk_usage",
                        return_value=_usage(free_gb=1.0)):
            with mock.patch("ouroboros.system.os.path.exists", return_value=False):
                with mock.patch("ouroboros.system.os.getloadavg", return_value=(0.0, 0.0, 0.0)):
                    health = get_system_stats()

    assert health.cpu_usage_pct == 0.0


# -- load average ------------------------------------------------------------

def test_getloadavg_failure_does_not_abort_the_call():
    """get_system_summary runs unguarded in the improvement cycle."""
    with mock.patch("ouroboros.system.subprocess.check_output", _fake_check_output()):
        with mock.patch("ouroboros.system.shutil.disk_usage",
                        return_value=_usage(free_gb=1.0)):
            with mock.patch("ouroboros.system.os.path.exists", return_value=False):
                with mock.patch("ouroboros.system.os.getloadavg",
                                side_effect=OSError("unavailable")):
                    health = get_system_stats()
                    summary = get_system_summary()

    assert health.load_avg == []
    assert "Load: []" in summary


# -- locale ------------------------------------------------------------------

def test_parsed_tools_run_under_a_fixed_locale():
    """A comma decimal separator would break the numeric parsing."""
    captured = []

    def capture(cmd, **kwargs):
        captured.append((cmd[0], kwargs.get("env", {})))
        return TOP_OUTPUT if cmd[0] == "top" else FREE_OUTPUT

    with mock.patch("ouroboros.system.subprocess.check_output", capture):
        with mock.patch("ouroboros.system.shutil.disk_usage",
                        return_value=_usage(free_gb=1.0)):
            with mock.patch("ouroboros.system.os.path.exists", return_value=False):
                with mock.patch("ouroboros.system.os.getloadavg", return_value=(0.0, 0.0, 0.0)):
                    get_system_stats()

    for program, env in captured:
        assert env.get("LC_ALL") == "C", program


def test_cpu_handles_per_core_top_output():
    """top emits %Cpu0/%Cpu1 when separate-CPU display is persisted in toprc."""
    per_core = (
        "%Cpu0  :  0.0 us, 90.0 sy,  0.0 ni, 10.0 id,  0.0 wa\n"
        "%Cpu1  :  0.0 us, 80.0 sy,  0.0 ni, 20.0 id,  0.0 wa\n"
    )

    with mock.patch("ouroboros.system.subprocess.check_output",
                    _fake_check_output(top=per_core)):
        with mock.patch("ouroboros.system.shutil.disk_usage",
                        return_value=_usage(free_gb=1.0)):
            with mock.patch("ouroboros.system.os.path.exists", return_value=False):
                with mock.patch("ouroboros.system.os.getloadavg", return_value=(0.0, 0.0, 0.0)):
                    health = get_system_stats()

    # Mean idle across cores is 15%.
    assert health.cpu_usage_pct == 85.0


def test_cpu_ignores_lines_that_merely_mention_cpu():
    noise = (
        "top - 12:00:00 up 1 day\n"
        "some text about %Cpu(s) in prose\n"
        "%Cpu(s):  0.0 us,  0.0 sy,  0.0 ni, 75.0 id,  0.0 wa\n"
    )

    with mock.patch("ouroboros.system.subprocess.check_output",
                    _fake_check_output(top=noise)):
        with mock.patch("ouroboros.system.shutil.disk_usage",
                        return_value=_usage(free_gb=1.0)):
            with mock.patch("ouroboros.system.os.path.exists", return_value=False):
                with mock.patch("ouroboros.system.os.getloadavg", return_value=(0.0, 0.0, 0.0)):
                    health = get_system_stats()

    assert health.cpu_usage_pct == 25.0


def test_cpu_line_without_an_idle_field_is_ignored():
    """Some top builds omit "id"; that is unknown, not 100% busy."""
    no_idle = "%Cpu(s):  5.0 us,  2.0 sy\n"

    with mock.patch("ouroboros.system.subprocess.check_output",
                    _fake_check_output(top=no_idle)):
        with mock.patch("ouroboros.system.shutil.disk_usage",
                        return_value=_usage(free_gb=1.0)):
            with mock.patch("ouroboros.system.os.path.exists", return_value=False):
                with mock.patch("ouroboros.system.os.getloadavg", return_value=(0.0, 0.0, 0.0)):
                    health = get_system_stats()

    assert health.cpu_usage_pct == 0.0
