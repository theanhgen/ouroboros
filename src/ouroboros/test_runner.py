"""Test execution -- runs pytest and returns structured results."""

import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .config import SafetyConfig

log = logging.getLogger(__name__)

@dataclass
class FailureDetail:
    test_name: str
    file: str
    line: Optional[int]
    message: str
    traceback: str

@dataclass
class RunnerOutcome:
    passed: int = 0
    failed: int = 0
    errors: int = 0
    failure_details: List[FailureDetail] = field(default_factory=list)
    stdout: str = ""
    returncode: int = 0
    coverage_percent: Optional[float] = None

    @property
    def success(self) -> bool:
        return self.returncode == 0 and self.failed == 0 and self.errors == 0

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors

    def summary(self) -> str:
        cov = f", coverage={self.coverage_percent}%" if self.coverage_percent is not None else ""
        return (
            f"{self.passed} passed, {self.failed} failed, "
            f"{self.errors} errors (returncode={self.returncode}){cov}"
        )


def _parse_pytest_output(output: str) -> dict:
    """Parse pytest output into counts and failure details."""
    result = {"passed": 0, "failed": 0, "errors": 0, "failures": [], "coverage": None}

    # Handle case for 'no tests ran'
    if 'no tests ran' in output:
        return result

    # Match summary line like "3 passed, 1 failed, 1 error in 0.52s"
    summary_match = re.search(r"(\d+) passed", output)
    if summary_match:
        result["passed"] = int(summary_match.group(1))

    failed_match = re.search(r"(\d+) failed", output)
    if failed_match:
        result["failed"] = int(failed_match.group(1))

    error_match = re.search(r"(\d+) error", output)
    if error_match:
        result["errors"] = int(error_match.group(1))

    # Match coverage line like "TOTAL                                          1272    169    87%"
    cov_match = re.search(r"TOTAL\s+\d+\s+\d+\s+(\d+)%", output)
    if cov_match:
        result["coverage"] = float(cov_match.group(1))

    # Parse FAILED lines like "FAILED tests/test_foo.py::test_bar - AssertionError: ..."
    for match in re.finditer(r"FAILED\s+([\w/._-]+)::(\S+)\s*(?:-\s*(.*))?", output):
        file_path = match.group(1)
        test_name = match.group(2)
        message = match.group(3) or ""

        # Try to extract line number from traceback sections
        line_num = None
        line_match = re.search(rf"{re.escape(file_path)}:(\d+)", output)
        if line_match:
            line_num = int(line_match.group(1))

        # Extract traceback for this test
        tb = ""
        tb_pattern = (
            r"_" + "{2,}" + r"\s+" + re.escape(test_name) + r"\s+_" + "{2,}"
            + r"(.*?)(?=_{2,}\s+\w|={2,}|$)"
        )
        tb_match = re.search(tb_pattern, output, re.DOTALL)
        if tb_match:
            tb = tb_match.group(1).strip()

        result["failures"].append(
            FailureDetail(
                test_name=test_name,
                file=file_path,
                line=line_num,
                message=message,
                traceback=tb,
            )
        )

    return result


def _run_tests_sandboxed(repo_root: Path, config: SafetyConfig, timeout: int) -> RunnerOutcome:
    """Run tests inside a Docker container."""
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{repo_root}:/app",
        "-w", "/app",
        config.sandbox_image,
        "bash", "-c", "pip install -e .[test] > /dev/null && pytest --tb=short -q --cov=ouroboros"
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        combined_output = proc.stdout + "\n" + proc.stderr
        parsed = _parse_pytest_output(combined_output)
        return RunnerOutcome(
            passed=parsed["passed"],
            failed=parsed["failed"],
            errors=parsed["errors"],
            failure_details=parsed["failures"],
            stdout=combined_output,
            returncode=proc.returncode,
            coverage_percent=parsed.get("coverage"),
        )
    except Exception as e:
        log.error("Sandbox execution failed: %s", e)
        return RunnerOutcome(stdout=f"Sandbox error: {e}", returncode=-1)


def _run_pytest(repo_root: Path, timeout: int, with_cov: bool) -> RunnerOutcome:
    cmd = [sys.executable, "-m", "pytest", "--tb=short", "-q"]
    if with_cov:
        cmd.append("--cov=ouroboros")
    try:
        proc = subprocess.run(
            cmd, cwd=repo_root, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return RunnerOutcome(stdout="Tests timed out", returncode=-1)
    except FileNotFoundError:
        return RunnerOutcome(stdout="pytest not found", returncode=-1)

    combined_output = proc.stdout + "\n" + proc.stderr
    parsed = _parse_pytest_output(combined_output)
    return RunnerOutcome(
        passed=parsed["passed"],
        failed=parsed["failed"],
        errors=parsed["errors"],
        failure_details=parsed["failures"],
        stdout=combined_output,
        returncode=proc.returncode,
        coverage_percent=parsed.get("coverage"),
    )


def run_tests(repo_root: Path, timeout: int = 120) -> RunnerOutcome:
    """Run pytest with coverage in the repo and return structured results."""
    config = SafetyConfig()
    if config.sandbox_enabled:
        return _run_tests_sandboxed(repo_root, config, timeout)

    outcome = _run_pytest(repo_root, timeout, with_cov=True)

    # If the coverage plugin is missing, pytest aborts with a usage error
    # (returncode 4, zero tests collected). That previously made the validation
    # gate silently hollow -- it saw "0 passed" before and after a change and
    # declared "no regression". Retry without --cov so a missing plugin can
    # never disable the safety net again.
    if outcome.returncode == 4 or (
        outcome.total == 0 and "unrecognized arguments" in outcome.stdout
    ):
        log.warning("pytest --cov failed (pytest-cov missing?); retrying without coverage")
        outcome = _run_pytest(repo_root, timeout, with_cov=False)

    return outcome
