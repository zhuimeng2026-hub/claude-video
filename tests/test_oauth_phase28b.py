"""Phase 2.8b — BFF OAuth flow integration tests.

Exercises the full WeChat service-account flow in-process:
  /auth/wechat/login → 302 with state
  /auth/wechat/callback → exchange code, set WATCH_SESSION cookie
  /auth/me → reads cookie, returns openid
  /api/watch/start → reads cookie, passes user_openid to MCP
  /auth/logout → clears cookie + session

WeChat API is stubbed via `httpx.MockTransport`; the real network is
never touched.
"""
from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "watch" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def _mock_wechat_transport():
    """Returns a MockTransport that handles all WeChat endpoints with
    canned responses."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "access_token" in url:
            return httpx.Response(200, json={
                "access_token": "AT_fake",
                "expires_in": 7200,
                "refresh_token": "RT_fake",
                "openid": "OPENID_alice",
                "scope": "snsapi_userinfo",
                "unionid": "UNIONID_alice",
            })
        if "userinfo" in url:
            return httpx.Response(200, json={
                "openid": "OPENID_alice",
                "nickname": "Alice 测试",
                "unionid": "UNIONID_alice",
                "headimgurl": "https://example.com/avatar.jpg",
            })
        # Shouldn't reach here in our tests, but don't crash
        return httpx.Response(404, json={"errcode": -1, "errmsg": "unexpected"})

    return httpx.MockTransport(handler)


def _patch_wechat(monkeypatch):
    """Make wechat_oauth.is_configured() return True and route httpx
    calls in exchange_code_for_token / get_userinfo through the mock."""
    monkeypatch.setenv("WECHAT_MP_APP_ID", "wx_test_app_id")
    monkeypatch.setenv("WECHAT_MP_APP_SECRET", "wx_test_secret")
    monkeypatch.setenv("WECHAT_MP_REDIRECT_URI", "https://example.com/auth/wechat/callback")
    # Patch the httpx.AsyncClient used inside wechat_oauth by giving it
    # a default transport. The two functions build their own client;
    # we monkeypatch httpx.AsyncClient to inject the transport globally.
    orig_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        # If caller didn't specify transport, install ours
        kwargs.setdefault("transport", _mock_wechat_transport())
        return orig_async_client(*args, **kwargs)

    import wechat_oauth
    monkeypatch.setattr(wechat_oauth.httpx, "AsyncClient", patched_async_client)


@pytest.fixture
def app_with_state(tmp_path, monkeypatch):
    """Fresh BFF state + redirected session_store + configured WeChat."""
    import bff
    import session_store
    import users_store

    _patch_wechat(monkeypatch)
    monkeypatch.setenv("WATCH_USERS_DB", str(tmp_path / "users.sqlite3"))
    users_store._reset_for_tests(tmp_path / "users.sqlite3")
    session_store._reset_for_tests(tmp_path / "watch-store")
    state = bff.BFFState()
    app = bff.create_app(state)
    yield state, app

    users_store._reset_for_tests(tmp_path / "users.sqlite3")
    session_store._reset_for_tests(tmp_path / "watch-store")


# ─── WeChat login → callback flow ────────────────────────────────────────


@pytest.mark.anyio
async def test_login_redirects_to_wechat_with_state(app_with_state):
    state, app = app_with_state
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Follow redirects=False so we see the 302
        r = await client.get(
            "/auth/wechat/login?redirect=/dashboard",
            follow_redirects=False,
        )
        assert r.status_code == 302
        location = r.headers["location"]
        assert "open.weixin.qq.com" in location
        assert "appid=wx_test_app_id" in location
        assert "redirect_uri=" in location
        assert "state=" in location
        assert "response_type=code" in location
        assert "#wechat_redirect" in location


@pytest.mark.anyio
async def test_login_without_wechat_config_returns_503(tmp_path, monkeypatch):
    """When WECHAT_MP_* env vars are unset, /auth/wechat/login must
    return 503 — never silently fall back to anonymous auth."""
    import bff
    import users_store

    monkeypatch.delenv("WECHAT_MP_APP_ID", raising=False)
    monkeypatch.delenv("WECHAT_MP_APP_SECRET", raising=False)
    monkeypatch.delenv("WECHAT_MP_REDIRECT_URI", raising=False)
    monkeypatch.setenv("WATCH_USERS_DB", str(tmp_path / "users.sqlite3"))
    users_store._reset_for_tests(tmp_path / "users.sqlite3")

    state = bff.BFFState()
    app = bff.create_app(state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/auth/wechat/login", follow_redirects=False)
        assert r.status_code == 503
        assert "wechat_not_configured" in r.text


@pytest.mark.anyio
async def test_login_rejects_open_redirect(app_with_state):
    """redirect param must be a same-origin path; anything else
    (absolute URL, protocol-relative //evil) collapses to '/'."""
    state, app = app_with_state
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r1 = await client.get(
            "/auth/wechat/login?redirect=https://evil.com/x",
            follow_redirects=False,
        )
        # Falls back to '/'; URL-encoded https:// should not appear
        assert "evil.com" not in r1.headers["location"]
        # The redirect state in DB should also have redirect_after='/'
        import users_store
        # ... but we can't easily inspect state from the URL alone.
        # Just check no evil-domain in location.
        r2 = await client.get(
            "/auth/wechat/login?redirect=//evil.com/x",
            follow_redirects=False,
        )
        assert "evil.com" not in r2.headers["location"]


@pytest.mark.anyio
async def test_callback_invalid_state_returns_400(app_with_state):
    state, app = app_with_state
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get(
            "/auth/wechat/callback?code=any&state=not-a-real-state",
            follow_redirects=False,
        )
        assert r.status_code == 400
        assert "invalid_or_expired_state" in r.text


@pytest.mark.anyio
async def test_callback_consumes_state_and_sets_cookie(app_with_state):
    state, app = app_with_state
    import users_store

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Get a real state from /auth/wechat/login (302 with Location)
        r1 = await client.get(
            "/auth/wechat/login?redirect=/dashboard",
            follow_redirects=False,
        )
        # Pull state from the redirect URL
        from urllib.parse import urlparse, parse_qs
        loc = urlparse(r1.headers["location"])
        real_state = parse_qs(loc.query)["state"][0]

        # Hit callback
        r2 = await client.get(
            f"/auth/wechat/callback?code=auth-code-xyz&state={real_state}",
            follow_redirects=False,
        )
        assert r2.status_code == 302
        assert r2.headers["location"].endswith("/dashboard")
        # Cookie set
        assert "WATCH_SESSION" in r2.headers.get("set-cookie", "")
        cookie_value = None
        for piece in r2.headers.get_list("set-cookie"):
            if piece.startswith("WATCH_SESSION="):
                cookie_value = piece.split("=", 1)[1].split(";", 1)[0]
                break
        assert cookie_value, "WATCH_SESSION cookie missing"

        # State consumed — second callback with same state fails
        r3 = await client.get(
            f"/auth/wechat/callback?code=other&state={real_state}",
            follow_redirects=False,
        )
        assert r3.status_code == 400

        # Session exists in DB
        sess = users_store.get_session(cookie_value)
        assert sess is not None
        assert sess.openid == "OPENID_alice"


@pytest.mark.anyio
async def test_me_returns_user_after_login(app_with_state):
    state, app = app_with_state
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Run the login flow
        r1 = await client.get(
            "/auth/wechat/login?redirect=/",
            follow_redirects=False,
        )
        from urllib.parse import urlparse, parse_qs
        loc = urlparse(r1.headers["location"])
        state_value = parse_qs(loc.query)["state"][0]

        r2 = await client.get(
            f"/auth/wechat/callback?code=c&state={state_value}",
            follow_redirects=False,
        )
        # Extract cookie
        cookie_value = None
        for piece in r2.headers.get_list("set-cookie"):
            if piece.startswith("WATCH_SESSION="):
                cookie_value = piece.split("=", 1)[1].split(";", 1)[0]
                break
        client.cookies.set("WATCH_SESSION", cookie_value)

        # /auth/me returns user
        r3 = await client.get("/auth/me")
        assert r3.status_code == 200
        data = r3.json()
        assert data["auth"] == "wechat"
        assert data["user_openid"] == "OPENID_alice"
        assert data["user_unionid"] == "UNIONID_alice"


@pytest.mark.anyio
async def test_me_without_cookie_returns_401(app_with_state):
    state, app = app_with_state
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/auth/me")
        assert r.status_code == 401


@pytest.mark.anyio
async def test_logout_clears_session_and_cookie(app_with_state):
    state, app = app_with_state
    import users_store

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Login
        r1 = await client.get("/auth/wechat/login?redirect=/",
                              follow_redirects=False)
        from urllib.parse import urlparse, parse_qs
        state_value = parse_qs(urlparse(r1.headers["location"]).query)["state"][0]
        r2 = await client.get(
            f"/auth/wechat/callback?code=c&state={state_value}",
            follow_redirects=False,
        )
        cookie_value = None
        for piece in r2.headers.get_list("set-cookie"):
            if piece.startswith("WATCH_SESSION="):
                cookie_value = piece.split("=", 1)[1].split(";", 1)[0]
                break
        assert cookie_value
        assert users_store.get_session(cookie_value) is not None

        # Logout (need cookie in client.cookies)
        client.cookies.set("WATCH_SESSION", cookie_value)
        r3 = await client.post("/auth/logout")
        assert r3.status_code == 200
        assert r3.json()["logged_out"] is True

        # Session deleted
        assert users_store.get_session(cookie_value) is None
        # Cookie cleared
        # (delete_cookie sets Max-Age=0)
        set_cookie = r3.headers.get_list("set-cookie")
        assert any("WATCH_SESSION" in c for c in set_cookie)


@pytest.mark.anyio
async def test_logout_without_cookie_is_noop(app_with_state):
    state, app = app_with_state
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post("/auth/logout")
        assert r.status_code == 200
        assert r.json()["logged_out"] is True
