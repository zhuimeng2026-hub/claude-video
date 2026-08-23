"""WeChat service-account OAuth client.

Phase 2.8 — uses the same flow as OpenMontage_Voicebox per
`OpenMontage_Voicebox/docs/doc-wechat-open-platform-oauth.md`. The
note in that doc says the project actually picked the 服务号
(service-account) path because the 开放平台 (open platform) website
app requires a paid annual registration; service-account is free
and works for web login via the public WeChat client.

Endpoints we call:
  GET  https://api.weixin.qq.com/sns/oauth2/access_token
       ?appid=...&secret=...&code=...&grant_type=authorization_code
  GET  https://api.weixin.qq.com/sns/oauth2/refresh_token
       ?appid=...&grant_type=refresh_token&refresh_token=...
  GET  https://api.weixin.qq.com/sns/userinfo
       ?access_token=...&openid=...&lang=zh_CN

We don't use the userinfo endpoint to derive identity — openid alone
is sufficient and is what session_store already keys on. userinfo is
optional (only fetched if a scope=userinfo token was granted).

Configuration
-------------
Reads from ~/.config/watch/.env via the existing config module
(Phase 1.2), with env-var fallback for tests / overrides:

  WECHAT_MP_APP_ID         required
  WECHAT_MP_APP_SECRET     required
  WECHAT_MP_REDIRECT_URI   required (must match 服务号 配置)
  WECHAT_MP_SCOPE          optional, default 'snsapi_userinfo'
                           (use 'snsapi_base' for silent login)

Failure modes
-------------
Missing config -> WeChatNotConfiguredError. The BFF /auth/wechat/login
route catches this and returns 503 with a clear "管理员未配置登录"
message, mirroring `web-multiuser-auth.md` §配置的 hard constraint.

WeChat API error -> WeChatAPIError with the WeChat error code + msg.
The callback route catches this and returns 400 with the message;
operator can debug via server logs.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

# Default scope = 'snsapi_userinfo' (returns nickname, avatar, etc.)
# Override to 'snsapi_base' for silent login where user just sees a
# confirmation screen in WeChat.
DEFAULT_SCOPE = "snsapi_userinfo"

OAUTH_AUTHORIZE_URL = "https://open.weixin.qq.com/connect/oauth2/authorize"
ACCESS_TOKEN_URL = "https://api.weixin.qq.com/sns/oauth2/access_token"
REFRESH_TOKEN_URL = "https://api.weixin.qq.com/sns/oauth2/refresh_token"
USERINFO_URL = "https://api.weixin.qq.com/sns/userinfo"


class WeChatNotConfiguredError(Exception):
    """Required env vars missing."""


class WeChatAPIError(Exception):
    """WeChat API returned an error payload."""

    def __init__(self, errcode: int, errmsg: str):
        self.errcode = errcode
        self.errmsg = errmsg
        super().__init__(f"WeChat API error {errcode}: {errmsg}")


@dataclass
class TokenResponse:
    access_token: str
    expires_in: int
    refresh_token: str
    openid: str
    scope: str
    unionid: str | None = None


def _config() -> dict[str, str]:
    """Read WeChat config from env. Raises WeChatNotConfiguredError
    if any required key is missing."""
    missing = []
    cfg = {
        "app_id": os.environ.get("WECHAT_MP_APP_ID", ""),
        "app_secret": os.environ.get("WECHAT_MP_APP_SECRET", ""),
        "redirect_uri": os.environ.get("WECHAT_MP_REDIRECT_URI", ""),
        "scope": os.environ.get("WECHAT_MP_SCOPE", DEFAULT_SCOPE),
    }
    for key, val in cfg.items():
        if key != "scope" and not val:
            missing.append(f"WECHAT_MP_{key.upper()}")
    if missing:
        raise WeChatNotConfiguredError(
            f"WeChat OAuth not configured. Missing env vars: "
            f"{', '.join(missing)}. See docs/todo.md §2.8."
        )
    return cfg


def is_configured() -> bool:
    """Cheap check used by preflight / healthz."""
    try:
        _config()
        return True
    except WeChatNotConfiguredError:
        return False


def build_authorize_url(state: str) -> str:
    """Build the URL the user gets redirected to for WeChat auth."""
    cfg = _config()
    params = {
        "appid": cfg["app_id"],
        "redirect_uri": cfg["redirect_uri"],
        "response_type": "code",
        "scope": cfg["scope"],
        "state": state,
    }
    return f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}#wechat_redirect"


async def exchange_code_for_token(code: str, *,
                                 client: httpx.AsyncClient | None = None,
                                 ) -> TokenResponse:
    """Exchange the OAuth `code` callback param for an access_token.

    See https://developers.weixin.qq.com/doc/oplatform/Website_App/WeChat_Login/Authorized_Interface_Call_UnionID.html
    """
    cfg = _config()
    params = {
        "appid": cfg["app_id"],
        "secret": cfg["app_secret"],
        "code": code,
        "grant_type": "authorization_code",
    }
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        r = await client.get(ACCESS_TOKEN_URL, params=params)
        data = r.json()
    finally:
        if owns_client:
            await client.aclose()
    return _parse_token(data)


async def refresh_access_token(refresh_token: str, *,
                               client: httpx.AsyncClient | None = None,
                               ) -> TokenResponse:
    cfg = _config()
    params = {
        "appid": cfg["app_id"],
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        r = await client.get(REFRESH_TOKEN_URL, params=params)
        data = r.json()
    finally:
        if owns_client:
            await client.aclose()
    return _parse_token(data)


def _parse_token(data: dict[str, Any]) -> TokenResponse:
    if "errcode" in data and data["errcode"] != 0:
        raise WeChatAPIError(
            errcode=data.get("errcode", -1),
            errmsg=data.get("errmsg", "unknown"),
        )
    return TokenResponse(
        access_token=data["access_token"],
        expires_in=int(data["expires_in"]),
        refresh_token=data["refresh_token"],
        openid=data["openid"],
        scope=data.get("scope", ""),
        unionid=data.get("unionid"),
    )


async def get_userinfo(access_token: str, openid: str, *,
                        client: httpx.AsyncClient | None = None) -> dict:
    """Optional: fetch nickname / avatar / unionid. Only available when
    scope was 'snsapi_userinfo' (vs 'snsapi_base')."""
    params = {"access_token": access_token, "openid": openid, "lang": "zh_CN"}
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        r = await client.get(USERINFO_URL, params=params)
        data = r.json()
    finally:
        if owns_client:
            await client.aclose()
    if "errcode" in data and data["errcode"] != 0:
        raise WeChatAPIError(
            errcode=data.get("errcode", -1),
            errmsg=data.get("errmsg", "unknown"),
        )
    return data
