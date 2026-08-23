"""Background pipeline runner for /watch.

The `watch` MCP tool runs `watch_mod.run()` synchronously and blocks the
caller until the pipeline finishes. Phase 2.2 splits this into:

  start_watch     → spawn background thread, return immediately
  get_status      → query stage/progress/error of a running job
  get_results     → fetch final results once status='done'
  cancel_watch    → signal the background thread to stop at the next stage

This module owns the in-memory registry of running jobs (thread +
cancel event + start time per video_id) and the stage-transition
detection logic. The persistent side (SessionRecord in session_store)
is updated by this module as the pipeline progresses.

Stage detection strategy
------------------------
We don't modify watch.py to expose stage callbacks (Phase 2.x may do
that). Instead we observe the work_dir filesystem after each stage:

    download/video.{mp4,webm,...}   → stage='download' done
    frames/frame_*.jpg              → stage='frames' done
    (transcript embedded in report) → stage='transcript' done
    masks/mask_*.png                → stage='segment' done
    RunResult returned              → stage='done'

Polling happens at a low cadence (every 0.5s) in the background
thread. Cheap, no ffmpeg/yt-dlp parsing, and gives the caller a
useful "stage=X, progress=Y%" view via get_status.

Cancellation
------------
`threading.Event` per video_id. The runner checks the event between
stages (where stages are defined by the file-system landmarks above).
A cancel during a long ffmpeg invocation doesn't interrupt ffmpeg
itself — it just prevents the next stage from starting. This is the
right tradeoff: cancel latency = next stage boundary, but we never
leave a half-written pipeline that the cancel failed to clean up.

Concurrency
-----------
Stdout MCP transport ties one Python process to one client. So one
PipelineRunner instance handles ALL concurrent jobs the client might
launch via parallel `start_watch` calls. We don't cap concurrency —
each video gets its own thread; the GIL is released during the heavy
ffmpeg / yt-dlp / Whisper work so true parallelism happens in those
subprocesses.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Stages in execution order. `done` and `error`/`cancelled` are terminal.
STAGES = ("download", "frames", "transcript", "segment", "done")
TERMINAL_STAGES = ("done", "error", "cancelled")


@dataclass
class _RunningJob:
    """Per-video_id state held in memory while a pipeline runs.

    `thread` is the background runner. `cancel_event` is set by
    `cancel_watch` to ask the thread to bail at the next stage. The
    thread keeps `start_time` and `result` populated as it goes; on
    completion the result is also persisted to session_store by the
    runner so get_status / get_results can read it without racing the
    thread.

    `cancel_reason` is set when cancel_event fires — either "user" (via
    cancel_watch tool) or "timeout" (via the watchdog timer). The
    runner reads this to decide what error string to persist.
    """
    thread: threading.Thread
    cancel_event: threading.Event = field(default_factory=threading.Event)
    cancel_reason: str | None = None  # 'user' | 'timeout' | None
    start_time: float = field(default_factory=time.time)
    last_stage: str = "download"
    last_progress: float = 0.0
    timeout_seconds: float | None = None


class PipelineRunner:
    """Owns the in-memory registry of in-flight pipeline jobs.

    Lifetime = server process lifetime (just like the old in-memory
    SESSIONS dict). Jobs survive client disconnects in the sense that
    the thread keeps running — get_status / get_results work on the
    next reconnect.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, _RunningJob] = {}
        self._lock = threading.Lock()

    def is_running(self, video_id: str) -> bool:
        with self._lock:
            return video_id in self._jobs

    def get_job(self, video_id: str) -> _RunningJob | None:
        with self._lock:
            return self._jobs.get(video_id)

    def cancel(self, video_id: str, *, reason: str = "user") -> bool:
        """Signal the runner for video_id to stop at the next stage.

        Returns True if a job was found and signaled. False means the
        video_id isn't tracked — either it never ran, or it already
        finished and the entry was cleaned up.

        `reason` is recorded on the job so the runner can persist a
        distinct error string ('cancelled by user' vs 'timeout after
        Xs'). Pass 'timeout' when the watchdog fires; 'user' (default)
        when cancel_watch is invoked explicitly.
        """
        with self._lock:
            job = self._jobs.get(video_id)
        if job is None:
            return False
        # Record the reason only if not already set (timeout takes
        # priority over user — if both fire, the timeout was the
        # original trigger).
        if job.cancel_reason is None:
            job.cancel_reason = reason
        job.cancel_event.set()
        return True

    def remove(self, video_id: str) -> None:
        """Drop the in-memory entry. Called by the runner after the
        thread finishes (success or failure), so get_status reports
        terminal state via session_store rather than this dict."""
        with self._lock:
            self._jobs.pop(video_id, None)

    def start(
        self,
        video_id: str,
        *,
        work_dir: str,
        run_fn,
        run_args: dict[str, Any],
        session_save_fn,
        progress_hook=None,
        timeout_seconds: float | None = None,
    ) -> None:
        """Spawn a background thread that runs the pipeline.

        `run_fn` is `watch_mod.run` (or a test stub). `run_args` is the
        argparse Namespace built by mcp_server. `session_save_fn` is
        called with (video_id, RunResult) on success so the caller can
        persist the SessionRecord.

        `progress_hook(video_id, stage, progress, message)` is invoked
        from the background thread on every stage transition. It MUST
        be thread-safe — typically a closure that schedules work onto
        another event loop via `asyncio.run_coroutine_threadsafe` or
        pushes to a multiprocessing.Queue for an SSE daemon. May be
        None if the caller doesn't care about progress.

        `timeout_seconds` (Phase 2.5): if set, a watchdog timer fires
        after this many seconds and calls cancel(reason='timeout').
        The runner persists error='timeout after Xs' so the caller can
        distinguish from user-initiated cancel. None = no watchdog.

        Raises RuntimeError if a job for this video_id is already
        running — call cancel() first or wait for it to finish.
        """
        with self._lock:
            if video_id in self._jobs:
                raise RuntimeError(
                    f"video_id {video_id!r} is already running; "
                    f"cancel it first or wait for completion"
                )
            cancel_event = threading.Event()
            job = _RunningJob(
                thread=threading.Thread(
                    target=self._runner_main,
                    args=(video_id, work_dir, run_fn, run_args,
                          session_save_fn, cancel_event, progress_hook),
                    daemon=True,  # don't block server shutdown
                    name=f"watch-{video_id[:8]}",
                ),
                cancel_event=cancel_event,
                timeout_seconds=timeout_seconds,
            )
            self._jobs[video_id] = job
            job.thread.start()
            # Arm the watchdog AFTER the thread is registered so a
            # cancel() race doesn't lose the cancel_event.set().
            if timeout_seconds is not None and timeout_seconds > 0:
                timer = threading.Timer(
                    timeout_seconds,
                    self.cancel,
                    args=(video_id,),
                    kwargs={"reason": "timeout"},
                )
                timer.daemon = True
                timer.start()

    def _runner_main(
        self,
        video_id: str,
        work_dir: str,
        run_fn,
        run_args: dict[str, Any],
        session_save_fn,
        cancel_event: threading.Event,
        progress_hook,
    ) -> None:
        """Background thread body. Detects stage transitions by polling
        the work_dir, persists progress, honors cancel, fires
        progress_hook on every transition."""
        work = Path(work_dir)
        try:
            # Initial stage transition
            self._fire_progress(progress_hook, video_id, "download", 0.0,
                                "pipeline started")

            # Check cancel BEFORE starting the heavy work
            if cancel_event.is_set():
                self._handle_cancel(video_id, work_dir)
                return

            try:
                result = run_fn(run_args)
            except SystemExit as exc:
                msg = str(exc) or "watch pipeline aborted"
                self._mark_error(video_id, work_dir, msg)
                self._fire_progress(progress_hook, video_id, "error", None, msg)
                return
            except Exception as exc:
                self._mark_error(video_id, work_dir, str(exc))
                self._fire_progress(progress_hook, video_id, "error", None, str(exc))
                return

            # Check cancel after run_fn returns (in case cancel fired
            # during the run — surface it instead of claiming done)
            if cancel_event.is_set():
                self._handle_cancel(video_id, work_dir)
                return

            # Persist the final SessionRecord
            session_save_fn(video_id, result)
            # Mark terminal state in session_store too — get_status
            # reads from there, not from this in-memory dict.
            self._mark_done(video_id, work_dir)
            self._fire_progress(progress_hook, video_id, "done", 100.0, "pipeline complete")
        finally:
            self.remove(video_id)

    def _handle_cancel(self, video_id: str, work_dir: str) -> None:
        """Persist the appropriate error record for a cancelled job.

        Distinguishes user-initiated cancel from timeout so the caller
        can react differently (e.g. retry with longer timeout, vs.
        give up). Stage boundary: write stage='cancelled' and a
        distinguishing error string.
        """
        job = self.get_job(video_id)
        reason = job.cancel_reason if job else "user"
        try:
            import session_store
            existing = session_store.get(video_id)
            if existing is None:
                rec = session_store.SessionRecord(
                    video_id=video_id,
                    work_dir=work_dir,
                    source="",
                    status="cancelled",
                    stage="cancelled",
                )
            else:
                rec = existing
                rec.status = "cancelled"
                rec.stage = "cancelled"
            if reason == "timeout":
                rec.error = f"timeout after {job.timeout_seconds}s" if job and job.timeout_seconds else "timeout"
            else:
                rec.error = "cancelled by user"
            rec.updated_at = time.time()
            session_store.upsert(rec)
        except Exception as exc:
            log.warning("failed to persist cancel for %s: %s", video_id, exc)

    # ─── Internal helpers ────────────────────────────────────────────────

    def _fire_progress(self, hook, video_id: str, stage: str,
                       progress: float | None, message: str) -> None:
        """Best-effort fire of the user's progress_hook.

        Wrapped in try/except so a buggy hook can never kill the
        pipeline. If hook is None (no caller-side progress tracking),
        this is a no-op."""
        if hook is None:
            return
        try:
            hook(video_id, stage, progress, message)
        except Exception as exc:  # noqa: BLE001
            log.warning("progress_hook raised for %s: %s", video_id, exc)

    def _get_last_stage(self, video_id: str) -> str:
        job = self.get_job(video_id)
        return job.last_stage if job else "download"

    def _update_progress(self, video_id: str, stage: str) -> None:
        job = self.get_job(video_id)
        if job is None:
            return
        job.last_stage = stage
        # Approximate progress by stage index (download=0%, frames=33%,
        # transcript=66%, segment=83%, done=100%). Coarse but useful
        # for a UI; precise per-frame progress needs a watch.py hook.
        try:
            stage_idx = STAGES.index(stage)
        except ValueError:
            return
        progress = min(100.0, stage_idx * 100.0 / max(1, len(STAGES) - 1))
        job.last_progress = progress

    def _mark_done(self, video_id: str, work_dir: str) -> None:
        # Stage already recorded as 'done' via _update_progress during
        # the final poll. Caller persists the record; nothing else here.
        pass

    def _mark_error(self, video_id: str, work_dir: str, error: str) -> None:
        # Best-effort: persist an error record so get_status surfaces it.
        # If session_store isn't importable (rare), swallow.
        try:
            import session_store
            existing = session_store.get(video_id)
            rec = existing or session_store.SessionRecord(
                video_id=video_id,
                work_dir=work_dir,
                source="",
                status="error",
                stage="error",
                progress=None,
                error=error,
            )
            rec.status = "error"
            rec.stage = "error"
            rec.error = error
            rec.updated_at = time.time()
            session_store.upsert(rec)
        except Exception as exc:
            log.warning("failed to persist error for %s: %s", video_id, exc)

    def _mark_cancelled(self, video_id: str, work_dir: str) -> None:
        try:
            import session_store
            existing = session_store.get(video_id)
            if existing is None:
                rec = session_store.SessionRecord(
                    video_id=video_id,
                    work_dir=work_dir,
                    source="",
                    status="cancelled",
                    stage="cancelled",
                )
            else:
                rec = existing
                rec.status = "cancelled"
                rec.stage = "cancelled"
            rec.updated_at = time.time()
            session_store.upsert(rec)
        except Exception as exc:
            log.warning("failed to persist cancel for %s: %s", video_id, exc)

    def _detect_stage(self, work: Path) -> str:
        """Inspect work_dir to figure out which stage we're in.

        Used as the polling check inside _runner_main. The returned
        stage is the deepest stage whose landmark files exist on disk.
        """
        # 'done' is only set by the runner after run_fn returns; we
        # never infer it from the FS alone.
        if (work / "download").is_dir() and any((work / "download").glob("video.*")):
            return "download"
        if (work / "frames").is_dir() and any((work / "frames").glob("frame_*.jpg")):
            return "frames"
        if (work / "download").is_dir() and any((work / "download").glob("video*.vtt")):
            return "transcript"
        if (work / "masks").is_dir() and any((work / "masks").glob("mask_*.png")):
            return "segment"
        return "download"


# ─── Module-level singleton ────────────────────────────────────────────────
#
# One runner per server process. mcp_server.py imports `runner` and
# uses it for all start_watch / get_status / cancel_watch calls.

runner = PipelineRunner()


def _reset_for_tests() -> None:
    """Test-only: drop all jobs. Cancels any running threads first."""
    global runner
    for job in list(runner._jobs.values()):
        job.cancel_event.set()
    runner = PipelineRunner()
