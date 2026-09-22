import http.client
import json
import os
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

# Historical archive (GH_Project_ARCHIVE Mainpage) and rules/templates (root hub).
ARCHIVE_DB_ID = "345138fb-f14f-4bd9-8801-bcb2cfd21dad"
GUIDES_DB_ID = "3b140bd1-0676-80ea-91a0-000b4e171b68"


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
        max_attempts = 3
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
                    time.sleep(2 * attempt)
                    continue
                raise RuntimeError(f"Notion API error {e.code} on {path}: {payload}") from e
            except (http.client.RemoteDisconnected, ConnectionError, TimeoutError, urllib.error.URLError) as e:
                if attempt < max_attempts:
                    time.sleep(1.5 * attempt)
                    continue
                raise RuntimeError(f"Network error calling Notion API on {path} after {max_attempts} attempts: {e}") from e

    def search(self, query: str, page_size: int = 10) -> list[dict]:
        result = self._request(
            "POST", "search", {"query": query, "page_size": page_size}
        )
        return [self._summarize_search_result(r) for r in result.get("results", [])]

    def _summarize_search_result(self, r: dict) -> dict:
        title = None
        for prop in r.get("properties", {}).values():
            if prop.get("type") == "title":
                title = "".join(t.get("plain_text", "") for t in prop.get("title", []))
        return {
            "object": r.get("object"),
            "id": r.get("id"),
            "title": title,
            "url": r.get("url"),
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
            all_pages.extend(result.get("results", []))
            if not result.get("has_more") or len(all_pages) >= max_rows:
                break
            cursor = result.get("next_cursor")
        return [self._flatten_page(p) for p in all_pages[:max_rows]]

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
            return [p.get("name") for p in value or []]
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
        query_projects/query_archive calls in one server process.
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
