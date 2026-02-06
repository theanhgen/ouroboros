import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SelfQuestion:
    question: str
    area: str


DEFAULT_QUESTIONS: List[SelfQuestion] = [
    SelfQuestion("Which parts of the Moltbook runner are untested?", "tests"),
    SelfQuestion("What safety policy is missing for autonomous comments?", "safety"),
    SelfQuestion("What data do we store that could be sensitive?", "privacy"),
    SelfQuestion("Which errors are not handled in Moltbook API requests?", "reliability"),
    SelfQuestion("What config defaults could cause unintended posting?", "safety"),
]


def generate_codebase_questions(repo_root: Path) -> List[SelfQuestion]:
    """Generate questions based on actual codebase state.

    Checks for test failures and untested modules, producing concrete
    questions that are prepended to DEFAULT_QUESTIONS.
    """
    questions: List[SelfQuestion] = []

    try:
        from .test_runner import run_tests
        result = run_tests(repo_root, timeout=60)

        # Questions from test failures
        for fail in result.failure_details:
            questions.append(SelfQuestion(
                question=(
                    f"Why does {fail.file}::{fail.test_name} fail with "
                    f"'{fail.message[:100]}'? What is the root cause and fix?"
                ),
                area="test_failure",
            ))

        # Questions about untested modules
        from .codebase import list_source_files, get_test_files
        source_files = list_source_files(repo_root)
        test_files = get_test_files(repo_root)

        test_names = {f.name for f in test_files}
        for src in source_files:
            if src.name.startswith("_"):
                continue
            expected_test = f"test_{src.name}"
            if expected_test not in test_names:
                questions.append(SelfQuestion(
                    question=f"What tests should exist for module {src.name}? What are the key functions to test?",
                    area="missing_tests",
                ))

    except Exception:
        _log.debug("generate_codebase_questions failed, returning empty", exc_info=True)

    return questions


def get_questions_with_codebase(repo_root: Optional[Path] = None) -> List[SelfQuestion]:
    """Return codebase-aware questions prepended to DEFAULT_QUESTIONS.

    Falls back to DEFAULT_QUESTIONS only on error.
    """
    if repo_root is None:
        try:
            from .codebase import get_repo_root
            repo_root = get_repo_root()
        except Exception:
            return DEFAULT_QUESTIONS

    try:
        codebase_qs = generate_codebase_questions(repo_root)
        return codebase_qs + DEFAULT_QUESTIONS
    except Exception:
        _log.debug("get_questions_with_codebase failed, using defaults", exc_info=True)
        return DEFAULT_QUESTIONS


def choose_question(state: Dict, questions: List[SelfQuestion]) -> Tuple[SelfQuestion, int]:
    index = int(state.get("self_question_index", 0))
    if index >= len(questions):
        index = 0
    return questions[index], index


def record_question(state: Dict, question: SelfQuestion, answer: Optional[str] = None) -> None:
    log = state.setdefault("self_question_log", [])
    entry = {
        "ts": int(time.time()),
        "question": question.question,
        "area": question.area,
    }
    if answer is not None:
        entry["answer"] = answer
    log.append(entry)

