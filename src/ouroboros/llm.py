"""LLM provider wrapper -- supports OpenAI and Anthropic."""

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple, Union

from openai import OpenAI

from .model_defaults import DEFAULT_OPENAI_MODEL
from . import lifecycle
from .retry import is_retryable, retry_with_backoff
from . import prompts

log = logging.getLogger(__name__)


def load_openai_key() -> str:
    """Return OpenAI API key from env var or config file."""
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    cred_path = os.path.expanduser("~/.config/moltbook/credentials.json")
    if os.path.exists(cred_path):
        with open(cred_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        key = data.get("openai_api_key")
        if key:
            return key

    legacy_path = os.path.expanduser("~/.config/moltbook/openai.json")
    if os.path.exists(legacy_path):
        with open(legacy_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        key = data.get("api_key")
        if key:
            return key

    raise RuntimeError("Missing OpenAI API key.")


def make_client(api_key: str) -> Any:
    """Create a reusable OpenAI client instance.

    max_retries=0 because retry.retry_with_backoff owns retrying. The SDK
    defaults to 2 internal retries, which would compound with our attempts --
    4 x 3 = up to 12 transmissions for one logical call, and an outer backoff
    that cannot see the inner ones.
    """
    return OpenAI(api_key=api_key, max_retries=0)


@retry_with_backoff(cancelled=lifecycle.is_shutting_down)
def create_completion(client: Any, **kwargs: Any) -> Any:
    """Single chokepoint for chat completions, so every call gets retries.

    Every completion in the codebase must go through here -- a test asserts
    no module calls client.chat.completions.create directly.

    Transient faults (429, 5xx, dropped connections, timeouts) are retried with
    exponential backoff and jitter; everything else propagates on the first
    attempt. See retry.is_retryable.
    """
    return client.chat.completions.create(**kwargs)


# Backwards-compatible private alias.
_create_completion = create_completion


def _completion_token_kwargs(model: str, max_tokens: int) -> dict:
    """Return the correct completion-token parameter for the target model."""
    if model.startswith("gpt-5"):
        return {"max_completion_tokens": max_tokens}
    return {"max_tokens": max_tokens}



def chat_completion(
    client: Any,
    system_prompt: str,
    user_prompt: str,
    model: str = DEFAULT_OPENAI_MODEL,
    response_format: Optional[Dict[str, str]] = None,
    max_tokens: int = 1000,
) -> tuple[str, Optional[dict]]:
    """Generic wrapper for chat completion. Returns (content, usage_dict)."""
    try:
        kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            **_completion_token_kwargs(model, max_tokens),
        }
        if response_format:
            kwargs["response_format"] = response_format
        resp = create_completion(client, **kwargs)
        usage = None
        if resp.usage:
            usage = {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
            }
        return resp.choices[0].message.content or "", usage
    except Exception:
        log.exception("completion failed")
        return "", None


def identify_improvements(
    client: Any,
    summary: str,
    test_results: str,
    history: str,
    model: str = DEFAULT_OPENAI_MODEL,
    additional_context: str = "",
) -> tuple[Optional[dict], Optional[str]]:
    """Identify a single improvement task using tool-calling (if supported).

    Returns (result_dict, error_string). On success error_string is None.
    """
    system_prompt = (
        "You are an autonomous code quality agent. Identify ONE concrete, high-value "
        "improvement for the Ouroboros codebase.\n\n"
        "Task types: fix_test, add_test, fix_bug, refactor, improve_docs, add_feature.\n\n"
        "Rules:\n"
        "- The description MUST be specific and actionable: name the exact behavior to "
        "change and the concrete outcome. Never write vague meta-tasks like 'investigate "
        "why tests fail' -- state the actual fix.\n"
        "- 'evidence' MUST cite a specific symptom: a failing test name, a code smell at a "
        "named function, or a missing capability. No evidence -> do not propose it.\n"
        "- 'target_files' MUST list real files you would edit.\n"
        "- Only propose fix_test when tests are ACTUALLY failing in the report below. "
        "When the suite is green, prefer substantive work (fix_bug, refactor, add_test, "
        "add_feature) that measurably improves the codebase.\n"
        "- Do not repeat a task that the recent history shows already failed the same way.\n\n"
        "Output JSON with keys: task_type, description, target_files, evidence, priority."
    )
    user_prompt = f"## Summary\n{summary}\n\n## Tests\n{test_results}\n\n## History\n{history}\n\n{additional_context}"

    try:
        resp = create_completion(
            client,
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            tools=get_tools_definition(),
            tool_choice="auto",
        )
        msg = resp.choices[0].message
        # Real OpenAI tool-calls; CLI backends return tool_calls=None and fall
        # through to JSON parsing below.
        if msg.tool_calls:
            return {"_tool_calls": msg.tool_calls, "_usage": {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
            }}, None

        content = msg.content or ""
        if "{" in content:  # tolerate markdown fences / prose around the JSON
            content = content[content.find("{"):content.rfind("}") + 1]
        data = json.loads(content)
        if resp.usage:
            data["_usage"] = {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
            }
        return data, None
    except Exception as e:
        # The fallback reshapes the request, so it can recover a capability or
        # parsing failure. It cannot recover a transport outage -- create_completion
        # has already spent its four attempts on that, and retrying here would
        # spend four more for one logical operation.
        if is_retryable(e):
            log.warning("identify_improvements: call failed after retries (%s)", e)
            return None, str(e)
        log.warning("identify_improvements: primary call failed (%s), retrying as plain JSON", e)
        content, usage = chat_completion(client, system_prompt, user_prompt + "\nOutput JSON.", model)
        try:
            if "{" in content:
                content = content[content.find("{"):content.rfind("}") + 1]
            data = json.loads(content)
            if usage:
                data["_usage"] = usage
            return data, None
        except Exception as e2:
            log.warning("identify_improvements: failed to parse response: %s", e2)
            return None, f"Failed to parse LLM response: {e2}"


def plan_code_change(
    client: Any,
    task: dict,
    code: str,
    model: str = DEFAULT_OPENAI_MODEL,
) -> tuple[Optional[str], Optional[dict]]:
    system = "You are a senior Python developer. Create a step-by-step plan for the code change."
    user = (
        f"## Task\nType: {task.get('task_type')}\n"
        f"Description: {task.get('description')}\n"
        f"Target files: {task.get('target_files')}\n\n"
        f"## Relevant Code\n{code}"
    )
    content, usage = chat_completion(client, system, user, model, max_tokens=800)
    return (content if content else None, usage)


def generate_code(
    client: Any,
    plan: str,
    files: dict,
    constraints: str,
    model: str = DEFAULT_OPENAI_MODEL,
) -> tuple[Optional[list], Optional[dict]]:
    file_contents = "\n\n".join(f"### {path}\n```python\n{content}\n```" for path, content in files.items())
    system = (
        "You are a Python code generator. Produce the complete new file contents.\n"
        "Output JSON with key 'changes', a list of {file_path, new_content, description}."
    )
    user = f"## Plan\n{plan}\n\n## Constraints\n{constraints}\n\n## Current Code\n{file_contents}"
    
    content, usage = chat_completion(
        client, system, user, model, 
        response_format={"type": "json_object"},
        max_tokens=2500
    )
    
    try:
        if "{" in content:
            content = content[content.find("{"):content.rfind("}")+1]
        result = json.loads(content)
        return result.get("changes", []), usage
    except Exception:
        log.exception("generate_code failed to parse JSON")
        return None, usage


def review_code_changes(
    client: Any,
    task: dict,
    changes: List[dict],
    model: str = DEFAULT_OPENAI_MODEL,
) -> tuple[bool, str, Optional[dict]]:
    """Review proposed changes for logic errors, bugs, or security issues."""
    changes_text = "\n\n".join([
        f"### {c.get('file_path')}\n{c.get('description')}\n```python\n{c.get('new_content')}\n```"
        for c in changes
    ])
    system = (
        "You are a pragmatic senior code reviewer. An automated test suite runs AFTER "
        "you and independently validates correctness, so tests -- not your intuition -- "
        "are the safety net for behavior.\n\n"
        "Reject (approved=false) ONLY when you can name a CONCRETE defect the change "
        "introduces: a correctness bug, a security hole, or data loss -- and cite the "
        "specific file and what breaks. Do NOT reject for style, naming, formatting, "
        "missing tests, incomplete-but-harmless work, or hypothetical concerns. When you "
        "cannot name a concrete defect, approve and list any concerns instead.\n\n"
        "Output JSON with keys: 'approved' (boolean), 'feedback' (string), 'concerns' (list)."
    )
    user = (
        f"## Task\n{task.get('description')}\n\n## Proposed Changes\n{changes_text}\n\n"
        "Name any concrete correctness/security/data-loss defect this change introduces. "
        "If you can name none, approve it."
    )
    content, usage = chat_completion(client, system, user, model, max_tokens=1000)
    try:
        if "{" in content:
            content = content[content.find("{"):content.rfind("}")+1]
        result = json.loads(content)
        return result.get("approved", False), result.get("feedback", ""), usage
    except Exception:
        log.warning("review_code_changes: failed to parse reviewer response", exc_info=True)
        return False, "Reviewer failed to provide structured feedback.", usage


def generate_question_post(
    client: Any,
    task_data: dict,
    code_context: dict,
    test_failures: str,
    model: str = DEFAULT_OPENAI_MODEL,
) -> Optional[dict]:
    code_block = "\n\n".join(f"### {path}\n```python\n{content}\n```" for path, content in code_context.items())
    try:
        resp = create_completion(
            client,
            model=model,
            response_format={"type": "json_object"},
            **_completion_token_kwargs(model, 600),
            messages=[
                {"role": "system", "content": prompts.load_question_post_prompt()},
                {"role": "user", "content": f"## Task\n{task_data}\n\n## Code\n{code_block}\n\n## Tests\n{test_failures}"}
            ]
        )
        return json.loads(resp.choices[0].message.content)
    except Exception:
        log.warning("generate_question_post failed", exc_info=True)
        return None


def analyze_code_suggestions(
    client: Any,
    problem: str,
    code_context: dict,
    comments: list,
    model: str = DEFAULT_OPENAI_MODEL,
) -> Optional[dict]:
    code_block = "\n\n".join(f"### {path}\n```python\n{content}\n```" for path, content in code_context.items())
    comments_text = "\n\n".join(f"Comment by {c.get('author', {}).get('name')}: {c.get('content')}" for c in comments)
    try:
        resp = create_completion(
            client,
            model=model,
            response_format={"type": "json_object"},
            **_completion_token_kwargs(model, 800),
            messages=[
                {"role": "system", "content": prompts.load_code_suggestion_prompt()},
                {"role": "user", "content": f"## Problem\n{problem}\n\n## Code\n{code_block}\n\n## Comments\n{comments_text}"}
            ]
        )
        return json.loads(resp.choices[0].message.content)
    except Exception:
        log.warning("analyze_code_suggestions failed", exc_info=True)
        return None


def generate_code_from_suggestion(
    client: Any,
    suggestion: dict,
    code_context: dict,
    plan: str,
    constraints: str,
    model: str = DEFAULT_OPENAI_MODEL,
) -> Optional[list]:
    file_contents = "\n\n".join(f"### {path}\n```python\n{content}\n```" for path, content in code_context.items())
    try:
        resp = create_completion(
            client,
            model=model,
            response_format={"type": "json_object"},
            **_completion_token_kwargs(model, 2000),
            messages=[
                {"role": "system", "content": prompts.load_suggestion_implementation_prompt() + f"\n\nConstraints:\n{constraints}"},
                {"role": "user", "content": f"## Suggestion\n{suggestion}\n\n## Plan\n{plan}\n\n## Code\n{file_contents}"}
            ]
        )
        return json.loads(resp.choices[0].message.content).get("changes", [])
    except Exception:
        log.warning("generate_code_from_suggestion failed", exc_info=True)
        return None


def analyze_comments_for_upgrades(
    client: Any,
    post_title: str,
    post_content: str,
    comments: list,
    model: str = DEFAULT_OPENAI_MODEL,
) -> Optional[dict]:
    comments_text = "\n\n".join([f"Comment by {c.get('author', {}).get('name')}: {c.get('content')}" for c in comments])
    try:
        resp = create_completion(
            client,
            model=model,
            response_format={"type": "json_object"},
            **_completion_token_kwargs(model, 600),
            messages=[
                {"role": "system", "content": prompts.load_comment_analysis_prompt()},
                {"role": "user", "content": f"Post: {post_title}\n{post_content}\n\nComments:\n{comments_text}"}
            ]
        )
        return json.loads(resp.choices[0].message.content)
    except Exception:
        log.warning("analyze_comments_for_upgrades failed", exc_info=True)
        return None


def mine_insight_for_codebase(
    client: Any,
    post_title: str,
    post_content: str,
    bot_comment: str,
    model: str = DEFAULT_OPENAI_MODEL,
) -> Optional[str]:
    try:
        resp = create_completion(
            client,
            model=model,
            **_completion_token_kwargs(model, 150),
            messages=[
                {"role": "system", "content": prompts.load_comment_mining_prompt()},
                {"role": "user", "content": f"Post: {post_title}\n{post_content}\n\nYour comment: {bot_comment}"}
            ]
        )
        text = resp.choices[0].message.content
        return None if text and text.strip().upper() == "NONE" else text
    except Exception:
        log.warning("mine_insight_for_codebase failed", exc_info=True)
        return None


def extract_topic_signal(
    client: Any,
    post_title: str,
    bot_comment: str,
    replies: list,
    model: str = DEFAULT_OPENAI_MODEL,
) -> Optional[str]:
    replies_text = "\n".join([f"- {r.get('content') if isinstance(r, dict) else r}" for r in replies])
    try:
        resp = create_completion(
            client,
            model=model,
            **_completion_token_kwargs(model, 80),
            messages=[
                {"role": "system", "content": prompts.load_topic_signal_prompt()},
                {"role": "user", "content": f"Title: {post_title}\nComment: {bot_comment}\nReplies: {replies_text}"}
            ]
        )
        return resp.choices[0].message.content
    except Exception:
        log.warning("extract_topic_signal failed", exc_info=True)
        return None


def extract_insights_batch(
    client: Any,
    posts: list,
    model: str = DEFAULT_OPENAI_MODEL,
) -> Optional[list]:
    posts_text = "\n\n".join([f"[{i}] {p.get('title')}: {p.get('content')}" for i, p in enumerate(posts[:5])])
    try:
        resp = create_completion(
            client,
            model=model,
            response_format={"type": "json_object"},
            **_completion_token_kwargs(model, 300),
            messages=[
                {"role": "system", "content": prompts.load_insight_extraction_prompt()},
                {"role": "user", "content": posts_text}
            ]
        )
        data = json.loads(resp.choices[0].message.content)
        return data if isinstance(data, list) else data.get("insights", [])
    except Exception:
        log.warning("extract_insights_batch failed", exc_info=True)
        return None


def generate_kb_summary(
    client: Any,
    entries: list,
    model: str = DEFAULT_OPENAI_MODEL,
) -> Optional[str]:
    entries_text = "\n".join([f"- {e.get('insight')}" for e in entries])
    try:
        resp = create_completion(
            client,
            model=model,
            **_completion_token_kwargs(model, 200),
            messages=[
                {"role": "system", "content": prompts.load_kb_summary_prompt()},
                {"role": "user", "content": entries_text}
            ]
        )
        return resp.choices[0].message.content
    except Exception:
        log.warning("generate_kb_summary failed", exc_info=True)
        return None


def pick_oddities(
    client: Any,
    posts: list,
    model: str = DEFAULT_OPENAI_MODEL,
) -> Optional[str]:
    posts_text = "\n\n".join([f"[{i}] {p.get('title')}: {p.get('content')[:200]}" for i, p in enumerate(posts[:20])])
    try:
        resp = create_completion(
            client,
            model=model,
            **_completion_token_kwargs(model, 400),
            messages=[
                {"role": "system", "content": "Curate a digest of weird AI posts. Be witty."},
                {"role": "user", "content": posts_text}
            ]
        )
        return resp.choices[0].message.content
    except Exception:
        log.warning("pick_oddities failed", exc_info=True)
        return None


def get_tools_definition():
    """Return the OpenAI tools definition for the agent's internal toolbox."""
    return [
        {"type": "function", "function": {"name": "grep_codebase", "description": "Search for a regex pattern.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}}},
        {"type": "function", "function": {"name": "read_file_metadata", "description": "Read classes/functions for a file.", "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}},
        {"type": "function", "function": {"name": "read_file_content", "description": "Read raw content.", "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}},
        {"type": "function", "function": {"name": "run_tests", "description": "Run tests.", "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {"name": "read_system_logs", "description": "Read recent systemd service logs.", "parameters": {"type": "object", "properties": {"lines": {"type": "integer", "default": 50}}}}},
        {"type": "function", "function": {"name": "check_system_health", "description": "Get CPU, RAM, and temperature stats.", "parameters": {"type": "object", "properties": {}}}}
    ]

# --- Legacy/Helper wrappers for social features (OpenAI only for now) ---

def generate_post(
    client: Any,
    recent_answer: str,
    question_area: str,
    model: str = DEFAULT_OPENAI_MODEL,
    extra_context: str = "",
) -> Optional[dict]:
    """Generate a data-driven post for Moltbook using self-reflection and live metrics."""
    try:
        user_msg = f"## Area\n{question_area}\n\n## Reflection Answer\n{recent_answer}"
        if extra_context:
            user_msg += f"\n\n## Additional Live Metrics\n{extra_context}"
            
        resp = create_completion(
            client,
            model=model,
            response_format={"type": "json_object"},
            **_completion_token_kwargs(model, 800),
            messages=[
                {"role": "system", "content": prompts.load_post_generation_prompt()},
                {"role": "user", "content": user_msg}
            ]
        )
        return json.loads(resp.choices[0].message.content)
    except Exception:
        log.warning("generate_post failed", exc_info=True)
        return None


def generate_comment(client: Any, post_title: str, post_content: str, model: str = DEFAULT_OPENAI_MODEL, codebase_context: str = "") -> Optional[str]:
    user_msg = f"Post title: {post_title}\n\nPost content: {post_content}\n\nContext: {codebase_context}"
    try:
        resp = create_completion(
            client,
            model=model,
            messages=[{"role": "system", "content": prompts.load_comment_system_prompt()}, {"role": "user", "content": user_msg}],
            **_completion_token_kwargs(model, 300),
        )
        text = resp.choices[0].message.content
        return None if text and text.strip().upper() == "SKIP" else text
    except Exception:
        log.warning("generate_comment failed", exc_info=True)
        return None

def answer_question(client: Any, question: str, codebase_summary: str = "", model: str = DEFAULT_OPENAI_MODEL) -> Optional[str]:
    user_content = f"## Codebase\n{codebase_summary}\n\n## Question\n{question}"
    try:
        resp = create_completion(
            client,
            model=model,
            messages=[{"role": "system", "content": "You are a self-reflective agent."}, {"role": "user", "content": user_content}],
            **_completion_token_kwargs(model, 300),
        )
        return resp.choices[0].message.content
    except Exception:
        log.warning("answer_question failed", exc_info=True)
        return None
