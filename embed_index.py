"""Builds/updates the SQLite semantic index for content that needs context
search rather than exact-field filtering (meeting notes, deliberation-heavy
docs, official Drive documents). Run manually or on a schedule:
`python embed_index.py`.

Incremental: each document's last_edited timestamp is tracked in the `pages`
table. A document is only re-fetched/re-chunked/re-embedded if that timestamp
changed since the last run - unchanged documents are skipped, which matters
once meeting notes / more Drive folders push this into the hundreds+ range.
Pass --full to force a full rebuild (drops both tables first).
"""
import sys
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from fastembed import TextEmbedding

from notion_client import NotionClient, NotionAccessError, ARCHIVE_DB_ID, PROJECTS_DB_ID
from drive_client import DriveClient, TEAM_FOLDERS

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

DB_PATH = Path(__file__).parent / "gh_bot.db"
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# Notion sources that need context/semantic search rather than field filtering.
# Add a data_source_id here once its database is shared with the integration
# ("..." -> Connections -> ghbot 연결 in Notion).
NOTION_SOURCES = [
    {"label": "프로젝트_백서(Archive)", "data_source_id": ARCHIVE_DB_ID, "title_prop": "Task name"},
    {"label": "진행중_프로젝트(Projects)", "data_source_id": PROJECTS_DB_ID, "title_prop": "Name"},
    # {"label": "운영진_회의록", "data_source_id": "<fill in once shared>", "title_prop": "Name"},
    # {"label": "회장단_회의록", "data_source_id": "<fill in once shared>", "title_prop": "Name"},
]

MAX_CHUNK_CHARS = 800


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
        self.unchanged = self.updated = self.skipped = 0

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
        for label, ids in self.seen_this_run.items():
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
            rows = notion.query_database(source["data_source_id"], page_size=100)
        except NotionAccessError as e:
            print(f"[skip] {label}: not accessible yet - {e}")
            continue

        print(f"[{label}] {len(rows)} pages found")
        for row in rows:
            title = row.get(source["title_prop"], "") or "(untitled)"
            page_id = row["id"]
            indexer.index_document(
                page_id, row.get("url"), title, label, row.get("last_edited_time"),
                lambda pid=page_id: notion.fetch_page_text(pid, max_blocks=1000),
            )


def index_drive(indexer: Indexer, drive: DriveClient):
    # file_id -> team, so search_drive(team=...) can do a fast local lookup
    # instead of a live recursive Drive folder walk. Resolved from one bulk
    # listing (list_all_files -> resolve_file_teams) instead of one API call
    # per folder, which is what made the earlier version take minutes.
    indexer.conn.execute(
        "CREATE TABLE IF NOT EXISTS drive_files (file_id TEXT PRIMARY KEY, team TEXT)"
    )
    print("[drive] listing all files...")
    all_files = drive.list_all_files()
    file_team, by_id = drive.resolve_file_teams(all_files)
    print(f"[drive] {len(all_files)} items total, {len(file_team)} resolved to a team")

    by_team: dict[str, list[dict]] = {}
    for file_id, team in file_team.items():
        by_team.setdefault(team, []).append(by_id[file_id])

    for team, files in by_team.items():
        label = f"구글드라이브_{team}"
        indexer.seen_this_run.setdefault(label, set())
        print(f"[{label}] {len(files)} files found")
        for f in files:
            indexer.conn.execute(
                "INSERT INTO drive_files (file_id, team) VALUES (?, ?) "
                "ON CONFLICT(file_id) DO UPDATE SET team = excluded.team",
                (f["id"], team),
            )
            indexer.index_document(
                f["id"], f.get("webViewLink"), f["name"], label, f.get("modifiedTime"),
                lambda fid=f["id"], mt=f["mimeType"]: drive.read_file_text(fid, mt),
            )
    indexer.conn.commit()


def main():
    full_rebuild = "--full" in sys.argv
    skip_notion = "--drive-only" in sys.argv
    skip_drive = "--notion-only" in sys.argv

    conn = sqlite3.connect(DB_PATH)
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

    model = TextEmbedding(model_name=MODEL_NAME)
    indexer = Indexer(conn, model)

    if not skip_notion:
        index_notion(indexer, NotionClient())
    if not skip_drive:
        index_drive(indexer, DriveClient())

    removed = indexer.sweep_removed()
    total_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    print(
        f"done - {indexer.updated} updated, {indexer.unchanged} unchanged (skipped re-embed), "
        f"{indexer.skipped} skipped (error/empty), {removed} removed - {total_chunks} chunks total in {DB_PATH}"
    )


if __name__ == "__main__":
    main()
