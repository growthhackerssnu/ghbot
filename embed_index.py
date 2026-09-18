"""Builds/rebuilds the SQLite semantic index for content that needs context
search rather than exact-field filtering (meeting notes, deliberation-heavy
docs). Run manually or on a schedule: `python embed_index.py`.

V1 does a full rebuild each run - the corpus is small (tens to low hundreds
of pages) so this takes seconds. Incremental re-embedding by last_edited_time
is a straightforward follow-up once the corpus grows.
"""
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from fastembed import TextEmbedding

from notion_client import NotionClient, NotionAccessError, ARCHIVE_DB_ID, PROJECTS_DB_ID

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

DB_PATH = Path(__file__).parent / "gh_bot.db"
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# Sources that need context/semantic search rather than field filtering.
# Add a data_source_id here once its database is shared with the integration
# ("..." -> Connections -> ghbot 연결 in Notion).
SOURCES = [
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


def main():
    notion = NotionClient()
    model = TextEmbedding(model_name=MODEL_NAME)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("DROP TABLE IF EXISTS chunks")
    conn.execute(
        """
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id TEXT,
            url TEXT,
            title TEXT,
            source_label TEXT,
            chunk_index INTEGER,
            chunk_text TEXT,
            embedding BLOB
        )
        """
    )

    total_chunks = 0
    for source in SOURCES:
        label = source["label"]
        try:
            rows = notion.query_database(source["data_source_id"], page_size=100)
        except NotionAccessError as e:
            print(f"[skip] {label}: not accessible yet - {e}")
            continue

        print(f"[{label}] {len(rows)} pages found")
        for i, row in enumerate(rows):
            title = row.get(source["title_prop"], "") or "(untitled)"
            page_id = row["id"]
            try:
                body = notion.fetch_page_text(page_id, max_blocks=1000)
            except (NotionAccessError, RuntimeError) as e:
                print(f"  [skip] {title}: {e}")
                continue
            if not body.strip():
                continue

            pieces = chunk_text(body)
            if not pieces:
                continue
            embeddings = list(model.embed(pieces))
            for idx, (piece, emb) in enumerate(zip(pieces, embeddings)):
                conn.execute(
                    "INSERT INTO chunks (page_id, url, title, source_label, chunk_index, chunk_text, embedding) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (page_id, row.get("url"), title, label, idx, piece, emb.astype("float32").tobytes()),
                )
                total_chunks += 1
            conn.commit()  # commit per page so a later crash doesn't lose earlier progress
            print(f"  ({i+1}/{len(rows)}) {title} - {len(pieces)} chunks")

    print(f"done - {total_chunks} chunks indexed into {DB_PATH}")


if __name__ == "__main__":
    main()
