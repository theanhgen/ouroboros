import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


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

