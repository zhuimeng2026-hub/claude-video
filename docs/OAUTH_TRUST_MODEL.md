# claude-video OAuth trust model — what BFF vs OpenMontage each verify

> This document is the contract for **who trusts whom** in the
> end-to-end flow:
>
> `Browser ─cookie WATCH_SESSION─> BFF (verifies) ─stdio MCP─> recompose tool (passes through) ─stdio MCP─> OpenMontage adapter (trusts)`
>
> If you change anything below, the matching change must land in
> `OpenMontage_Voicebox/docs/claude-video-integration.md` §4.4
> (error code table) and `docs/todo.md` §2.8 in lockstep.

## The short version

**OpenMontage treats `user_openid` as untrusted input.** It uses the
string to compute `projects/users/<user_openid>/<project_id>/assets/`,
nothing else. The guarantee that "user A cannot write into user B's
directory" depends entirely on the BFF correctly:

1. validating the `WATCH_SESSION` cookie,
2. resolving it to exactly one `user_openid`, and
3. passing that `user_openid` (and only that) into `recompose`'s
   `user_openid` parameter.

If the BFF is buggy and forwards a cookie-bearing user's chosen
`user_openid` instead of the one bound to the cookie, OM will silently
honor it. **OM cannot detect this. BFF is the single point of
defense.**

## What BFF verifies

| Check | Where | Failure mode |
|---|---|---|
| `WATCH_SESSION` cookie present and signed | `Depends(require_user)` middleware | 401 `{error: "not_authenticated"}` → frontend redirects to `/auth/wechat/login` |
| Cookie's `session_id` exists in `users.sqlite3` `sessions` table | same | 401 `{error: "session_expired"}` |
| Session not expired | same | 401 `{error: "session_expired"}` |
| `user_openid` passed to MCP tools matches the cookie's `user_openid` (NOT the caller's claim) | `recompose` tool wrapper | 403 `{error: "user_openid_mismatch"}` — see `OAUTH_TRUST_MODEL.md` §MCP tool layer below |

The BFF MUST NOT trust a `user_openid` parameter from the browser.
If the browser sends `{user_openid: "other_users_openid"}` in the
POST body, BFF silently overrides it with the one bound to the cookie.

## What `recompose` MCP tool verifies

The MCP tool layer adds a second line of defense at the boundary
between "what the HTTP caller asked for" and "what the stdio MCP
session actually does":

| Check | Where | Failure mode |
|---|---|---|
| `video_id` exists in `session_store` (Phase 2.1) | `recompose` tool body | `ToolError(code="video_id_unknown")` |
| `session_store[video_id]["user_openid"]` matches the call's `user_openid` parameter | `recompose` tool body | `ToolError(code="user_not_found")` or a tighter `code="video_id_user_mismatch"` (decision pending — see "Open questions" below) |
| `pipeline` in whitelist | `recompose` tool body | `ToolError(code="pipeline_not_in_whitelist")` |
| `pipeline` not in GPU-required blacklist | `recompose` tool body | `ToolError(code="gpu_required")` |
| `assets_copy_failed` check after rsync | `recompose` tool body | `ToolError(code="assets_copy_failed")` |

The `video_id ↔ user_openid` check exists to prevent horizontal
access at the MCP layer even if the BFF gets compromised: an attacker
who somehow knows another user's `video_id` still can't recompose it
without also having access to that user's `user_openid`.

## What OpenMontage verifies

**Nothing about the user's identity.** OM's
`tools/external/claude_video.py` adapter:

- Receives `user_openid` as a string parameter.
- Computes `target_dir = projects/users/<user_openid>/<project_id>/assets/`.
- Creates that directory (mkdir -p).
- Copies artifacts there.
- Returns success/failure.

If `user_openid` is empty when OM needs it, OM is allowed to fail
with a tool error — but OM MUST NOT try to validate the string
content (e.g. UUID format, length, character set). It is the BFF's
job to deliver a clean string; OM's job is to use it.

## User-key uniqueness model

Three namespaces are possible, and they need a deliberate choice:

| Option | Use case | Risk |
|---|---|---|
| `openid` alone | Same user across multiple service accounts (公众号 / open platform) would get different `openid` and be treated as different users | Data fragmentation if user grants access across channels |
| `unionid` alone | Same WeChat Open Platform union across all channels under one org | Doesn't disambiguate across multiple orgs the same human is in (rare) |
| `openid@provider` (e.g. `oXyz...@wechat_mp`) | Namespaced, unambiguous, easy to filter | Bigger strings in OM paths |

**Current decision (subject to §7 open question in
`docs/openmontage-integration-inputs.md`)**: Use `openid` alone for
MVP. WeChat service accounts (`公众号`) and open-platform accounts
(`开放平台`) are not expected to share users in claude-video's MVP
scope. Document the trade-off here so OM can switch to namespaced
later without changing the BFF contract.

## Env-var compatibility

Both repos read the **same names** for WeChat OAuth config:

```dotenv
WECHAT_MP_APP_ID=...
WECHAT_MP_APP_SECRET=...
WECHAT_MP_REDIRECT_URI=https://your-domain/auth/wechat/callback
WATCH_BFF_PUBLIC_URL=https://your-domain
WATCH_BFF_COOKIE_SECURE=true
```

If a rename is needed, it lands in **both** repos in the same commit.
Do not ship a rename on one side without the other.

## Untrusted-input matrix (one-shot reference)

| Field | BFF accepts from browser | BFF forwards to MCP | MCP forwards to OM |
|---|---|---|---|
| `source` (URL/path) | yes | yes | yes |
| `video_id` (Phase 2.1) | yes | yes | yes |
| `pipeline` | yes | yes (after whitelist check) | yes (OM re-checks) |
| `style` | yes | yes (after playbook check) | yes (OM re-checks) |
| `user_openid` | **no — silently overridden by cookie-bound value** | yes (after cookie verify) | yes (string only) |
| `extra` (transparent dict) | yes | yes | yes — OM must whitelist keys |

## Related docs

- `docs/BFF_API_CONTRACT.md` — the HTTP surface that sits above this trust model
- `docs/MCP_SERVER_PRD.md` §2.6 — the `recompose` tool and its error envelope
- `docs/todo.md` §2.8 — the OAuth implementation checklist
- `docs/openmontage-integration-inputs.md` §4, §7 — origin of this doc + open questions
