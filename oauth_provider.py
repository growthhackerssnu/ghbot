"""Minimal OAuth 2.1 authorization server for the remote (streamable-http)
deployment. Claude's custom-connector UI only offers OAuth or fully-open
access - no field for a static bearer token - so Claude requires this to
connect at all. "Login" is pasting the same ghbot_<token> issued by
manage_members.py into a plain HTML form; there's no separate identity
system, this just wraps the existing member allowlist in a real OAuth flow.

Static tokens keep working too (checked directly in load_access_token), so
ChatGPT's existing token-based connections aren't broken by this.
"""
import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import load_json_env
from mcp.server.auth.provider import AccessToken, AuthorizationCode, AuthorizationParams, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

DB_PATH = Path(__file__).parent / "oauth.db"
CODE_TTL_SECONDS = 300
ACCESS_TOKEN_TTL_SECONDS = 60 * 60 * 24 * 180  # 180 days - low-stakes internal tool, avoid re-login churn
ADMIN_TOKEN_SUBJECT_PREFIX = "admin-token:"


def _admin_auth_url() -> str | None:
    base_url = os.environ.get("ADMIN_API_URL", "").rstrip("/")
    secret = os.environ.get("GHBOT_AUTH_SHARED_SECRET", "")
    return f"{base_url}/api/v1/internal/ghbot/authenticate" if base_url and secret else None


async def _verify_admin_token(*, token: str | None = None, token_hash: str | None = None) -> str | None:
    """Ask the admin service whether an active acting-member token is valid.

    ghbot never receives the admin database URL or its Supabase credentials.
    The shared secret authenticates this server-to-server request; the raw
    token is sent only for direct bearer authentication, while OAuth-issued
    sessions use the token hash saved in their subject.
    """
    url = _admin_auth_url()
    if not url:
        return None
    payload = {"token": token} if token is not None else {"tokenHash": token_hash}
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-ghbot-auth-secret": os.environ["GHBOT_AUTH_SHARED_SECRET"],
        },
    )

    def send() -> str | None:
        try:
            with urlopen(request, timeout=float(os.environ.get("GHBOT_AUTH_TIMEOUT_SECONDS", "3"))) as response:
                data = json.loads(response.read().decode("utf-8"))
            display_name = data.get("data", {}).get("displayName")
            return display_name if isinstance(display_name, str) and display_name else None
        except (HTTPError, URLError, TimeoutError, ValueError, OSError):
            # An unavailable admin service must fail closed. Do not log tokens
            # or response bodies, which could inadvertently contain secrets.
            return None

    return await asyncio.to_thread(send)


async def resolve_member_token(token: str) -> tuple[str, str] | None:
    """Return (stored OAuth subject, display name) for a valid member token."""
    members = load_members()
    if token in members:
        return members[token], members[token]

    display_name = await _verify_admin_token(token=token)
    if not display_name:
        return None
    return f"{ADMIN_TOKEN_SUBJECT_PREFIX}{hashlib.sha256(token.encode('utf-8')).hexdigest()}", display_name


async def resolve_admin_subject(subject: str) -> str | None:
    if not subject.startswith(ADMIN_TOKEN_SUBJECT_PREFIX):
        return subject
    token_hash = subject.removeprefix(ADMIN_TOKEN_SUBJECT_PREFIX)
    if len(token_hash) != 64 or any(char not in "0123456789abcdef" for char in token_hash):
        return None
    return await _verify_admin_token(token_hash=token_hash)


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
        row = conn.execute("SELECT subject FROM oauth_tokens WHERE refresh_token = ?", (refresh_token.token,)).fetchone()
        conn.execute("DELETE FROM oauth_tokens WHERE refresh_token = ?", (refresh_token.token,))
        access_token = "ghoauth_" + secrets.token_urlsafe(32)
        new_refresh = "ghrefresh_" + secrets.token_urlsafe(32)
        expires_at = time.time() + ACCESS_TOKEN_TTL_SECONDS
        use_scopes = scopes or refresh_token.scopes
        conn.execute(
            "INSERT INTO oauth_tokens (access_token, refresh_token, client_id, scopes, expires_at, resource, subject) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (access_token, new_refresh, client.client_id, json.dumps(use_scopes), expires_at, refresh_token.resource, row[0] if row else None),
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
            # Admin-issued OAuth sessions retain a token-hash subject and are
            # revalidated on every request. Older static OAuth sessions may
            # have a name (or NULL from the historical refresh implementation)
            # and remain valid during the migration.
            if subject and subject.startswith(ADMIN_TOKEN_SUBJECT_PREFIX):
                subject = await resolve_admin_subject(subject)
                if not subject:
                    return None
            return AccessToken(
                token=token,
                client_id=client_id,
                scopes=json.loads(scopes),
                expires_at=int(expires_at),
                resource=resource,
                subject=subject,
            )
        # Legacy static tokens remain valid during migration. Tokens issued by
        # admin are validated remotely against the admin DB instead.
        resolved = await resolve_member_token(token)
        if resolved:
            _, display_name = resolved
            return AccessToken(token=token, client_id="static", scopes=["*"], expires_at=None, subject=display_name)
        return None

    async def revoke_token(self, token) -> None:
        conn = connect()
        conn.execute(
            "DELETE FROM oauth_tokens WHERE access_token = ? OR refresh_token = ?", (token.token, token.token)
        )
        conn.commit()
        conn.close()
