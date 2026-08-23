#!/usr/bin/env python3
"""Cron entry: GC expired sessions from both stores.

Phase 3.1 — replaces todo.md's deferred "自动清理过期 session" item.
Both stores already have cleanup helpers; this script:

  - Loads both DBs (session_store.json + users.sqlite3)
  - Calls each store's cleanup routine
  - Prints structured stats (counts deleted, counts remaining)
  - Exits 0 always — cleanup is best-effort, errors are logged but
    don't fail the cron job (we don't want ops woken up for a
    transient SQLite lock contention).

Why a script (not in-process timer):
  - Avoids a thread per BFF instance
  - External scheduler (cron / systemd timer / k8s CronJob) is the
    single source of truth for "when to clean"
  - Operators can change cadence without code changes
  - DB connections don't need to live inside any process

Run:
  # One-shot:
  python3 skills/watch/scripts/cleanup.py

  # Cron entry (every hour):
  0 * * * * cd /opt/claude-video && python3 skills/watch/scripts/cleanup.py --quiet

  # Systemd timer example (cleanup.timer):
  [Timer]
  OnCalendar=hourly
  Persistent=true
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import session_store  # noqa: E402
import users_store  # noqa: E402

log = logging.getLogger("cleanup")


def _cleanup_session_store() -> dict:
    """Delete SessionRecords whose status is in a terminal state
    AND whose updated_at is older than the retention window.

    Retention: 30 days for done, 7 days for error/cancelled
    (Phase 3.x tunable; conservatively long for now).
    """
    DONE_RETENTION = 30 * 24 * 3600
    ERROR_RETENTION = 7 * 24 * 3600
    now = time.time()

    all_records = session_store.load_all()
    to_delete: list[str] = []
    for vid, rec in all_records.items():
        age = now - (rec.updated_at or 0)
        if rec.status == "done" and age > DONE_RETENTION:
            to_delete.append(vid)
        elif rec.status in ("error", "cancelled") and age > ERROR_RETENTION:
            to_delete.append(vid)
        # running records: keep (something might still be active)

    for vid in to_delete:
        session_store.delete(vid)

    return {
        "scanned": len(all_records),
        "deleted": len(to_delete),
        "remaining": len(all_records) - len(to_delete),
    }


def _cleanup_users_store() -> dict:
    """Delegate to users_store.cleanup_expired_sessions which already
    handles both `sessions` and `oauth_states` tables."""
    deleted_sessions = users_store.cleanup_expired_sessions()
    # Count current state for the report
    with users_store.get_conn() as conn:
        cur_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        cur_states = conn.execute("SELECT COUNT(*) FROM oauth_states").fetchone()[0]
        cur_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    return {
        "sessions_deleted": deleted_sessions,
        "sessions_remaining": cur_sessions,
        "oauth_states_remaining": cur_states,
        "users_total": cur_users,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="watch MCP cleanup")
    parser.add_argument("--quiet", action="store_true",
                        help="only log warnings/errors (cron-friendly)")
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable stats instead of human prose")
    args = parser.parse_args()

    level = logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    started = time.time()
    log.info("cleanup starting")

    try:
        session_stats = _cleanup_session_store()
        log.info("session_store: %s", session_stats)
    except Exception as exc:
        log.exception("session_store cleanup failed: %s", exc)
        session_stats = {"error": str(exc)}

    try:
        user_stats = _cleanup_users_store()
        log.info("users_store: %s", user_stats)
    except Exception as exc:
        log.exception("users_store cleanup failed: %s", exc)
        user_stats = {"error": str(exc)}

    elapsed = time.time() - started

    payload = {
        "elapsed_seconds": round(elapsed, 3),
        "session_store": session_stats,
        "users_store": user_stats,
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        log.info("done in %.3fs", elapsed)

    # Always exit 0 — best-effort cleanup; cron shouldn't page ops for
    # transient errors. Per-store failures are recorded in the payload.
    return 0


if __name__ == "__main__":
    sys.exit(main())
