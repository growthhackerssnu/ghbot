import os
import re
import time
from copy import deepcopy
from pathlib import Path

import numpy as np
from fastembed import TextEmbedding
from mcp.server.mcpserver import MCPServer

from notion_client import (
    NotionClient,
    NotionAccessError,
    PROJECTS_DB_ID,
    COMPANIES_DB_ID,
    GUIDES_DB_ID,
    PEOPLE_DB_ID,
    TASKS_DB_ID,
)
from drive_client import DriveClient, TEAM_FOLDERS
from config import load_env_file
from supabase_store import SupabaseStore

EMBED_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"  # must match embed_index.py
NOTION_ROOT_PAGE_ID = "3aa40bd1-0676-80f0-ab3e-e189c7b6380b"
NOTION_ROOT_TITLE = "Growth Hackers"

# Explicit path so this loads correctly no matter what directory the MCP
# client launches this process from.
load_env_file(Path(__file__).parent / ".env")
supabase = SupabaseStore()

USAGE_GUIDE = """\
이 서버는 GH(Growth Hackers) 학회의 Notion과 Google Drive를 조회하는 도구입니다.
노션은 활동 기록(프로젝트/기업/사람), 드라이브는 공식 문서/사실 조회 용도로
역할이 나뉘어 있습니다 - 아래 순서로 도구를 고르세요.

0. GH 내부 지식에 관한 질문을 받으면 먼저 get_workspace_catalog을 호출해
   팀/DB/문서 유형의 의미와 추천 scope를 확인하세요. 카탈로그의 scope_database와
   scope_filters를 search_notion_content에 그대로 넘길 수 있습니다. 질문에 날짜가
   명시되지 않았다면 날짜 필터를 임의로 추가하지 마세요.

[노션 - 활동 기록]

1. 기업명/기술분류/분기/방법론 태그/규칙 종류처럼 "필드로 정확히 좁혀지는" 질문
   -> query_database를 먼저 쓰세요. describe_database는 필드/옵션을 확인할 때만
      필요합니다.
   -> 결과가 나오면, 한줄설명만으로 부족하면 fetch_page로 해당 페이지 본문을 읽어서
      구체적 근거(문제 정의, 의사결정 이유, 인원별 역할 등)를 확인한 뒤 답하세요.

2. "OO이 어떤 프로젝트 했어?", "OO 직책이 뭐야?" 같은 사람 중심 질문
   -> query_database(database="people")로 사람 페이지를 찾고, 필요한 경우
      explore_pages에서 참여 프로젝트 relation을 따라가세요. 전화번호·이메일 등
      개인 연락처는 반환하지 않습니다.

3. 여러 프로젝트/문서를 종합해야 하는 질문("보통 어떻게 하는지", "사례들을 종합하면")
   -> 1번으로 후보를 좁힌 뒤, 후보 각각에 fetch_page를 호출해 본문을 교차 확인하세요.
      후보가 1~2개라고 답을 끝내지 말고, 관련 있어 보이는 건 다 열어보세요.

4. 제목/키워드로만 찾아지는 질문(태그가 없는 문서, 팀 소개, 템플릿 등)
   -> search_pages 또는 get_sitemap/list_pages로 위치를 확인하고, fetch_page로 본문을 읽으세요.

5. 회의록/본문에서 특정 키워드를 찾는 질문
   -> search_notion_content를 쓰세요. 회의록은 Tasks DB에서 종류=회의와
      담당부서(운영진/회장단 등)를 먼저 필터링한 뒤, 그 결과 페이지만 본문 검색하세요.
   -> 여러 문서의 방법론/의사결정 맥락을 종합하는 질문은 semantic_search를 쓰세요.

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

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from oauth_provider import MemberOAuthProvider, load_members

OAUTH_ISSUER_URL = os.environ.get("OAUTH_ISSUER_URL", "https://api.ghsnu.com")

mcp = MCPServer(
    name="gh-notion-bot",
    instructions=USAGE_GUIDE,
    auth_server_provider=MemberOAuthProvider(),
    auth=AuthSettings(
        issuer_url=OAUTH_ISSUER_URL,
        resource_server_url=OAUTH_ISSUER_URL,
        client_registration_options=ClientRegistrationOptions(enabled=True, default_scopes=["*"]),
        # Left unset (not True): our AccessToken.resource is often None (legacy
        # static tokens, and OAuth tokens when the client doesn't send a RFC8707
        # resource indicator) - _issued_for_this_resource() has no None-guard,
        # so enabling this would 500 on exactly those tokens. Revisit once every
        # token path reliably sets `resource`.
        validate_token_resource=False,
    ),
)
notion = NotionClient()
drive = DriveClient()

# These are stable aliases over Notion data-source IDs. The public API accepts
# the aliases so clients do not need to know Notion's internal identifiers.
# database_id is retained for sitemap/schema correlation; data_source_id is
# what the Notion query endpoint actually accepts.
DATABASE_REGISTRY = {
    "tasks": {
        "data_source_id": TASKS_DB_ID,
        "database_id": "3ae40bd1-0676-8076-8b51-cccd75bfe12d",
        "aliases": {"task", "tasks", "업무", "태스크"},
    },
    "companies": {
        "data_source_id": COMPANIES_DB_ID,
        "database_id": "3ae40bd1-0676-8088-9fe0-f7ce3f4b54d5",
        "aliases": {"company", "companies", "기업"},
    },
    "people": {
        "data_source_id": PEOPLE_DB_ID,
        "database_id": "3ab40bd1-0676-8018-921c-feb99ec75fe3",
        "aliases": {"person", "people", "member", "members", "사람", "멤버"},
    },
    "projects": {
        "data_source_id": PROJECTS_DB_ID,
        "database_id": "3ae40bd1-0676-8047-9aa2-d62acf91b87e",
        "aliases": {"project", "projects", "프로젝트"},
    },
    "guides": {
        "data_source_id": GUIDES_DB_ID,
        "database_id": "3b140bd1-0676-8045-9c2b-e634060ae251",
        "aliases": {"guide", "guides", "안내서", "가이드"},
    },
}

# Compact semantic routing metadata. This is intentionally separate from the
# raw sitemap: IDs/titles describe structure, while this catalog explains what
# each area means and which structured filters are authoritative.
WORKSPACE_CATALOG = {
    "version": 1,
    "overview": "Growth Hackers의 활동 기록은 Notion, 공식 사실/문서는 Google Drive에 있습니다.",
    "notion_databases": [
        {
            "name": "projects",
            "meaning": "기업과 함께 수행한 프로젝트의 분기, 기술, PM, 참여인원, 한줄설명과 본문",
            "use_for": ["어떤 프로젝트를 했는지", "기술 분류", "분기별 사례", "프로젝트 본문"],
            "key_fields": ["분기", "기술분류", "기업", "PM", "참여인원", "Name"],
        },
        {
            "name": "companies",
            "meaning": "협업 기업과 산업 분류, 홈페이지, 연결된 프로젝트",
            "use_for": ["기업 정보", "기업별 프로젝트"],
            "key_fields": ["산업 분류", "프로젝트", "홈페이지", "Name"],
        },
        {
            "name": "people",
            "meaning": "회원의 소속팀, 직책, 기수와 참여 프로젝트 relation",
            "use_for": ["사람의 조직 정보", "사람에서 프로젝트로 점프"],
            "key_fields": ["소속팀", "직책", "기수", "참여 프로젝트", "Name"],
        },
        {
            "name": "tasks",
            "meaning": "운영 업무와 회의록 인덱스. 회의/부서/날짜 필터가 가장 중요함",
            "use_for": ["운영진·회장단·팀별 회의록", "업무 일정", "리크루팅·수주·예산 업무"],
            "key_fields": ["종류", "담당부서", "날짜", "운영 기수", "진행상태", "Name"],
        },
        {
            "name": "guides",
            "meaning": "안내서, 규칙, 인수인계, 계획서, 템플릿과 아카이빙 문서",
            "use_for": ["운영 방법", "규칙·내규", "팀 인수인계", "템플릿"],
            "key_fields": ["종류", "태그", "상태", "운영 기수", "Name"],
        },
    ],
    "domains": [
        {
            "name": "운영진/회장단",
            "meaning": "학회 전체 운영과 주요 의사결정",
            "scope_database": "tasks",
            "scope_filters": [
                {"and": [
                    {"field": "종류", "op": "equals", "value": "회의"},
                    {"or": [
                        {"field": "담당부서", "op": "contains", "value": "운영진"},
                        {"field": "담당부서", "op": "contains", "value": "회장단"},
                    ]},
                ]},
            ],
            "hints": ["법인", "정관", "조직", "의사결정", "전체 운영"],
        },
        {
            "name": "DH",
            "meaning": "대외협력, 기업 발굴, 수주, 계약, 프로젝트 커뮤니케이션",
            "scope_database": "tasks",
            "scope_filters": [{"field": "담당부서", "op": "contains", "value": "DH"}],
            "drive_team": "DH",
            "hints": ["수주", "계약", "기업", "파트너십", "대외협력"],
        },
        {
            "name": "NUT",
            "meaning": "내부 운영, 총무, 예산, 법인화와 정관 관련 공식 운영 자료",
            "drive_team": "NUT",
            "hints": ["예산", "정관", "법인", "총무", "내부 운영"],
        },
        {
            "name": "HR",
            "meaning": "리크루팅, 인사, 출석, 벌점, 알럼나이",
            "scope_database": "tasks",
            "scope_filters": [{"field": "담당부서", "op": "contains", "value": "HR"}],
            "drive_team": "HR",
            "hints": ["리크루팅", "면접", "출석", "벌점", "알럼나이"],
        },
        {
            "name": "PR",
            "meaning": "홈페이지, 인스타그램, 콘텐츠와 외부 홍보",
            "scope_database": "tasks",
            "scope_filters": [{"field": "담당부서", "op": "contains", "value": "PR"}],
            "drive_team": "PR",
            "hints": ["홈페이지", "인스타", "카드뉴스", "홍보"],
        },
        {
            "name": "EDU",
            "meaning": "교육 세션, 과제, 커리큘럼과 교육 자료",
            "drive_team": "EDU",
            "hints": ["교육", "세션", "과제", "커리큘럼"],
        },
    ],
    "routes": [
        {
            "intent": ["법인", "정관", "조직 구조", "전체 운영"],
            "scopes": ["운영진/회장단", "NUT"],
            "next": "search_notion_content 또는 search_drive",
        },
        {
            "intent": ["수주", "계약", "기업 협업", "파트너십"],
            "scopes": ["DH", "운영진/회장단"],
            "next": "search_notion_content와 search_drive를 둘 다 고려",
        },
        {
            "intent": ["리크루팅", "면접", "출석", "알럼나이"],
            "scopes": ["HR", "PR"],
            "next": "query_database 또는 read_spreadsheet",
        },
    ],
    "tool_guidance": {
        "get_sitemap": "원시 구조·ID·부모 관계를 확인할 때만 사용",
        "describe_database": "카탈로그에 없는 최신 필드·옵션이 필요할 때 사용",
        "search_notion_content": "DB 필터로 후보를 줄인 뒤 본문 키워드를 찾을 때 사용",
        "semantic_search": "여러 문서의 의미·방법론·의사결정을 종합할 때 사용",
    },
}
_schema_cache: dict[str, dict] = {}
_notion_scope_cache: dict[str, dict] | None = None


def _resolve_database(name: str) -> tuple[str, dict]:
    value = (name or "").strip().lower()
    for key, entry in DATABASE_REGISTRY.items():
        if value == key or value in {a.lower() for a in entry["aliases"]}:
            return key, entry
    valid = ", ".join(DATABASE_REGISTRY)
    raise ValueError(f"Unknown database {name!r}. Use one of: {valid}")


def _get_schema(key: str, entry: dict) -> dict:
    if key not in _schema_cache:
        _schema_cache[key] = notion.get_database_schema(entry["data_source_id"])
    return _schema_cache[key]


def _parent_id(node: dict) -> str | None:
    parent = node.get("parent") or {}
    return next((value for key, value in parent.items() if key.endswith("_id")), None)


def _get_notion_scope() -> dict[str, dict]:
    """Enumerate only objects descended from the Growth Hackers root.

    This deliberately walks the root's children instead of using Notion's
    workspace-wide search endpoint. Database rows are included as children of
    their database; nested page contents are discovered later through
    list_pages/fetch_page when the client asks for them.
    """
    global _notion_scope_cache
    if _notion_scope_cache is not None:
        return _notion_scope_cache

    scoped = {
        NOTION_ROOT_PAGE_ID: {
            "object": "page",
            "id": NOTION_ROOT_PAGE_ID,
            "title": NOTION_ROOT_TITLE,
            "url": f"https://www.notion.so/{NOTION_ROOT_PAGE_ID.replace('-', '')}",
            "parent": None,
        }
    }

    visited_pages: set[str] = set()

    def row_title(row: dict) -> str | None:
        for key in ("Name", "Task name", "Title"):
            if isinstance(row.get(key), str) and row[key]:
                return row[key]
        for key, value in row.items():
            if key in ("id", "url", "last_edited_time"):
                continue
            if isinstance(value, str) and value:
                return value
        return None

    def walk_page(page_id: str) -> None:
        if page_id in visited_pages:
            return
        visited_pages.add(page_id)
        for child in notion.list_page_children(page_id, page_size=100):
            child_id = child.get("id")
            if not child_id:
                continue
            parent = {"type": "page_id", "page_id": page_id}
            node = {
                "object": child.get("object"),
                "id": child_id,
                "title": child.get("title"),
                "url": child.get("url"),
                "parent": parent,
                "last_edited_time": child.get("last_edited_time"),
            }
            scoped[child_id] = node
            if child.get("object") == "database":
                info = notion.get_database_info(child_id)
                data_source_id = info.get("data_source_id")
                node["title"] = info.get("title") or node["title"]
                node["data_source_id"] = data_source_id
                if not data_source_id:
                    continue
                for row in notion.query_database(data_source_id, max_rows=5000):
                    scoped[row["id"]] = {
                        "object": "page",
                        "id": row["id"],
                        "title": row_title(row),
                        "url": row.get("url"),
                        "parent": {"type": "database_id", "database_id": child_id},
                        "last_edited_time": row.get("last_edited_time"),
                    }
            else:
                walk_page(child_id)

    walk_page(NOTION_ROOT_PAGE_ID)
    _notion_scope_cache = scoped
    return scoped


def _assert_notion_in_scope(page_id: str) -> None:
    if page_id not in _get_notion_scope():
        raise ValueError("The requested Notion page is outside the Growth Hackers root")


def _compile_filter(node: dict, properties: dict) -> dict:
    """Compile the small public filter DSL into Notion's filter format."""
    if "and" in node or "or" in node:
        logic = "and" if "and" in node else "or"
        children = node[logic]
        if not isinstance(children, list) or not children:
            raise ValueError(f"{logic} must contain a non-empty list")
        return {logic: [_compile_filter(child, properties) for child in children]}

    field = node.get("field")
    operator = node.get("op", node.get("operator"))
    if field not in properties:
        raise ValueError(f"Unknown property {field!r}. Available properties: {', '.join(properties)}")
    if not operator:
        raise ValueError(f"Filter for {field!r} is missing op")

    ptype = properties[field]["type"]
    operators = {
        "title": {"equals", "not_equals", "contains", "does_not_contain", "is_empty", "is_not_empty"},
        "rich_text": {"equals", "not_equals", "contains", "does_not_contain", "is_empty", "is_not_empty"},
        "select": {"equals", "not_equals", "is_empty", "is_not_empty"},
        "status": {"equals", "not_equals", "is_empty", "is_not_empty"},
        "multi_select": {"contains", "does_not_contain", "is_empty", "is_not_empty"},
        "relation": {"contains", "does_not_contain", "is_empty", "is_not_empty"},
        "people": {"contains", "does_not_contain", "is_empty", "is_not_empty"},
        "date": {"equals", "before", "after", "on_or_before", "on_or_after", "is_empty", "is_not_empty"},
        "number": {"equals", "not_equals", "greater_than", "greater_than_or_equal_to", "less_than", "less_than_or_equal_to", "is_empty", "is_not_empty"},
        "checkbox": {"equals", "not_equals"},
        "url": {"equals", "contains", "does_not_contain", "is_empty", "is_not_empty"},
        "email": {"equals", "contains", "does_not_contain", "is_empty", "is_not_empty"},
        "phone_number": {"equals", "contains", "does_not_contain", "is_empty", "is_not_empty"},
    }
    if ptype not in operators or operator not in operators[ptype]:
        raise ValueError(f"Operator {operator!r} is not supported for {field!r} ({ptype})")

    notion_operator = {
        "not_equals": "does_not_equal",
        "does_not_contain": "does_not_contain",
    }.get(operator, operator)
    value = node.get("value")
    if operator not in ("is_empty", "is_not_empty") and value is None:
        raise ValueError(f"Filter {field!r}/{operator!r} requires value")
    if ptype == "checkbox":
        value = bool(value)
    elif ptype == "relation" and isinstance(value, dict):
        value = value.get("id")
    return {"property": field, ptype: {notion_operator: value} if operator not in ("is_empty", "is_not_empty") else {notion_operator: True}}


def _compile_filters(filters: list[dict] | None, properties: dict) -> dict | None:
    if not filters:
        return None
    compiled = [_compile_filter(item, properties) for item in filters]
    return compiled[0] if len(compiled) == 1 else {"and": compiled}


def _compile_sorts(sorts: list[dict] | None, properties: dict) -> list[dict] | None:
    if not sorts:
        return None
    compiled = []
    for item in sorts:
        field = item.get("field")
        if field not in properties:
            raise ValueError(f"Unknown sort property {field!r}")
        direction = item.get("direction", "descending")
        if direction not in ("ascending", "descending"):
            raise ValueError("sort direction must be ascending or descending")
        compiled.append({"property": field, "direction": direction})
    return compiled


@mcp.tool()
def describe_database(database: str = "") -> dict:
    """Describe a supported database's fields, types, and select options.

    The schema is cached for the lifetime of the server. Call without a name
    to discover the stable aliases accepted by query_database.
    """
    if not database:
        return {
            "databases": [
                {"name": key, "aliases": sorted(entry["aliases"])}
                for key, entry in DATABASE_REGISTRY.items()
            ]
        }
    key, entry = _resolve_database(database)
    schema = _get_schema(key, entry)
    return {
        "database": key,
        "data_source_id": entry["data_source_id"],
        "database_id": entry["database_id"],
        "title": schema.get("title") or key,
        "properties": schema.get("properties", {}),
    }


@mcp.tool()
def get_workspace_catalog(include_live_schemas: bool = False) -> dict:
    """Return the compact semantic map for routing GH questions.

    This explains what each Notion database, team area, and document family
    means. It is intentionally smaller than get_sitemap, which is a raw
    structural inventory. Set include_live_schemas when current field options
    are needed in addition to the curated routing metadata.
    """
    catalog = deepcopy(WORKSPACE_CATALOG)
    catalog["database_aliases"] = {
        key: sorted(entry["aliases"])
        for key, entry in DATABASE_REGISTRY.items()
    }
    catalog["drive_teams"] = [
        {"name": name, "meaning": next(
            (domain["meaning"] for domain in catalog["domains"] if domain.get("drive_team") == name),
            "공식 문서·운영 자료",
        )}
        for name in TEAM_FOLDERS
    ]
    if include_live_schemas:
        catalog["live_schemas"] = {
            key: _get_schema(key, entry)
            for key, entry in DATABASE_REGISTRY.items()
        }
    return catalog


@mcp.tool()
def query_database(
    database: str,
    filters: list[dict] | None = None,
    sorts: list[dict] | None = None,
    page_size: int = 100,
    max_rows: int = 1000,
) -> list[dict]:
    """Query a supported Notion database with typed, database-aware filters.

    Each filter is {field, op, value}; multiple filters are ANDed. Use an
    {"or": [...]} or {"and": [...]} group for nested logic. Relation values
    must be Notion page IDs (or {"id": "..."}).
    """
    key, entry = _resolve_database(database)
    schema = _get_schema(key, entry)
    properties = schema.get("properties", {})
    return notion.query_database(
        entry["data_source_id"],
        filter_=_compile_filters(filters, properties),
        sorts=_compile_sorts(sorts, properties),
        page_size=min(max(page_size, 1), 100),
        max_rows=min(max(max_rows, 1), 5000),
    )


@mcp.tool()
def get_sitemap(
    source: str = "all",
    include_schema: bool = False,
    include_files: bool = False,
) -> dict:
    """Return a lightweight inventory scoped to the Growth Hackers root."""
    if source not in ("all", "notion", "drive"):
        raise ValueError("source must be all, notion, or drive")
    result: dict = {}
    if source in ("all", "notion"):
        nodes = [node for node in _get_notion_scope().values()]
        normalized = []
        known_by_db_id = {e["database_id"]: (k, e) for k, e in DATABASE_REGISTRY.items()}
        for node in nodes:
            parent = node.get("parent") or {}
            parent_id = _parent_id(node)
            item = {
                "id": node.get("id"),
                "type": node.get("object"),
                "title": node.get("title"),
                "url": node.get("url"),
                "parent_id": parent_id,
                "parent_type": parent.get("type"),
            }
            if node.get("object") == "database" and node.get("id") in known_by_db_id:
                key, entry = known_by_db_id[node["id"]]
                item["database"] = key
                if include_schema:
                    item["schema"] = _get_schema(key, entry)
            normalized.append(item)
        result["notion"] = normalized
    if source in ("all", "drive"):
        folders = drive.list_all_folders()
        normalized = [
            {
                "id": folder["id"],
                "type": "folder",
                "title": folder["name"],
                "parent_id": (folder.get("parents") or [None])[0],
                "modifiedTime": folder.get("modifiedTime"),
            }
            for folder in folders
        ]
        if include_files:
            for file in drive.list_all_files():
                normalized.append(
                    {
                        "id": file["id"],
                        "type": "file",
                        "title": file["name"],
                        "mimeType": file.get("mimeType"),
                        "parent_id": (file.get("parents") or [None])[0],
                        "modifiedTime": file.get("modifiedTime"),
                        "url": file.get("webViewLink"),
                    }
                )
        result["drive"] = normalized
    return result


@mcp.tool()
def list_pages(
    source: str,
    parent_id: str,
    page_size: int = 100,
    cursor: str = "",
) -> dict:
    """List immediate child pages/databases or Drive files/folders."""
    if source == "notion":
        _assert_notion_in_scope(parent_id)
        return {
            "source": "notion",
            "parent_id": parent_id,
            "results": notion.list_page_children(parent_id, page_size=min(page_size, 100)),
            "next_cursor": None,
        }
    if source == "drive":
        result = drive.list_children(parent_id, page_size=page_size, page_token=cursor or None)
        return {
            "source": "drive",
            "parent_id": parent_id,
            "results": result["results"],
            "next_cursor": result["next_page_token"],
        }
    raise ValueError("source must be notion or drive")


@mcp.tool()
def search_pages(query: str) -> list[dict]:
    """Title search limited to pages/databases under the Growth Hackers root."""
    needle = query.strip().lower()
    return [
        {
            "object": node.get("object"),
            "id": node.get("id"),
            "title": node.get("title"),
            "url": node.get("url"),
            "parent": node.get("parent"),
        }
        for node in _get_notion_scope().values()
        if not needle or needle in (node.get("title") or "").lower()
    ]


@mcp.tool()
def fetch_page(page_id: str) -> str:
    """Fetch a Notion page's body as plain text, for reading a specific whitepaper/meeting note in full."""
    _assert_notion_in_scope(page_id)
    return notion.fetch_page_text(page_id)


def _page_title(page: dict, scoped: dict | None = None) -> str:
    for key in ("Name", "Task name", "Title"):
        value = page.get(key)
        if isinstance(value, str) and value:
            return value
    if scoped and scoped.get("title"):
        return scoped["title"]
    return "(untitled)"


def _relation_ids(value) -> list[str]:
    """Normalize a flattened Notion relation property to page IDs."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _extract_sections(text: str, requested: list[str]) -> dict[str, str]:
    """Extract markdown heading sections whose heading contains a request."""
    if not requested:
        return {}
    wanted = [(item, item.casefold()) for item in requested if item.strip()]
    lines = text.splitlines()
    headings = []
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            headings.append((index, len(match.group(1)), match.group(2).strip()))

    found: dict[str, str] = {}
    for index, level, heading in headings:
        heading_folded = heading.casefold()
        matches = [name for name, folded in wanted if folded in heading_folded]
        if not matches:
            continue
        end = len(lines)
        for next_index, next_level, _ in headings:
            if next_index > index and next_level <= level:
                end = next_index
                break
        section = "\n".join(lines[index:end]).strip()
        for name in matches:
            found[name] = section
    return found


def _page_snapshot(
    page_id: str,
    depth: int,
    include_properties: bool,
    include_body: bool,
    sections: list[str],
) -> tuple[dict, dict]:
    _assert_notion_in_scope(page_id)
    page = notion.get_page(page_id)
    scoped = _get_notion_scope().get(page_id, {})
    body = ""
    if include_body or sections:
        body = notion.fetch_page_text(page_id, max_blocks=1000)
    result = {
        "id": page_id,
        "title": _page_title(page, scoped),
        "url": page.get("url") or scoped.get("url"),
        "depth": depth,
        "last_edited_time": page.get("last_edited_time") or scoped.get("last_edited_time"),
    }
    if include_properties:
        result["properties"] = page
    if include_body:
        result["body"] = body
    if sections:
        result["sections"] = _extract_sections(body, sections)
    return result, page


@mcp.tool()
def explore_pages(
    page_ids: list[str],
    relation_properties: list[str] | None = None,
    depth: int = 1,
    include_properties: bool = True,
    include_body: bool = False,
    sections: list[str] | None = None,
    max_pages: int = 50,
) -> dict:
    """Traverse Notion pages through relation properties.

    This is a generic graph/join-like operation. For example, pass a People
    page ID and relation_properties=["참여 프로젝트"] to follow that relation
    to project pages. Any relation property name is accepted; the server
    resolves relation IDs and keeps the traversal within the GH root.
    """
    if not page_ids:
        raise ValueError("page_ids must contain at least one Notion page ID")
    relation_properties = relation_properties or []
    depth = min(max(depth, 0), 3)
    max_pages = min(max(max_pages, 1), 100)
    sections = sections or []

    queue = [(page_id, 0) for page_id in dict.fromkeys(page_ids)]
    seen: set[str] = set()
    snapshots: list[dict] = []
    edges: list[dict] = []
    raw_pages: dict[str, dict] = {}

    while queue and len(snapshots) < max_pages:
        page_id, current_depth = queue.pop(0)
        if page_id in seen:
            continue
        snapshot, raw_page = _page_snapshot(
            page_id,
            current_depth,
            include_properties,
            include_body,
            sections,
        )
        seen.add(page_id)
        snapshots.append(snapshot)
        raw_pages[page_id] = raw_page

        if current_depth >= depth:
            continue
        for property_name in relation_properties:
            targets = _relation_ids(raw_page.get(property_name))
            for target_id in targets:
                edges.append({
                    "from_id": page_id,
                    "property": property_name,
                    "to_id": target_id,
                })
                if target_id not in seen and len(seen) + len(queue) < max_pages:
                    queue.append((target_id, current_depth + 1))

    titles = {item["id"]: item["title"] for item in snapshots}
    for edge in edges:
        edge["from_title"] = titles.get(edge["from_id"])
        edge["to_title"] = titles.get(edge["to_id"])
    return {
        "pages": snapshots,
        "edges": edges,
        "truncated": bool(queue),
        "max_pages": max_pages,
    }


def _content_snippet(title: str, body: str, query: str, width: int = 260) -> str:
    haystack = f"{title}\n{body}"
    folded = haystack.casefold()
    needle = query.casefold().strip()
    position = folded.find(needle) if needle else -1
    if position < 0:
        terms = [term for term in re.findall(r"[\w가-힣]+", needle) if term]
        position = min((folded.find(term) for term in terms if folded.find(term) >= 0), default=0)
    start = max(0, position - width // 2)
    end = min(len(haystack), start + width)
    prefix = "…" if start else ""
    suffix = "…" if end < len(haystack) else ""
    return prefix + haystack[start:end].strip() + suffix


@mcp.tool()
def search_notion_content(
    query: str,
    max_results: int = 20,
    refresh: bool = False,
    max_pages: int = 2000,
    scan_budget_seconds: float = 20,
    scope_keywords: list[str] | None = None,
    scope_database: str = "",
    scope_filters: list[dict] | None = None,
) -> dict:
    """Search keyword occurrences in page titles and bodies under GH root.

    Notion's native search is title-only, so this tool maintains an incremental
    body cache in Supabase. The first call can be slow because it fetches all
    scoped page bodies; later calls only re-fetch pages whose last-edited time
    changed. Results include a short matching snippet and the page URL.

    scan_budget_seconds limits work for one call. Use 0 for an unlimited scan;
    an incomplete response can be called again to continue filling the cache.
    scope_keywords restricts the scan to pages whose title (or ancestor title)
    contains at least one keyword. For structured scoping, prefer
    scope_database + scope_filters, e.g. Tasks with 종류=회의 and 담당부서
    containing 운영진 or 회장단.
    """
    query = query.strip()
    if not query:
        raise ValueError("query must not be empty")
    max_results = min(max(max_results, 1), 100)
    max_pages = min(max(max_pages, 1), 5000)
    scan_budget_seconds = max(float(scan_budget_seconds), 0)
    scope_keywords = [item.casefold().strip() for item in (scope_keywords or []) if item.strip()]

    if scope_database:
        database_key, database_entry = _resolve_database(scope_database)
        schema = _get_schema(database_key, database_entry)
        properties = schema.get("properties", {})
        compiled_filter = _compile_filters(scope_filters, properties)
        rows = notion.query_database(
            database_entry["data_source_id"],
            filter_=compiled_filter,
            max_rows=5000,
        )
        all_scope_pages = [
            {
                "object": "page",
                "id": row["id"],
                "title": _page_title(row),
                "url": row.get("url"),
                "parent": {"type": "database_id", "database_id": database_entry["database_id"]},
                "last_edited_time": row.get("last_edited_time"),
            }
            for row in rows
        ]
    else:
        all_scope_pages = [
            node for node in _get_notion_scope().values()
            if node.get("object") == "page"
        ]

    def matches_scope(node: dict) -> bool:
        if not scope_keywords:
            return True
        current = node
        visited: set[str] = set()
        while current and current.get("id") not in visited:
            current_id = current.get("id")
            if current_id:
                visited.add(current_id)
            title = (current.get("title") or "").casefold()
            if any(keyword in title for keyword in scope_keywords):
                return True
            parent_id = _parent_id(current)
            current = _get_notion_scope().get(parent_id) if parent_id else None
        return False

    scope_pages = [node for node in all_scope_pages if matches_scope(node)][:max_pages]
    # Make title matches available early even when the first full body scan is
    # still in progress. Subsequent calls continue indexing the remaining
    # pages and eventually make the body search complete.
    query_folded = query.casefold()
    scope_pages.sort(
        key=lambda node: 0
        if query_folded in (node.get("title") or "").casefold()
        else 1
    )
    scope_by_id = {node["id"]: node for node in scope_pages}

    supabase.require_private()
    cached_rows = supabase.select_all("ghbot_notion_content")
    cached = {row["page_id"]: row.get("last_edited_time") for row in cached_rows}
    started = time.monotonic()
    scanned_this_call = 0
    complete = True
    for node in scope_pages:
        page_id = node["id"]
        current_edit = node.get("last_edited_time")
        if not refresh and page_id in cached and cached[page_id] == current_edit:
            continue
        if scan_budget_seconds and time.monotonic() - started >= scan_budget_seconds:
            complete = False
            break
        try:
            body = notion.fetch_page_text(page_id, max_blocks=1000)
        except (NotionAccessError, RuntimeError):
            scanned_this_call += 1
            continue
        scanned_this_call += 1
        supabase.upsert(
            "ghbot_notion_content",
            [{
                "page_id": page_id,
                "url": node.get("url"),
                "title": node.get("title") or "(untitled)",
                "body": body,
                "last_edited_time": current_edit,
            }],
            on_conflict="page_id",
        )

    stale_ids = set(cached) - set(scope_by_id)
    for page_id in stale_ids:
        supabase.delete("ghbot_notion_content", [("page_id", f"eq.{page_id}")])

    needle = query.casefold()
    terms = [term.casefold() for term in re.findall(r"[\w가-힣]+", query)]
    matches = []
    current_rows = supabase.select_all("ghbot_notion_content")
    for row in current_rows:
        page_id = row["page_id"]
        title = row.get("title") or ""
        body = row.get("body") or ""
        title_folded = title.casefold()
        body_folded = body.casefold()
        phrase_in_title = needle in title_folded
        phrase_in_body = needle in body_folded
        term_hits = sum(term in title_folded or term in body_folded for term in terms)
        if not phrase_in_title and not phrase_in_body and term_hits < len(terms):
            continue
        score = (4 if phrase_in_title else 0) + (3 if phrase_in_body else 0) + term_hits
        matches.append({
            "page_id": page_id,
            "url": row.get("url") or scope_by_id.get(page_id, {}).get("url"),
            "title": title,
            "match": "title_and_body" if phrase_in_title and phrase_in_body else "title" if phrase_in_title else "body",
            "snippet": _content_snippet(title, body, query),
            "score": score,
            "last_edited_time": row.get("last_edited_time"),
        })
    return {
        "results": sorted(matches, key=lambda item: (-item["score"], item["title"] or ""))[:max_results],
        "complete": complete,
        "indexed_pages": len(current_rows),
        "scanned_this_call": scanned_this_call,
        "total_pages": len(scope_pages),
        "scan_budget_seconds": scan_budget_seconds,
        "scope_keywords": scope_keywords,
        "scope_database": scope_database or None,
        "scope_filters": scope_filters or [],
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
        allowed_ids = {
            row["file_id"]
            for row in supabase.select(
                "ghbot_drive_files",
                columns="file_id",
                filters=[("team", f"eq.{team}")],
            )
        } if supabase.private_enabled else set()
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


def _load_semantic_index():
    global _embed_model
    if _embed_model is None:
        _embed_model = TextEmbedding(model_name=EMBED_MODEL_NAME)
    supabase.require_private()
    rows = supabase.select_all(
        "ghbot_chunks",
        columns="page_id,url,title,source_label,chunk_text,embedding,last_edited,chunk_index",
    )
    chunks = [
        {
            "page_id": row["page_id"],
            "url": row.get("url"),
            "title": row.get("title"),
            "source_label": row.get("source_label", ""),
            "chunk_text": row.get("chunk_text", ""),
            "vector": np.asarray(row.get("embedding") or [], dtype="float32"),
            "last_edited": row.get("last_edited"),
        }
        for row in rows
        if row.get("embedding")
    ]
    return _embed_model, chunks


@mcp.tool()
def semantic_search(query: str, top_k: int = 5, source: str = "") -> list[dict]:
    """Hybrid keyword + semantic search across indexed Notion and Drive content.

    Use for questions that need synthesis across documents or don't map to a
    clean field/tag. Does NOT yet cover meeting notes. Optional `source` filters
    to a label prefix, e.g. '프로젝트_백서', '진행중_프로젝트', or '구글드라이브_HR'.
    Exact term matches and vector similarity are fused with reciprocal-rank
    fusion. Each result includes both component scores and `last_edited`.
    """
    model, chunks = _load_semantic_index()
    if not chunks:
        return []
    query_vec = next(model.embed([query]))
    pool = [
        c
        for c in chunks
        if not c["source_label"].startswith("프로젝트_백서(Archive)")
        and (not source or c["source_label"].startswith(source))
    ]

    def cos_sim(a, b):
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))

    semantic_ranked = sorted(
        ({**c, "semantic_score": cos_sim(query_vec, c["vector"])} for c in pool),
        key=lambda c: c["semantic_score"],
        reverse=True,
    )

    query_terms = set(re.findall(r"[\w가-힣]+", query.lower()))

    def lexical_score(item):
        haystack = f"{item['title']} {item['chunk_text']}".lower()
        term_hits = sum(haystack.count(term) for term in query_terms)
        phrase_hit = 1 if query.strip().lower() in haystack else 0
        return float(term_hits + (phrase_hit * 2))

    lexical_ranked = sorted(
        ({**c, "lexical_score": lexical_score(c)} for c in pool),
        key=lambda c: c["lexical_score"],
        reverse=True,
    )
    # Rank chunks by identity, not only page ID: a document may have several
    # chunks and each can be useful evidence for a different query.
    def identity(item):
        return (item["page_id"], item["chunk_text"])
    semantic_positions = {identity(c): i for i, c in enumerate(semantic_ranked)}
    lexical_positions = {identity(c): i for i, c in enumerate(lexical_ranked) if c["lexical_score"] > 0}
    lexical_scores = {identity(c): c["lexical_score"] for c in lexical_ranked}
    fused = []
    for item in semantic_ranked:
        key = identity(item)
        lexical_score_value = lexical_scores.get(key, 0.0)
        rrf = 0.65 / (60 + semantic_positions[key] + 1)
        if key in lexical_positions:
            rrf += 0.35 / (60 + lexical_positions[key] + 1)
        fused.append({**item, "lexical_score": lexical_score_value, "score": rrf})
    scored = sorted(fused, key=lambda c: c["score"], reverse=True)[:top_k]
    return [
        {
            "page_id": c["page_id"],
            "url": c["url"],
            "title": c["title"],
            "source_label": c["source_label"],
            "text": c["chunk_text"],
            "score": round(c["score"], 5),
            "semantic_score": round(c["semantic_score"], 3),
            "lexical_score": round(c["lexical_score"], 3),
            "last_edited": c["last_edited"],
        }
        for c in scored
    ]


LOGIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>그핵봇 로그인</title>
<style>
body{{font-family:-apple-system,sans-serif;max-width:420px;margin:60px auto;padding:0 16px;color:#111;}}
input{{width:100%;padding:10px;font-size:16px;box-sizing:border-box;margin:8px 0;border:1px solid #ccc;border-radius:6px;}}
button{{width:100%;padding:10px;font-size:16px;background:#111;color:#fff;border:none;border-radius:6px;cursor:pointer;}}
.err{{color:#c0392b;font-size:14px;}}
</style></head>
<body>
<h2>그핵봇 로그인</h2>
<p>운영진에게 발급받은 토큰(<code>ghbot_</code>로 시작)을 입력하세요.</p>
{error}
<form method="post">
<input type="hidden" name="client_id" value="{client_id}">
<input type="hidden" name="state" value="{state}">
<input type="hidden" name="redirect_uri" value="{redirect_uri}">
<input type="hidden" name="code_challenge" value="{code_challenge}">
<input type="hidden" name="scope" value="{scope}">
<input type="hidden" name="resource" value="{resource}">
<input type="text" name="token" placeholder="ghbot_..." autofocus required>
<button type="submit">로그인</button>
</form>
</body></html>"""


def _login_page(params, error: str = "") -> str:
    import html as html_lib

    def esc(key):
        return html_lib.escape(params.get(key, "") or "")

    return LOGIN_PAGE.format(
        error=f'<p class="err">{error}</p>' if error else "",
        client_id=esc("client_id"),
        state=esc("state"),
        redirect_uri=esc("redirect_uri"),
        code_challenge=esc("code_challenge"),
        scope=esc("scope"),
        resource=esc("resource"),
    )


@mcp.custom_route("/login", methods=["GET", "POST"])
async def login(request):
    import json as _json
    import secrets as _secrets
    import time as _time
    from urllib.parse import urlencode

    from starlette.responses import HTMLResponse, RedirectResponse

    import oauth_provider

    if request.method == "GET":
        params = dict(request.query_params)
        return HTMLResponse(_login_page(params))

    form = await request.form()
    params = dict(form)
    token = (params.get("token") or "").strip()
    members = oauth_provider.load_members()
    member_name = members.get(token)
    if not member_name:
        return HTMLResponse(_login_page(params, error="토큰이 올바르지 않습니다."), status_code=401)

    code = "ghcode_" + _secrets.token_urlsafe(24)
    conn = oauth_provider.connect()
    conn.execute(
        "INSERT INTO oauth_codes (code, client_id, scopes, expires_at, code_challenge, redirect_uri, "
        "redirect_uri_explicit, resource, subject) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            code,
            params.get("client_id", ""),
            _json.dumps((params.get("scope") or "").split()),
            _time.time() + oauth_provider.CODE_TTL_SECONDS,
            params.get("code_challenge", ""),
            params.get("redirect_uri", ""),
            1,
            params.get("resource") or None,
            member_name,
        ),
    )
    conn.commit()
    conn.close()

    redirect_uri = params.get("redirect_uri", "")
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        f"{redirect_uri}{sep}{urlencode({'code': code, 'state': params.get('state', '')})}",
        status_code=302,
    )


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    from starlette.responses import PlainTextResponse

    return PlainTextResponse("ok")


def _build_http_app():
    """mcp.streamable_http_app() defaults to allowing only 127.0.0.1/localhost
    Host headers (DNS-rebinding protection) when no transport_security is
    given - which 421s every request from a real public domain. Explicitly
    allow the deployed host(s); comma-separated via ALLOWED_HOSTS for
    flexibility (e.g. adding the *.up.railway.app fallback domain).

    Bearer-token auth (both OAuth-issued and legacy static tokens) is handled
    by the mcp SDK itself via auth_server_provider - no custom middleware
    needed here anymore.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    allowed_hosts = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "api.ghsnu.com").split(",") if h.strip()]
    allowed_hosts += ["127.0.0.1:*", "localhost:*"]
    transport_security = TransportSecuritySettings(
        allowed_hosts=allowed_hosts,
        allowed_origins=[f"https://{h}" for h in allowed_hosts] + ["http://127.0.0.1:*", "http://localhost:*"],
    )

    members = load_members()
    print(f"[gh-notion-bot] {len(members)} legacy static token(s), OAuth login at /login, serving at /mcp")
    return mcp.streamable_http_app(
        streamable_http_path=os.environ.get("MCP_PATH", "/mcp"),
        transport_security=transport_security,
    )


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
