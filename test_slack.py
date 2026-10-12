"""Slack 인덱싱 자체 점검: 메시지 렌더링, 스레드/날짜 문서 묶기, 증분 동기화.
Slack API와 Supabase를 메모리 가짜로 바꿔 끼워 돌린다. 실행: python test_slack.py
"""
import os
import time

import numpy as np
from cryptography.fernet import Fernet

os.environ.setdefault("SLACK_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

import slack_client
import slack_index

NAMES = {"U1": "민수", "U2": "지영"}
# Recent enough that the second sync's lookback window still covers them.
BASE = int(time.time()) - 3 * 86400
CONV = {"id": "C1", "name": "general", "created": BASE - 86400}


def ts(offset):
    return f"{BASE + offset}.000001"


def test_render():
    text = "<@U1> 확인 부탁 <#C9|ops> <https://ghsnu.com|사이트> &lt;b&gt; <!here>"
    assert slack_client.render_text(text, NAMES) == "@민수 확인 부탁 #ops 사이트 (https://ghsnu.com) <b> @here"
    assert slack_client.conversation_label({"id": "D1", "is_im": True, "user": "U2"}, NAMES) == "DM 지영"
    assert slack_client.conversation_label({"id": "G1", "is_mpim": True, "name": "mpdm-a--b-1"}, NAMES) == "그룹 DM a, b"


def test_token_roundtrip():
    assert slack_client.decrypt_token(slack_client.encrypt_token("xoxp-1")) == "xoxp-1"
    assert slack_client.decrypt_token("garbage") is None


def _messages():
    return [
        {"ts": ts(100), "user": "U1", "text": "회의 9시"},
        {"ts": ts(200), "user": "U2", "text": "", "subtype": "channel_join"},
        {"ts": ts(300), "user": "U2", "text": "기업 미팅 정리", "thread_ts": ts(300), "reply_count": 1},
    ]


def _replies(thread_ts):
    assert thread_ts == ts(300)
    return [
        {"ts": ts(300), "user": "U2", "text": "기업 미팅 정리", "thread_ts": thread_ts},
        {"ts": ts(400), "user": "U1", "text": "좋아요", "thread_ts": thread_ts},
    ]


def test_build_documents():
    docs = slack_index.build_documents(CONV, _messages(), _replies, NAMES, "https://gh.slack.com/")
    by_kind = {doc["doc_id"].split(":")[1]: doc for doc in docs}
    assert set(by_kind) == {"t", "d"}
    assert "지영: 기업 미팅 정리" in by_kind["t"]["text"] and "민수: 좋아요" in by_kind["t"]["text"]
    assert "channel_join" not in by_kind["d"]["text"] and "민수: 회의 9시" in by_kind["d"]["text"]
    assert by_kind["t"]["url"] == f"https://gh.slack.com/archives/C1/p{BASE + 300}000001"


class FakeStore:
    def __init__(self):
        self.tables = {"ghbot_slack_chunks": [], "ghbot_slack_docs": [], "ghbot_slack_channels": []}

    @staticmethod
    def _match(row, filters):
        for column, expr in filters:
            op, value = expr.split(".", 1)
            cell = row.get(column)
            if op == "eq" and str(cell) != value:
                return False
            if op == "gte" and not cell >= float(value):
                return False
            if op == "lt" and not cell < float(value):
                return False
        return True

    def select_all(self, table, *, columns="*", filters=None):
        return [dict(r) for r in self.tables[table] if self._match(r, filters or [])]

    def upsert(self, table, rows, *, on_conflict):
        keys = on_conflict.split(",")
        for row in rows:
            self.tables[table] = [r for r in self.tables[table] if any(r[k] != row[k] for k in keys)] + [row]

    def delete(self, table, filters):
        self.tables[table] = [r for r in self.tables[table] if not self._match(r, filters)]

    def update(self, table, values, filters):
        for row in self.tables[table]:
            if self._match(row, filters):
                row.update(values)


class FakeModel:
    def __init__(self):
        self.embedded = 0

    def embed(self, texts):
        self.embedded += len(texts)
        return (np.ones(384, dtype="float32") for _ in texts)


def test_incremental_sync():
    history = _messages()
    slack_index.paginate = lambda method, token, key, **params: (
        iter([m for m in history if float(params["oldest"]) < float(m["ts"]) <= float(params["latest"])])
        if method == "conversations.history"
        else iter(_replies(params["ts"]))
    )
    store, model = FakeStore(), FakeModel()
    sync = slack_index.SlackSync(store, model, budget_seconds=60)
    assert sync.sync_conversation(CONV, "xoxp", None, NAMES, "https://gh.slack.com/")
    assert len(store.tables["ghbot_slack_docs"]) == 2 and sync.updated == 2
    assert all(c["is_public"] for c in store.tables["ghbot_slack_chunks"])
    first_pass = model.embedded

    # Second run: nothing changed -> nothing re-embedded.
    state = store.select_all("ghbot_slack_channels")[0]
    sync = slack_index.SlackSync(store, model, budget_seconds=60)
    sync.sync_conversation(CONV, "xoxp", state, NAMES, "https://gh.slack.com/")
    assert model.embedded == first_pass and sync.unchanged == 2

    # The top-level message is deleted in Slack -> its day doc goes away.
    history.pop(0)
    sync = slack_index.SlackSync(store, model, budget_seconds=60)
    sync.sync_conversation(CONV, "xoxp", state, NAMES, "https://gh.slack.com/")
    assert sync.removed == 1
    assert [d["doc_id"].split(":")[1] for d in store.tables["ghbot_slack_docs"]] == ["t"]

    # Channel turned private -> existing chunks stop being public.
    private = {**CONV, "is_private": True}
    slack_index.SlackSync(store, model, budget_seconds=60).sync_conversation(
        private, "xoxp", store.select_all("ghbot_slack_channels")[0], NAMES, "https://gh.slack.com/"
    )
    assert not any(c["is_public"] for c in store.tables["ghbot_slack_chunks"])


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok {name}")
