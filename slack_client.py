"""Slack Web API access with each member's own user token.

ghbot never uses a bot that has to be invited into channels. Every member
connects their own Slack account once (OAuth, user scopes only). The sync
worker reads conversations through those tokens, and searches are filtered by
what the asking member's token can see right now, so private channels, group
DMs and DMs are only searchable by the people who are in them.

Tokens are stored Fernet-encrypted in Supabase (ghbot_slack_users) under the
ghbot member subject; SLACK_TOKEN_ENCRYPTION_KEY never leaves Cloud Run.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet, InvalidToken

KST = timezone(timedelta(hours=9))
CONVERSATION_TYPES = "public_channel,private_channel,mpim,im"
USER_SCOPES = [
    "channels:history",
    "channels:read",
    "groups:history",
    "groups:read",
    "im:history",
    "im:read",
    "mpim:history",
    "mpim:read",
    "users:read",
]
# Join/leave/topic noise carries no knowledge worth embedding.
SKIP_SUBTYPES = {
    "channel_join",
    "channel_leave",
    "channel_topic",
    "channel_purpose",
    "channel_name",
    "channel_archive",
    "channel_unarchive",
    "group_join",
    "group_leave",
    "pinned_item",
    "thread_broadcast",  # the same message is already part of its thread
}
# Slack revoked or otherwise invalidated the token; the member must reconnect.
DEAD_TOKEN_ERRORS = {"invalid_auth", "token_revoked", "account_inactive", "not_authed", "token_expired"}


class SlackError(RuntimeError):
    def __init__(self, method: str, error: str):
        super().__init__(f"Slack {method} failed: {error}")
        self.method = method
        self.error = error


def call(method: str, token: str | None = None, *, max_wait: float = 120, **params) -> dict:
    """POST one Web API method, waiting out 429s (Retry-After) up to max_wait."""
    body = urlencode({k: v for k, v in params.items() if v is not None}).encode("utf-8")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    waited = 0.0
    while True:
        request = Request(f"https://slack.com/api/{method}", data=body, method="POST", headers=headers)
        try:
            with urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code == 429:
                delay = float(exc.headers.get("Retry-After", "5"))
                if waited + delay > max_wait:
                    raise SlackError(method, "ratelimited") from exc
                time.sleep(delay)
                waited += delay
                continue
            raise SlackError(method, f"http_{exc.code}") from exc
        except URLError as exc:
            raise SlackError(method, str(exc.reason)) from exc
        if not data.get("ok"):
            raise SlackError(method, data.get("error", "unknown_error"))
        return data


def paginate(method: str, token: str, key: str, **params):
    """Yield items of `key` across cursor pages."""
    cursor = None
    while True:
        data = call(method, token, cursor=cursor, **params)
        yield from data.get(key, [])
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return


def list_conversations(token: str) -> list[dict]:
    """Every conversation (public/private/group DM/DM) this user is a member of."""
    return list(
        paginate(
            "users.conversations",
            token,
            "channels",
            types=CONVERSATION_TYPES,
            exclude_archived="false",
            limit=999,
        )
    )


def conversation_type(conv: dict) -> str:
    if conv.get("is_im"):
        return "im"
    if conv.get("is_mpim"):
        return "mpim"
    if conv.get("is_private") or conv.get("is_group"):
        return "private_channel"
    return "public_channel"


# ---------------------------------------------------------------------------
# token storage


def _fernet() -> Fernet:
    key = os.environ.get("SLACK_TOKEN_ENCRYPTION_KEY", "")
    if not key:
        raise RuntimeError("SLACK_TOKEN_ENCRYPTION_KEY is not set")
    return Fernet(key.encode("utf-8"))


def encrypt_token(token: str) -> str:
    return _fernet().encrypt(token.encode("utf-8")).decode("utf-8")


def decrypt_token(ciphertext: str) -> str | None:
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return None


def load_connection(store, subject: str) -> dict | None:
    rows = store.select(
        "ghbot_slack_users",
        filters=[("subject", f"eq.{subject}"), ("revoked_at", "is.null")],
        limit=1,
    )
    if not rows:
        return None
    token = decrypt_token(rows[0]["token_ciphertext"])
    return {**rows[0], "token": token} if token else None


def load_all_connections(store) -> list[dict]:
    connections = []
    for row in store.select_all("ghbot_slack_users", filters=[("revoked_at", "is.null")]):
        token = decrypt_token(row["token_ciphertext"])
        if token:
            connections.append({**row, "token": token})
    return connections


def mark_revoked(store, subject: str) -> None:
    store.update(
        "ghbot_slack_users",
        {"revoked_at": datetime.now(timezone.utc).isoformat()},
        [("subject", f"eq.{subject}")],
    )


# ---------------------------------------------------------------------------
# rendering messages as text


def ts_to_kst(ts: str | float) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=KST)


def permalink(workspace_url: str, channel_id: str, ts: str, thread_ts: str | None = None) -> str:
    url = f"{workspace_url.rstrip('/')}/archives/{channel_id}/p{ts.replace('.', '')}"
    if thread_ts and thread_ts != ts:
        url += f"?thread_ts={thread_ts}&cid={channel_id}"
    return url


_MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
_CHANNEL = re.compile(r"<#([CG][A-Z0-9]+)(?:\|([^>]*))?>")
_LINK = re.compile(r"<((?:https?|mailto):[^|>]+)(?:\|([^>]*))?>")
_SPECIAL = re.compile(r"<!(here|channel|everyone)[^>]*>")
_SUBTEAM = re.compile(r"<!subteam\^[A-Z0-9]+(?:\|([^>]*))?>")


def render_text(text: str, names: dict[str, str]) -> str:
    text = _MENTION.sub(lambda m: "@" + names.get(m.group(1), m.group(1)), text or "")
    text = _CHANNEL.sub(lambda m: "#" + (m.group(2) or m.group(1)), text)
    text = _LINK.sub(lambda m: f"{m.group(2)} ({m.group(1)})" if m.group(2) else m.group(1), text)
    text = _SPECIAL.sub(lambda m: "@" + m.group(1), text)
    text = _SUBTEAM.sub(lambda m: m.group(1) or "@group", text)
    return html.unescape(text)


def render_message(message: dict, names: dict[str, str]) -> str:
    author = names.get(message.get("user", ""), "") or message.get("username") or (
        (message.get("bot_profile") or {}).get("name") or message.get("user") or "unknown"
    )
    when = ts_to_kst(message["ts"]).strftime("%Y-%m-%d %H:%M")
    line = f"[{when}] {author}: {render_text(message.get('text', ''), names)}"
    files = [f.get("title") or f.get("name") for f in message.get("files") or [] if f.get("title") or f.get("name")]
    if files:
        line += f" (첨부: {', '.join(files)})"
    return line


def user_names(token: str) -> dict[str, str]:
    names: dict[str, str] = {}
    for member in paginate("users.list", token, "members", limit=200):
        profile = member.get("profile") or {}
        names[member["id"]] = profile.get("display_name") or profile.get("real_name") or member.get("name", "")
    return names


def conversation_label(conv: dict, names: dict[str, str]) -> str:
    kind = conversation_type(conv)
    if kind == "im":
        return f"DM {names.get(conv.get('user', ''), conv.get('user', ''))}"
    if kind == "mpim":
        # mpdm-a--b--c-1 -> 그룹 DM a, b, c
        raw = re.sub(r"^mpdm-|-\d+$", "", conv.get("name", ""))
        return "그룹 DM " + ", ".join(part for part in raw.split("--") if part)
    return f"#{conv.get('name', conv.get('id', ''))}"
