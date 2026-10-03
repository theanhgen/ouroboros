"""Test execution -- runs pytest and returns structured results."""

import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .config import SafetyConfig

_COVERAGE_PATTERN = re.compile(r"\s*TOTAL\s+\d+\s+\d+\s+(\d+(?:\.\d+)?)%")
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
    skipped: int = 0
    failure_details: List[FailureDetail] = field(default_factory=list)
    stdout: str = ""
    returncode: int = 0
    coverage_percent: Optional[float] = None

    @property
    def success(self) -> bool:
        # Fail closed when nothing executed: pytest exits 0 when every
        # collected test is skipped, and a run with zero passed tests cannot
        # validate anything (#147).
        return (
            self.returncode == 0 and self.failed == 0 and self.errors == 0
            and self.passed > 0
        )

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors + self.skipped

    def summary(self) -> str:
        cov = f", coverage={self.coverage_percent}%" if self.coverage_percent is not None else ""
        return (
            f"{self.passed} passed, {self.failed} failed, "
            f"{self.skipped} skipped, "
            f"{self.errors} errors (returncode={self.returncode}){cov}"
        )

    def cluster_failures_by_root_cause(self) -> dict:
        """
        Group failures by fault category (file, error type, line range) with associated tracebacks.
        
        Returns:
            A dictionary mapping categories to their failure information:
            {
                "file:error_type:line_range": {
                    "file": "normalized_file_path",
                    "error_type": "normalized_error_type",
                    "line_range": "start-end" or "single_line",
                    "failures": [
                        {
                            "test_name": str,
                            "file": str,
                            "line": int or None,
                            "message": str,
                            "traceback": str
                        },
                        ...
                    ],
                    "count": int
                },
                ...
            }
        """
        # Step 1: Extract failures from input
        failures = self.failure_details
        
        # Step 2: Initialize result structure
        clustered = {}
        
        # Step 3: Process each failure
        for failure in failures:
            # Step 3a: Extract and normalize error type from message
            error_type = _extract_error_type(failure.message)
            
            # Step 3b: Normalize file path
            normalized_file = _normalize_file_path(failure.file)
            
            # Step 3c: Create line range string
            line_range = _create_line_range(failure.line)
            
            # Step 3d: Create category key
            category_key = f"{normalized_file}:{error_type}:{line_range}"
            
            # Step 3e: Initialize category entry if not exists
            if category_key not in clustered:
                clustered[category_key] = {
                    "file": normalized_file,
                    "error_type": error_type,
                    "line_range": line_range,
                    "failures": [],
                    "count": 0
                }
            
            # Step 3f: Add failure to category
            clustered[category_key]["failures"].append({
                "test_name": failure.test_name,
                "file": failure.file,
                "line": failure.line,
                "message": failure.message,
                "traceback": failure.traceback
            })
            clustered[category_key]["count"] += 1
        
        # Step 4: Return clustered results
        return clustered


def extract_failure_location(failure_detail: FailureDetail) -> tuple[str, Optional[int], str]:
    """
    Extract structured failure location information from a FailureDetail object.
    
    This helper function parses failure detail strings and returns structured
    (file_path, line_number, traceback) tuples while preserving all existing
    traceback data in the RunnerOutcome.failure_details.
    
    Args:
        failure_detail: FailureDetail object containing failure information
        
    Returns:
        Tuple containing (file_path, line_number, traceback):
        - file_path: String representation of the file where failure occurred
        - line_number: Optional integer line number where failure occurred
        - traceback: Full traceback string from the failure
        
    Examples:
        >>> failure = FailureDetail(
        ...     test_name="test_example",
        ...     file="src/ouroboros/test_runner.py",
        ...     line=42,
        ...     message="AssertionError: expected 5 got 3",
        ...     traceback="Traceback (most recent call last):\\n  File ...\\nAssertionError: expected 5 got 3"
        ... )
        >>> file_path, line_number, traceback = extract_failure_location(failure)
        >>> file_path
        'src/ouroboros/test_runner.py'
        >>> line_number
        42
        >>> traceback.startswith('Traceback (most recent call last):')
        True
    """
    # Extract file path directly from failure_detail
    file_path = failure_detail.file
    
    # Extract line number directly from failure_detail
    line_number = failure_detail.line
    
    # Extract full traceback directly from failure_detail (preserving all data)
    traceback = failure_detail.traceback
    
    return (file_path, line_number, traceback)


def _extract_error_type(message: str) -> str:
    """
    Extract normalized error type from failure message.
    
    Args:
        message: The failure message
        
    Returns:
        Normalized error type string
    """
    if not message:
        return "Unknown"
    
    # Standard Python error types
    error_types = [
        "AssertionError", "ValueError", "TypeError", "ImportError", "KeyError",
        "AttributeError", "SyntaxError", "NameError", "IndexError", "TimeoutError",
        "ConnectionError", "OSError", "PermissionError", "FileNotFoundError",
        "NotImplementedError", "RuntimeError", "StopIteration", "StopAsyncIteration",
        "MemoryError", "OverflowError", "ZeroDivisionError", "RecursionError",
        "NotADirectoryError", "IsADirectoryError", "BlockingIOError", "ChildProcessError",
        "BrokenPipeError", "ConnectionAbortedError", "ConnectionRefusedError",
        "ConnectionResetError", "FileExistsError", "InterruptedError", "ProcessLookupError"
    ]
    
    # First check for standard error types
    for error_type in error_types:
        if error_type in message:
            return error_type
    
    # Handle warnings
    if "Warning" in message:
        # Extract specific warning type
        warning_match = re.search(r'\b(\w+Warning)\b', message)
        if warning_match:
            return warning_match.group(1)
        return "Warning"
    
    # Handle "error:" pattern
    if "error:" in message.lower():
        error_desc = re.search(r'error:\s*(.+)', message, re.IGNORECASE)
        if error_desc:
            words = error_desc.group(1).strip().split()
            if words:
                return words[0].title()
        return "Error"
    
    # Extract from "File "...", line N" pattern
    file_line_match = re.search(r'File\s+"[^"]+",\s*line\s+\d+', message)
    if file_line_match:
        # Try to get error message after file:line
        after_file_line = re.search(r'File\s+"[^"]+",\s*line\s+\d+[:\s]+(.+)', message)
        if after_file_line:
            error_desc = after_file_line.group(1).strip()
            if error_desc:
                # Get first word of error description
                first_word = error_desc.split()[0]
                return first_word.title() if first_word else "Error"
        return "Error"
    
    # Extract first meaningful word from message as error type
    skip_words = {"Error", "Exception", "Warning", "Traceback", "File", "test", "Test"}
    words = message.split()
    for word in words:
        word = re.sub(r'[^\w]', '', word)
        if word and word not in skip_words and len(word) > 2:
            return word.title()
    
    return "Unknown"


def _normalize_file_path(file_path: str) -> str:
    """
    Normalize file path for consistent categorization.
    
    Args:
        file_path: Original file path
        
    Returns:
        Normalized file path
    """
    if not file_path:
        return "unknown"
    
    # Convert to Path object
    path = Path(file_path)
    
    # Try to get absolute path
    try:
        normalized = str(path.resolve())
    except:
        normalized = file_path
    
    # Normalize path separators
    normalized = normalized.replace('/', '.').replace('\\', '.')
    
    # Remove leading ./ or .\\
    if normalized.startswith('./'):
        normalized = normalized[2:]
    elif normalized.startswith('.\\\\'):
        normalized = normalized[3:]
    
    # Remove .py extension
    if normalized.endswith('.py'):
        normalized = normalized[:-3]
    
    return normalized


def _create_line_range(line: Optional[int]) -> str:
    """
    Create a line range string from a single line number.
    
    Args:
        line: Line number or None
        
    Returns:
        Line range string (e.g., "10-15" or "42")
    """
    if line is None:
        return "unknown"
    
    return str(line)


def _parse_pytest_output(output: str) -> dict:
    """Parse pytest output into counts and failure details."""
    result = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "failures": [], "coverage": None}

    summary_line = ""
    for line in reversed(output.splitlines()):
        if re.search(r"\bin\s+\d+(?:\.\d+)?s\b", line):
            summary_line = line
            break

    # Handle case for 'no tests ran'
    if "no tests ran" in summary_line or "no tests ran" in output:
        return result

    if summary_line:
        # Match counts from the terminal summary line only, e.g.
        # "3 passed, 1 failed, 1 error in 0.52s".
        summary_match = re.search(r"(\d+)\s+passed", summary_line)
        if summary_match:
            result["passed"] = int(summary_match.group(1))

        failed_match = re.search(r"(\d+)\s+failed", summary_line)
        if failed_match:
            result["failed"] = int(failed_match.group(1))

        error_match = re.search(r"(\d+)\s+error", summary_line)
        if error_match:
            result["errors"] = int(error_match.group(1))

        skipped_match = re.search(r"(\d+)\s+skipped", summary_line)
        if skipped_match:
            result["skipped"] = int(skipped_match.group(1))

    # Match coverage line like "TOTAL                                          1272    169    87%"
    result["coverage"] = _extract_coverage_from_output(output)

    # Parse FAILED and ERROR summary lines. Collection errors omit the
    # "::test_name" delimiter, e.g. "ERROR tests/test_foo.py - ImportError".
    for match in re.finditer(
        r"^(?:FAILED|ERROR)\s+([\w/._-]+)(?:::([^\n]+?))?(?:\s+-\s+(.*))?$",
        output,
        re.MULTILINE,
    ):
        file_path = match.group(1)
        test_name = match.group(2) or ""
        message = match.group(3) or ""

        # Extract traceback for this test
        tb = ""
        if test_name:
            dot_name = test_name.replace("::", ".")
            header_pattern = rf"(?:ERROR at (?:setup|teardown)\s+of\s+)?(?:{re.escape(test_name)}|{re.escape(dot_name)})"
        else:
            header_pattern = rf"ERROR collecting (?:[^\n]*[/\\])?{re.escape(file_path)}"
        tb_pattern = (
            r"_" + "{2,}" + r"\s+" + header_pattern + r"\s+_" + "{2,}"
            + r"(.*?)(?=_{2,}\s+\w|={2,}|$)"
        )
        tb_match = re.search(tb_pattern, output, re.DOTALL)
        if tb_match:
            tb = tb_match.group(1).strip()

        if not message and not test_name and tb:
            error_lines = [
                line.strip()
                for line in re.findall(r"^E\s+(.+)$", tb, re.MULTILINE)
                if line.strip()
            ]
            if error_lines:
                message = error_lines[-1]

        # Try to extract line number from traceback sections
        line_num = None
        search_target = tb if tb else output
        line_match = re.search(rf"{re.escape(file_path)}:(\d+)", search_target)
        if line_match:
            line_num = int(line_match.group(1))

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

def _extract_coverage_from_output(output: str) -> Optional[float]:
    """Extract coverage percent from pytest output using the TOTAL line.
    Supports both integer and decimal percentages (e.g., 80.5%)."""
    # Allow optional leading whitespace for robustness.
    match = _COVERAGE_PATTERN.search(output)
    return float(match.group(1)) if match else None

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
            skipped=parsed["skipped"],
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
        skipped=parsed["skipped"],
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
