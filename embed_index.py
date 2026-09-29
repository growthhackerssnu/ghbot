"""Builds/updates the SQLite staging index and publishes it to Supabase.

The MCP server reads Supabase directly. This file remains the incremental
builder used by the daily sync job and by local one-off rebuilds.

Builds/updates the SQLite semantic index for Notion content that needs context
search rather than exact-field filtering (project pages and meeting notes).
Drive remains a live, metadata-first source and is not embedded. Run manually
or on a schedule:
`python embed_index.py`.

Incremental: each document's last_edited timestamp is tracked in the `pages`
table. A document is only re-fetched/re-chunked/re-embedded if that timestamp
changed since the last run - unchanged documents are skipped, which matters
once meeting notes / more Drive folders push this into the hundreds+ range.
Pass --full to force a full rebuild (drops both tables first).
"""
from __future__ import annotations

import sys
import sqlite3
from pathlib import Path

import numpy as np

from notion_client import NotionClient, NotionAccessError, PROJECTS_DB_ID, TASKS_DB_ID
from config import load_env_file
from supabase_store import SupabaseStore

load_env_file(Path(__file__).parent / ".env")

DB_PATH = Path(__file__).parent / "gh_bot.db"
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# Notion sources that need context/semantic search rather than field filtering.
# Add a data_source_id here once its database is shared with the integration
# ("..." -> Connections -> ghbot 연결 in Notion).
NOTION_SOURCES = [
    {"label": "진행중_프로젝트(Projects)", "data_source_id": PROJECTS_DB_ID, "title_prop": "Name"},
    # Tasks contains meeting notes and operating decisions. People DB is
    # intentionally excluded so personal contact fields never enter the index.
    {
        "label": "운영업무_회의록(Tasks)",
        "data_source_id": TASKS_DB_ID,
        "title_prop": "Name",
        "filter": {"property": "종류", "select": {"equals": "회의"}},
    },
]

MAX_CHUNK_CHARS = 800


def _batches(rows, size=200):
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


DRIVE_SOURCE_PREFIX = "구글드라이브_"


def purge_drive_index(conn) -> None:
    """Remove legacy Drive embeddings from the local staging index.

    Drive is searched live by team/path/name/content instead of semantic
    retrieval, so retaining its old chunks would only dilute Notion results.
    """
    for table, statement, params in (
        ("chunks", "DELETE FROM chunks WHERE source_label LIKE ?", (f"{DRIVE_SOURCE_PREFIX}%",)),
        ("pages", "DELETE FROM pages WHERE source_label LIKE ?", (f"{DRIVE_SOURCE_PREFIX}%",)),
        ("drive_files", "DELETE FROM drive_files", ()),
    ):
        try:
            conn.execute(statement, params)
        except sqlite3.OperationalError as exc:
            if f"no such table: {table}" not in str(exc):
                raise
    conn.commit()


def publish_sqlite_index(conn, store: SupabaseStore) -> None:
    """Publish the local build result into the canonical Supabase tables."""
    store.require_write_enabled()

    chunk_rows = []
    for row in conn.execute(
        "SELECT page_id, chunk_index, url, title, source_label, chunk_text, embedding, last_edited FROM chunks"
    ):
        page_id, chunk_index, url, title, source_label, chunk_text, embedding, last_edited = row
        chunk_rows.append({
            "page_id": page_id,
            "chunk_index": chunk_index,
            "url": url,
            "title": title,
            "source_label": source_label,
            "chunk_text": chunk_text,
            "embedding": np.frombuffer(embedding, dtype="float32").tolist(),
            "last_edited": last_edited,
        })
    # A changed document can produce fewer chunks than before. Clear that
    # document's remote chunks first so old chunk indexes cannot survive an
    # upsert and pollute later searches.
    for page_id in {row["page_id"] for row in chunk_rows}:
        store.delete("ghbot_chunks", [("page_id", f"eq.{page_id}")])
    for batch in _batches(chunk_rows):
        store.upsert("ghbot_chunks", batch, on_conflict="page_id,chunk_index")

    page_rows = [
        {"page_id": page_id, "source_label": source_label, "last_edited_time": edited}
        for page_id, source_label, edited in conn.execute(
            "SELECT page_id, source_label, last_edited_time FROM pages"
        )
    ]
    for batch in _batches(page_rows):
        store.upsert("ghbot_pages", batch, on_conflict="page_id")

    # Drive is deliberately not indexed.  Clear the legacy lookup table so
    # search_drive falls back to its live path-based team filter.
    store.delete("ghbot_drive_files", [("file_id", "not.is.null")])

    # Remove rows deleted by the local sweep so the remote table does not keep
    # returning stale documents. This is intentionally done by primary key
    # rather than a broad DELETE, which makes partial indexes safer to publish.
    local_page_ids = {row["page_id"] for row in page_rows}
    for remote in store.select_all("ghbot_pages", columns="page_id"):
        if remote["page_id"] not in local_page_ids:
            page_id = remote["page_id"]
            store.delete("ghbot_chunks", [("page_id", f"eq.{page_id}")])
            store.delete("ghbot_pages", [("page_id", f"eq.{page_id}")])

    print(f"published {len(chunk_rows)} chunks to Supabase")


def hydrate_staging_index(conn, store: SupabaseStore) -> None:
    """Restore the last published index into the ephemeral worker filesystem."""
    pages = store.select_all("ghbot_pages")
    chunks = store.select_all("ghbot_chunks")

    conn.execute("DELETE FROM chunks")
    conn.execute("DELETE FROM pages")
    conn.executemany(
        "INSERT INTO pages(page_id, source_label, last_edited_time) VALUES (?, ?, ?)",
        [(row["page_id"], row.get("source_label", ""), row.get("last_edited_time")) for row in pages],
    )
    conn.executemany(
        """INSERT INTO chunks(
            page_id, url, title, source_label, chunk_index, chunk_text, embedding, last_edited
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                row["page_id"],
                row.get("url"),
                row.get("title"),
                row.get("source_label", ""),
                row["chunk_index"],
                row.get("chunk_text", ""),
                sqlite3.Binary(np.asarray(row.get("embedding") or [], dtype="float32").tobytes()),
                row.get("last_edited"),
            )
            for row in chunks
        ],
    )
    conn.commit()
    print(f"hydrated staging index from Supabase: {len(pages)} pages, {len(chunks)} chunks")


def chunk_text(text: str) -> list[str]:
    """Split on heading lines first, then hard-wrap oversized sections."""
    sections: list[str] = []
    current: list[str] = []
    for line in text.split("\n"):
        if line.startswith("#") and current:
            sections.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append("\n".join(current))

    chunks: list[str] = []
    for section in sections:
        section = section.strip()
        if not section:
            continue
        if len(section) <= MAX_CHUNK_CHARS:
            chunks.append(section)
            continue
        # hard-wrap by paragraph for oversized sections
        buf = ""
        for para in section.split("\n"):
            if len(buf) + len(para) > MAX_CHUNK_CHARS and buf:
                chunks.append(buf.strip())
                buf = ""
            buf += para + "\n"
        if buf.strip():
            chunks.append(buf.strip())
    return chunks


class Indexer:
    def __init__(self, conn, model):
        self.conn = conn
        self.model = model
        self.seen_this_run: dict[str, set[str]] = {}
        self.successfully_enumerated: set[str] = set()
        self.unchanged = self.updated = self.skipped = 0

    def mark_enumerated(self, label: str) -> None:
        """Allow stale-row cleanup only after a complete source listing."""
        self.successfully_enumerated.add(label)

    def index_document(self, doc_id: str, url: str, title: str, label: str, edited: str, fetch_body):
        """fetch_body is a zero-arg callable so unchanged documents skip the
        (often slow/rate-limited) network fetch entirely."""
        self.seen_this_run.setdefault(label, set()).add(doc_id)

        prev = self.conn.execute(
            "SELECT last_edited_time FROM pages WHERE page_id = ?", (doc_id,)
        ).fetchone()
        if prev and prev[0] == edited:
            self.unchanged += 1
            return

        try:
            body = fetch_body()
        except (NotionAccessError, RuntimeError) as e:
            print(f"  [skip] {title}: fetch failed - {e}")
            self.skipped += 1
            return
        if not body or not body.strip():
            print(f"  [skip] {title}: empty body")
            self.skipped += 1
            return

        pieces = chunk_text(body)
        if not pieces:
            self.skipped += 1
            return
        embeddings = list(self.model.embed(pieces))
        self.conn.execute("DELETE FROM chunks WHERE page_id = ?", (doc_id,))
        for idx, (piece, emb) in enumerate(zip(pieces, embeddings)):
            self.conn.execute(
                "INSERT INTO chunks (page_id, url, title, source_label, chunk_index, chunk_text, embedding, last_edited) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (doc_id, url, title, label, idx, piece, emb.astype("float32").tobytes(), edited),
            )
        self.conn.execute(
            "INSERT INTO pages (page_id, source_label, last_edited_time) VALUES (?, ?, ?) "
            "ON CONFLICT(page_id) DO UPDATE SET last_edited_time = excluded.last_edited_time",
            (doc_id, label, edited),
        )
        self.conn.commit()  # commit per document so a later crash doesn't lose earlier progress
        self.updated += 1
        print(f"  [updated] {title} - {len(pieces)} chunks")

    def sweep_removed(self):
        removed = 0
        for label in self.successfully_enumerated:
            ids = self.seen_this_run.get(label, set())
            stale = self.conn.execute(
                "SELECT page_id FROM pages WHERE source_label = ?", (label,)
            ).fetchall()
            for (pid,) in stale:
                if pid not in ids:
                    self.conn.execute("DELETE FROM chunks WHERE page_id = ?", (pid,))
                    self.conn.execute("DELETE FROM pages WHERE page_id = ?", (pid,))
                    removed += 1
        self.conn.commit()
        return removed


def index_notion(indexer: Indexer, notion: NotionClient):
    for source in NOTION_SOURCES:
        label = source["label"]
        indexer.seen_this_run.setdefault(label, set())
        try:
            rows = notion.query_database(
                source["data_source_id"],
                filter_=source.get("filter"),
                page_size=100,
                max_rows=5000,
            )
        except NotionAccessError as e:
            print(f"[skip] {label}: not accessible yet - {e}")
            continue

        # A failed listing must not be treated as an empty database: preserve
        # the last good index until a complete listing succeeds.
        indexer.mark_enumerated(label)

        print(f"[{label}] {len(rows)} pages found")
        for row in rows:
            title = row.get(source["title_prop"], "") or "(untitled)"
            page_id = row["id"]
            indexer.index_document(
                page_id, row.get("url"), title, label, row.get("last_edited_time"),
                lambda pid=page_id: notion.fetch_page_text(pid, max_blocks=1000),
            )


def run(
    *,
    full_rebuild: bool = False,
    upload_only: bool = False,
    skip_notion: bool = False,
):
    """Run one incremental build and publish pass."""
    conn = sqlite3.connect(DB_PATH)
    store = SupabaseStore()
    if upload_only:
        purge_drive_index(conn)
        publish_sqlite_index(conn, store)
        conn.close()
        return {"published": True, "upload_only": True, "db_path": str(DB_PATH)}
    if full_rebuild:
        conn.execute("DROP TABLE IF EXISTS chunks")
        conn.execute("DROP TABLE IF EXISTS pages")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id TEXT,
            url TEXT,
            title TEXT,
            source_label TEXT,
            chunk_index INTEGER,
            chunk_text TEXT,
            embedding BLOB,
            last_edited TEXT
        )
        """
    )
    # migrate older DBs that predate the last_edited column
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
    if "last_edited" not in existing_cols:
        conn.execute("ALTER TABLE chunks ADD COLUMN last_edited TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pages (
            page_id TEXT PRIMARY KEY,
            source_label TEXT,
            last_edited_time TEXT
        )
        """
    )
    conn.commit()
    if store.private_enabled and not full_rebuild:
        hydrate_staging_index(conn, store)

    # Hydration may restore legacy Drive chunks.  Remove them before either
    # building or publishing so the next successful sync cleans Supabase too.
    purge_drive_index(conn)

    from fastembed import TextEmbedding

    model = TextEmbedding(model_name=MODEL_NAME)
    indexer = Indexer(conn, model)

    if not skip_notion:
        index_notion(indexer, NotionClient())

    removed = indexer.sweep_removed()
    total_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    if store.private_enabled:
        publish_sqlite_index(conn, store)
    else:
        print("warning: private SUPABASE_KEY not set; index was only written locally")
    summary = {
        "updated": indexer.updated,
        "unchanged": indexer.unchanged,
        "skipped": indexer.skipped,
        "removed": removed,
        "total_chunks": total_chunks,
        "db_path": str(DB_PATH),
        "published": store.private_enabled,
        "drive_embedded": False,
    }
    print(
        f"done - {indexer.updated} updated, {indexer.unchanged} unchanged (skipped re-embed), "
        f"{indexer.skipped} skipped (error/empty), {removed} removed - {total_chunks} chunks total in {DB_PATH}"
    )
    conn.close()
    return summary


def main():
    run(
        full_rebuild="--full" in sys.argv,
        upload_only="--upload-only" in sys.argv,
    )


if __name__ == "__main__":
    main()
