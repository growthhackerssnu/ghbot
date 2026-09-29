import http.client
import json
import os
import socket
import time
import urllib.request
import urllib.error

NOTION_VERSION = "2022-06-28"
DATA_SOURCE_API_VERSION = "2025-09-03"  # required for the /data_sources/{id}/query endpoint
BASE_URL = "https://api.notion.com/v1"

# Data source IDs discovered while exploring the GH Notion structure (via
# GET /v1/databases/{database_id} with Notion-Version 2025-09-03, which lists
# each database's data_sources). Query these with /v1/data_sources/{id}/query,
# not the classic /v1/databases/{id}/query - some of these DBs return
# "no data sources accessible" on the classic endpoint even when shared.
# All of these must be shared with the Integration in Notion
# ("..." -> Connections) before queries against them will succeed.

# Canonical, actively-maintained project tracker (root "Growth Hackers" hub).
PROJECTS_DB_ID = "3ae40bd1-0676-8032-8b69-000bdebab973"
COMPANIES_DB_ID = "3ae40bd1-0676-80fe-bbd3-000bb4c32074"
PEOPLE_DB_ID = "3ab40bd1-0676-800f-8fd9-000be08aef96"
TASKS_DB_ID = "3ae40bd1-0676-80ff-8782-000b9897f7ec"

# Historical archive (GH_Project_ARCHIVE Mainpage) and rules/templates (root hub).
ARCHIVE_DB_ID = "345138fb-f14f-4bd9-8801-bcb2cfd21dad"
GUIDES_DB_ID = "3b140bd1-0676-80ea-91a0-000b4e171b68"
INSIGHTS_DB_ID = "3b940bd1-0676-8084-8b67-000b6f1bd4a5"


class NotionAccessError(RuntimeError):
    """Raised when the integration cannot see the requested page/database."""


class NotionClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("NOTION_API_KEY")
        if not self.api_key:
            raise RuntimeError("NOTION_API_KEY is not set (check your .env file)")
        self._title_cache: dict[str, str] = {}

    def _request(self, method: str, path: str, body: dict | None = None, version: str = NOTION_VERSION) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{BASE_URL}/{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Notion-Version": version,
                "Content-Type": "application/json",
            },
        )
        max_attempts = 4
        for attempt in range(1, max_attempts + 1):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                payload = e.read().decode()
                if e.code == 404:
                    raise NotionAccessError(
                        f"Notion returned 404 for {path}. The page/database is probably "
                        f"not shared with this Integration yet (open it in Notion -> "
                        f"'...' menu -> Connections -> add the integration)."
                    ) from e
                if e.code == 429 and attempt < max_attempts:
                    # Notion supplies a retry_after value in the error body
                    # for burst limits. Respect it so chained DB joins do not
                    # immediately exhaust the next retry as well.
                    retry_after = 2 * attempt
                    try:
                        detail = json.loads(payload).get("additional_data", {})
                        retry_after = max(retry_after, float(detail.get("retry_after", 0)))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
                    time.sleep(retry_after)
                    continue
                raise RuntimeError(f"Notion API error {e.code} on {path}: {payload}") from e
            except (http.client.RemoteDisconnected, ConnectionError, TimeoutError, urllib.error.URLError) as e:
                if isinstance(e, urllib.error.URLError) and isinstance(e.reason, socket.gaierror):
                    raise RuntimeError(
                        "DNS resolution failed for api.notion.com. Check the runtime's network/DNS policy; "
                        "this is not a Notion filter or authentication error."
                    ) from e
                if attempt < max_attempts:
                    time.sleep(1.5 * attempt)
                    continue
                raise RuntimeError(f"Network error calling Notion API on {path} after {max_attempts} attempts: {e}") from e

    def search(self, query: str, page_size: int = 10) -> list[dict]:
        result = self._request(
            "POST", "search", {"query": query, "page_size": page_size}
        )
        return [self._summarize_search_result(r) for r in result.get("results", [])]

    def search_all(self, max_results: int = 5000, page_size: int = 100) -> list[dict]:
        """Return all search-visible pages and databases, with parent metadata.

        Notion's search endpoint is the only inexpensive way to enumerate all
        objects shared with an integration. This is used for sitemap discovery,
        not for full-text retrieval.
        """
        results: list[dict] = []
        cursor = None
        while True:
            body: dict = {"page_size": min(page_size, 100)}
            if cursor:
                body["start_cursor"] = cursor
            result = self._request("POST", "search", body)
            results.extend(result.get("results", []))
            if not result.get("has_more") or len(results) >= max_results:
                break
            cursor = result.get("next_cursor")
        return [self._summarize_search_result(r) for r in results[:max_results]]

    def _summarize_search_result(self, r: dict) -> dict:
        title = None
        if r.get("object") == "database":
            title = "".join(t.get("plain_text", "") for t in r.get("title", []))
        else:
            for prop in r.get("properties", {}).values():
                if prop.get("type") == "title":
                    title = "".join(t.get("plain_text", "") for t in prop.get("title", []))
                    break
        return {
            "object": r.get("object"),
            "id": r.get("id"),
            "title": title,
            "url": r.get("url"),
            "parent": r.get("parent"),
        }

    def get_database_schema(self, data_source_id: str) -> dict:
        """Return the live schema for a queryable Notion data source."""
        raw = self._request(
            "GET", f"data_sources/{data_source_id}", version=DATA_SOURCE_API_VERSION
        )
        properties = {}
        for name, prop in raw.get("properties", {}).items():
            ptype = prop.get("type")
            config = prop.get(ptype) or {}
            item = {"type": ptype}
            if ptype in ("select", "status", "multi_select"):
                item["options"] = [o.get("name") for o in config.get("options", [])]
            if ptype == "relation":
                item["database_id"] = config.get("database_id")
            properties[name] = item
        return {
            "id": raw.get("id", data_source_id),
            "title": "".join(t.get("plain_text", "") for t in raw.get("title", [])),
            "properties": properties,
        }

    def get_database_info(self, database_id: str) -> dict:
        """Return database metadata and its queryable data-source ID."""
        raw = self._request(
            "GET", f"databases/{database_id}", version=DATA_SOURCE_API_VERSION
        )
        data_sources = raw.get("data_sources", [])
        return {
            "id": raw.get("id", database_id),
            "title": "".join(t.get("plain_text", "") for t in raw.get("title", [])),
            "data_source_id": data_sources[0].get("id") if data_sources else None,
        }

    def query_database(
        self,
        data_source_id: str,
        filter_: dict | None = None,
        sorts: list[dict] | None = None,
        page_size: int = 100,
        max_rows: int = 1000,
    ) -> list[dict]:
        """Query a data source (the queryable schema+rows behind a database).

        Follows pagination automatically up to max_rows - a single 100-row
        page silently truncating a larger database was a real bug here before.
        """
        all_pages: list[dict] = []
        cursor = None
        while True:
            result = self.query_database_page(
                data_source_id,
                filter_=filter_,
                sorts=sorts,
                page_size=page_size,
                cursor=cursor,
            )
            all_pages.extend(result["results"])
            if not result["has_more"] or len(all_pages) >= max_rows:
                break
            cursor = result["next_cursor"]
        return all_pages[:max_rows]

    def query_database_page(
        self,
        data_source_id: str,
        filter_: dict | None = None,
        sorts: list[dict] | None = None,
        page_size: int = 100,
        cursor: str | None = None,
    ) -> dict:
        """Query one page and preserve the Notion pagination cursor."""
        body: dict = {"page_size": min(page_size, 100)}
        if filter_:
            body["filter"] = filter_
        if sorts:
            body["sorts"] = sorts
        if cursor:
            body["start_cursor"] = cursor
        result = self._request(
            "POST",
            f"data_sources/{data_source_id}/query",
            body,
            version=DATA_SOURCE_API_VERSION,
        )
        return {
            "results": [self._flatten_page(p) for p in result.get("results", [])],
            "next_cursor": result.get("next_cursor"),
            "has_more": bool(result.get("has_more")),
        }

    def _flatten_page(self, page: dict) -> dict:
        flat = {
            "id": page.get("id"),
            "url": page.get("url"),
            "last_edited_time": page.get("last_edited_time"),
        }
        for name, prop in page.get("properties", {}).items():
            flat[name] = self._flatten_property(prop)
        return flat

    def _flatten_property(self, prop: dict):
        ptype = prop.get("type")
        value = prop.get(ptype)
        if ptype == "title" or ptype == "rich_text":
            return "".join(t.get("plain_text", "") for t in value or [])
        if ptype == "select":
            return value.get("name") if value else None
        if ptype == "multi_select":
            return [v.get("name") for v in value or []]
        if ptype == "status":
            return value.get("name") if value else None
        if ptype == "date":
            if not value:
                return None
            return {"start": value.get("start"), "end": value.get("end")}
        if ptype == "people":
            # Notion's user object can omit ``name`` (for example when the
            # integration cannot read a member profile), but its stable ID is
            # still present. Keep that ID so a People DB's ``Notion ID`` can
            # be used to filter Tasks' ``관련 인원`` people property.
            return [p.get("id") for p in value or []]
        if ptype == "relation":
            return [r.get("id") for r in value or []]
        if ptype in ("number", "checkbox", "url", "email", "phone_number"):
            return value
        return value

    def get_page(self, page_id: str) -> dict:
        """Fetch a single page and flatten its properties, same shape as a query_database row."""
        return self._flatten_page(self._request("GET", f"pages/{page_id}"))

    def resolve_titles(self, page_ids: list[str]) -> dict[str, str]:
        """Given page ids (e.g. from a relation property), fetch their titles.

        Cached per-client since the same company/person ids recur across many
        relation lookups in one server process.
        """
        titles: dict[str, str] = {}
        for pid in dict.fromkeys(page_ids):  # dedupe, keep order
            if pid in self._title_cache:
                titles[pid] = self._title_cache[pid]
                continue
            page = self._request("GET", f"pages/{pid}")
            title = ""
            for prop in page.get("properties", {}).values():
                if prop.get("type") == "title":
                    title = "".join(t.get("plain_text", "") for t in prop.get("title", []))
                    break
            self._title_cache[pid] = title
            titles[pid] = title
        return titles

    def fetch_page_text(self, page_id: str, max_blocks: int = 200) -> str:
        """Fetch a page's body as plain text (paragraphs, headings, lists, callouts)."""
        lines: list[str] = []
        self._collect_block_text(page_id, lines, max_blocks, depth=0)
        return "\n".join(lines)

    def list_page_children(self, page_id: str, page_size: int = 100, max_results: int = 1000) -> list[dict]:
        """List child pages/databases directly contained in a Notion page."""
        children: list[dict] = []
        cursor = None
        while True:
            path = f"blocks/{page_id}/children?page_size={min(page_size, 100)}"
            if cursor:
                path += f"&start_cursor={cursor}"
            result = self._request("GET", path)
            for block in result.get("results", []):
                btype = block.get("type")
                if btype not in ("child_page", "child_database"):
                    continue
                content = block.get(btype) or {}
                children.append(
                    {
                        "id": block.get("id"),
                        "object": "page" if btype == "child_page" else "database",
                        "title": content.get("title"),
                        "parent_id": page_id,
                        "has_children": block.get("has_children", False),
                        "last_edited_time": block.get("last_edited_time"),
                    }
                )
                if len(children) >= max_results:
                    return children[:max_results]
            if not result.get("has_more"):
                break
            cursor = result.get("next_cursor")
        return children

    def _collect_block_text(self, block_id: str, lines: list[str], max_blocks: int, depth: int):
        if len(lines) >= max_blocks or depth > 3:
            return
        cursor = None
        while True:
            path = f"blocks/{block_id}/children?page_size=100"
            if cursor:
                path += f"&start_cursor={cursor}"
            result = self._request("GET", path)
            for block in result.get("results", []):
                if len(lines) >= max_blocks:
                    return
                text = self._block_to_text(block)
                if text:
                    lines.append(("  " * depth) + text)
                if block.get("has_children"):
                    self._collect_block_text(block["id"], lines, max_blocks, depth + 1)
            if not result.get("has_more"):
                break
            cursor = result.get("next_cursor")

    def _block_to_text(self, block: dict) -> str:
        btype = block.get("type")
        content = block.get(btype, {})
        rich_text = content.get("rich_text")
        if rich_text is None:
            return ""
        text = "".join(t.get("plain_text", "") for t in rich_text)
        prefix = {
            "heading_1": "# ",
            "heading_2": "## ",
            "heading_3": "### ",
            "bulleted_list_item": "- ",
            "numbered_list_item": "- ",
            "to_do": "[ ] ",
            "quote": "> ",
            "callout": "> ",
        }.get(btype, "")
        return prefix + text if text else ""
