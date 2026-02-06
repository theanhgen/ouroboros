"""Test execution -- runs pytest and returns structured results."""

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)


@dataclass
class TestFailure:
    test_name: str
    file: str
    line: Optional[int]
    message: str
    traceback: str


@dataclass
class TestResult:
    passed: int = 0
    failed: int = 0
    errors: int = 0
    failure_details: List[TestFailure] = field(default_factory=list)
    stdout: str = ""
    returncode: int = 0

    @property
    def success(self) -> bool:
        return self.returncode == 0 and self.failed == 0 and self.errors == 0

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors

    def summary(self) -> str:
        return (
            f"{self.passed} passed, {self.failed} failed, "
            f"{self.errors} errors (returncode={self.returncode})"
        )


def _parse_pytest_output(output: str) -> dict:
    """Parse pytest --tb=short -q output into counts and failure details."""
    result = {"passed": 0, "failed": 0, "errors": 0, "failures": []}

    # Match summary line like "3 passed, 1 failed, 1 error in 0.52s"
    summary_match = re.search(
        r"(\d+) passed", output
    )
    if summary_match:
        result["passed"] = int(summary_match.group(1))

    failed_match = re.search(r"(\d+) failed", output)
    if failed_match:
        result["failed"] = int(failed_match.group(1))

    error_match = re.search(r"(\d+) error", output)
    if error_match:
        result["errors"] = int(error_match.group(1))

    # Parse FAILED lines like "FAILED tests/test_foo.py::test_bar - AssertionError: ..."
    for match in re.finditer(
        r"FAILED\s+([\w/._-]+)::(\S+)\s*(?:-\s*(.*))?",
        output,
    ):
        file_path = match.group(1)
        test_name = match.group(2)
        message = match.group(3) or ""

        # Try to extract line number from traceback sections
        line_num = None
        line_match = re.search(
            rf"{re.escape(file_path)}:(\d+)", output
        )
        if line_match:
            line_num = int(line_match.group(1))

        # Extract traceback for this test
        tb = ""
        tb_pattern = (
            r"_" + "{2,}" + r"\s+" + re.escape(test_name) + r"\s+_" + "{2,}"
            + r"(.*?)(?=_" + "{2,}" + r"\s+\w|=" + "{2,}" + r"|$)"
        )
        tb_match = re.search(tb_pattern, output, re.DOTALL)
        if tb_match:
            tb = tb_match.group(1).strip()

        result["failures"].append(
            TestFailure(
                test_name=test_name,
                file=file_path,
                line=line_num,
                message=message,
                traceback=tb,
            )
        )

    return result


def run_tests(repo_root: Path, timeout: int = 120) -> TestResult:
    """Run pytest in the repo and return structured results.

    Uses pytest --tb=short -q for concise output that's easy to parse.
    """
    try:
        proc = subprocess.run(
            ["python", "-m", "pytest", "--tb=short", "-q"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return TestResult(
            stdout="Tests timed out",
            returncode=-1,
        )
    except FileNotFoundError:
        return TestResult(
            stdout="pytest not found",
            returncode=-1,
        )

    combined_output = proc.stdout + "\n" + proc.stderr
    parsed = _parse_pytest_output(combined_output)

    return TestResult(
        passed=parsed["passed"],
        failed=parsed["failed"],
        errors=parsed["errors"],
        failure_details=parsed["failures"],
        stdout=combined_output,
        returncode=proc.returncode,
    )
