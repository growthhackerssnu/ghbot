"""Incremental Slack -> Supabase index sync (run by sync_worker.py).

Every connected member's token lists the conversations they are in; each
conversation is crawled once with any one member's token. Messages become
documents: a thread (parent + all replies) or one KST day of top-level
messages. Documents are chunked and embedded with the same model as Notion and
written to ghbot_slack_chunks with their channel_id. Access is not stored per
chunk - the server checks the asking member's live Slack membership.

A conversation is crawled in KST-day-aligned windows, oldest first, and
ghbot_slack_channels.synced_until advances after each window, so a long first
backfill can stop at the time budget and resume on the next run. Each run
re-reads the last LOOKBACK_DAYS so edits, deletions and new thread replies are
picked up. Replies added to threads older than that are not.
"""

from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timezone

from embed_index import chunk_text
from slack_client import (
    DEAD_TOKEN_ERRORS,
    SKIP_SUBTYPES,
    SlackError,
    call,
    conversation_label,
    conversation_type,
    list_conversations,
    load_all_connections,
    mark_revoked,
    paginate,
    permalink,
    render_message,
    ts_to_kst,
    user_names,
)

LOOKBACK_DAYS = 14
WINDOW_DAYS = 30
DEFAULT_BUDGET_SECONDS = 3000


def _day_start(ts: float) -> float:
    day = ts_to_kst(ts).replace(hour=0, minute=0, second=0, microsecond=0)
    return day.timestamp()


def _keep(message: dict) -> bool:
    return message.get("subtype") not in SKIP_SUBTYPES and bool(
        (message.get("text") or "").strip() or message.get("files")
    )


def build_documents(
    conv: dict,
    messages: list[dict],
    fetch_replies,
    names: dict[str, str],
    workspace_url: str,
) -> list[dict]:
    """Group one window of conversations.history into thread and day documents.

    fetch_replies(thread_ts) returns the full thread including its parent.
    """
    channel_id = conv["id"]
    label = conversation_label(conv, names)
    days: dict[str, list[dict]] = {}
    docs: list[dict] = []
    for message in messages:
        if not _keep(message):
            continue
        thread_ts = message.get("thread_ts")
        if thread_ts and thread_ts != message["ts"]:
            continue  # a reply surfaced in history; it is part of its thread doc
        if thread_ts and message.get("reply_count", 0) > 0:
            thread = [m for m in fetch_replies(thread_ts) if _keep(m)] or [message]
            thread.sort(key=lambda m: float(m["ts"]))
            first_line = " ".join((message.get("text") or "").split())[:50]
            date = ts_to_kst(message["ts"]).strftime("%Y-%m-%d")
            docs.append({
                "doc_id": f"{channel_id}:t:{message['ts']}",
                "title": f"{label} 스레드 {date}: {first_line}",
                "url": permalink(workspace_url, channel_id, message["ts"]),
                "text": "\n".join(render_message(m, names) for m in thread),
                "ts_start": float(message["ts"]),
                "ts_end": max(float(m["ts"]) for m in thread),
            })
            continue
        days.setdefault(ts_to_kst(message["ts"]).strftime("%Y-%m-%d"), []).append(message)
    for date, day_messages in days.items():
        day_messages.sort(key=lambda m: float(m["ts"]))
        docs.append({
            "doc_id": f"{channel_id}:d:{date}",
            "title": f"{label} {date}",
            "url": permalink(workspace_url, channel_id, day_messages[0]["ts"]),
            "text": "\n".join(render_message(m, names) for m in day_messages),
            "ts_start": float(day_messages[0]["ts"]),
            "ts_end": float(day_messages[-1]["ts"]),
        })
    return docs


class SlackSync:
    def __init__(self, store, model, *, budget_seconds: float | None = None):
        self.store = store
        self.model = model
        budget = budget_seconds or float(os.environ.get("SLACK_SYNC_BUDGET_SECONDS", DEFAULT_BUDGET_SECONDS))
        self.deadline = time.monotonic() + budget
        self.updated = self.unchanged = self.removed = 0

    def _write_doc(self, conv: dict, is_public: bool, doc: dict) -> None:
        pieces = chunk_text(doc["text"])
        if not pieces:
            return
        # The title (channel/DM + date) carries context the chat lines lack.
        embeddings = list(self.model.embed([f"{doc['title']}\n{piece}" for piece in pieces]))
        self.store.delete("ghbot_slack_chunks", [("doc_id", f"eq.{doc['doc_id']}")])
        self.store.upsert(
            "ghbot_slack_chunks",
            [
                {
                    "doc_id": doc["doc_id"],
                    "chunk_index": index,
                    "channel_id": conv["id"],
                    "is_public": is_public,
                    "title": doc["title"],
                    "url": doc["url"],
                    "chunk_text": piece,
                    "embedding": embedding.astype("float32").tolist(),
                    "ts_start": doc["ts_start"],
                    "ts_end": doc["ts_end"],
                }
                for index, (piece, embedding) in enumerate(zip(pieces, embeddings))
            ],
            on_conflict="doc_id,chunk_index",
        )
        self.store.upsert(
            "ghbot_slack_docs",
            [{
                "doc_id": doc["doc_id"],
                "channel_id": conv["id"],
                "content_hash": doc["hash"],
                "ts_start": doc["ts_start"],
                "ts_end": doc["ts_end"],
            }],
            on_conflict="doc_id",
        )

    def _delete_doc(self, doc_id: str) -> None:
        self.store.delete("ghbot_slack_chunks", [("doc_id", f"eq.{doc_id}")])
        self.store.delete("ghbot_slack_docs", [("doc_id", f"eq.{doc_id}")])

    def sync_conversation(self, conv: dict, token: str, state: dict | None, names, workspace_url) -> bool:
        """Crawl one conversation up to now. Returns False if the budget ran out."""
        channel_id = conv["id"]
        kind = conversation_type(conv)
        is_public = kind == "public_channel"
        if state and state.get("channel_type") != kind:
            # e.g. a public channel converted to private: stop treating it as public.
            self.store.update("ghbot_slack_chunks", {"is_public": is_public}, [("channel_id", f"eq.{channel_id}")])

        now = time.time()
        if state and state.get("synced_until"):
            start = state["synced_until"] - LOOKBACK_DAYS * 86400
        else:
            start = float(conv.get("created") or 0)
        window_start = _day_start(max(start, 0))

        while window_start < now:
            if time.monotonic() > self.deadline:
                return False
            window_end = min(window_start + WINDOW_DAYS * 86400, now)
            messages = list(
                paginate(
                    "conversations.history",
                    token,
                    "messages",
                    channel=channel_id,
                    oldest=f"{window_start:.6f}",
                    latest=f"{window_end:.6f}",
                    limit=200,
                )
            )
            docs = build_documents(
                conv,
                messages,
                lambda thread_ts: list(
                    paginate("conversations.replies", token, "messages", channel=channel_id, ts=thread_ts, limit=200)
                ),
                names,
                workspace_url,
            )
            existing = {
                row["doc_id"]: row["content_hash"]
                for row in self.store.select_all(
                    "ghbot_slack_docs",
                    columns="doc_id,content_hash",
                    filters=[
                        ("channel_id", f"eq.{channel_id}"),
                        ("ts_start", f"gte.{window_start}"),
                        ("ts_start", f"lt.{window_end}"),
                    ],
                )
            }
            for doc in docs:
                doc["hash"] = hashlib.sha256(f"{doc['title']}\n{doc['text']}".encode("utf-8")).hexdigest()
                if existing.get(doc["doc_id"]) == doc["hash"]:
                    self.unchanged += 1
                    continue
                self._write_doc(conv, is_public, doc)
                self.updated += 1
            produced = {doc["doc_id"] for doc in docs}
            for doc_id in existing.keys() - produced:
                self._delete_doc(doc_id)
                self.removed += 1

            self.store.upsert(
                "ghbot_slack_channels",
                [{
                    "channel_id": channel_id,
                    "channel_type": kind,
                    "name": conversation_label(conv, names),
                    "synced_until": window_end,
                    "synced_at": datetime.now(timezone.utc).isoformat(),
                }],
                on_conflict="channel_id",
            )
            window_start = window_end
        return True

    def run(self) -> dict:
        connections = load_all_connections(self.store)
        conversations: dict[str, tuple[dict, str]] = {}
        live_tokens: list[str] = []
        for connection in connections:
            try:
                listed = list_conversations(connection["token"])
            except SlackError as exc:
                if exc.error in DEAD_TOKEN_ERRORS:
                    mark_revoked(self.store, connection["subject"])
                print(f"[slack] skip {connection['subject']}: {exc}")
                continue
            live_tokens.append(connection["token"])
            for conv in listed:
                conversations.setdefault(conv["id"], (conv, connection["token"]))
        if not live_tokens:
            print("[slack] no connected members; skipping")
            return {"connected": 0}

        names = user_names(live_tokens[0])
        workspace_url = call("auth.test", live_tokens[0])["url"]
        states = {row["channel_id"]: row for row in self.store.select_all("ghbot_slack_channels")}
        # Keep already-indexed conversations fresh first; backfills fill the rest of the budget.
        order = sorted(conversations, key=lambda cid: (cid not in states, cid))
        completed = 0
        for channel_id in order:
            conv, token = conversations[channel_id]
            try:
                if not self.sync_conversation(conv, token, states.get(channel_id), names, workspace_url):
                    print("[slack] time budget reached; the next run continues from here")
                    break
                completed += 1
            except SlackError as exc:
                print(f"[slack] skip {channel_id}: {exc}")
        summary = {
            "connected": len(live_tokens),
            "conversations": len(conversations),
            "completed": completed,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "removed": self.removed,
        }
        print(f"[slack] {summary}")
        return summary


def run(store, *, budget_seconds: float | None = None) -> dict:
    from fastembed import TextEmbedding

    from embed_index import MODEL_NAME

    if not os.environ.get("SLACK_TOKEN_ENCRYPTION_KEY"):
        print("[slack] SLACK_TOKEN_ENCRYPTION_KEY not set; skipping")
        return {"skipped": True}
    store.require_write_enabled()
    return SlackSync(store, TextEmbedding(model_name=MODEL_NAME), budget_seconds=budget_seconds).run()
