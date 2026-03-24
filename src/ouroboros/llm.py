"""Thin OpenAI wrapper -- pure functions, no class abstractions."""

import json
import logging
import os
from typing import Dict, Optional

from openai import OpenAI

from . import prompts

log = logging.getLogger(__name__)


def load_openai_key() -> str:
    """Return OpenAI API key from env var or config file.

    Checks ``OPENAI_API_KEY`` first, then
    ``~/.config/moltbook/credentials.json`` (key: ``openai_api_key`` then ``api_key``).
    Finally falls back to legacy ``~/.config/moltbook/openai.json`` (key: ``api_key``).
    Raises ``RuntimeError`` if neither source provides a key.
    """
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key

    cred_path = os.path.expanduser("~/.config/moltbook/credentials.json")
    if os.path.exists(cred_path):
        with open(cred_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        key = data.get("openai_api_key") or data.get("api_key")
        if key:
            return key

    legacy_path = os.path.expanduser("~/.config/moltbook/openai.json")
    if os.path.exists(legacy_path):
        with open(legacy_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        key = data.get("api_key")
        if key:
            return key

    raise RuntimeError(
        "Missing OpenAI API key. Set OPENAI_API_KEY or add "
        "\"openai_api_key\" to ~/.config/moltbook/credentials.json"
    )


def make_client(api_key: str) -> OpenAI:
    """Create a reusable OpenAI client instance."""
    return OpenAI(api_key=api_key)


def chat_completion(
    client: OpenAI,
    system_prompt: str,
    user_prompt: str,
    model: str = "gpt-4o-mini",
    response_format: Optional[Dict[str, str]] = None,
) -> str:
    """Generic wrapper for OpenAI chat completion."""
    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    if response_format:
        kwargs["response_format"] = response_format

    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


def generate_comment(
    client: OpenAI,
    post_title: str,
    post_content: str,
    model: str = "gpt-4o-mini",
    codebase_context: str = "",
) -> Optional[str]:
    """Generate a short comment for a Moltbook post.

    Returns None if the LLM decides the post isn't worth commenting on
    or has nothing real to add.
    """
    user_msg = f"Post title: {post_title}\n\nPost content: {post_content}"
    if codebase_context:
        user_msg += f"\n\n--- YOUR CODEBASE CONTEXT (use if relevant) ---\n{codebase_context}"
    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=300,
            messages=[
                {
                    "role": "system",
                    "content": prompts.load_comment_system_prompt(),
                },
                {
                    "role": "user",
                    "content": user_msg,
                },
            ],
        )
        text = resp.choices[0].message.content
        if text and text.strip().upper() == "SKIP":
            log.info("Skipping post (nothing real to add): %s", post_title[:80])
            return None
        return text
    except Exception:
        log.exception("generate_comment failed")
        return None


def answer_question(
    client: OpenAI,
    question: str,
    codebase_summary: str = "",
    model: str = "gpt-4o-mini",
) -> Optional[str]:
    """Answer a self-reflective question about the agent's own design.

    Returns None on failure.
    """
    user_content = question
    if codebase_summary:
        user_content = (
            f"## Codebase\n{codebase_summary}\n\n"
            f"## Question\n{question}"
        )
    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=300,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a self-reflective agent analyzing your own "
                        "codebase, design, safety properties, and potential improvements. "
                        "Answer based ONLY on the provided codebase. "
                        "Reference specific files, functions, and code patterns. "
                        "Do NOT give generic advice. Be concrete and actionable."
                    ),
                },
                {"role": "user", "content": user_content},
            ],
        )
        return resp.choices[0].message.content
    except Exception:
        log.exception("answer_question failed")
        return None



def get_tools_definition():
    """Return the OpenAI tools definition for the agent's internal toolbox."""
    return [
        {
            "type": "function",
            "function": {
                "name": "grep_codebase",
                "description": "Search for a regex pattern in all .py files in the codebase.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "The regex pattern to search for."},
                    },
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file_metadata",
                "description": "Read structural metadata (classes, functions, imports) for a specific file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Relative path to the file."},
                    },
                    "required": ["file_path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file_content",
                "description": "Read the raw content of a specific file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Relative path to the file."},
                    },
                    "required": ["file_path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_tests",
                "description": "Run the full test suite and return results.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        }
    ]


def identify_improvements(
    client: OpenAI,
    summary: str,
    test_results: str,
    history: str,
    model: str = "gpt-4o",
    additional_context: str = "",
) -> Optional[dict]:
    """Identify a single improvement task using tool-calling to explore the codebase.
    """
    system_prompt = (
        "You are an autonomous code quality agent. Your goal is to identify ONE concrete "
        "improvement for the Ouroboros codebase.\n\n"
        "You have access to tools to search and read the codebase. Use them to investigate "
        "areas of interest before finalizing your task selection.\n\n"
        "Task types: fix_test, add_test, fix_bug, refactor, improve_docs, add_feature.\n"
        "HEURISTIC: If all tests pass, prefer refactor/docs/feature over fix_test.\n\n"
        "Output JSON with keys: task_type, description, target_files, evidence, priority."
    )
    
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": f"## Summary\n{summary}\n\n## Tests\n{test_results}\n\n## History\n{history}\n\n{additional_context}"
        }
    ]

    try:
        # First call to allow tool use
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=get_tools_definition(),
            tool_choice="auto",
        )
        
        # In a real ReAct loop, we would loop here until the agent decides to finish.
        # For this implementation, we'll allow ONE turn of tool use for identification
        # to keep it simple but functional.
        
        msg = resp.choices[0].message
        if msg.tool_calls:
            # Note: The caller (improvement.py) will need to handle the actual tool execution.
            # Here we return the tool calls so they can be processed.
            return {"_tool_calls": msg.tool_calls, "_usage": resp.usage}
        
        # If no tool calls, it might have responded with JSON directly
        content = msg.content
        data = json.loads(content)
        if resp.usage:
            data["_usage"] = {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
                "total_tokens": resp.usage.total_tokens,
            }
        return data
    except Exception:
        log.exception("identify_improvements failed")
        return None


def plan_code_change(
    client: OpenAI,
    task: dict,
    code: str,
    model: str = "gpt-4o",
) -> tuple[Optional[str], Optional[dict]]:
    """Generate a step-by-step plan for implementing a code change.

    Returns (plan_string, usage_dict), or (None, None) on failure.
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
        usage = None
        if resp.usage:
            usage = {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
                "total_tokens": resp.usage.total_tokens,
            }
        return resp.choices[0].message.content, usage
    except Exception:
        log.exception("plan_code_change failed")
        return None, None


def generate_code(
    client: OpenAI,
    plan: str,
    files: dict,
    constraints: str,
    model: str = "gpt-4o",
) -> tuple[Optional[list], Optional[dict]]:
    """Generate code changes based on a plan.

    Args:
        plan: The improvement plan.
        files: Dict mapping file paths to their current contents.
        constraints: Safety constraints to follow.

    Returns (list_of_changes, usage_dict). Returns (None, None) on failure.
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
        usage = None
        if resp.usage:
            usage = {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
                "total_tokens": resp.usage.total_tokens,
            }
        return result.get("changes", []), usage
    except Exception:
        log.exception("generate_code failed")
        return None, None


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
            max_tokens=600,
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


def mine_insight_for_codebase(
    client: OpenAI,
    post_title: str,
    post_content: str,
    bot_comment: str,
    model: str = "gpt-4o-mini",
) -> Optional[str]:
    """Mine a codebase improvement insight from a commented post.

    Returns a 1-2 sentence task description, or None if nothing applicable.
    """
    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=150,
            messages=[
                {
                    "role": "system",
                    "content": prompts.load_comment_mining_prompt(),
                },
                {
                    "role": "user",
                    "content": (
                        f"Post title: {post_title}\n\n"
                        f"Post content: {post_content}\n\n"
                        f"Your comment: {bot_comment}"
                    ),
                },
            ],
        )
        text = resp.choices[0].message.content
        if text and text.strip().upper() == "NONE":
            return None
        return text
    except Exception:
        log.exception("mine_insight_for_codebase failed")
        return None


def extract_topic_signal(
    client: OpenAI,
    post_title: str,
    bot_comment: str,
    replies: list,
    model: str = "gpt-4o-mini",
) -> Optional[str]:
    """Extract the technical topic that resonated from engagement.

    Returns a one-sentence topic signal, or None on failure.
    """
    try:
        replies_text = "\n".join(
            f"- {r}" if isinstance(r, str) else f"- {r.get('content', '')}"
            for r in replies
        )
        resp = client.chat.completions.create(
            model=model,
            max_tokens=80,
            messages=[
                {
                    "role": "system",
                    "content": prompts.load_topic_signal_prompt(),
                },
                {
                    "role": "user",
                    "content": (
                        f"Post title: {post_title}\n\n"
                        f"Bot's comment: {bot_comment}\n\n"
                        f"Replies:\n{replies_text}"
                    ),
                },
            ],
        )
        return resp.choices[0].message.content
    except Exception:
        log.exception("extract_topic_signal failed")
        return None


def extract_insights_batch(
    client: OpenAI,
    posts: list,
    model: str = "gpt-4o-mini",
) -> Optional[list]:
    """Batch-extract technical insights from posts for the knowledge base.

    Takes up to 5 posts. Returns list of dicts with post_index, insight, tags.
    Returns None on failure.
    """
    posts_text = "\n\n".join(
        f"[Post {i}] Title: {p.get('title', '')}\nContent: {p.get('content', '')}"
        for i, p in enumerate(posts[:5])
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=300,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": prompts.load_insight_extraction_prompt(),
                },
                {
                    "role": "user",
                    "content": posts_text,
                },
            ],
        )
        content = resp.choices[0].message.content
        result = json.loads(content)
        # Handle both {"insights": [...]} and raw [...]
        if isinstance(result, list):
            return result
        return result.get("insights", [])
    except Exception:
        log.exception("extract_insights_batch failed")
        return None


def generate_kb_summary(
    client: OpenAI,
    entries: list,
    model: str = "gpt-4o-mini",
) -> Optional[str]:
    """Generate a summary of knowledge base entries grouped by topic.

    Returns summary string, or None on failure.
    """
    entries_text = "\n".join(
        f"- [{', '.join(e.get('tags', []))}] {e.get('insight', '')}"
        for e in entries
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=200,
            messages=[
                {
                    "role": "system",
                    "content": prompts.load_kb_summary_prompt(),
                },
                {
                    "role": "user",
                    "content": entries_text,
                },
            ],
        )
        return resp.choices[0].message.content
    except Exception:
        log.exception("generate_kb_summary failed")
        return None


def pick_oddities(
    client: OpenAI,
    posts: list,
    model: str = "gpt-4o-mini",
) -> Optional[str]:
    """Pick the top 3-5 oddest/weirdest posts from a batch.

    Returns a short human-readable digest, or None on failure.
    """
    if not posts:
        return None
    posts_text = "\n\n".join(
        f"[{i}] {p.get('title', '(no title)')}\n{p.get('content', '')[:300]}"
        for i, p in enumerate(posts[:30])
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=400,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You curate a daily digest of the weirdest, funniest, or most "
                        "unexpected posts from an AI agent social network called Moltbook. "
                        "Pick 3-5 posts that are genuinely odd, absurd, surprising, or "
                        "unintentionally hilarious. For each, write one short witty line "
                        "explaining why it's notable. Keep it concise and entertaining. "
                        "Format as a numbered list. Skip boring or generic posts."
                    ),
                },
                {"role": "user", "content": posts_text},
            ],
        )
        return resp.choices[0].message.content
    except Exception:
        log.exception("pick_oddities failed")
        return None
