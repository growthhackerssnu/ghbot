"""Minimal OAuth 2.1 authorization server for the remote (streamable-http)
deployment. Claude's custom-connector UI only offers OAuth or fully-open
access - no field for a static bearer token - so Claude requires this to
connect at all. "Login" is pasting the same ghbot_<token> issued by
manage_members.py into a plain HTML form; there's no separate identity
system, this just wraps the existing member allowlist in a real OAuth flow.

Static tokens keep working too (checked directly in load_access_token), so
ChatGPT's existing token-based connections aren't broken by this.
"""
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

from config import load_json_env
from mcp.server.auth.provider import AccessToken, AuthorizationCode, AuthorizationParams, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

DB_PATH = Path(__file__).parent / "oauth.db"
CODE_TTL_SECONDS = 300
ACCESS_TOKEN_TTL_SECONDS = 60 * 60 * 24 * 180  # 180 days - low-stakes internal tool, avoid re-login churn


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS oauth_clients (client_id TEXT PRIMARY KEY, data TEXT)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS oauth_codes (
            code TEXT PRIMARY KEY, client_id TEXT, scopes TEXT, expires_at REAL,
            code_challenge TEXT, redirect_uri TEXT, redirect_uri_explicit INTEGER,
            resource TEXT, subject TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS oauth_tokens (
            access_token TEXT PRIMARY KEY, refresh_token TEXT, client_id TEXT,
            scopes TEXT, expires_at REAL, resource TEXT, subject TEXT
        )"""
    )
    conn.commit()
    return conn


def load_members() -> dict[str, str]:
    inline = load_json_env("MEMBERS_JSON")
    if inline:
        return inline
    members_file = Path(__file__).parent / "members.json"
    if members_file.exists():
        return json.loads(members_file.read_text(encoding="utf-8"))
    return {}


class MemberOAuthProvider:
    """Implements the mcp.server.auth.provider.OAuthAuthorizationServerProvider
    protocol (duck-typed, not inherited - it's a typing.Protocol)."""

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        conn = connect()
        row = conn.execute("SELECT data FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
        conn.close()
        return OAuthClientInformationFull.model_validate_json(row[0]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        conn = connect()
        conn.execute(
            "INSERT INTO oauth_clients (client_id, data) VALUES (?, ?) "
            "ON CONFLICT(client_id) DO UPDATE SET data = excluded.data",
            (client_info.client_id, client_info.model_dump_json()),
        )
        conn.commit()
        conn.close()

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        from urllib.parse import urlencode

        base_url = os.environ.get("OAUTH_ISSUER_URL", "https://api.ghsnu.com")
        query = urlencode(
            {
                "client_id": client.client_id,
                "state": params.state or "",
                "redirect_uri": str(params.redirect_uri),
                "code_challenge": params.code_challenge,
                "scope": " ".join(params.scopes or []),
                "resource": params.resource or "",
            }
        )
        return f"{base_url}/login?{query}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        conn = connect()
        row = conn.execute(
            "SELECT client_id, scopes, expires_at, code_challenge, redirect_uri, "
            "redirect_uri_explicit, resource, subject FROM oauth_codes WHERE code = ?",
            (authorization_code,),
        ).fetchone()
        conn.close()
        if not row:
            return None
        client_id, scopes, expires_at, code_challenge, redirect_uri, redirect_explicit, resource, subject = row
        if expires_at < time.time():
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=json.loads(scopes),
            expires_at=expires_at,
            client_id=client_id,
            code_challenge=code_challenge,
            redirect_uri=redirect_uri,
            redirect_uri_provided_explicitly=bool(redirect_explicit),
            resource=resource or None,
            subject=subject,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        conn = connect()
        conn.execute("DELETE FROM oauth_codes WHERE code = ?", (authorization_code.code,))
        access_token = "ghoauth_" + secrets.token_urlsafe(32)
        refresh_token = "ghrefresh_" + secrets.token_urlsafe(32)
        expires_at = time.time() + ACCESS_TOKEN_TTL_SECONDS
        conn.execute(
            "INSERT INTO oauth_tokens (access_token, refresh_token, client_id, scopes, expires_at, resource, subject) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                access_token,
                refresh_token,
                client.client_id,
                json.dumps(authorization_code.scopes),
                expires_at,
                authorization_code.resource,
                authorization_code.subject,
            ),
        )
        conn.commit()
        conn.close()
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            refresh_token=refresh_token,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
        )

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        conn = connect()
        row = conn.execute(
            "SELECT client_id, scopes, expires_at, resource FROM oauth_tokens WHERE refresh_token = ?",
            (refresh_token,),
        ).fetchone()
        conn.close()
        if not row:
            return None
        client_id, scopes, expires_at, resource = row
        return RefreshToken(
            token=refresh_token,
            client_id=client_id,
            scopes=json.loads(scopes),
            expires_at=int(expires_at) if expires_at else None,
            resource=resource,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        conn = connect()
        conn.execute("DELETE FROM oauth_tokens WHERE refresh_token = ?", (refresh_token.token,))
        access_token = "ghoauth_" + secrets.token_urlsafe(32)
        new_refresh = "ghrefresh_" + secrets.token_urlsafe(32)
        expires_at = time.time() + ACCESS_TOKEN_TTL_SECONDS
        use_scopes = scopes or refresh_token.scopes
        conn.execute(
            "INSERT INTO oauth_tokens (access_token, refresh_token, client_id, scopes, expires_at, resource, subject) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (access_token, new_refresh, client.client_id, json.dumps(use_scopes), expires_at, refresh_token.resource),
        )
        conn.commit()
        conn.close()
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            refresh_token=new_refresh,
            scope=" ".join(use_scopes) if use_scopes else None,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        conn = connect()
        row = conn.execute(
            "SELECT client_id, scopes, expires_at, resource, subject FROM oauth_tokens WHERE access_token = ?",
            (token,),
        ).fetchone()
        conn.close()
        if row:
            client_id, scopes, expires_at, resource, subject = row
            if expires_at < time.time():
                return None
            return AccessToken(
                token=token,
                client_id=client_id,
                scopes=json.loads(scopes),
                expires_at=int(expires_at),
                resource=resource,
                subject=subject,
            )
        # Legacy static tokens from manage_members.py - kept working so
        # ChatGPT's existing direct-token connections aren't broken by this.
        members = load_members()
        if token in members:
            return AccessToken(token=token, client_id="static", scopes=["*"], expires_at=None, subject=members[token])
        return None

    async def revoke_token(self, token) -> None:
        conn = connect()
        conn.execute(
            "DELETE FROM oauth_tokens WHERE access_token = ? OR refresh_token = ?", (token.token, token.token)
        )
        conn.commit()
        conn.close()
