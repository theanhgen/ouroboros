"""LLM provider wrapper -- supports OpenAI and Anthropic."""

import json
import logging
import os
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

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

    Messages are also fitted to the model's input budget here. Doing it at the
    call sites alone would only cover the prompts someone remembered to cap --
    and would miss the ReAct loop, whose tool results are appended to a message
    history that is resent whole.
    """
    messages = kwargs.get("messages")
    if messages:
        kwargs["messages"] = fit_messages_to_budget(messages, kwargs.get("model", ""))
    return client.chat.completions.create(**kwargs)


def fit_messages_to_budget(
    messages: List[Dict[str, Any]], model: str
) -> List[Dict[str, Any]]:
    """Trim message contents so the request fits the model's input budget.

    Trims the largest message first and re-measures, so one oversized blob is
    cut rather than every message being shaved evenly. System messages are
    left alone unless nothing else is left to give: they carry the
    instructions, and losing those changes what the model is being asked to
    do.
    """
    budget = model_input_budget(model)
    if _messages_tokens(messages) <= budget:
        return messages

    trimmed = [dict(m) for m in messages]

    def _reduce(indices: List[int]) -> None:
        # Largest first, re-measuring each time, so one oversized blob is cut
        # rather than every message shaved evenly.
        for _ in range(len(indices) * 2):
            total = _messages_tokens(trimmed)
            if total <= budget:
                return
            candidates = [i for i in indices if trimmed[i].get("content")]
            if not candidates:
                return
            biggest = max(candidates, key=lambda i: len(trimmed[i]["content"]))
            content = trimmed[biggest]["content"]
            target = max(estimate_tokens(content) - (total - budget), 0)
            reduced = truncate_to_tokens(content, target, label="prompt")
            if reduced == content:
                return  # cannot shrink further
            trimmed[biggest]["content"] = reduced

    is_text = [i for i, m in enumerate(trimmed) if isinstance(m.get("content"), str)]
    _reduce([i for i in is_text if trimmed[i].get("role") != "system"])
    # Only if trimming everything else still does not fit. The system message
    # carries the instructions, so losing it changes what is being asked --
    # but a request that cannot be sent is worse.
    _reduce([i for i in is_text if trimmed[i].get("role") == "system"])

    final = _messages_tokens(trimmed)
    log.warning(
        "prompt exceeded the %s input budget (%d tokens); trimmed to ~%d",
        model or "default",
        budget,
        final,
    )
    if final > budget:
        log.error(
            "prompt still over budget after trimming (~%d > %d); "
            "non-text content dominates the request",
            final,
            budget,
        )
    return trimmed


def _message_tokens(message: Dict[str, Any]) -> int:
    """Estimate a message's cost, including non-string parts.

    tool_calls and structured content carry real tokens. Counting only string
    content would report a message with a 100k-character tool-call argument as
    free, and the budget check would pass on a request that cannot be sent.
    """
    total = 0
    for key, value in message.items():
        if isinstance(value, str):
            total += estimate_tokens(value)
        elif value is not None:
            try:
                total += estimate_tokens(json.dumps(value, default=str))
            except (TypeError, ValueError):
                total += estimate_tokens(str(value))
    return total


def _messages_tokens(messages: List[Dict[str, Any]]) -> int:
    return sum(_message_tokens(m) for m in messages)


# Backwards-compatible private alias.
_create_completion = create_completion


def _completion_token_kwargs(model: str, max_tokens: int) -> dict:
    """Return the correct completion-token parameter for the target model."""
    if model.startswith("gpt-5"):
        return {"max_completion_tokens": max_tokens}
    return {"max_tokens": max_tokens}


# -- Prompt sizing -----------------------------------------------------------
#
# The agent injects whole-codebase summaries and full test output into prompts.
# Both grow with the repo, so without a ceiling the request eventually exceeds
# the model's context window and every cycle fails with a 400.

# Characters per token. 4 is the usual English rule of thumb, but this mostly
# carries Python source and pytest output, where dense punctuation pushes the
# real ratio nearer 2.5-3. Estimating low is the safe direction: it yields a
# slightly smaller prompt rather than one that overflows.
CHARS_PER_TOKEN = 3

# Input budget per model, in tokens. The value is the context window minus
# generous room for the reply, since these are input-side limits.
_MODEL_INPUT_BUDGETS = (
    ("gpt-5", 300_000),
    ("gpt-4.1", 800_000),
    ("gpt-4o", 100_000),
    ("gpt-4", 100_000),
    ("claude", 150_000),
    # Local Ollama models are typically 8k-32k; assume the small end.
    ("gemma", 6_000),
    ("qwen", 24_000),
    ("llama", 6_000),
    ("ollama/", 6_000),
)

# Used when the model is unrecognised. Deliberately modest: overshooting a
# small local model's window fails the request, while undershooting a large
# one only trims context.
DEFAULT_MAX_REQUEST_TOKENS = 48_000

# Per-section ceilings, applied where prompts are assembled. These are a
# backstop against one section crowding out the others; the request-wide
# budget enforced in create_completion is what actually guarantees the window
# is respected.
MAX_CODEBASE_SUMMARY_TOKENS = 24_000
MAX_TEST_OUTPUT_TOKENS = 6_000
MAX_HISTORY_TOKENS = 4_000
MAX_CODE_CONTEXT_TOKENS = 24_000


def estimate_tokens(text: str) -> int:
    """Approximate the token count of text."""
    if not text:
        return 0
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def model_input_budget(model: str) -> int:
    """Return the input-token budget for model."""
    name = (model or "").lower()
    for prefix, budget in _MODEL_INPUT_BUDGETS:
        if name.startswith(prefix):
            return budget
    return DEFAULT_MAX_REQUEST_TOKENS


def _cut_at_line_start(text: str) -> str:
    """Drop a trailing partial line, unless there is no line structure at all."""
    return text[: text.rindex("\n") + 1] if "\n" in text else text


def _cut_to_line_start(text: str) -> str:
    """Drop a leading partial line, unless there is no line structure at all."""
    return text[text.index("\n") + 1:] if "\n" in text else text


def truncate_to_tokens(text: str, max_tokens: int, *, label: str = "content") -> str:
    """Trim text to roughly max_tokens, keeping the start and the end.

    Middle-out rather than a plain head cut, because for the things this
    truncates both ends carry the signal: a codebase summary's tail lists
    modules the head does not, and pytest output puts the first failure at the
    top and the summary line at the bottom. Cutting the tail would routinely
    discard the part naming what actually failed.

    Cuts land on line boundaries. Fenced content is truncated head-only
    instead: splicing a head onto a tail can leave one file's opening fence
    joined to another file's closing fence, so the model reads the second
    file's code under the first file's heading. Balancing the fence count
    would make that look well-formed while still being wrong, and
    structurally false context is worse than less context.

    The elision is marked so the model is told the input was abridged rather
    than silently seeing a partial picture.
    """
    if max_tokens <= 0 or not text:
        return ""

    budget = max_tokens * CHARS_PER_TOKEN
    if len(text) <= budget:
        return text

    dropped = len(text) - budget
    marker = f"\n\n... [{label} truncated: {dropped:,} of {len(text):,} characters omitted] ...\n\n"
    remaining = budget - len(marker)
    if remaining <= 0:
        # The marker alone would exceed the budget. Returning it anyway would
        # make this function overshoot the limit it exists to enforce, so drop
        # the annotation and hard-cut instead.
        return text[:budget]

    if "```" in text:
        # Head-only: never join two fenced regions that were not adjacent.
        head = _cut_at_line_start(text[:remaining])
        if head.count("```") % 2:
            # The cut landed inside a fence; drop back to before it opened.
            head = head[: head.rindex("```")]
        return head + marker

    # Favour the head slightly: it holds the overview, the tail holds the
    # summary line.
    head_len = (remaining * 2) // 3
    tail_len = remaining - head_len

    head = _cut_at_line_start(text[:head_len])
    tail = _cut_to_line_start(text[-tail_len:]) if tail_len > 0 else ""

    return head + marker + tail



def chat_completion(
    client: Any,
    system_prompt: str,
    user_prompt: str,
    model: str = DEFAULT_OPENAI_MODEL,
    response_format: Optional[Dict[str, str]] = None,
    max_tokens: int = 1000,
    on_error: Optional[Callable[[str], None]] = None,
) -> tuple[str, Optional[dict]]:
    """Generic wrapper for chat completion. Returns (content, usage_dict).

    A failed call and a genuinely empty response both return "". That ambiguity
    cost three weeks: every August 2026 improvement was recorded as "no plan
    generated" when the call itself may never have succeeded. Pass `on_error`
    to find out which happened -- it receives "ExcType: message" and is called
    only when the request actually raised.
    """
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
    except Exception as exc:
        log.exception("completion failed")
        if on_error is not None:
            on_error(f"{type(exc).__name__}: {exc}")
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
    summary = truncate_to_tokens(
        summary, MAX_CODEBASE_SUMMARY_TOKENS, label="codebase summary"
    )
    test_results = truncate_to_tokens(
        test_results, MAX_TEST_OUTPUT_TOKENS, label="test output"
    )
    history = truncate_to_tokens(history, MAX_HISTORY_TOKENS, label="history")
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
    on_error: Optional[Callable[[str], None]] = None,
) -> tuple[Optional[str], Optional[dict]]:
    system = "You are a senior Python developer. Create a step-by-step plan for the code change."
    user = (
        f"## Task\nType: {task.get('task_type')}\n"
        f"Description: {task.get('description')}\n"
        f"Target files: {task.get('target_files')}\n\n"
        f"## Relevant Code\n{code}"
    )
    content, usage = chat_completion(client, system, user, model, max_tokens=800,
                                     on_error=on_error)
    return (content if content else None, usage)


def generate_code(
    client: Any,
    plan: str,
    files: dict,
    constraints: str,
    model: str = DEFAULT_OPENAI_MODEL,
    on_error: Optional[Callable[[str], None]] = None,
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
        max_tokens=2500,
        on_error=on_error,
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
    # Fail-closed is right here -- an unreadable review must not merge code.
    # But the REASON has to survive: 5 of 18 historical rejections were this
    # branch, logged as though the reviewer had judged the change and objected.
    call_errors: list = []
    content, usage = chat_completion(
        client, system, user, model, max_tokens=1000, on_error=call_errors.append
    )
    if call_errors:
        log.warning("review_code_changes: the review call failed: %s", call_errors[0])
        return False, f"Review call failed (not a rejection): {call_errors[0]}", usage
    if not content.strip():
        log.warning("review_code_changes: reviewer returned empty output")
        return False, "Reviewer returned no output (not a rejection).", usage
    try:
        if "{" in content:
            content = content[content.find("{"):content.rfind("}")+1]
        result = json.loads(content)
        return result.get("approved", False), result.get("feedback", ""), usage
    except Exception:
        log.warning("review_code_changes: failed to parse reviewer response", exc_info=True)
        return False, (
            "Reviewer output was not valid JSON (not a rejection): "
            f"{content[:160]}"
        ), usage


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
                {"role": "user", "content": f"## Task\n{task_data}\n\n## Code\n{truncate_to_tokens(code_block, MAX_CODE_CONTEXT_TOKENS, label='code context')}\n\n## Tests\n{truncate_to_tokens(test_failures, MAX_TEST_OUTPUT_TOKENS, label='test output')}"}
            ]
        )
        return json.loads(resp.choices[0].message.content)
    except Exception:
        log.warning("generate_question_post failed", exc_info=True)
        return None


def _format_comments(comments: list) -> str:
    """Render comments for a prompt, tolerating a malformed author.

    `c.get("author", {})` only falls back when the key is absent, so an
    explicit JSON null -- or a bare string author -- raised AttributeError
    and killed the analysis of an otherwise usable comment set. An author
    that carries no name renders as "unknown" instead.
    """
    lines = []
    for c in comments:
        author = c.get("author")
        if isinstance(author, dict):
            author = author.get("name")
        if not isinstance(author, str) or not author:
            author = "unknown"
        lines.append(f"Comment by {author}: {c.get('content')}")
    return "\n\n".join(lines)


def analyze_code_suggestions(
    client: Any,
    problem: str,
    code_context: dict,
    comments: list,
    model: str = DEFAULT_OPENAI_MODEL,
) -> Optional[dict]:
    code_block = "\n\n".join(f"### {path}\n```python\n{content}\n```" for path, content in code_context.items())
    try:
        comments_text = _format_comments(comments)
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
    try:
        comments_text = _format_comments(comments)
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
    try:
        posts_text = "\n\n".join([
            f"[{i}] {p.get('title') or ''}: {(p.get('content') or '')[:200]}"
            for i, p in enumerate(posts[:20])
        ])
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
    codebase_context = truncate_to_tokens(
        codebase_context, MAX_CODEBASE_SUMMARY_TOKENS, label="codebase context"
    )
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
    codebase_summary = truncate_to_tokens(
        codebase_summary, MAX_CODEBASE_SUMMARY_TOKENS, label="codebase summary"
    )
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
