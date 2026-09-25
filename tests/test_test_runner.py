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

def test_parse_pytest_output_ignores_log_messages():
    output = """
tests/test_service.py::test_pipeline
[INFO] Processed 100 passed records and 20 failed records, 5 errors encountered
PASSED
1 passed in 0.45s
"""
    result = _parse_pytest_output(output)
    assert result["passed"] == 1
    assert result["failed"] == 0
    assert result["errors"] == 0

def test_parse_pytest_output_terminal_summary_banner():
    output = """
[DEBUG] 50 passed items and 12 failed attempts
=========================== short test summary info ============================
FAILED tests/test_foo.py::test_bar - AssertionError
=================== 2 passed, 1 failed, 1 error in 1.50s ===================
"""
    result = _parse_pytest_output(output)
    assert result["passed"] == 2
    assert result["failed"] == 1
    assert result["errors"] == 1
    assert len(result["failures"]) == 1

def test_parse_pytest_output_with_warnings():
    output = """
[DEBUG] 10 passed setup checks, 3 failed retries, 2 errors ignored
=================== 4 passed, 2 warnings in 0.30s ===================
"""
    result = _parse_pytest_output(output)
    assert result["passed"] == 4
    assert result["failed"] == 0
    assert result["errors"] == 0

def test_parse_pytest_output_missing_summary_line():
    output = """
INTERNALERROR> RuntimeError: pytest crashed before writing its summary
[DEBUG] 10 passed setup checks, 3 failed retries, 2 errors ignored
"""
    result = _parse_pytest_output(output)
    assert result["passed"] == 0
    assert result["failed"] == 0
    assert result["errors"] == 0

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

def test_parse_pytest_output_collection_error_with_message():
    output = """
____________________ ERROR collecting tests/test_mod.py ____________________
ImportError while importing test module '/repo/tests/test_mod.py'.
Traceback:
tests/test_mod.py:3: in <module>
    from app import missing
E   ImportError: cannot import name 'missing' from 'app'
=========================== short test summary info ============================
ERROR tests/test_mod.py - ImportError: cannot import name 'missing' from 'app'
1 error in 0.52s
"""
    result = _parse_pytest_output(output)
    assert result["errors"] == 1
    assert len(result["failures"]) == 1
    fail = result["failures"][0]
    assert fail.test_name == ""
    assert fail.file == "tests/test_mod.py"
    assert fail.line == 3
    assert fail.message == "ImportError: cannot import name 'missing' from 'app'"
    assert "ImportError while importing test module" in fail.traceback

def test_parse_pytest_output_collection_error_without_message():
    output = """
____________________ ERROR collecting tests/test_mod.py ____________________
ImportError while importing test module '/repo/tests/test_mod.py'.
Traceback:
tests/test_mod.py:1: in <module>
    import missing_dep
E   ModuleNotFoundError: No module named 'missing_dep'
=========================== short test summary info ============================
ERROR tests/test_mod.py
1 error in 0.52s
"""
    result = _parse_pytest_output(output)
    assert result["errors"] == 1
    assert len(result["failures"]) == 1
    fail = result["failures"][0]
    assert fail.test_name == ""
    assert fail.file == "tests/test_mod.py"
    assert fail.line == 1
    assert fail.message == "ModuleNotFoundError: No module named 'missing_dep'"
    assert "missing_dep" in fail.traceback

def test_parse_pytest_output_collection_error_mixed_with_failure():
    output = """
_____________________________ test_bar ______________________________
tests/test_foo.py:12: in test_bar
    assert False
E   AssertionError: boom
____________________ ERROR collecting tests/test_mod.py ____________________
tests/test_mod.py:2: in <module>
    raise RuntimeError("import boom")
E   RuntimeError: import boom
=========================== short test summary info ============================
FAILED tests/test_foo.py::test_bar - AssertionError: boom
ERROR tests/test_mod.py - RuntimeError: import boom
1 failed, 1 error in 0.52s
"""
    result = _parse_pytest_output(output)
    assert result["failed"] == 1
    assert result["errors"] == 1
    assert len(result["failures"]) == 2
    failures = {fail.file: fail for fail in result["failures"]}
    assert failures["tests/test_foo.py"].test_name == "test_bar"
    assert failures["tests/test_foo.py"].line == 12
    assert "AssertionError: boom" in failures["tests/test_foo.py"].traceback
    assert failures["tests/test_mod.py"].test_name == ""
    assert failures["tests/test_mod.py"].line == 2
    assert failures["tests/test_mod.py"].message == "RuntimeError: import boom"

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

def test_parse_pytest_output_parameterized_test_with_spaces():
    output = """
_____________________ test_format[param with spaces] _____________________
tests/test_foo.py:21: in test_format
    assert actual
E   AssertionError: expected value
=========================== short test summary info ============================
FAILED tests/test_foo.py::test_format[param with spaces] - AssertionError: expected value
3 passed, 1 failed in 0.52s
"""
    result = _parse_pytest_output(output)
    assert result["failed"] == 1
    assert len(result["failures"]) == 1
    fail = result["failures"][0]
    assert fail.test_name == "test_format[param with spaces]"
    assert fail.file == "tests/test_foo.py"
    assert fail.line == 21
    assert fail.message == "AssertionError: expected value"
    assert "expected value" in fail.traceback

def test_parse_pytest_output_parameterized_test_without_message():
    output = """
=========================== short test summary info ============================
FAILED tests/test_foo.py::test_format[param with spaces]
3 passed, 1 failed in 0.52s
"""
    result = _parse_pytest_output(output)
    assert result["failed"] == 1
    assert len(result["failures"]) == 1
    fail = result["failures"][0]
    assert fail.test_name == "test_format[param with spaces]"
    assert fail.file == "tests/test_foo.py"
    assert fail.message == ""

def test_parse_pytest_output_parameterized_class_fixture_error():
    output = """
____________ ERROR at setup of TestClass.test_method[param with spaces] ____________
tests/test_foo.py:33: in bad_fixture
    raise RuntimeError("fixture failed")
E   RuntimeError: fixture failed
=========================== short test summary info ============================
ERROR tests/test_foo.py::TestClass::test_method[param with spaces] - RuntimeError: fixture failed
3 passed, 1 error in 0.52s
"""
    result = _parse_pytest_output(output)
    assert result["errors"] == 1
    assert len(result["failures"]) == 1
    fail = result["failures"][0]
    assert fail.test_name == "TestClass::test_method[param with spaces]"
    assert fail.file == "tests/test_foo.py"
    assert fail.line == 33
    assert fail.message == "RuntimeError: fixture failed"
    assert "fixture failed" in fail.traceback

def test_parse_pytest_output_skipped():
    output = "5 skipped in 0.52s"
    result = _parse_pytest_output(output)
    assert result["passed"] == 0
    assert result["failed"] == 0
    assert result["errors"] == 0
    assert result["skipped"] == 5

def test_parse_pytest_output_mixed_with_skipped():
    output = "3 passed, 1 failed, 2 skipped, 1 error in 1.23s"
    result = _parse_pytest_output(output)
    assert result["passed"] == 3
    assert result["failed"] == 1
    assert result["errors"] == 1
    assert result["skipped"] == 2

def test_runner_hollow_success():
    # Scenario: Tests were collected, but all were skipped, none passed.
    r = RunnerOutcome(passed=0, failed=0, errors=0, skipped=5, returncode=0)
    assert not r.success # Should be False because 0 passed out of 5 total (skipped)
    assert r.total == 5
    assert "5 skipped" in r.summary()

    # Scenario: All collected tests passed, should be success
    r_pass = RunnerOutcome(passed=5, failed=0, errors=0, skipped=0, returncode=0)
    assert r_pass.success
    assert r_pass.total == 5

    # Scenario: Some passed, some skipped, still success because some passed
    r_mixed_pass_skip = RunnerOutcome(passed=2, failed=0, errors=0, skipped=3, returncode=0)
    assert r_mixed_pass_skip.success
    assert r_mixed_pass_skip.total == 5

    # Scenario: No tests collected at all (total=0), should still be success (or rather, not failure)
    r_no_tests = RunnerOutcome(passed=0, failed=0, errors=0, skipped=0, returncode=0)
    assert r_no_tests.success
    assert r_no_tests.total == 0

def test_parse_pytest_output_no_tests():
    output = "no tests ran in 0.01s"
    result = _parse_pytest_output(output)
    assert result["passed"] == 0
    assert result["failed"] == 0
    assert result["errors"] == 0
    assert result["skipped"] == 0
    assert result["failures"] == []
