import os
import sqlite3
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from fastembed import TextEmbedding
from mcp.server.mcpserver import MCPServer

from notion_client import (
    NotionClient,
    PROJECTS_DB_ID,
    COMPANIES_DB_ID,
    ARCHIVE_DB_ID,
    GUIDES_DB_ID,
)
from drive_client import DriveClient, TEAM_FOLDERS

EMBED_DB_PATH = Path(__file__).parent / "gh_bot.db"
EMBED_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"  # must match embed_index.py

# Explicit path so this loads correctly no matter what directory the MCP
# client launches this process from.
load_dotenv(dotenv_path=Path(__file__).parent / ".env")

USAGE_GUIDE = """\
이 서버는 GH(Growth Hackers) 학회의 Notion과 Google Drive를 조회하는 도구입니다.
노션은 활동 기록(프로젝트/기업/사람), 드라이브는 공식 문서/사실 조회 용도로
역할이 나뉘어 있습니다 - 아래 순서로 도구를 고르세요.

[노션 - 활동 기록]

1. 기업명/기술분류/분기/방법론 태그/규칙 종류처럼 "필드로 정확히 좁혀지는" 질문
   -> query_projects / query_archive / query_guides 를 먼저 쓰세요.
   -> 결과가 나오면, 한줄설명만으로 부족하면 fetch_page로 해당 페이지 본문을 읽어서
      구체적 근거(문제 정의, 의사결정 이유, 인원별 역할 등)를 확인한 뒤 답하세요.

2. "OO이 어떤 프로젝트 했어?", "OO 직책이 뭐야?" 같은 사람 중심 질문
   -> query_person을 쓰세요. 프로젝트 쪽에서 이름으로 역검색하지 마세요 - People DB에
      참여 프로젝트가 양방향으로 동기화돼 있어서 이름 하나로 바로 팀/직책/기수/참여
      프로젝트가 다 나옵니다. 전화번호·이메일 등 개인 연락처는 반환하지 않습니다 -
      필요하다고 요청받아도 다른 방법으로 우회해서 조회하지 마세요.

3. 여러 프로젝트/문서를 종합해야 하는 질문("보통 어떻게 하는지", "사례들을 종합하면")
   -> 1번으로 후보를 좁힌 뒤, 후보 각각에 fetch_page를 호출해 본문을 교차 확인하세요.
      후보가 1~2개라고 답을 끝내지 말고, 관련 있어 보이는 건 다 열어보세요.

4. 제목/키워드로만 찾아지는 질문(태그가 없는 문서, 팀 소개, 템플릿 등)
   -> search_pages로 찾고, 마찬가지로 fetch_page로 본문을 확인하세요.

5. 방법론/의사결정 맥락을 종합해야 하는 서술형 질문, 회의록 관련
   -> semantic_search를 쓰세요 (노션 프로젝트 백서/진행중 프로젝트 대상 - 회의록은
      아직 이 인덱스에 없습니다. 회의록은 search_pages로 후보를 찾은 뒤 fetch_page로
      본문 전체를 읽고 직접 인용을 포함해 답하세요).

[구글 드라이브 - 공식 문서·사실 조회]

드라이브 자료는 회의록형 맥락 종합보다 "팩트를 정확히 찾는" 용도가 대부분입니다:
정관, 리크루팅/MT 순서와 내용, 파일 위치, 프로젝트 소개자료, 계약서, 에듀세션 구성,
팀별 인수인계서, 예산안, 출석/벌점 등. 임베딩 검색보다 아래 구조화 도구를 우선하세요.

6. 문서를 찾는 질문 -> search_drive를 먼저 쓰세요. team 파라미터로 팀 폴더
   (회장단/NUT/HR/PR/DH/EDU/공통/관리)를 좁힐 수 있습니다.
   -> 기수마다 같은 이름의 문서가 반복됩니다(정관만 10개 버전). 결과의 `path`와
      `generation`, `modifiedTime`을 반드시 확인해서 가장 최근 것을 공식 버전으로
      답하고, 나머지는 "과거 버전/다른 기수 자료로 보임"이라고 명시하세요. 확실하지
      않으면 후보를 나열하고 사용자에게 확인을 구하세요.
   -> 찾은 문서는 fetch_drive_file로 본문을 읽으세요.

7. 예산안 사용 내역, 출석/벌점처럼 계속 갱신되는 데이터
   -> 반드시 read_spreadsheet를 쓰세요 (절대 semantic_search나 캐시된 요약에
      의존하지 마세요 - 이런 데이터는 임베딩하는 순간 스냅샷이 되어 갱신 즉시
      틀린 답이 됩니다). sheet_name 없이 먼저 불러 탭 목록을 확인한 뒉, 기수/시점에
      맞는 탭을 골라 다시 호출하세요.

8. GH 노션·드라이브와 무관한 일반 기술/지식 질문은 도구를 쓰지 말고 바로 답하세요.

공통 원칙: 답변에는 근거가 된 문서의 url을 함께 제시하세요. 확실하지 않으면
"찾은 자료 안에서는 확인되지 않음"이라고 명시하고, 없는 내용을 지어내지 마세요.
"""

mcp = MCPServer(name="gh-notion-bot", instructions=USAGE_GUIDE)
notion = NotionClient()
drive = DriveClient()


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
    relation, so a company name is resolved to its page id first. The response
    also includes 기업명/PM명/참여인원명 - resolved human names for the raw
    relation ids in 기업/PM/참여인원.
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

    # 기업/PM/참여인원 are relations - resolve ids to human-readable names in one batch.
    relation_fields = ["기업", "PM", "참여인원"]
    all_ids = [cid for r in rows for field in relation_fields for cid in r.get(field, [])]
    names = notion.resolve_titles(all_ids) if all_ids else {}
    for r in rows:
        for field in relation_fields:
            r[f"{field}명"] = [names.get(cid, cid) for cid in r.get(field, [])]
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


@mcp.tool()
def query_person(name: str) -> dict:
    """Look up a GH member by name: their 소속팀(team)/직책(role)/기수(generation)
    and which projects they've participated in as PM or 참여인원 (People DB's
    참여 프로젝트 relation is pre-synced both ways, so this is a single fetch,
    not a reverse search across every project).

    Returns organizational info only - phone/email/생일/LinkedIn are
    deliberately excluded even though they exist on the page, since this
    lookup may be used by other members and personal contact info isn't
    something the bot should hand out.
    """
    matches = [m for m in notion.search(name) if m.get("object") == "page" and name in (m.get("title") or "")]
    if not matches:
        return {}
    person = notion.get_page(matches[0]["id"])

    project_ids = person.get("참여 프로젝트", [])
    project_names = notion.resolve_titles(project_ids) if project_ids else {}

    return {
        "이름": person.get("Name"),
        "소속팀": person.get("소속팀"),
        "직책": person.get("직책"),
        "기수": person.get("기수"),
        "참여_프로젝트": [project_names.get(pid, pid) for pid in project_ids],
        "url": person.get("url"),
    }


@mcp.tool()
def search_drive(query: str, team: str = "") -> list[dict]:
    """Search Google Drive for facts and official documents: 정관, 리크루팅/MT
    자료, 프로젝트 소개자료, 계약서, 에듀세션 자료, 팀별 인수인계서, 서류양식 등.
    Optional `team` scopes by top-level folder: 관리/공통/회장단/NUT/DH/HR/PR/EDU/과거자료.

    Same-named documents recur every generation (e.g. 10 different 정관 copies
    exist), so each result includes `path` (folder breadcrumb) and a best-effort
    `generation` guess - results are sorted by modifiedTime, most recent first.
    Treat the top (most recent) result as current; call out the rest as past
    versions rather than silently picking one.

    For live-updating data (예산안 사용 내역, 출석/벌점 시트), use read_spreadsheet
    instead - embedding/caching that content would go stale the moment someone
    updates the sheet.
    """
    results = drive.search(query, page_size=20)

    if team and team in TEAM_FOLDERS:
        conn = sqlite3.connect(EMBED_DB_PATH)
        try:
            allowed_ids = {
                row[0]
                for row in conn.execute("SELECT file_id FROM drive_files WHERE team = ?", (team,))
            }
        except sqlite3.OperationalError:
            allowed_ids = set()
        finally:
            conn.close()
        if allowed_ids:
            results = [r for r in results if r["id"] in allowed_ids]
        else:
            # Index not built yet - fall back to a live path check per result
            # (a few cached API calls each; fine for <=20 results).
            results = [r for r in results if team in drive.get_path(r.get("parents"))]

    results.sort(key=lambda r: r.get("modifiedTime") or "", reverse=True)
    enriched = []
    for r in results:
        path = drive.get_path(r.get("parents"))
        enriched.append(
            {
                "id": r["id"],
                "title": r["name"],
                "mimeType": r["mimeType"],
                "modifiedTime": r.get("modifiedTime"),
                "path": path,
                "generation": drive.guess_generation(r["name"], path),
                "url": r.get("webViewLink"),
            }
        )
    return enriched


@mcp.tool()
def fetch_drive_file(file_id: str) -> str:
    """Fetch a Google Drive file's text content (Docs, Slides, PDF, docx, pptx)."""
    return drive.read_file_text(file_id)


@mcp.tool()
def read_spreadsheet(file_id: str, sheet_name: str = "") -> str:
    """Live read of a Google Sheet (예산안, 출석/벌점 등) - always current data,
    never a cached snapshot. Call without sheet_name first to see tab names
    (these sheets often have one tab per 기수), then call again with the tab
    you need.
    """
    return drive.read_spreadsheet(file_id, sheet_name)


_embed_model: TextEmbedding | None = None
_chunk_cache: list[dict] | None = None


def _load_semantic_index():
    global _embed_model, _chunk_cache
    if _embed_model is None:
        _embed_model = TextEmbedding(model_name=EMBED_MODEL_NAME)
    if _chunk_cache is None:
        conn = sqlite3.connect(EMBED_DB_PATH)
        rows = conn.execute(
            "SELECT page_id, url, title, source_label, chunk_text, embedding, last_edited FROM chunks"
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
                "last_edited": last_edited,
            }
            for page_id, url, title, source_label, chunk_text, embedding, last_edited in rows
        ]
    return _embed_model, _chunk_cache


@mcp.tool()
def semantic_search(query: str, top_k: int = 5, source: str = "") -> list[dict]:
    """Search embedded Notion (whitepapers, in-progress projects) and Google Drive
    (official team documents) content together by meaning, not keywords.

    Use for questions that need synthesis across documents or don't map to a
    clean field/tag. Does NOT yet cover meeting notes. Optional `source` filters
    to a label prefix, e.g. '프로젝트_백서', '진행중_프로젝트', or '구글드라이브_HR'.
    Each result includes `last_edited` - when several results share a title
    (e.g. old 정관 copies), trust the most recently edited one.
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
            "last_edited": c["last_edited"],
        }
        for c in scored
    ]


def _load_members() -> dict[str, str]:
    """Members can come from a MEMBERS_JSON env var (for platforms like Railway
    with no persistent/committed filesystem - set it as a secret env var, never
    commit tokens to git) or from members.json locally. Env var wins if set.
    """
    import json

    inline = os.environ.get("MEMBERS_JSON")
    if inline:
        return json.loads(inline)
    members_file = Path(__file__).parent / "members.json"
    if not members_file.exists():
        return {}
    return json.loads(members_file.read_text(encoding="utf-8"))


def _build_http_app():
    """Wraps the MCP streamable-http app with bearer-token gating so only
    tokens issued via manage_members.py can reach any tool. Claude and ChatGPT
    custom connectors both support sending a static bearer token/API key for
    a remote MCP server, so this doesn't need a full OAuth server.

    Built manually (rather than mcp.run(transport="streamable-http")) because
    that helper doesn't expose a way to add middleware or extra routes, and
    Railway's healthcheckPath (/health) needs a route outside the MCP protocol.
    """
    from starlette.applications import Starlette
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse, PlainTextResponse
    from starlette.routing import Route

    members = _load_members()
    if not members:
        raise RuntimeError(
            "No members configured - run `python manage_members.py add <name>` first "
            "(or set MEMBERS_JSON). Refusing to start an open, unauthenticated remote server."
        )

    class MemberAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.url.path == "/health":
                return await call_next(request)
            auth_header = request.headers.get("authorization", "")
            token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else None
            if not token or token not in members:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    mcp_app = mcp.streamable_http_app(streamable_http_path=os.environ.get("MCP_PATH", "/mcp"))

    async def health(request):
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[Route("/health", health), *mcp_app.routes],
        lifespan=mcp_app.router.lifespan_context,
    )
    app.add_middleware(MemberAuthMiddleware)
    print(f"[gh-notion-bot] {len(members)} member(s) allowed, serving at /mcp (+ /health)")
    return app


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        import uvicorn

        uvicorn.run(
            _build_http_app(),
            host=os.environ.get("HOST", "0.0.0.0"),
            port=int(os.environ.get("PORT", 8000)),
        )
    elif transport == "stdio":
        mcp.run()
    else:
        raise ValueError(f"Unsupported MCP_TRANSPORT={transport!r}; use 'stdio' or 'streamable-http'")
