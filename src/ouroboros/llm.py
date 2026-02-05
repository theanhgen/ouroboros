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
