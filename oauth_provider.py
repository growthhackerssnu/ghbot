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
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import load_json_env
from mcp.server.auth.provider import AccessToken, AuthorizationCode, AuthorizationParams, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

CODE_TTL_SECONDS = 300
ACCESS_TOKEN_TTL_SECONDS = 60 * 60 * 24 * 180  # 180 days - low-stakes internal tool, avoid re-login churn
ADMIN_TOKEN_SUBJECT_PREFIX = "admin-token:"


def _admin_url(path: str) -> str | None:
    base_url = os.environ.get("ADMIN_API_URL", "").rstrip("/")
    secret = os.environ.get("GHBOT_AUTH_SHARED_SECRET", "")
    return f"{base_url}{path}" if base_url and secret else None


def _admin_timeout() -> float:
    # admin은 Cloud Run에서 0까지 내려가므로 콜드 스타트(수 초)를 견딜 만큼 넉넉히.
    return float(os.environ.get("GHBOT_AUTH_TIMEOUT_SECONDS", "10"))


async def _verify_admin_token(*, token: str | None = None, token_hash: str | None = None) -> str | None:
    """Ask the admin service whether an active acting-member token is valid.

    ghbot never receives the admin database URL or its Supabase credentials.
    The shared secret authenticates this server-to-server request; the raw
    token is sent only for direct bearer authentication, while OAuth-issued
    sessions use the token hash saved in their subject.
    """
    url = _admin_url("/api/v1/internal/ghbot/authenticate")
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
            with urlopen(request, timeout=_admin_timeout()) as response:
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


def _secret_key(kind: str, value: str) -> str:
    # 코드·토큰 원문은 admin DB에 남기지 않는다. 조회 키로 해시만 보낸다.
    return f"{kind}:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


async def store(op: str, key: str, data: dict | None = None, expires_at: float | None = None) -> dict | None:
    """OAuth 상태를 admin의 ghbot.oauth_entries(Postgres)에 읽고 쓴다.

    예전엔 컨테이너 디스크의 oauth.db(SQLite)였는데, 인스턴스가 내려가면 모든
    claude.ai 연결이 끊겼다. ghbot은 DB 접속 정보를 받지 않으므로 내부 API로만 접근한다.
    전송 오류는 예외로 올린다 — 여기서 None을 돌려주면 '토큰 없음'(401)이 되어
    사용자가 다시 로그인해야 하지만, 예외(500)면 클라이언트가 그냥 재시도한다.
    """
    url = _admin_url("/api/v1/internal/ghbot/oauth-store")
    if not url:
        raise RuntimeError("ADMIN_API_URL and GHBOT_AUTH_SHARED_SECRET must be set for OAuth storage")
    payload: dict = {"op": op, "key": key}
    if data is not None:
        payload["data"] = data
    if expires_at is not None:
        payload["expiresAt"] = expires_at
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-ghbot-auth-secret": os.environ["GHBOT_AUTH_SHARED_SECRET"],
        },
    )

    def send() -> dict | None:
        with urlopen(request, timeout=_admin_timeout()) as response:
            return json.loads(response.read().decode("utf-8")).get("data", {}).get("value")

    return await asyncio.to_thread(send)


async def save_authorization_code(code: str, record: dict) -> None:
    await store("put", _secret_key("code", code), record, record["expires_at"])


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
        data = await store("get", f"client:{client_id}")
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await store("put", f"client:{client_info.client_id}", client_info.model_dump(mode="json"))

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
        row = await store("get", _secret_key("code", authorization_code))
        if not row or row["expires_at"] < time.time():
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=row["scopes"],
            expires_at=row["expires_at"],
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=bool(row["redirect_uri_explicit"]),
            resource=row.get("resource") or None,
            subject=row.get("subject"),
        )

    async def _issue_tokens(self, client_id: str, scopes: list[str], resource: str | None, subject: str | None) -> OAuthToken:
        access_token = "ghoauth_" + secrets.token_urlsafe(32)
        refresh_token = "ghrefresh_" + secrets.token_urlsafe(32)
        expires_at = time.time() + ACCESS_TOKEN_TTL_SECONDS
        access_key = _secret_key("access", access_token)
        refresh_key = _secret_key("refresh", refresh_token)
        record = {"client_id": client_id, "scopes": scopes, "expires_at": expires_at, "resource": resource, "subject": subject}
        await store("put", access_key, {**record, "refresh_key": refresh_key}, expires_at)
        await store("put", refresh_key, {**record, "access_key": access_key}, expires_at)
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            refresh_token=refresh_token,
            scope=" ".join(scopes) if scopes else None,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        await store("delete", _secret_key("code", authorization_code.code))
        return await self._issue_tokens(
            client.client_id, authorization_code.scopes, authorization_code.resource, authorization_code.subject
        )

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        row = await store("get", _secret_key("refresh", refresh_token))
        if not row:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=row["scopes"],
            expires_at=int(row["expires_at"]) if row.get("expires_at") else None,
            resource=row.get("resource"),
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        # take = 조회+삭제 한 번에. 같은 리프레시 토큰을 두 번 써도 한 번만 교환된다.
        row = await store("take", _secret_key("refresh", refresh_token.token))
        if row and row.get("access_key"):
            await store("delete", row["access_key"])
        return await self._issue_tokens(
            client.client_id, scopes or refresh_token.scopes, refresh_token.resource, row.get("subject") if row else None
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = await store("get", _secret_key("access", token))
        if row:
            if row["expires_at"] < time.time():
                return None
            subject = row.get("subject")
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
                client_id=row["client_id"],
                scopes=row["scopes"],
                expires_at=int(row["expires_at"]),
                resource=row.get("resource"),
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
        # 액세스·리프레시 어느 쪽으로 폐기해도 짝까지 같이 지운다.
        for kind, pair_field in (("access", "refresh_key"), ("refresh", "access_key")):
            key = _secret_key(kind, token.token)
            row = await store("take", key)
            if row and row.get(pair_field):
                await store("delete", row[pair_field])
