"""Tests for test_runner module."""

from ouroboros.test_runner import TestResult, _parse_pytest_output

def test_test_result_success():
    r = TestResult(passed=5, failed=0, errors=0, returncode=0)
    assert r.success
    assert r.total == 5
    assert "5 passed" in r.summary()

def test_test_result_failure():
    r = TestResult(passed=3, failed=2, errors=0, returncode=1)
    assert not r.success
    assert r.total == 5
    assert "2 failed" in r.summary()

def test_test_result_errors():
    r = TestResult(passed=0, failed=0, errors=1, returncode=2)
    assert not r.success

def test_parse_pytest_output_all_pass():
    output = "5 passed in 0.52s"
    result = _parse_pytest_output(output)
    assert result["passed"] == 5
    assert result["failed"] == 0
    assert result["errors"] == 0

def test_parse_pytest_output_mixed():
    output = "3 passed, 2 failed, 1 error in 1.23s"
    result = _parse_pytest_output(output)
    assert result["passed"] == 3
    assert result["failed"] == 2
    assert result["errors"] == 1

def test_parse_pytest_output_failed_details():
    output = """
FAILED tests/test_foo.py::test_bar - AssertionError: expected 1 got 2
3 passed, 1 failed in 0.52s
"""
    result = _parse_pytest_output(output)
    assert result["failed"] == 1
    assert len(result["failures"]) == 1
    fail = result["failures"][0]
    assert fail.test_name == "test_bar"
    assert fail.file == "tests/test_foo.py"
    assert "AssertionError" in fail.message

def test_parse_pytest_output_no_tests():
    output = "no tests ran in 0.01s"
    result = _parse_pytest_output(output)
    assert result["passed"] == 0
    assert result["failed"] == 0
    assert result["errors"] == 0
    assert result["failures"] == []
