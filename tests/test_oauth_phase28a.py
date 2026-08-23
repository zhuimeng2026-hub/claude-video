"""Phase 2.8a — users_store + wechat_oauth unit tests.

Uses httpx.MockTransport to stub WeChat endpoints (no real network)
and a per-test tmp sqlite DB so tests are isolated.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import httpx
import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "watch" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import users_store  # noqa: E402
import wechat_oauth  # noqa: E402


# ─── users_store ─────────────────────────────────────────────────────────


@pytest.fixture
def fresh_users_db(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCH_USERS_DB", str(tmp_path / "users.sqlite3"))
    users_store._reset_for_tests(tmp_path / "users.sqlite3")
    yield tmp_path / "users.sqlite3"
    users_store._reset_for_tests(tmp_path / "users.sqlite3")


def test_upsert_user_creates_then_updates(fresh_users_db):
    users_store.upsert_user("alice-openid", nickname="Alice")
    u = users_store.get_user("alice-openid")
    assert u is not None
    assert u.openid == "alice-openid"
    assert u.nickname == "Alice"
    created_at = u.created_at

    # Bump last_seen by upserting again. SQLite has 1-second resolution
    # for CURRENT_TIMESTAMP in some builds; use time.sleep(1.1).
    time.sleep(1.1)
    users_store.upsert_user("alice-openid", nickname="Alice 2")
    u2 = users_store.get_user("alice-openid")
    assert u2.last_seen > created_at, f"last_seen={u2.last_seen} created_at={created_at}"
    assert u2.nickname == "Alice 2"


def test_upsert_user_with_unionid(fresh_users_db):
    users_store.upsert_user("bob-openid", unionid="bob-unionid")
    u = users_store.get_user("bob-openid")
    assert u.unionid == "bob-unionid"


def test_create_and_get_session(fresh_users_db):
    users_store.upsert_user("alice")
    s = users_store.create_session("alice", ttl=3600)
    assert s.openid == "alice"
    assert s.expires_at > int(time.time())

    fetched = users_store.get_session(s.id)
    assert fetched is not None
    assert fetched.id == s.id


def test_session_expires(fresh_users_db):
    users_store.upsert_user("alice")
    s = users_store.create_session("alice", ttl=1)  # 1 second
    time.sleep(1.5)
    assert users_store.get_session(s.id) is None  # expired, returned None


def test_extend_session_sliding_window(fresh_users_db):
    users_store.upsert_user("alice")
    s = users_store.create_session("alice", ttl=10)
    time.sleep(0.01)
    extended = users_store.extend_session(s.id, ttl=3600)
    assert extended is True
    assert users_store.get_session(s.id).expires_at > s.expires_at


def test_delete_session(fresh_users_db):
    users_store.upsert_user("alice")
    s = users_store.create_session("alice")
    assert users_store.delete_session(s.id) is True
    assert users_store.get_session(s.id) is None
    # Idempotent
    assert users_store.delete_session(s.id) is False


def test_cleanup_expired_sessions(fresh_users_db):
    users_store.upsert_user("alice")
    s_expired = users_store.create_session("alice", ttl=1)
    s_alive = users_store.create_session("alice", ttl=3600)
    time.sleep(1.5)
    deleted = users_store.cleanup_expired_sessions()
    assert deleted == 1
    assert users_store.get_session(s_expired.id) is None
    assert users_store.get_session(s_alive.id) is not None


def test_oauth_state_one_time_use(fresh_users_db):
    """State must be consumed atomically — second consume() returns None."""
    state = users_store.create_oauth_state("/dashboard")
    fetched = users_store.consume_oauth_state(state)
    assert fetched is not None
    assert fetched.redirect_after == "/dashboard"

    # Second consume returns None — one-time use enforced
    second = users_store.consume_oauth_state(state)
    assert second is None


def test_oauth_state_expires(fresh_users_db):
    state = users_store.create_oauth_state("/x", ttl=1)
    time.sleep(1.5)
    assert users_store.consume_oauth_state(state) is None


def test_oauth_state_hash_not_plaintext(fresh_users_db):
    """DB row must store SHA-256(state), not plaintext."""
    state = users_store.create_oauth_state("/x")
    with users_store.get_conn() as conn:
        row = conn.execute(
            "SELECT state_hash FROM oauth_states"
        ).fetchone()
    # Plaintext state must NOT appear in the DB
    assert state not in str(row["state_hash"]), \
        "oauth_states stored plaintext instead of hash"
    # The hash should be 64 hex chars (SHA-256)
    assert len(row["state_hash"]) == 64


def test_corrupt_state_hash_returns_none(fresh_users_db):
    """Tampered state must not be accepted."""
    users_store.create_oauth_state("/x")
    assert users_store.consume_oauth_state("not-the-real-state") is None


# ─── wechat_oauth ─────────────────────────────────────────────────────────


@pytest.fixture
def wechat_env(monkeypatch):
    monkeypatch.setenv("WECHAT_MP_APP_ID", "wx_test_app_id")
    monkeypatch.setenv("WECHAT_MP_APP_SECRET", "wx_test_secret")
    monkeypatch.setenv("WECHAT_MP_REDIRECT_URI", "https://example.com/auth/wechat/callback")


def test_is_configured_requires_all_env(wechat_env):
    assert wechat_oauth.is_configured() is True


def test_is_configured_returns_false_when_missing(monkeypatch):
    monkeypatch.delenv("WECHAT_MP_APP_ID", raising=False)
    assert wechat_oauth.is_configured() is False


def test_exchange_code_for_token_parses_success(wechat_env):
    async def go():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "access_token": "AT_xxx",
                "expires_in": 7200,
                "refresh_token": "RT_xxx",
                "openid": "OPENID_xxx",
                "scope": "snsapi_userinfo",
                "unionid": "UNIONID_xxx",
            })

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            tok = await wechat_oauth.exchange_code_for_token(
                "auth-code-abc", client=client
            )
        assert tok.access_token == "AT_xxx"
        assert tok.openid == "OPENID_xxx"
        assert tok.unionid == "UNIONID_xxx"
        assert tok.expires_in == 7200

    asyncio.run(go())


def test_exchange_code_for_token_raises_on_api_error(wechat_env):
    async def go():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "errcode": 40029,
                "errmsg": "invalid code",
            })

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(wechat_oauth.WeChatAPIError) as ei:
                await wechat_oauth.exchange_code_for_token("bad-code", client=client)
        assert ei.value.errcode == 40029

    asyncio.run(go())


def test_refresh_access_token(wechat_env):
    async def go():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "access_token": "AT_NEW",
                "expires_in": 7200,
                "refresh_token": "RT_NEW",
                "openid": "OPENID_xxx",
                "scope": "snsapi_userinfo",
            })

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            tok = await wechat_oauth.refresh_access_token("RT_OLD", client=client)
        assert tok.access_token == "AT_NEW"
        assert tok.refresh_token == "RT_NEW"

    asyncio.run(go())


def test_get_userinfo_returns_full_payload(wechat_env):
    async def go():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "openid": "OPENID_xxx",
                "nickname": "Test User",
                "sex": 1,
                "province": "Beijing",
                "city": "Beijing",
                "country": "CN",
                "headimgurl": "https://example.com/avatar.jpg",
                "privilege": [],
                "unionid": "UNIONID_xxx",
            })

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            info = await wechat_oauth.get_userinfo("AT", "OPENID_xxx", client=client)
        assert info["nickname"] == "Test User"
        assert info["unionid"] == "UNIONID_xxx"

    asyncio.run(go())


def test_build_authorize_url_includes_state_and_redirect(wechat_env):
    url = wechat_oauth.build_authorize_url("csrf-state-xyz")
    assert "appid=wx_test_app_id" in url
    assert "redirect_uri=" in url
    assert "state=csrf-state-xyz" in url
    assert "response_type=code" in url
    assert "scope=snsapi_userinfo" in url
    # WeChat OAuth URL must end with the magic fragment
    assert url.endswith("#wechat_redirect")


def test_exchange_code_raises_when_not_configured(monkeypatch):
    """Unconfigured env must surface as WeChatNotConfiguredError, not
    silently fall back to a default appid."""
    monkeypatch.delenv("WECHAT_MP_APP_ID", raising=False)
    monkeypatch.delenv("WECHAT_MP_APP_SECRET", raising=False)
    monkeypatch.delenv("WECHAT_MP_REDIRECT_URI", raising=False)

    async def go():
        with pytest.raises(wechat_oauth.WeChatNotConfiguredError):
            await wechat_oauth.exchange_code_for_token("any-code")

    asyncio.run(go())
