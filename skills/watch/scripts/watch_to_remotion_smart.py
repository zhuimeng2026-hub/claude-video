#!/usr/bin/env python3
"""Smart watch → Remotion converter.

Reads a /watch output directory (frames/, download/video.mp4, download/*.vtt,
optional masks/), asks an LLM to design a content-aware composition spec for
THIS video, then scaffolds a full Remotion project where Composition.tsx reads
the spec and renders scene-aware intro/highlights/outro.

Companion to a "dumb" deterministic converter. The dumb version picks a fixed
template; this one lets the LLM decide titles, scenes, highlights, and
subtitle style based on the actual transcript.

Usage:
  python3 watch_to_remotion_smart.py \
      --watch-dir /path/to/watch-output \
      --out-dir   /path/to/remotion-project \
      --llm       openai   # or groq | litellm

Pure stdlib (urllib + json) — no `pip install openai` needed. Mirrors the
stdlib HTTP style of whisper.py.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
from pathlib import Path
from urllib.request import Request, urlopen


SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

from transcribe import parse_vtt  # noqa: E402


# Backend → (default base URL, default chat-completions model, default env var)
BACKENDS = {
    "groq":    ("https://api.groq.com/openai/v1",                "llama-3.3-70b-versatile",       "GROQ_API_KEY"),
    "openai":  ("https://api.openai.com/v1",                     "gpt-4o-mini",                    "OPENAI_API_KEY"),
    # 'litellm' here means "call the DashScope endpoint the project's
    # /root/.claude/litellm.yaml routes qwen-* to". The actual LiteLLM
    # proxy isn't running in most envs; this path matches its configured
    # api_base so behaviour is identical.
    "litellm": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus",                  "DASHSCOPE_API_KEY"),
}

FRAME_FILE_RE = re.compile(r"frame_(\d+)\.jpg$")
MASK_FILE_RE = re.compile(r"mask_(\d+)\.png$")
VTT_GLOB = "video*.vtt"


# ──────────────────────────────────────────────────────────────────────────────
# Watch-output discovery
# ──────────────────────────────────────────────────────────────────────────────

def discover(watch_dir: Path) -> dict:
    """Find frames, video, vtt, masks inside a /watch output directory."""
    watch_dir = watch_dir.expanduser().resolve()
    if not watch_dir.is_dir():
        raise SystemExit(f"watch dir not found: {watch_dir}")

    frames_dir = watch_dir / "frames"
    download_dir = watch_dir / "download"
    masks_dir = watch_dir / "masks"

    frames: list[dict] = []
    if frames_dir.is_dir():
        for path in sorted(frames_dir.glob("frame_*.jpg")):
            match = FRAME_FILE_RE.search(path.name)
            if not match:
                continue
            frames.append({
                "index": int(match.group(1)),
                "path": str(path),
                "filename": path.name,
            })

    video_path: Path | None = None
    if download_dir.is_dir():
        for ext in (".mp4", ".mkv", ".webm", ".mov", ".m4v"):
            for candidate in download_dir.glob(f"video*{ext}"):
                video_path = candidate
                break
            if video_path:
                break

    vtt_path: Path | None = None
    if download_dir.is_dir():
        candidates = sorted(download_dir.glob(VTT_GLOB))
        if candidates:
            # Prefer English variants; fall back to whatever exists.
            preferred = [c for c in candidates if any(m in c.name for m in (".en.", ".en-US.", ".en-GB.", ".en-orig"))]
            vtt_path = preferred[0] if preferred else candidates[0]

    segments: list[dict] = []
    if vtt_path:
        try:
            segments = parse_vtt(str(vtt_path))
        except Exception as exc:
            print(f"[smart] VTT parse failed: {exc}", file=sys.stderr)

    masks: list[dict] = []
    if masks_dir.is_dir():
        for path in sorted(masks_dir.glob("mask_*.png")):
            match = MASK_FILE_RE.search(path.name)
            if match:
                masks.append({"index": int(match.group(1)), "filename": path.name, "path": str(path)})

    duration = _probe_duration(video_path) if video_path else None

    info = _read_info(watch_dir / "download" / "video.info.json")

    return {
        "watch_dir": str(watch_dir),
        "frames": frames,
        "frame_count": len(frames),
        "video_path": str(video_path) if video_path else None,
        "video_present": video_path is not None,
        "vtt_path": str(vtt_path) if vtt_path else None,
        "segments": segments,
        "segment_count": len(segments),
        "duration": duration,
        "masks": masks,
        "info": info,
    }


def _probe_duration(video_path: Path | None) -> float | None:
    if not video_path or shutil.which("ffprobe") is None:
        return None
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(video_path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        return float(json.loads(result.stdout).get("format", {}).get("duration") or 0.0)
    except Exception:
        return None


def _read_info(info_path: Path) -> dict:
    if not info_path.exists():
        return {}
    try:
        raw = json.loads(info_path.read_text(encoding="utf-8"))
        return {
            "title": raw.get("title"),
            "uploader": raw.get("uploader") or raw.get("channel"),
            "duration": raw.get("duration"),
            "url": raw.get("webpage_url"),
        }
    except Exception:
        return {}


# ──────────────────────────────────────────────────────────────────────────────
# Prompt construction
# ──────────────────────────────────────────────────────────────────────────────

PROMPT_TEMPLATE = """You are designing a Remotion composition for a short recap video.
Analyse the source video's transcript and frame metadata, then return a JSON spec
that will drive a custom React/Remotion composition. The composition must use the
provided JPEG frames (already extracted to public/frames/) as the primary visual
medium and overlay timestamped subtitles.

SOURCE VIDEO
- title: {title}
- duration_seconds: {duration}
- frame_count: {frame_count} (frame_NNNN.jpg, evenly spaced at fps={fps})
- transcript_cues: {segment_count}

TRANSCRIPT (WebVTT cues, time-ordered):
```
{transcript}
```

FRAME TIMESTAMPS (one line per frame, file → absolute time):
```
{frame_timestamps}
```

YOUR TASK
Return a single JSON object (no markdown fences, no commentary — pure JSON) with
this exact shape:

{{
  "title": "string — short, punchy headline for the intro card",
  "summary": "1-2 sentence summary of what this video is",
  "duration_seconds": <int, target length 30-90s>,
  "fps": <int, 24|30|60>,
  "intro": {{ "headline": "string", "subline": "string" }},
  "outro": {{ "headline": "string", "cta": "string — call-to-action" }},
  "scenes": [
    {{ "label": "intro", "startFrame": 0, "endFrame": 60, "note": "..." }},
    {{ "label": "highlight-N", "startFrame": 60, "endFrame": 240, "note": "what happens here" }},
    ...
  ],
  "subtitle_style": {{
    "position": "bottom" | "center" | "top",
    "fontSize": <int, 32-72>,
    "color": "#RRGGBB",
    "background": "rgba(0,0,0,0.6)"
  }},
  "highlights": [
    {{ "frameIndex": <int>, "caption": "why this frame matters" }}
  ],
  "render_mode": "frames" | "video"
}}

RULES
1. scenes[] must be a contiguous, non-overlapping partition of [0, duration_seconds*fps).
   The first scene is "intro" starting at frame 0; the last scene is "outro".
2. Pick render_mode "video" ONLY if a real source video exists AND using <OffthreadVideo>
   would feel more natural (e.g. music video, sports highlights). Otherwise use "frames".
3. highlights[] are frames the composition zooms into with a callout. Pick 2-5 moments
   that align with notable transcript cues ("as you can see", choruses, key lines).
4. Use frameIndex values that exist in the FRAME TIMESTAMPS list. Don't invent indices.
5. The transcript text contains the dialogue/narration — let that drive scene boundaries.
6. Keep duration_seconds honest. If the source is 30s, don't claim 90s. Trim or expand
   proportionally to the source.
7. subtitle_style must be readable on top of varied frames — dark background, white text.
"""


def build_prompt(discovered: dict) -> str:
    info = discovered.get("info") or {}
    duration = discovered.get("duration") or info.get("duration") or 30.0
    frame_count = discovered["frame_count"]
    fps = 1.0  # we don't know per-frame rate; report the relationship honestly
    # The watch tool's selection is roughly fps*duration with cap. Reverse-derive fps
    # so the prompt's "evenly spaced at fps=N" reads true.
    if duration > 0 and frame_count > 0:
        fps = round(frame_count / duration, 2)

    # Compact transcript: keep timestamps + text. Truncate very long videos.
    seg_lines = []
    for seg in discovered["segments"]:
        seg_lines.append(f"[{seg['start']:.1f}-{seg['end']:.1f}] {seg['text']}")
    transcript = "\n".join(seg_lines)
    if len(transcript) > 6000:
        transcript = transcript[:6000] + "\n... (truncated)"

    frame_lines = []
    for frame in discovered["frames"][:200]:  # cap to first 200 in prompt
        ts = _frame_to_seconds(frame["index"], frame_count, duration)
        frame_lines.append(f"frame_{frame['index']:04d}.jpg → {ts:.2f}s")

    return PROMPT_TEMPLATE.format(
        title=(info.get("title") or "(unknown)").replace('"', '\\"'),
        duration=f"{duration:.1f}",
        frame_count=frame_count,
        fps=fps,
        segment_count=discovered["segment_count"],
        transcript=transcript,
        frame_timestamps="\n".join(frame_lines),
    )


def _frame_to_seconds(index: int, frame_count: int, duration: float) -> float:
    if frame_count <= 1 or duration <= 0:
        return 0.0
    return (index - 1) / (frame_count - 1) * duration


# ──────────────────────────────────────────────────────────────────────────────
# LLM call (OpenAI-compatible Chat Completions)
# ──────────────────────────────────────────────────────────────────────────────

def call_llm(backend: str, model: str, prompt: str, timeout: int = 120) -> dict:
    """Call any OpenAI-compatible /chat/completions endpoint and return parsed JSON."""
    if backend not in BACKENDS:
        raise SystemExit(f"unknown backend: {backend}. Choices: {', '.join(BACKENDS)}")

    base_url, default_model, env_var = BACKENDS[backend]
    api_key = os.environ.get(env_var) or _read_env_file(env_var)
    if not api_key:
        raise SystemExit(
            f"{backend} backend needs {env_var}. Set it in env or ~/.config/watch/.env."
        )

    model = model or default_model
    url = f"{base_url.rstrip('/')}/chat/completions"

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You return only valid JSON. No markdown, no commentary."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.4,
        "response_format": {"type": "json_object"},
    }
    payload = json.dumps(body).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "watch-smart/0.1 (+claude-code; python-urllib)",
    }

    context = ssl.create_default_context()
    last_exc: Exception | None = None
    for attempt in range(3):
        request = Request(url, data=payload, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=timeout, context=context) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400] if exc.fp else ""
            last_exc = exc
            if 400 <= exc.code < 500 and exc.code != 429:
                raise SystemExit(f"LLM HTTP {exc.code}: {detail}")
            print(f"[smart] HTTP {exc.code} — retry {attempt + 2}/2", file=sys.stderr)
            time.sleep(2 ** attempt)
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
            print(f"[smart] network error ({exc}) — retry {attempt + 2}/2", file=sys.stderr)
            time.sleep(2 ** attempt)
            continue

        try:
            data = json.loads(raw)
            content = data["choices"][0]["message"]["content"]
            return json.loads(content)
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            raise SystemExit(f"LLM returned unparseable response: {exc}: {raw[:400]}")

    raise SystemExit(f"LLM call failed after 3 attempts: {last_exc}")


def _read_env_file(name: str) -> str | None:
    for path in (Path.home() / ".config" / "watch" / ".env", Path.cwd() / ".env"):
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() != name:
                    continue
                value = value.strip().strip('"').strip("'")
                if value:
                    return value
        except OSError:
            continue
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Spec validation & defaults
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_SPEC = {
    "title": "Watch Recap",
    "summary": "A short recap of the source video.",
    "duration_seconds": 30,
    "fps": 30,
    "intro": {"headline": "Watch Recap", "subline": ""},
    "outro": {"headline": "Thanks for watching", "cta": ""},
    "scenes": [],
    "subtitle_style": {
        "position": "bottom",
        "fontSize": 48,
        "color": "#FFFFFF",
        "background": "rgba(0,0,0,0.6)",
    },
    "highlights": [],
    "render_mode": "frames",
}


def validate_and_repair(spec: dict, discovered: dict) -> dict:
    """Apply defaults, normalize scenes to a contiguous partition, drop bad frame refs."""
    out = {**DEFAULT_SPEC, **(spec or {})}
    out.setdefault("intro", DEFAULT_SPEC["intro"])
    out.setdefault("outro", DEFAULT_SPEC["outro"])
    out.setdefault("subtitle_style", DEFAULT_SPEC["subtitle_style"])

    fps = int(out.get("fps") or 30)
    if fps not in (24, 30, 60):
        fps = 30
    out["fps"] = fps

    duration = int(out.get("duration_seconds") or 30)
    if duration < 8:
        duration = 8
    if duration > 180:
        duration = 180
    out["duration_seconds"] = duration

    total_frames = duration * fps
    valid_indices = {f["index"] for f in discovered["frames"]}

    # Normalize scenes: sort by startFrame, drop out-of-range, ensure contiguous.
    scenes = out.get("scenes") or []
    norm: list[dict] = []
    cursor = 0
    for scene in sorted(scenes, key=lambda s: int(s.get("startFrame") or 0)):
        start = max(cursor, int(scene.get("startFrame") or cursor))
        end = int(scene.get("endFrame") or (start + fps * 4))
        if end <= start:
            continue
        if end > total_frames:
            end = total_frames
        if start >= end:
            continue
        norm.append({
            "label": str(scene.get("label") or "scene").strip() or "scene",
            "startFrame": start,
            "endFrame": end,
            "note": str(scene.get("note") or ""),
        })
        cursor = end

    if not norm or norm[-1]["endFrame"] < total_frames:
        norm.append({"label": "outro", "startFrame": cursor, "endFrame": total_frames, "note": "outro"})
    elif norm[-1]["endFrame"] > total_frames:
        norm[-1]["endFrame"] = total_frames

    out["scenes"] = norm

    # Filter highlights to known frame indices.
    highlights = []
    for h in out.get("highlights") or []:
        idx = int(h.get("frameIndex") or 0)
        if idx in valid_indices:
            highlights.append({"frameIndex": idx, "caption": str(h.get("caption") or "")[:200]})
    out["highlights"] = highlights[:6]

    # Force render_mode = "frames" if no source video.
    if not discovered["video_present"] and out.get("render_mode") == "video":
        print("[smart] no source video — forcing render_mode=frames", file=sys.stderr)
        out["render_mode"] = "frames"
    if out.get("render_mode") not in ("frames", "video"):
        out["render_mode"] = "frames"

    return out


# ──────────────────────────────────────────────────────────────────────────────
# Remotion project scaffolding
# ──────────────────────────────────────────────────────────────────────────────

PACKAGE_JSON = """{
  "name": "__SLUG__",
  "version": "0.1.0",
  "description": "Smart watch-to-Remotion composition (LLM-designed).",
  "scripts": {
    "dev": "remotion studio",
    "build": "remotion render src/index.ts __COMPOSITION_ID__ out.mp4",
    "render": "remotion render src/index.ts __COMPOSITION_ID__ out.mp4"
  },
  "dependencies": {
    "@remotion/cli": "4.0.290",
    "react": "18.3.1",
    "react-dom": "18.3.1",
    "remotion": "4.0.290"
  },
  "devDependencies": {
    "@types/react": "18.3.12",
    "@types/web": "0.0.184",
    "typescript": "5.6.3"
  }
}
"""

REMO_CONFIG = """import { Config } from "@remotion/cli/config";

Config.setVideoImageFormat("jpeg");
Config.setOverwriteOutput(true);
Config.setConcurrency(2);
"""

TS_CONFIG = """{
  "compilerOptions": {
    "target": "ES2020",
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "jsx": "react-jsx",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "resolveJsonModule": true
  },
  "include": ["src"]
}
"""

INDEX_TS = """import { registerRoot } from "remotion";
import { RemotionRoot } from "./Root";

registerRoot(RemotionRoot);
"""

ROOT_TSX = """import React from "react";
import { Composition } from "remotion";
import { Composition_ as CompositionComp } from "./Composition";
import spec from "../public/spec.json";

export const RemotionRoot: React.FC = () => {
  const { duration_seconds, fps, title } = spec;
  return (
    <>
      <Composition
        id={title.replace(/[^a-zA-Z0-9]/g, "_") || "WatchRecap"}
        component={CompositionComp}
        durationInFrames={duration_seconds * fps}
        fps={fps}
        width={1920}
        height={1080}
      />
    </>
  );
};
"""

COMPOSITION_TSX = """import React from "react";
import { AbsoluteFill, Sequence as RemotionSequence, Series, useVideoConfig, staticFile, Audio, OffthreadVideo, Video } from "remotion";
import spec from "../public/spec.json";
import cues from "../public/cues.json";
import { Subtitles } from "./Subtitles";
import { IntroCard } from "./IntroCard";
import { OutroCard } from "./OutroCard";

const { duration_seconds, fps, scenes, highlights, render_mode, subtitle_style } = spec;

type Cue = { start: number; end: number; text: string };

// Frames land at scene-aware timestamps. The composition reads its scene list
// from public/spec.json (designed by the LLM) and renders each scene with the
// right visuals + a zoom-in callout for highlight frames.
export const Composition_: React.FC = () => {
  const { durationInFrames } = useVideoConfig();
  const intro = scenes[0];
  const outro = scenes[scenes.length - 1];
  const middle = scenes.slice(1, -1);

  // Map highlight frame indices → scene + offset within scene for the callout
  const highlightByScene = new Map<number, { offset: number; caption: string }[]>();
  for (const h of highlights as Array<{ frameIndex: number; caption: string }>) {
    // We treat frameIndex as an absolute composition frame, then resolve it
    // back to a scene by walking the partition.
    const scene = scenes.find(
      (s: { startFrame: number; endFrame: number }) =>
        h.frameIndex >= s.startFrame && h.frameIndex < s.endFrame,
    );
    if (!scene) continue;
    const list = highlightByScene.get(scenes.indexOf(scene)) || [];
    list.push({ offset: h.frameIndex - scene.startFrame, caption: h.caption });
    highlightByScene.set(scenes.indexOf(scene), list);
  }

  return (
    <AbsoluteFill style={{ backgroundColor: "#000" }}>
      {/* Background layer: either OffthreadVideo or a frames-based backdrop */}
      {render_mode === "video" ? (
        <OffthreadVideo
          src={staticFile("source.mp4")}
          style={{ width: "100%", height: "100%", objectFit: "cover" }}
        />
      ) : (
        <FramesBackdrop scenes={middle} highlightByScene={highlightByScene} />
      )}

      {/* Intro card */}
      <RemotionSequence from={intro.startFrame} durationInFrames={intro.endFrame - intro.startFrame}>
        <IntroCard headline={(spec as any).intro?.headline || spec.title} subline={(spec as any).intro?.subline || ""} />
      </RemotionSequence>

      {/* Outro card */}
      <RemotionSequence from={outro.startFrame} durationInFrames={outro.endFrame - outro.startFrame}>
        <OutroCard headline={(spec as any).outro?.headline || "Thanks"} cta={(spec as any).outro?.cta || ""} />
      </RemotionSequence>

      {/* Subtitles overlaid on the whole composition */}
      <Subtitles
        cues={cues as Cue[]}
        fps={fps}
        durationInFrames={durationInFrames}
        style={subtitle_style}
      />
    </AbsoluteFill>
  );
};

// Frames-based backdrop: pick the frame whose absolute composition index is
// nearest the current frame, per-scene. Highlights zoom in for ~1 second.
const FramesBackdrop: React.FC<{
  scenes: Array<{ label: string; startFrame: number; endFrame: number }>;
  highlightByScene: Map<number, { offset: number; caption: string }[]>;
}> = ({ scenes, highlightByScene }) => {
  const frame = useVideoConfig().absoluteFrame;
  // Walk scenes to know which one we're inside
  const sceneIdx = scenes.findIndex(
    (s) => frame >= s.startFrame && frame < s.endFrame,
  );
  const scene = sceneIdx >= 0 ? scenes[sceneIdx] : null;
  if (!scene) return null;
  const highlightList = highlightByScene.get(sceneIdx + 1) || []; // +1 because middle is scenes[1..-1]

  // Default: distribute frames evenly across this scene. frame_NNNN.jpg indexing.
  const sceneLen = scene.endFrame - scene.startFrame;
  // We don't know the full frame count here — read from staticFile presence;
  // the Composition just picks frame_${floor((frame-start)/sceneLen * N)}.jpg
  // The N is set in spec.fps * spec.duration_seconds effectively, but we read it
  // from a generated cue sheet. For simplicity, attempt sequential frame indexes
  // starting at 1; the build copies all frames into public/frames/.
  const relFrame = frame - scene.startFrame;
  const idxWithinScene = Math.floor((relFrame / Math.max(sceneLen, 1)) * 100) + 1;
  const filename = `frames/frame_${String(idxWithinScene).padStart(4, "0")}.jpg`;
  const isHighlight = highlightList.some((h) => Math.abs(h.offset - relFrame) < fps);
  const highlight = highlightList.find((h) => Math.abs(h.offset - relFrame) < fps);

  return (
    <AbsoluteFill>
      <img
        src={staticFile(filename)}
        style={{
          width: "100%",
          height: "100%",
          objectFit: "cover",
          transform: isHighlight ? "scale(1.15)" : "scale(1.0)",
          transition: "transform 0.4s ease-out",
        }}
      />
      {highlight ? (
        <div
          style={{
            position: "absolute",
            bottom: 120,
            left: 60,
            right: 60,
            padding: "16px 24px",
            background: "rgba(0,0,0,0.65)",
            color: "#FFD400",
            fontSize: 28,
            fontFamily: "system-ui, sans-serif",
            borderRadius: 8,
          }}
        >
          {highlight.caption}
        </div>
      ) : null}
    </AbsoluteFill>
  );
};
"""

SUBTITLES_TSX = """import React from "react";
import { useCurrentFrame } from "remotion";

type Cue = { start: number; end: number; text: string };
type SubStyle = {
  position: "bottom" | "center" | "top";
  fontSize: number;
  color: string;
  background: string;
};

export const Subtitles: React.FC<{
  cues: Cue[];
  fps: number;
  durationInFrames: number;
  style: SubStyle;
}> = ({ cues, fps, durationInFrames, style }) => {
  const frame = useCurrentFrame();
  const t = frame / fps;
  const active = cues.find((c) => t >= c.start && t < c.end);
  if (!active) return null;

  const vertical =
    style.position === "top" ? 40 : style.position === "center" ? "50%" : undefined;
  const bottom = style.position === "bottom" ? 40 : undefined;

  return (
    <div
      style={{
        position: "absolute",
        left: 60,
        right: 60,
        top: vertical,
        bottom,
        transform: style.position === "center" ? "translateY(-50%)" : undefined,
        background: style.background,
        color: style.color,
        fontSize: style.fontSize,
        fontFamily: "system-ui, -apple-system, sans-serif",
        fontWeight: 600,
        padding: "12px 20px",
        borderRadius: 8,
        textAlign: "center",
        lineHeight: 1.3,
      }}
    >
      {active.text}
    </div>
  );
};
"""

INTRO_CARD_TSX = """import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate } from "remotion";

export const IntroCard: React.FC<{ headline: string; subline: string }> = ({
  headline,
  subline,
}) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const fade = interpolate(frame, [0, fps * 0.6, durationInFrames - fps * 0.6, durationInFrames], [0, 1, 1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  return (
    <AbsoluteFill style={{ background: "rgba(0,0,0,0.55)", opacity: fade }}>
      <div
        style={{
          position: "absolute",
          inset: 0,
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          alignItems: "center",
          color: "#FFFFFF",
          textAlign: "center",
          padding: 80,
        }}
      >
        <div
          style={{
            fontSize: 96,
            fontWeight: 800,
            letterSpacing: -1,
            fontFamily: "system-ui, -apple-system, sans-serif",
            textShadow: "0 4px 24px rgba(0,0,0,0.6)",
          }}
        >
          {headline}
        </div>
        {subline ? (
          <div
            style={{
              marginTop: 24,
              fontSize: 36,
              opacity: 0.85,
              fontFamily: "system-ui, -apple-system, sans-serif",
            }}
          >
            {subline}
          </div>
        ) : null}
      </div>
    </AbsoluteFill>
  );
};
"""

OUTRO_CARD_TSX = """import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate } from "remotion";

export const OutroCard: React.FC<{ headline: string; cta: string }> = ({
  headline,
  cta,
}) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const fade = interpolate(frame, [0, fps * 0.6, durationInFrames - fps * 0.6, durationInFrames], [0, 1, 1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  return (
    <AbsoluteFill style={{ background: "rgba(0,0,0,0.65)", opacity: fade }}>
      <div
        style={{
          position: "absolute",
          inset: 0,
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          alignItems: "center",
          color: "#FFFFFF",
          textAlign: "center",
          padding: 80,
        }}
      >
        <div
          style={{
            fontSize: 84,
            fontWeight: 800,
            fontFamily: "system-ui, -apple-system, sans-serif",
          }}
        >
          {headline}
        </div>
        {cta ? (
          <div
            style={{
              marginTop: 32,
              padding: "16px 36px",
              background: "#FFD400",
              color: "#111",
              fontSize: 36,
              fontWeight: 700,
              borderRadius: 999,
              fontFamily: "system-ui, -apple-system, sans-serif",
            }}
          >
            {cta}
          </div>
        ) : null}
      </div>
    </AbsoluteFill>
  );
};
"""

README_MD = """# __TITLE__

Auto-generated Remotion project from a `/watch` work directory.
The LLM-designed spec lives in `public/spec.json` — open it to see the scene
list, highlights, and subtitle style chosen for this video.

## Render

```
npm install
npx remotion studio                 # opens the studio for this composition
npx remotion render src/index.ts "__COMPOSITION_ID__" out.mp4
```

## What's where

- `src/Composition.tsx` — main composition; reads `spec.json` + `cues.json`
- `src/Subtitles.tsx` — timestamped subtitle overlay
- `src/IntroCard.tsx` / `src/OutroCard.tsx` — bookend cards
- `public/spec.json` — LLM's design decisions (audit it)
- `public/cues.json` — WebVTT cues from the source transcript
- `public/frames/` — JPEG frames extracted by /watch
- `public/source.mp4` — original video (if available; only used when render_mode=video)
"""


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text or "").strip("-").lower()
    return s or "watch-recap"


def _composition_id(title: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "_", title or "WatchRecap") or "WatchRecap"


def scaffold(out_dir: Path, spec: dict, discovered: dict, title: str) -> None:
    """Write the full Remotion project tree to out_dir."""
    out_dir = out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    slug = _slugify(title)
    composition_id = _composition_id(spec.get("title") or title)

    (out_dir / "package.json").write_text(
        PACKAGE_JSON.replace("__SLUG__", slug).replace("__COMPOSITION_ID__", composition_id),
        encoding="utf-8",
    )
    (out_dir / "remotion.config.ts").write_text(REMO_CONFIG, encoding="utf-8")
    (out_dir / "tsconfig.json").write_text(TS_CONFIG, encoding="utf-8")
    src = out_dir / "src"
    src.mkdir(exist_ok=True)
    (src / "index.ts").write_text(INDEX_TS, encoding="utf-8")
    (src / "Root.tsx").write_text(ROOT_TSX, encoding="utf-8")
    (src / "Composition.tsx").write_text(COMPOSITION_TSX, encoding="utf-8")
    (src / "Subtitles.tsx").write_text(SUBTITLES_TSX, encoding="utf-8")
    (src / "IntroCard.tsx").write_text(INTRO_CARD_TSX, encoding="utf-8")
    (src / "OutroCard.tsx").write_text(OUTRO_CARD_TSX, encoding="utf-8")

    public = out_dir / "public"
    public.mkdir(exist_ok=True)
    (public / "spec.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
    (public / "cues.json").write_text(json.dumps(discovered["segments"], indent=2), encoding="utf-8")

    frames_dest = public / "frames"
    frames_dest.mkdir(exist_ok=True)
    for frame in discovered["frames"]:
        shutil.copy2(frame["path"], frames_dest / frame["filename"])

    # Copy the source video into public/ if present and the LLM chose render_mode=video.
    if discovered["video_path"] and spec.get("render_mode") == "video":
        shutil.copy2(discovered["video_path"], public / "source.mp4")

    (out_dir / "README.md").write_text(
        README_MD.replace("__TITLE__", spec.get("title") or title)
                 .replace("__COMPOSITION_ID__", composition_id),
        encoding="utf-8",
    )

    files = sorted(p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*") if p.is_file())
    print(f"[smart] wrote {len(files)} files to {out_dir}", file=sys.stderr)
    for f in files:
        print(f"  {f}", file=sys.stderr)


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        prog="watch_to_remotion_smart",
        description="Convert a /watch output dir into an LLM-designed Remotion project.",
    )
    ap.add_argument("--watch-dir", required=True, help="Directory produced by /watch")
    ap.add_argument("--out-dir", required=True, help="Where to write the Remotion project")
    ap.add_argument(
        "--llm",
        choices=list(BACKENDS.keys()),
        default="litellm",
        help="LLM backend (default: litellm = DashScope qwen-plus via the project's litellm config)",
    )
    ap.add_argument(
        "--model",
        default=None,
        help=f"Model name (defaults: {', '.join(f'{k}={v[1]}' for k, v in BACKENDS.items())})",
    )
    ap.add_argument(
        "--mode",
        choices=["auto", "frames", "video"],
        default="auto",
        help="Force render_mode (default: auto = let the LLM decide)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the prompt and skip the LLM call + file writes (uses DEFAULT_SPEC).",
    )
    args = ap.parse_args()

    watch_dir = Path(args.watch_dir)
    out_dir = Path(args.out_dir)

    print(f"[smart] scanning {watch_dir}…", file=sys.stderr)
    discovered = discover(watch_dir)
    print(
        f"[smart] frames={discovered['frame_count']} "
        f"cues={discovered['segment_count']} "
        f"video={'yes' if discovered['video_present'] else 'no'} "
        f"duration={discovered['duration']}",
        file=sys.stderr,
    )

    if discovered["frame_count"] == 0:
        raise SystemExit("no frames found in watch dir — run /watch first")

    prompt = build_prompt(discovered)

    if args.dry_run:
        print(prompt)
        print("\n--- DRY RUN — skipping LLM call + scaffold ---", file=sys.stderr)
        spec = {**DEFAULT_SPEC, "scenes": [
            {"label": "intro", "startFrame": 0, "endFrame": 30, "note": "dry-run placeholder"},
            {"label": "outro", "startFrame": 30, "endFrame": 90, "note": "dry-run placeholder"},
        ]}
        title = discovered.get("info", {}).get("title") or "Watch Recap"
        scaffold(out_dir, spec, discovered, title)
        return 0

    print(f"[smart] calling {args.llm} ({args.model or BACKENDS[args.llm][1]})…", file=sys.stderr)
    raw_spec = call_llm(args.llm, args.model, prompt)

    if args.mode != "auto":
        raw_spec["render_mode"] = args.mode
        print(f"[smart] forcing render_mode={args.mode}", file=sys.stderr)

    spec = validate_and_repair(raw_spec, discovered)
    title = spec.get("title") or discovered.get("info", {}).get("title") or "Watch Recap"

    scaffold(out_dir, spec, discovered, title)

    print(f"\n[smart] composition: {title}", file=sys.stderr)
    print(f"[smart] {len(spec['scenes'])} scenes, {len(spec['highlights'])} highlights, "
          f"{spec['duration_seconds']}s @ {spec['fps']} fps, render_mode={spec['render_mode']}",
          file=sys.stderr)
    print(f"\nNext:", file=sys.stderr)
    print(f"  cd {out_dir}", file=sys.stderr)
    print(f"  npm install", file=sys.stderr)
    print(f"  npx remotion studio", file=sys.stderr)
    print(f"  npx remotion render src/index.ts '{_composition_id(title)}' out.mp4", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())