import pytest
from ouroboros.test_runner import FailureDetail, extract_failure_location

def test_extract_failure_location() -> None:
    """Test extraction of failure location details from a FailureDetail object."""
    failure = FailureDetail(
        test_name="test_example",
        file="src/ouroboros/test_runner.py",
        line=42,
        message="AssertionError: expected 5 got 3",
        traceback="Traceback (most recent call last):\\n  File ...\\nAssertionError: expected 5 got 3"
    )
    file_path, line_number, traceback = extract_failure_location(failure)
    assert file_path == "src/ouroboros/test_runner.py"
    assert line_number == 42
    assert traceback.startswith("Traceback (most recent call last):")

def test_extract_failure_location_line_none() -> None:
    """Test extraction when line number is None."""
    failure = FailureDetail(
        test_name="test_no_line",
        file="src/ouroboros/test_runner.py",
        line=None,
        message="Some error",
        traceback="Traceback: something"
    )
    file_path, line_number, traceback = extract_failure_location(failure)
    assert file_path == "src/ouroboros/test_runner.py"
    assert line_number is None
    assert traceback == "Traceback: something"

def test_extract_failure_location_empty_traceback() -> None:
    """Test extraction when traceback is empty string."""
    failure = FailureDetail(
        test_name="test_empty",
        file="some/file.py",
        line=1,
        message="",
        traceback=""
    )
    file_path, line_number, traceback = extract_failure_location(failure)
    assert file_path == "some/file.py"
    assert line_number == 1
    assert traceback == ""
