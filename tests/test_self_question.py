import time

from ouroboros import test_runner
from ouroboros.self_question import (
    DEFAULT_QUESTIONS,
    SelfQuestion,
    choose_question,
    generate_codebase_questions,
    record_question,
)


def test_choose_question_returns_first_by_default():
    state = {"self_question_index": 0}
    q, idx = choose_question(state, DEFAULT_QUESTIONS)
    assert idx == 0
    assert q is DEFAULT_QUESTIONS[0]


def test_choose_question_wraps_around():
    state = {"self_question_index": len(DEFAULT_QUESTIONS)}
    q, idx = choose_question(state, DEFAULT_QUESTIONS)
    assert idx == 0
    assert q is DEFAULT_QUESTIONS[0]


def test_choose_question_picks_by_index():
    state = {"self_question_index": 2}
    q, idx = choose_question(state, DEFAULT_QUESTIONS)
    assert idx == 2
    assert q is DEFAULT_QUESTIONS[2]


def test_record_question_without_answer():
    state = {}
    q = SelfQuestion(question="Why?", area="test")
    record_question(state, q)

    log = state["self_question_log"]
    assert len(log) == 1
    assert log[0]["question"] == "Why?"
    assert log[0]["area"] == "test"
    assert "answer" not in log[0]
    assert isinstance(log[0]["ts"], int)


def test_record_question_with_answer():
    state = {}
    q = SelfQuestion(question="Why?", area="test")
    record_question(state, q, answer="Because.")

    log = state["self_question_log"]
    assert len(log) == 1
    assert log[0]["answer"] == "Because."


def test_record_question_appends():
    state = {"self_question_log": [{"ts": 0, "question": "old", "area": "old"}]}
    q = SelfQuestion(question="new?", area="new")
    record_question(state, q)

    assert len(state["self_question_log"]) == 2


def test_default_questions_not_empty():
    assert len(DEFAULT_QUESTIONS) > 0
    for q in DEFAULT_QUESTIONS:
        assert q.question
        assert q.area


def test_generate_codebase_questions_for_repo_with_source_files(tmp_path, monkeypatch):
    src = tmp_path / "src" / "ouroboros"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    (src / "alpha.py").write_text("def a():\n    return 1\n")
    (src / "beta.py").write_text("def b():\n    return 2\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_alpha.py").write_text("def test_a():\n    pass\n")

    # Don't spawn a real pytest run inside the temp repo.
    calls = []

    def fake_run_tests(repo_root, timeout=120):
        calls.append(repo_root)
        return test_runner.RunnerOutcome(passed=1, returncode=0)

    monkeypatch.setattr(test_runner, "run_tests", fake_run_tests)

    questions = generate_codebase_questions(tmp_path)

    assert calls == [tmp_path]
    assert len(questions) > 0
    for q in questions:
        assert isinstance(q, SelfQuestion)
        assert isinstance(q.question, str) and q.question
    # beta.py has no test_beta.py; alpha.py is covered and __init__.py is skipped.
    assert [q.area for q in questions] == ["missing_tests"]
    assert "beta.py" in questions[0].question
