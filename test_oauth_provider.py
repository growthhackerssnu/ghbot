"""OAuth 흐름 자체 점검: 등록 -> 코드 -> 토큰 교환 -> 리프레시 -> 폐기.
admin의 oauth-store를 메모리 dict로 바꿔 끼워 돌린다. 실행: python test_oauth_provider.py
"""
import asyncio
import time

import oauth_provider
from mcp.shared.auth import OAuthClientInformationFull

_db: dict[str, tuple[dict, float | None]] = {}


async def fake_store(op, key, data=None, expires_at=None):
    assert ":" in key and "ghoauth_" not in key and "ghrefresh_" not in key and "ghcode_" not in key, "raw secret in key"
    if op == "put":
        _db[key] = (data, expires_at)
        return None
    row = _db.pop(key, None) if op in ("take", "delete") else _db.get(key)
    return row[0] if row and op != "delete" else None


async def main():
    oauth_provider.store = fake_store
    p = oauth_provider.MemberOAuthProvider()

    client = OAuthClientInformationFull(client_id="c1", redirect_uris=["https://claude.ai/cb"])
    await p.register_client(client)
    assert (await p.get_client("c1")).client_id == "c1"

    await oauth_provider.save_authorization_code(
        "ghcode_x",
        {"client_id": "c1", "scopes": ["*"], "expires_at": time.time() + 60, "code_challenge": "cc",
         "redirect_uri": "https://claude.ai/cb", "redirect_uri_explicit": True, "resource": None, "subject": "홍길동"},
    )
    code = await p.load_authorization_code(client, "ghcode_x")
    assert code and code.subject == "홍길동"

    tok = await p.exchange_authorization_code(client, code)
    assert await p.load_authorization_code(client, "ghcode_x") is None, "code must be single-use"
    access = await p.load_access_token(tok.access_token)
    assert access and access.subject == "홍길동" and access.client_id == "c1"

    rt = await p.load_refresh_token(client, tok.refresh_token)
    tok2 = await p.exchange_refresh_token(client, rt, [])
    assert await p.load_access_token(tok.access_token) is None, "old access token must die on refresh"
    assert await p.load_refresh_token(client, tok.refresh_token) is None, "refresh token must be single-use"
    assert (await p.load_access_token(tok2.access_token)).subject == "홍길동"

    await p.revoke_token(await p.load_refresh_token(client, tok2.refresh_token))
    assert await p.load_access_token(tok2.access_token) is None, "revoking refresh must kill its access token"
    print("ok")


asyncio.run(main())
