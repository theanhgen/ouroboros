"""Tests for test_runner module."""

from ouroboros.test_runner import RunnerOutcome, _parse_pytest_output

def test_test_result_success():
    r = RunnerOutcome(passed=5, failed=0, errors=0, returncode=0)
    assert r.success
    assert r.total == 5
    assert "5 passed" in r.summary()

def test_test_result_failure():
    r = RunnerOutcome(passed=3, failed=2, errors=0, returncode=1)
    assert not r.success
    assert r.total == 5
    assert "2 failed" in r.summary()

def test_test_result_errors():
    r = RunnerOutcome(passed=0, failed=0, errors=1, returncode=2)
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

def test_parse_pytest_output_error_details():
    output = """
ERROR tests/test_foo.py::test_setup - RuntimeError: fixture failed
3 passed, 1 error in 0.52s
"""
    result = _parse_pytest_output(output)
    assert result["errors"] == 1
    assert len(result["failures"]) == 1
    fail = result["failures"][0]
    assert fail.test_name == "test_setup"
    assert fail.file == "tests/test_foo.py"
    assert fail.message == "RuntimeError: fixture failed"

def test_parse_pytest_output_class_method_traceback():
    output = """
__________________________ TestClass.test_method ___________________________
tests/test_foo.py:12: in test_method
    assert actual
E   AssertionError: class method failed
=========================== short test summary info ============================
FAILED tests/test_foo.py::TestClass::test_method - AssertionError: class method failed
3 passed, 1 failed in 0.52s
"""
    result = _parse_pytest_output(output)
    assert result["failed"] == 1
    assert len(result["failures"]) == 1
    fail = result["failures"][0]
    assert fail.test_name == "TestClass::test_method"
    assert fail.file == "tests/test_foo.py"
    assert fail.line == 12
    assert "class method failed" in fail.traceback

def test_parse_pytest_output_fixture_setup_error_traceback():
    output = """
_____________________ ERROR at setup of test_setup _____________________
tests/test_foo.py:8: in bad_fixture
    raise RuntimeError("setup failed")
E   RuntimeError: setup failed
=========================== short test summary info ============================
ERROR tests/test_foo.py::test_setup - RuntimeError: setup failed
3 passed, 1 error in 0.52s
"""
    result = _parse_pytest_output(output)
    assert result["errors"] == 1
    assert len(result["failures"]) == 1
    fail = result["failures"][0]
    assert fail.test_name == "test_setup"
    assert fail.file == "tests/test_foo.py"
    assert fail.line == 8
    assert "setup failed" in fail.traceback

def test_parse_pytest_output_fixture_teardown_error_traceback():
    output = """
__________________ ERROR at teardown of test_teardown __________________
tests/test_foo.py:15: in bad_fixture
    raise RuntimeError("teardown failed")
E   RuntimeError: teardown failed
=========================== short test summary info ============================
ERROR tests/test_foo.py::test_teardown - RuntimeError: teardown failed
3 passed, 1 error in 0.52s
"""
    result = _parse_pytest_output(output)
    assert result["errors"] == 1
    assert len(result["failures"]) == 1
    fail = result["failures"][0]
    assert fail.test_name == "test_teardown"
    assert fail.file == "tests/test_foo.py"
    assert fail.line == 15
    assert "teardown failed" in fail.traceback

def test_parse_pytest_output_class_fixture_error_traceback():
    output = """
_________________ ERROR at setup of TestClass.test_method _________________
tests/test_foo.py:10: in bad_fixture
    raise RuntimeError("fixture failed")
E   RuntimeError: fixture failed
=========================== short test summary info ============================
ERROR tests/test_foo.py::TestClass::test_method - RuntimeError: fixture failed
3 passed, 1 error in 0.52s
"""
    result = _parse_pytest_output(output)
    assert result["errors"] == 1
    assert len(result["failures"]) == 1
    fail = result["failures"][0]
    assert fail.test_name == "TestClass::test_method"
    assert fail.file == "tests/test_foo.py"
    assert fail.line == 10
    assert "fixture failed" in fail.traceback

def test_parse_pytest_output_no_tests():
    output = "no tests ran in 0.01s"
    result = _parse_pytest_output(output)
    assert result["passed"] == 0
    assert result["failed"] == 0
    assert result["errors"] == 0
    assert result["failures"] == []
