#!/usr/bin/env python3
"""watch_to_remotion.py — convert a /watch work dir into a runnable Remotion project.

Pure deterministic conversion. Parses the VTT, copies the frames (and original
video if available), and emits a Remotion 4.x project skeleton that renders the
extracted assets with the transcript burned in as styled subtitles. No LLM in
the loop — for an intelligent variant that tailors the composition to the video
content, see ``watch_to_remotion_smart.py``.

Usage::

    python3 watch_to_remotion.py --watch-dir <dir> --out-dir <dir>
    python3 watch_to_remotion.py --watch-dir <dir> --out-dir <dir> --mode video
    python3 watch_to_remotion.py --watch-dir <dir> --out-dir <dir> --fps 60

Layout produced under ``--out-dir``::

    <out-dir>/
    ├── package.json
    ├── remotion.config.ts
    ├── tsconfig.json
    ├── README.md
    ├── public/
    │   ├── frames/frame_NNNN.jpg   (copied from /watch)
    │   ├── masks/mask_NNNN.png     (only if --include-masks)
    │   ├── video.mp4               (only if --mode video)
    │   ├── transcript.vtt          (copy for reference)
    │   └── cues.json               (parsed cues + duration + frame index)
    └── src/
        ├── index.ts
        ├── Root.tsx
        ├── Composition.tsx
        └── Subtitles.tsx

After generation::

    cd <out-dir>
    npm install
    npx remotion studio                              # preview
    npx remotion render src/index.ts WatchComp out.mp4   # render
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

from transcribe import parse_vtt  # noqa: E402


# ── Discovery ───────────────────────────────────────────────────────────────


def find_frames_dir(watch_dir: Path) -> Path | None:
    """Find the frames/ directory under watch_dir (top-level or one nesting deep)."""
    candidates = [watch_dir / "frames"]
    candidates.extend(watch_dir.glob("*/frames"))
    for c in candidates:
        if c.is_dir() and any(c.glob("frame_*.jpg")):
            return c
    return None


def find_video(watch_dir: Path) -> Path | None:
    """Find the downloaded source video."""
    candidates = sorted(watch_dir.glob("download/video.*"))
    candidates += [watch_dir / "video.mp4"]
    for c in candidates:
        if c.is_file() and c.suffix.lower() in (".mp4", ".webm", ".mkv", ".mov"):
            return c
    return None


def find_vtt(watch_dir: Path) -> Path | None:
    """Find a VTT transcript. yt-dlp writes ``download/video.<lang>.vtt``."""
    candidates = sorted(watch_dir.glob("download/video*.vtt"))
    candidates.append(watch_dir / "transcript.vtt")
    for c in candidates:
        if c.is_file():
            return c
    return None


def find_masks_dir(watch_dir: Path) -> Path | None:
    """Find the masks/ directory produced by --segment."""
    candidates = [watch_dir / "masks"]
    candidates.extend(watch_dir.glob("*/masks"))
    for c in candidates:
        if c.is_dir() and any(c.glob("mask_*.png")):
            return c
    return None


def get_video_duration(video: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            str(video.resolve()),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    info = json.loads(result.stdout)
    return float(info["format"]["duration"])


# ── Argv ────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Convert a /watch work dir into a runnable Remotion project.",
    )
    p.add_argument(
        "--watch-dir",
        required=True,
        type=Path,
        help="Path to the /watch work directory (contains frames/, download/, etc.)",
    )
    p.add_argument(
        "--out-dir",
        required=True,
        type=Path,
        help="Destination directory for the generated Remotion project",
    )
    p.add_argument(
        "--fps",
        type=int,
        default=30,
        help="Composition fps (default 30). Video mode is frame-rate-agnostic.",
    )
    p.add_argument(
        "--mode",
        choices=["frames", "video", "auto"],
        default="auto",
        help="Render mode. frames = slideshow of extracted frames. "
             "video = original video as background with subtitles overlaid. "
             "auto = video if available, else frames (default).",
    )
    p.add_argument(
        "--include-masks",
        action="store_true",
        help="Copy the SAM 2 segmentation masks into public/masks/ for overlay use.",
    )
    p.add_argument(
        "--width",
        type=int,
        default=1280,
        help="Composition width in pixels (default 1280)",
    )
    p.add_argument(
        "--height",
        type=int,
        default=720,
        help="Composition height in pixels (default 720)",
    )
    p.add_argument(
        "--composition-id",
        default="WatchComp",
        help="Composition ID used by Remotion (default WatchComp)",
    )
    p.add_argument(
        "--title",
        default="Watch Composition",
        help="Human-readable title used in README and render CLI",
    )
    return p.parse_args()


# ── File writers ────────────────────────────────────────────────────────────


PACKAGE_JSON_TEMPLATE = """{
  "name": "watch-to-remotion",
  "version": "0.1.0",
  "private": true,
  "description": "Auto-generated from /opt/claude-video/watch — see README.md",
  "scripts": {
    "start": "remotion studio",
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
    "@types/web": "0.0.166",
    "typescript": "5.6.3"
  }
}
"""

TSCONFIG_TEMPLATE = """{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "jsx": "react-jsx",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true,
    "allowSyntheticDefaultImports": true
  },
  "include": ["src", "remotion.config.ts"]
}
"""

REMOTION_CONFIG_TEMPLATE = """import { Config } from "@remotion/cli/config";

Config.setVideoImageFormat("jpeg");
Config.setOverwriteOutput(true);
Config.setConcurrency(1);
"""

INDEX_TEMPLATE = """import { registerRoot } from "remotion";
import { RemotionRoot } from "./Root";

registerRoot(RemotionRoot);
"""

ROOT_TEMPLATE = """import React from "react";
import { Composition } from "remotion";
import { WatchComposition } from "./Composition";
import cues from "../public/cues.json";

export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition
        id="__COMPOSITION_ID__"
        component={WatchComposition}
        durationInFrames={cues.durationInFrames}
        fps={cues.fps}
        width={__WIDTH__}
        height={__HEIGHT__}
      />
    </>
  );
};
"""

# Single Composition that handles both frames-mode (slideshow) and video-mode
# (OffthreadVideo background), selected by cues.mode. Uses the actual sorted
# frame filenames in cues.framePaths so dedup gaps in /watch's frame numbering
# don't break lookup.
COMPOSITION_TEMPLATE = """import React from "react";
import {
  AbsoluteFill,
  Img,
  OffthreadVideo,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import cues from "../public/cues.json";
import { Subtitles } from "./Subtitles";

const data = cues as {
  mode: "frames" | "video";
  frameCount: number;
  hasVideo: boolean;
  framePaths: string[];
};

export const WatchComposition: React.FC = () => {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();

  // Frames mode: pick the right JPEG for the current playback time. Use the
  // actual sorted paths so dedup gaps (e.g. frame_0008 dropped) don't 404.
  let frameSrc: string | null = null;
  if (data.mode === "frames" && data.frameCount > 0) {
    const perFrame = durationInFrames / data.frameCount;
    const idx = Math.min(Math.floor(frame / perFrame), data.frameCount - 1);
    frameSrc = staticFile(`frames/${data.framePaths[idx]}`);
  }

  return (
    <AbsoluteFill style={{ backgroundColor: "black" }}>
      {data.mode === "video" && data.hasVideo && (
        <OffthreadVideo src={staticFile("video.mp4")} />
      )}
      {frameSrc && (
        <Img
          src={frameSrc}
          style={{
            width: "100%",
            height: "100%",
            objectFit: "contain",
          }}
        />
      )}
      <Subtitles />
    </AbsoluteFill>
  );
};
"""

SUBTITLES_TEMPLATE = """import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import cues from "../public/cues.json";

interface Cue {
  start: number;
  end: number;
  text: string;
  startFrame: number;
  endFrame: number;
}

const data = cues as {
  fps: number;
  durationInFrames: number;
  cues: Cue[];
};

export const Subtitles: React.FC = () => {
  const frame = useCurrentFrame();
  const active = data.cues.find(
    (c) => c.startFrame <= frame && frame < c.endFrame,
  );
  if (!active) return null;
  return (
    <AbsoluteFill
      style={{
        justifyContent: "flex-end",
        alignItems: "center",
        paddingBottom: 60,
        pointerEvents: "none",
      }}
    >
      <div
        style={{
          background: "rgba(0, 0, 0, 0.7)",
          color: "white",
          padding: "12px 24px",
          fontSize: 36,
          fontFamily:
            "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
          borderRadius: 8,
          maxWidth: "80%",
          textAlign: "center",
          lineHeight: 1.3,
        }}
      >
        {active.text}
      </div>
    </AbsoluteFill>
  );
};
"""


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_package_json(out: Path, composition_id: str) -> None:
    write_text(
        out / "package.json",
        PACKAGE_JSON_TEMPLATE.replace("__COMPOSITION_ID__", composition_id),
    )


def write_tsconfig(out: Path) -> None:
    write_text(out / "tsconfig.json", TSCONFIG_TEMPLATE)


def write_remotion_config(out: Path) -> None:
    write_text(out / "remotion.config.ts", REMOTION_CONFIG_TEMPLATE)


def write_index(out: Path) -> None:
    write_text(out / "src/index.ts", INDEX_TEMPLATE)


def write_root(
    out: Path,
    composition_id: str,
    width: int,
    height: int,
) -> None:
    content = (
        ROOT_TEMPLATE.replace("__COMPOSITION_ID__", composition_id)
        .replace("__WIDTH__", str(width))
        .replace("__HEIGHT__", str(height))
    )
    write_text(out / "src/Root.tsx", content)


def write_composition(out: Path) -> None:
    write_text(out / "src/Composition.tsx", COMPOSITION_TEMPLATE)


def write_subtitles(out: Path) -> None:
    write_text(out / "src/Subtitles.tsx", SUBTITLES_TEMPLATE)


def write_readme(
    out: Path,
    watch_dir: Path,
    title: str,
    composition_id: str,
    mode: str,
    fps: int,
) -> None:
    readme = f"""# {title}

Auto-generated by `watch_to_remotion.py` from a `/watch` work directory.

- **Source**: `{watch_dir}`
- **Mode**: `{mode}`
- **FPS**: {fps}

## Run

```bash
npm install
npx remotion studio
```

This opens the Remotion Studio at <http://localhost:3000> with the composition
loaded.

## Render

```bash
npx remotion render src/index.ts {composition_id} out.mp4
```

Output lands at `./out.mp4`.

## Layout

- `public/frames/` — extracted JPEGs (one per scene/keyframes).
- `public/video.mp4` — original source video (only in `video` mode).
- `public/masks/` — SAM 2 segmentation masks (only if `--include-masks`).
- `public/transcript.vtt` — original transcript, kept for reference.
- `public/cues.json` — parsed cues with `startFrame` / `endFrame` already
  computed at `{fps}` fps. Edit this file to retime subtitles without touching
  code.
- `src/Composition.tsx` — the actual composition. Two render modes:
  - `frames` mode (default when no video): slideshow, picks a JPEG per time slice.
  - `video` mode: original video plays as background; subtitles overlaid.
- `src/Subtitles.tsx` — the subtitle component.

## Retiming

Edit `public/cues.json` `durationInFrames` to lengthen/shorten the composition.
Remotion will rescale the underlying frames; for `frames` mode the slideshow
runs slower or faster proportionally.
"""
    write_text(out / "README.md", readme)


# ── Main ────────────────────────────────────────────────────────────────────


def main() -> int:
    args = parse_args()
    watch = args.watch_dir.resolve()
    out = args.out_dir.resolve()

    if not watch.is_dir():
        print(f"error: --watch-dir {watch} is not a directory", file=sys.stderr)
        return 2

    frames_dir = find_frames_dir(watch)
    video = find_video(watch)
    vtt = find_vtt(watch)
    masks_dir = find_masks_dir(watch) if args.include_masks else None

    if not frames_dir and not video:
        print(
            f"error: no frames/ or video found under {watch}",
            file=sys.stderr,
        )
        return 2
    if not vtt:
        print(
            f"warning: no VTT found under {watch} — generating cues-free project",
            file=sys.stderr,
        )

    # Resolve duration
    duration_sec = 0.0
    if video:
        try:
            duration_sec = get_video_duration(video)
        except Exception as exc:
            print(f"warning: ffprobe failed ({exc}); falling back to frame count", file=sys.stderr)
    if duration_sec <= 0 and frames_dir:
        # Heuristic: assume 1 second per frame when no video metadata is available
        n = len(list(frames_dir.glob("frame_*.jpg")))
        duration_sec = float(n)
    if duration_sec <= 0:
        print("error: could not determine video duration", file=sys.stderr)
        return 2

    fps = args.fps
    duration_in_frames = max(int(round(duration_sec * fps)), 1)

    # Parse cues
    raw_cues = parse_vtt(str(vtt)) if vtt else []
    cues = [
        {
            "start": c["start"],
            "end": c["end"],
            "text": c["text"],
            "startFrame": int(round(c["start"] * fps)),
            "endFrame": int(round(c["end"] * fps)),
        }
        for c in raw_cues
    ]

    # Pick mode
    if args.mode == "auto":
        mode = "video" if video else "frames"
    else:
        mode = args.mode
        if mode == "video" and not video:
            print(
                "warning: --mode video requested but no video found; falling back to frames",
                file=sys.stderr,
            )
            mode = "frames"

    frame_count = len(list(frames_dir.glob("frame_*.jpg"))) if frames_dir else 0

    # Materialize project tree
    out.mkdir(parents=True, exist_ok=True)
    public = out / "public"
    src = out / "src"
    public.mkdir(exist_ok=True)
    src.mkdir(exist_ok=True)

    # Copy frames
    if frames_dir:
        target_frames = public / "frames"
        target_frames.mkdir(exist_ok=True)
        copied = 0
        for f in sorted(frames_dir.glob("frame_*.jpg")):
            shutil.copy2(f, target_frames / f.name)
            copied += 1
        print(f"[ok] copied {copied} frames -> {target_frames}")

    # Copy video
    if video and mode == "video":
        target_video = public / "video.mp4"
        shutil.copy2(video, target_video)
        print(f"[ok] copied video -> {target_video}")

    # Copy VTT for reference
    if vtt:
        shutil.copy2(vtt, public / "transcript.vtt")
        print(f"[ok] copied transcript -> {public / 'transcript.vtt'}")

    # Copy masks
    if masks_dir:
        target_masks = public / "masks"
        target_masks.mkdir(exist_ok=True)
        n_masks = 0
        for f in sorted(masks_dir.glob("mask_*.png")):
            shutil.copy2(f, target_masks / f.name)
            n_masks += 1
        print(f"[ok] copied {n_masks} masks -> {target_masks}")

    # Write cues.json — single source of truth for the .tsx components
    frame_paths = (
        sorted(p.name for p in frames_dir.glob("frame_*.jpg")) if frames_dir else []
    )
    cues_json = {
        "fps": fps,
        "durationInFrames": duration_in_frames,
        "durationSeconds": round(duration_sec, 3),
        "mode": mode,
        "frameCount": len(frame_paths),
        "framePaths": frame_paths,
        "hasVideo": video is not None,
        "hasMasks": masks_dir is not None,
        "width": args.width,
        "height": args.height,
        "cues": cues,
    }
    (public / "cues.json").write_text(
        json.dumps(cues_json, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(
        f"[ok] wrote cues.json ({len(cues)} cues, {duration_in_frames} frames @ {fps}fps, mode={mode})"
    )

    # Write Remotion source files
    write_package_json(out, args.composition_id)
    write_tsconfig(out)
    write_remotion_config(out)
    write_index(out)
    write_root(out, args.composition_id, args.width, args.height)
    write_composition(out)
    write_subtitles(out)
    write_readme(out, watch, args.title, args.composition_id, mode, fps)

    print()
    print(f"[done] Remotion project scaffolded at: {out}")
    print()
    print("Next steps:")
    print(f"  cd {out}")
    print("  npm install")
    print("  npx remotion studio                              # preview")
    print(
        f"  npx remotion render src/index.ts {args.composition_id} out.mp4   # render"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())