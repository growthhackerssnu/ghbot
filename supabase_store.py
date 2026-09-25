"""Small PostgREST client used for the GH bot's indexed data.

The server only needs ``SUPABASE_URL`` and ``SUPABASE_KEY``.  Using the
Supabase REST endpoint keeps the deployment lightweight and means the MCP
process reads the index directly from Postgres instead of downloading a
SQLite snapshot.
"""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _json_safe(value):
    """Postgres text fields reject NUL characters found in some Drive exports."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


class SupabaseStore:
    """Minimal table/upsert/select/delete wrapper for Supabase PostgREST."""

    def __init__(self, url: str | None = None, key: str | None = None):
        self.url = (
            url
            or os.environ.get("SUPABASE_URL", "")
            or os.environ.get("NEXT_PUBLIC_SUPABASE_URL", "")
        ).rstrip("/")
        self.key = (
            key
            or os.environ.get("SUPABASE_KEY", "")
            or os.environ.get("NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY", "")
        )

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.key)

    def require_enabled(self) -> None:
        if not self.enabled:
            raise RuntimeError(
                "Supabase index is not configured. Set SUPABASE_URL and SUPABASE_KEY "
                "(or the NEXT_PUBLIC_SUPABASE_* equivalents)."
            )

    def _request(
        self,
        method: str,
        table: str,
        *,
        params: list[tuple[str, str]] | None = None,
        body=None,
        prefer: str | None = None,
    ):
        self.require_enabled()
        query = f"?{urlencode(params)}" if params else ""
        request = Request(
            f"{self.url}/rest/v1/{table}{query}",
            method=method,
            headers={
                "apikey": self.key,
                "Authorization": f"Bearer {self.key}",
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if body is not None else {}),
                **({"Prefer": prefer} if prefer else {}),
            },
            data=json.dumps(_json_safe(body), ensure_ascii=False).encode("utf-8") if body is not None else None,
        )
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Supabase {method} {table} failed ({exc.code}): {detail[:500]}") from exc
        except URLError as exc:
            raise RuntimeError(f"Supabase {method} {table} failed: {exc.reason}") from exc
        if not raw:
            return []
        return json.loads(raw.decode("utf-8"))

    def select(
        self,
        table: str,
        *,
        columns: str = "*",
        filters: list[tuple[str, str]] | None = None,
        order: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict]:
        params: list[tuple[str, str]] = [("select", columns)]
        params.extend(filters or [])
        if order:
            params.append(("order", order))
        if limit is not None:
            params.append(("limit", str(limit)))
        if offset is not None:
            params.append(("offset", str(offset)))
        result = self._request("GET", table, params=params)
        return result if isinstance(result, list) else []

    def select_all(self, table: str, *, columns: str = "*", filters=None) -> list[dict]:
        """Read all rows in pages, avoiding Supabase's default row limit."""
        rows: list[dict] = []
        offset = 0
        page_size = 1000
        while True:
            page = self.select(
                table,
                columns=columns,
                filters=filters,
                limit=page_size,
                offset=offset,
            )
            rows.extend(page)
            if len(page) < page_size:
                return rows
            offset += page_size

    def upsert(self, table: str, rows: list[dict], *, on_conflict: str) -> list[dict]:
        if not rows:
            return []
        result = self._request(
            "POST",
            table,
            params=[("on_conflict", on_conflict)],
            body=rows,
            prefer="resolution=merge-duplicates,return=minimal",
        )
        return result if isinstance(result, list) else []

    def delete(self, table: str, filters: list[tuple[str, str]]) -> None:
        self._request("DELETE", table, params=filters, prefer="return=minimal")
