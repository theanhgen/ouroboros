import time

from ouroboros.self_question import (
    DEFAULT_QUESTIONS,
    SelfQuestion,
    choose_question,
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
