"""Bluesky client: publish an incident post as a thread.

Bluesky's rules for automated accounts are short (docs.bsky.app, Bots):
posting on a schedule is welcome, the profile should carry the `bot`
self-label, and a bot may only interact with users who tagged it. So this
posts and labels, and does not reply, like, follow or mention.

A post holds 300 graphemes and an incident runs to about 900 characters, so
each one goes out as a thread: the title and opening in the first post, the
rest as replies to it.
"""

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import moltbook

log = logging.getLogger(__name__)

SERVICE = "https://bsky.social"
WEB_BASE = "https://bsky.app"
# The lexicon limit is 300 graphemes. len() counts code points, which is
# never fewer, so staying under it by this measure is always within the limit.
MAX_POST_CHARS = 300
POST_COLLECTION = "app.bsky.feed.post"
PROFILE_COLLECTION = "app.bsky.actor.profile"
BOT_LABEL = {"$type": "com.atproto.label.defs#selfLabels", "values": [{"val": "bot"}]}


class BlueskyError(RuntimeError):
    pass


@dataclass
class Credentials:
    handle: str
    app_password: str = field(repr=False)


@dataclass
class Session:
    did: str
    handle: str
    access_jwt: str = field(repr=False)


def load_credentials() -> Optional[Credentials]:
    """Handle and app password, or None when the account is not set up.

    None rather than an error: the setting can be on before the account
    exists, and that must not fail the cycle that also posts to Moltbook.
    """
    handle = os.environ.get("BLUESKY_HANDLE")
    password = os.environ.get("BLUESKY_APP_PASSWORD")
    cred_path = os.path.expanduser("~/.config/moltbook/credentials.json")
    if (not handle or not password) and os.path.exists(cred_path):
        data = moltbook._read_json_file(cred_path)
        handle = handle or data.get("bluesky_handle")
        password = password or data.get("bluesky_app_password")
    if not handle or not password:
        return None
    return Credentials(handle=handle, app_password=password)


def _xrpc(
    method: str,
    nsid: str,
    *,
    token: Optional[str] = None,
    body: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    url = f"{SERVICE}/xrpc/{nsid}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        # Same rule as Moltbook: reads are retried, writes get one attempt,
        # because a retried write that had landed is a duplicate public post.
        return moltbook._urlopen_json(req)
    except Exception as exc:
        raise BlueskyError(f"{nsid} failed: {exc}") from exc


def create_session(creds: Credentials) -> Session:
    data = _xrpc(
        "POST",
        "com.atproto.server.createSession",
        body={"identifier": creds.handle, "password": creds.app_password},
    )
    return Session(did=data["did"], handle=data.get("handle", creds.handle), access_jwt=data["accessJwt"])


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


def create_post(
    session: Session, text: str, reply: Optional[Dict[str, Any]] = None
) -> Dict[str, str]:
    """Create one post. Returns its {"uri", "cid"} reference."""
    record: Dict[str, Any] = {
        "$type": POST_COLLECTION,
        "text": text,
        "createdAt": _now(),
        "langs": ["en"],
    }
    if reply:
        record["reply"] = reply
    data = _xrpc(
        "POST",
        "com.atproto.repo.createRecord",
        token=session.access_jwt,
        body={"repo": session.did, "collection": POST_COLLECTION, "record": record},
    )
    return {"uri": data["uri"], "cid": data["cid"]}


def ensure_bot_label(session: Session) -> bool:
    """Put the `bot` self-label on the profile. True when it is there after.

    Reads the profile first and writes the whole record back, so a name,
    description or avatar set by hand is kept.
    """
    try:
        current = _xrpc(
            "GET",
            "com.atproto.repo.getRecord",
            token=session.access_jwt,
            params={"repo": session.did, "collection": PROFILE_COLLECTION, "rkey": "self"},
        )
        profile = dict(current.get("value") or {})
    except BlueskyError as exc:
        # An account that has never edited its profile has no record yet, and
        # the server says so with a 400. Anything else is a failed read, and
        # writing after a failed read would replace the profile with a blank.
        cause = exc.__cause__
        if not (isinstance(cause, urllib.error.HTTPError) and cause.code == 400):
            raise
        profile = {}
    values = (profile.get("labels") or {}).get("values") or []
    if any(v.get("val") == "bot" for v in values):
        return True
    profile["$type"] = PROFILE_COLLECTION
    profile["labels"] = BOT_LABEL
    _xrpc(
        "POST",
        "com.atproto.repo.putRecord",
        token=session.access_jwt,
        body={"repo": session.did, "collection": PROFILE_COLLECTION, "rkey": "self", "record": profile},
    )
    return True


def _pieces(text: str, limit: int) -> List[str]:
    """Sentences of text, with any longer than limit broken on spaces."""
    out: List[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", " ".join(text.split())):
        while len(sentence) > limit:
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            out.append(sentence[:cut])
            sentence = sentence[cut:].lstrip()
        if sentence:
            out.append(sentence)
    return out


def split_thread(title: str, content: str, limit: int = MAX_POST_CHARS) -> List[str]:
    """Break a post into thread parts of at most limit characters.

    The title stands alone at the top of the first part: it is the line that
    was written to be read on its own. Sentences are kept whole, and a
    paragraph break is kept as one when both sides fit in the same part.
    """
    parts: List[str] = []
    current = title.strip()
    separator = "\n\n"
    for paragraph in content.split("\n\n"):
        for piece in _pieces(paragraph, limit):
            if current and len(current) + len(separator) + len(piece) <= limit:
                current += separator + piece
            else:
                if current:
                    parts.append(current)
                current = piece
            separator = " "
        separator = "\n\n"
    if current:
        parts.append(current)
    return parts


def rkey(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def post_url(handle: str, uri: str) -> str:
    return f"{WEB_BASE}/profile/{handle}/post/{rkey(uri)}"


def post_thread(session: Session, parts: List[str]) -> List[Dict[str, str]]:
    """Publish parts as one thread. Returns the reference of each post made.

    A failure part-way leaves a shorter thread and raises; what was posted is
    on the exception as `.posted`, so the caller can still record the root.
    """
    posted: List[Dict[str, str]] = []
    for text in parts:
        reply = {"root": posted[0], "parent": posted[-1]} if posted else None
        try:
            posted.append(create_post(session, text, reply))
        except BlueskyError as exc:
            exc.posted = posted  # type: ignore[attr-defined]
            raise
    return posted
