import os
import sqlite3
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from fastembed import TextEmbedding
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse

from notion_client import (
    NotionClient,
    PROJECTS_DB_ID,
    COMPANIES_DB_ID,
    ARCHIVE_DB_ID,
    GUIDES_DB_ID,
)

EMBED_DB_PATH = Path(__file__).parent / "gh_bot.db"
EMBED_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"  # must match embed_index.py

# Explicit path so this loads correctly no matter what directory the MCP
# client launches this process from.
load_dotenv(dotenv_path=Path(__file__).parent / ".env")

USAGE_GUIDE = """\
이 서버는 GH(Growth Hackers) 학회 노션을 조회하는 도구입니다. 질문 성격에 따라
아래 순서로 도구를 고르세요 - 얕은 요약으로 끝내지 말고, 구체적인 답이 필요하면
반드시 fetch_page로 본문까지 읽고 인용하세요.

1. 기업명/기술분류/분기/방법론 태그/규칙 종류처럼 "필드로 정확히 좁혀지는" 질문
   -> query_projects / query_archive / query_guides 를 먼저 쓰세요.
   -> 결과가 나오면, 한줄설명만으로 부족하면 fetch_page로 해당 페이지 본문을 읽어서
      구체적 근거(문제 정의, 의사결정 이유, 인원별 역할 등)를 확인한 뒤 답하세요.

2. 여러 프로젝트/문서를 종합해야 하는 질문("보통 어떻게 하는지", "사례들을 종합하면")
   -> 1번으로 후보를 좁힌 뒤, 후보 각각에 fetch_page를 호출해 본문을 교차 확인하세요.
      후보가 1~2개라고 답을 끝내지 말고, 관련 있어 보이는 건 다 열어보세요.

3. 제목/키워드로만 찾아지는 질문(태그가 없는 문서, 팀 소개, 템플릿 등)
   -> search_pages로 찾고, 마찬가지로 fetch_page로 본문을 확인하세요.

4. 방법론/의사결정 맥락을 종합해야 하거나, 태그로 안 잡히는 서술형 질문
   ("보통 어떻게 하는지", "비슷한 문제의식 가진 프로젝트", 회의록 관련 등)
   -> semantic_search를 먼저 쓰세요. 프로젝트 백서(완료된 프로젝트)와 진행중
      프로젝트 본문을 모두 검색합니다. 결과로 나온 스니펫이 부분적이면 fetch_page로
      해당 page_id 전체 본문을 마저 읽고 답하세요.
   -> 회의록은 아직 이 인덱스에 없습니다 - search_pages로 후보를 찾은 뒤
      fetch_page로 본문 전체를 읽고 직접 인용을 포함해 답하세요.

5. GH 노션과 무관한 일반 기술/지식 질문은 도구를 쓰지 말고 바로 답하세요.

공통 원칙: 답변에는 근거가 된 페이지의 url을 함께 제시하세요. 확실하지 않으면
"찾은 자료 안에서는 확인되지 않음"이라고 명시하고, 없는 내용을 지어내지 마세요.
"""

mcp = MCPServer(name="gh-notion-bot", instructions=USAGE_GUIDE)
notion = NotionClient()


@mcp.custom_route("/health", methods=["GET"])
async def health_check(_request: Request) -> JSONResponse:
    """Railway liveness/readiness endpoint."""
    return JSONResponse({"status": "ok"})


def _and(*filters):
    filters = [f for f in filters if f]
    if not filters:
        return None
    if len(filters) == 1:
        return filters[0]
    return {"and": filters}


@mcp.tool()
def search_pages(query: str) -> list[dict]:
    """Keyword/title search across Notion pages shared with this integration."""
    return notion.search(query)


@mcp.tool()
def query_projects(company: str = "", tech: str = "", quarter: str = "") -> list[dict]:
    """Look up GH project records by client/company name, 기술분류 (e.g. '추천시스템'), or 분기 (e.g. '26.3Q').

    Company names live in a separate Companies database and are linked via a
    relation, so a company name is resolved to its page id first.
    """
    company_filter = None
    if company:
        matches = notion.query_database(
            COMPANIES_DB_ID,
            filter_={"property": "Name", "title": {"contains": company}},
        )
        if not matches:
            return []
        company_filter = {
            "or": [{"property": "기업", "relation": {"contains": m["id"]}} for m in matches]
        }

    filter_ = _and(
        company_filter,
        {"property": "기술분류", "multi_select": {"contains": tech}} if tech else None,
        {"property": "분기", "select": {"equals": quarter}} if quarter else None,
    )
    rows = notion.query_database(PROJECTS_DB_ID, filter_=filter_)

    company_ids = [cid for r in rows for cid in r.get("기업", [])]
    company_names = notion.resolve_titles(company_ids) if company_ids else {}
    for r in rows:
        r["기업명"] = [company_names.get(cid, cid) for cid in r.get("기업", [])]
    return rows


@mcp.tool()
def query_archive(topic_tag: str = "", project_page_id: str = "") -> list[dict]:
    """Look up project archive/whitepaper entries by methodology tag (e.g. 'Sequential', 'RecSys') or linked project page id."""
    filter_ = _and(
        {"property": "Topic", "multi_select": {"contains": topic_tag}} if topic_tag else None,
        {"property": "Project", "relation": {"contains": project_page_id}} if project_page_id else None,
    )
    return notion.query_database(ARCHIVE_DB_ID, filter_=filter_)


@mcp.tool()
def query_guides(kind: str = "", owner: str = "", tag: str = "") -> list[dict]:
    """Look up rules/templates/handover docs by 종류 (규칙/템플릿/인수인계/...), 책임자, or 태그."""
    filter_ = _and(
        {"property": "종류", "select": {"equals": kind}} if kind else None,
        {"property": "책임자", "select": {"equals": owner}} if owner else None,
        {"property": "태그", "multi_select": {"contains": tag}} if tag else None,
    )
    return notion.query_database(GUIDES_DB_ID, filter_=filter_)


@mcp.tool()
def fetch_page(page_id: str) -> str:
    """Fetch a Notion page's body as plain text, for reading a specific whitepaper/meeting note in full."""
    return notion.fetch_page_text(page_id)


_embed_model: TextEmbedding | None = None
_chunk_cache: list[dict] | None = None


def _load_semantic_index():
    global _embed_model, _chunk_cache
    if _embed_model is None:
        _embed_model = TextEmbedding(model_name=EMBED_MODEL_NAME)
    if _chunk_cache is None:
        conn = sqlite3.connect(EMBED_DB_PATH)
        rows = conn.execute(
            "SELECT page_id, url, title, source_label, chunk_text, embedding FROM chunks"
        ).fetchall()
        conn.close()
        _chunk_cache = [
            {
                "page_id": page_id,
                "url": url,
                "title": title,
                "source_label": source_label,
                "chunk_text": chunk_text,
                "vector": np.frombuffer(embedding, dtype="float32"),
            }
            for page_id, url, title, source_label, chunk_text, embedding in rows
        ]
    return _embed_model, _chunk_cache


@mcp.tool()
def semantic_search(query: str, top_k: int = 5, source: str = "") -> list[dict]:
    """Search embedded project whitepaper / in-progress project bodies by meaning, not keywords.

    Use for questions that need synthesis across documents or don't map to a
    clean field/tag (e.g. "similar problem framing across projects"). Does NOT
    yet cover meeting notes. Optional `source` filters to a label prefix, e.g.
    '프로젝트_백서' or '진행중_프로젝트'.
    """
    model, chunks = _load_semantic_index()
    if not chunks:
        return []
    query_vec = next(model.embed([query]))
    pool = [c for c in chunks if not source or c["source_label"].startswith(source)]

    def cos_sim(a, b):
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))

    scored = sorted(
        ({**c, "score": cos_sim(query_vec, c["vector"])} for c in pool),
        key=lambda c: c["score"],
        reverse=True,
    )[:top_k]
    return [
        {
            "page_id": c["page_id"],
            "url": c["url"],
            "title": c["title"],
            "source_label": c["source_label"],
            "text": c["chunk_text"],
            "score": round(c["score"], 3),
        }
        for c in scored
    ]


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        mcp.run(
            transport="streamable-http",
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8000")),
            streamable_http_path=os.getenv("MCP_PATH", "/mcp"),
        )
    elif transport == "stdio":
        mcp.run()
    else:
        raise ValueError(
            f"Unsupported MCP_TRANSPORT={transport!r}; use 'stdio' or 'streamable-http'"
        )
