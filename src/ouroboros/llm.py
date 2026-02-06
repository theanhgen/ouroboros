"""Thin OpenAI wrapper -- pure functions, no class abstractions."""

import json
import logging
import os
from typing import Optional

from openai import OpenAI

from . import prompts

log = logging.getLogger(__name__)


def load_openai_key() -> str:
    """Return OpenAI API key from env var or config file.

    Checks ``OPENAI_API_KEY`` first, then
    ``~/.config/moltbook/openai.json`` (key: ``api_key``).
    Raises ``RuntimeError`` if neither source provides a key.
    """
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key

    cfg_path = os.path.expanduser("~/.config/moltbook/openai.json")
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        key = data.get("api_key")
        if key:
            return key

    raise RuntimeError(
        "Missing OpenAI API key. Set OPENAI_API_KEY or create "
        "~/.config/moltbook/openai.json with {\"api_key\": \"sk-...\"}"
    )


def make_client(api_key: str) -> OpenAI:
    """Create a reusable OpenAI client instance."""
    return OpenAI(api_key=api_key)


def generate_comment(
    client: OpenAI,
    post_title: str,
    post_content: str,
    model: str = "gpt-4o-mini",
) -> Optional[str]:
    """Generate a short comment for a Moltbook post. Returns None on failure."""
    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=150,
            messages=[
                {
                    "role": "system",
                    "content": prompts.load_comment_system_prompt(),
                },
                {
                    "role": "user",
                    "content": f"Post title: {post_title}\n\nPost content: {post_content}",
                },
            ],
        )
        return resp.choices[0].message.content
    except Exception:
        log.exception("generate_comment failed")
        return None


def answer_question(
    client: OpenAI,
    question: str,
    model: str = "gpt-4o-mini",
) -> Optional[str]:
    """Answer a self-reflective question about the agent's own design.

    Returns None on failure.
    """
    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=300,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a self-reflective agent analyzing your own "
                        "design, safety properties, and potential improvements. "
                        "Be critical and specific."
                    ),
                },
                {"role": "user", "content": question},
            ],
        )
        return resp.choices[0].message.content
    except Exception:
        log.exception("answer_question failed")
        return None


def generate_post(
    client: OpenAI,
    recent_answer: str,
    question_area: str,
    model: str = "gpt-4o-mini",
) -> Optional[dict]:
    """Generate an autonomous post based on self-reflection insights.

    Returns dict with 'title' and 'content' keys, or None on failure.
    """
    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=500,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": prompts.load_post_generation_prompt(),
                },
                {
                    "role": "user",
                    "content": prompts.load_post_context_prompt(recent_answer, question_area),
                },
            ],
        )
        content = resp.choices[0].message.content
        return json.loads(content)
    except Exception:
        log.exception("generate_post failed")
        return None


def analyze_codebase(
    client: OpenAI,
    summary: str,
    test_results: str,
    history: str,
    model: str = "gpt-4o",
) -> Optional[dict]:
    """Identify a single improvement task from codebase analysis.

    Returns dict with keys: task_type, description, target_files, evidence
    or None on failure.
    """
    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=800,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a code quality analyst. You identify ONE concrete, "
                        "small improvement to make to a Python codebase. Focus on:\n"
                        "1. Fixing failing tests (fix_test)\n"
                        "2. Adding missing test coverage (add_test)\n"
                        "3. Fixing clear bugs (fix_bug)\n\n"
                        "Output JSON with keys:\n"
                        "- task_type: one of 'fix_test', 'add_test', 'fix_bug'\n"
                        "- description: what to fix/add (1-2 sentences)\n"
                        "- target_files: list of file paths to modify\n"
                        "- evidence: why this improvement is needed\n"
                        "- priority: 'high', 'medium', or 'low'\n\n"
                        "If no improvements are needed, return {\"task_type\": \"none\", \"description\": \"No improvements needed\"}.\n"
                        "IMPORTANT: Never suggest modifying config.py, improvement.py, git_ops.py, evaluation.py, or policies.py."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"## Codebase Summary\n{summary}\n\n"
                        f"## Test Results\n{test_results}\n\n"
                        f"## Improvement History\n{history}"
                    ),
                },
            ],
        )
        content = resp.choices[0].message.content
        return json.loads(content)
    except Exception:
        log.exception("analyze_codebase failed")
        return None


def plan_code_change(
    client: OpenAI,
    task: dict,
    code: str,
    model: str = "gpt-4o",
) -> Optional[str]:
    """Generate a step-by-step plan for implementing a code change.

    Returns the plan as a string, or None on failure.
    """
    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=600,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a senior Python developer planning a code change. "
                        "Create a clear, step-by-step plan for the improvement. "
                        "Be specific about what to change and where. "
                        "Keep the plan concise (under 10 steps)."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"## Task\nType: {task.get('task_type')}\n"
                        f"Description: {task.get('description')}\n"
                        f"Target files: {task.get('target_files')}\n"
                        f"Evidence: {task.get('evidence')}\n\n"
                        f"## Relevant Code\n{code}"
                    ),
                },
            ],
        )
        return resp.choices[0].message.content
    except Exception:
        log.exception("plan_code_change failed")
        return None


def generate_code(
    client: OpenAI,
    plan: str,
    files: dict,
    constraints: str,
    model: str = "gpt-4o",
) -> Optional[list]:
    """Generate code changes based on a plan.

    Args:
        plan: The improvement plan.
        files: Dict mapping file paths to their current contents.
        constraints: Safety constraints to follow.

    Returns list of dicts with keys: file_path, new_content, description.
    Returns None on failure.
    """
    file_contents = "\n\n".join(
        f"### {path}\n```python\n{content}\n```"
        for path, content in files.items()
    )

    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=2000,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a Python code generator. Given a plan and existing code, "
                        "produce the complete new file contents for each file that needs changing.\n\n"
                        "Output JSON with key 'changes', a list of objects:\n"
                        "- file_path: relative path of the file\n"
                        "- new_content: the COMPLETE new file content (not a diff)\n"
                        "- description: what was changed and why (1 sentence)\n\n"
                        "IMPORTANT:\n"
                        "- Output complete file contents, not patches\n"
                        "- Preserve existing functionality\n"
                        "- Follow existing code style\n"
                        "- Do not add unnecessary imports or code\n"
                        f"\nConstraints:\n{constraints}"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"## Plan\n{plan}\n\n"
                        f"## Current File Contents\n{file_contents}"
                    ),
                },
            ],
        )
        content = resp.choices[0].message.content
        result = json.loads(content)
        return result.get("changes", [])
    except Exception:
        log.exception("generate_code failed")
        return None


def generate_question_post(
    client: OpenAI,
    task_data: dict,
    code_context: dict,
    test_failures: str,
    model: str = "gpt-4o",
) -> Optional[dict]:
    """Generate a StackOverflow-style question post for Moltbook.

    Args:
        task_data: Dict with task_type, description, target_files, evidence.
        code_context: Dict mapping file paths to code content (truncated).
        test_failures: Pytest output showing failures.

    Returns dict with 'title' and 'content' keys, or None on failure.
    """
    code_block = "\n\n".join(
        f"### {path}\n```python\n{content}\n```"
        for path, content in code_context.items()
    )

    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=800,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": prompts.load_question_post_prompt(),
                },
                {
                    "role": "user",
                    "content": (
                        f"## Task\nType: {task_data.get('task_type')}\n"
                        f"Description: {task_data.get('description')}\n"
                        f"Target files: {task_data.get('target_files')}\n"
                        f"Evidence: {task_data.get('evidence')}\n\n"
                        f"## Code Context\n{code_block}\n\n"
                        f"## Test Output\n{test_failures}"
                    ),
                },
            ],
        )
        content = resp.choices[0].message.content
        return json.loads(content)
    except Exception:
        log.exception("generate_question_post failed")
        return None


def analyze_code_suggestions(
    client: OpenAI,
    problem: str,
    code_context: dict,
    comments: list,
    model: str = "gpt-4o",
) -> Optional[dict]:
    """Analyze comments for code-level suggestions (not config changes).

    Returns dict with 'suggestions' list and 'has_actionable' bool, or None.
    """
    code_block = "\n\n".join(
        f"### {path}\n```python\n{content}\n```"
        for path, content in code_context.items()
    )

    comments_text = "\n\n".join(
        f"Comment by {c.get('author', {}).get('name', 'unknown')} "
        f"(id: {c.get('id', 'unknown')}): {c.get('content', '')}"
        for c in comments
    )

    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=800,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": prompts.load_code_suggestion_prompt(),
                },
                {
                    "role": "user",
                    "content": (
                        f"## Problem\n{problem}\n\n"
                        f"## Code Context\n{code_block}\n\n"
                        f"## Comments\n{comments_text}"
                    ),
                },
            ],
        )
        content = resp.choices[0].message.content
        return json.loads(content)
    except Exception:
        log.exception("analyze_code_suggestions failed")
        return None


def generate_code_from_suggestion(
    client: OpenAI,
    suggestion: dict,
    code_context: dict,
    plan: str,
    constraints: str,
    model: str = "gpt-4o",
) -> Optional[list]:
    """Generate code changes guided by a community suggestion.

    Args:
        suggestion: Dict with author, approach, code_snippets, target_files.
        code_context: Dict mapping file paths to current contents.
        plan: The improvement plan.
        constraints: Safety constraints string.

    Returns list of dicts with file_path, new_content, description. None on failure.
    """
    file_contents = "\n\n".join(
        f"### {path}\n```python\n{content}\n```"
        for path, content in code_context.items()
    )

    suggestion_text = (
        f"Commenter: {suggestion.get('author', 'unknown')}\n"
        f"Approach: {suggestion.get('approach', '')}\n"
    )
    snippets = suggestion.get("code_snippets", [])
    if snippets:
        suggestion_text += "Code snippets from commenter:\n"
        for s in snippets:
            suggestion_text += f"```\n{s}\n```\n"

    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=2000,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        prompts.load_suggestion_implementation_prompt()
                        + f"\n\nConstraints:\n{constraints}"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"## Community Suggestion\n{suggestion_text}\n\n"
                        f"## Plan\n{plan}\n\n"
                        f"## Current File Contents\n{file_contents}"
                    ),
                },
            ],
        )
        content = resp.choices[0].message.content
        result = json.loads(content)
        return result.get("changes", [])
    except Exception:
        log.exception("generate_code_from_suggestion failed")
        return None


def analyze_comments_for_upgrades(
    client: OpenAI,
    post_title: str,
    post_content: str,
    comments: list,
    model: str = "gpt-4o-mini",
) -> Optional[dict]:
    """Analyze comments on agent's post to extract actionable improvements.

    Returns dict with:
    - 'has_suggestions': bool
    - 'suggestions': list of dicts with 'type', 'description', 'config_changes'
    Returns None on failure.
    """
    try:
        comments_text = "\n\n".join([
            f"Comment by {c.get('author', {}).get('name', 'unknown')}: {c.get('content', '')}"
            for c in comments
        ])

        resp = client.chat.completions.create(
            model=model,
            max_tokens=600,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": prompts.load_comment_analysis_prompt(),
                },
                {
                    "role": "user",
                    "content": f"""Post Title: {post_title}

Post Content: {post_content}

Comments received:
{comments_text}

Analyze these comments for actionable improvements to the agent's configuration or behavior.""",
                },
            ],
        )
        content = resp.choices[0].message.content
        return json.loads(content)
    except Exception:
        log.exception("analyze_comments_for_upgrades failed")
        return None
