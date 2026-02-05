import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import prompts

log = logging.getLogger(__name__)

MAX_PROMPT_CHARS = 900


def _repo_root() -> Path:
    # Try git first
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if out:
            return Path(out)
    except Exception:
        pass

    # Fallback: walk up from this file
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists():
            return parent
    return here.parents[2]


def _safe_git_env() -> Dict[str, str]:
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "ouroboros-bot")
    env.setdefault("GIT_AUTHOR_EMAIL", "ouroboros-bot@localhost")
    env.setdefault("GIT_COMMITTER_NAME", "ouroboros-bot")
    env.setdefault("GIT_COMMITTER_EMAIL", "ouroboros-bot@localhost")
    return env


def _git_clean(repo: Path) -> bool:
    out = subprocess.check_output(
        ["git", "status", "--porcelain"],
        cwd=repo,
        text=True,
    )
    return out.strip() == ""


def _load_prompt_context(state: Dict[str, Any]) -> Dict[str, Any]:
    comment_hist = state.get("comment_history") or []
    recent_comments = comment_hist[-8:]
    self_log = state.get("self_question_log") or []
    recent_questions = self_log[-6:]
    return {
        "recent_comments": recent_comments,
        "recent_questions": recent_questions,
    }


def _build_prompt_update_request(
    current_prompt: str,
    context: Dict[str, Any],
) -> str:
    return (
        "You are improving a system prompt that generates short, helpful comments "
        "for a community Q&A feed. Propose a better system prompt based on the "
        "examples and reflections below.\n\n"
        "Constraints:\n"
        "- Keep it under 900 characters.\n"
        "- Avoid salesy or overly generic language.\n"
        "- Encourage concrete, actionable suggestions.\n"
        "- Do not mention policies or safety boilerplate.\n"
        "- Output valid JSON with keys: new_prompt, rationale.\n\n"
        f"Current prompt:\n{current_prompt}\n\n"
        f"Recent comments (title, content, comment):\n{json.dumps(context['recent_comments'], indent=2)}\n\n"
        f"Recent self-questions and answers:\n{json.dumps(context['recent_questions'], indent=2)}\n"
    )


def _parse_prompt_update(payload: str) -> Optional[Tuple[str, str]]:
    try:
        data = json.loads(payload)
    except Exception:
        log.warning("Self-improve response was not valid JSON")
        return None

    new_prompt = data.get("new_prompt")
    rationale = data.get("rationale", "")
    if not isinstance(new_prompt, str) or not new_prompt.strip():
        return None
    new_prompt = new_prompt.strip()
    if len(new_prompt) > MAX_PROMPT_CHARS:
        new_prompt = new_prompt[:MAX_PROMPT_CHARS].rstrip()
    if len(new_prompt) < 60:
        return None
    return new_prompt, str(rationale).strip()


def _write_prompt(new_prompt: str) -> None:
    path = Path(prompts._prompts_path())
    data: Dict[str, Any] = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    data["comment_system_prompt"] = new_prompt
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    data["updated_by"] = "self-improve"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def _append_log(repo: Path, rationale: str, new_prompt: str) -> None:
    log_path = repo / "docs" / "self_improve_log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"- {ts}\n")
        f.write("  - change: updated comment system prompt\n")
        if rationale:
            f.write(f"  - rationale: {rationale}\n")
        f.write(f"  - new_prompt: {new_prompt}\n")


def _git_commit_push(repo: Path, message: str) -> None:
    env = _safe_git_env()
    subprocess.check_call(["git", "add", "src/ouroboros/prompts.json", "docs/self_improve_log.md"], cwd=repo, env=env)
    subprocess.check_call(["git", "commit", "-m", message], cwd=repo, env=env)
    subprocess.check_call(["git", "push", "origin", "main"], cwd=repo, env=env)


def run_self_improve(client: Any, state: Dict[str, Any], model: str = "gpt-4o-mini") -> Optional[str]:
    repo = _repo_root()
    if not _git_clean(repo):
        log.warning("Repo has uncommitted changes; skipping self-improve.")
        return None

    current_prompt = prompts.load_comment_system_prompt()
    context = _load_prompt_context(state)
    req = _build_prompt_update_request(current_prompt, context)

    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=400,
            messages=[
                {"role": "system", "content": "You revise prompts carefully and output JSON only."},
                {"role": "user", "content": req},
            ],
        )
    except Exception:
        log.exception("Self-improve LLM call failed")
        return None

    parsed = _parse_prompt_update(resp.choices[0].message.content)
    if not parsed:
        log.warning("Self-improve response invalid; skipping.")
        return None

    new_prompt, rationale = parsed
    if new_prompt.strip() == current_prompt.strip():
        log.info("Self-improve produced no prompt changes.")
        return None

    _write_prompt(new_prompt)
    _append_log(repo, rationale, new_prompt)

    msg = f"self-improve: update comment prompt ({time.strftime('%Y-%m-%d')})"
    _git_commit_push(repo, msg)
    return "updated comment prompt and pushed to main"
