"""Thin OpenAI wrapper -- pure functions, no class abstractions."""

import json
import logging
import os
from typing import Optional

from openai import OpenAI

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
                    "content": (
                        "You are a thoughtful commenter on a social platform. "
                        "Write a short, insightful comment that adds value to "
                        "the discussion. Be genuine and concise."
                    ),
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
