#!/usr/bin/env python3
"""Go→Python 工作流桥接脚本

接受 Go 后端传来的 JSON 参数，执行视频处理流水线，输出 JSON 进度到 stdout。

Usage:
    python3 run_pipeline.py --task-id task_xxx --params '{"video_url":"...","replacements":[...]}'
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path


VIDEO_W, VIDEO_H = 1280, 720
WORK_DIR = Path("/opt/claude-video/web-platform/work")


def emit(status: str, progress: int, message: str, **kwargs) -> None:
    """输出一行 JSON 进度到 stdout（Go 后端逐行读取解析）。"""
    line = json.dumps({"status": status, "progress": progress, "message": message, **kwargs}, ensure_ascii=False)
    print(line, flush=True)


def extract_frames(video_path: str, frames_dir: Path, fps: float = 1 / 5) -> int:
    frames_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", video_path,
         "-vf", f"fps={fps},scale=640:-1",
         "-q:v", "3",
         str(frames_dir / "frame_%04d.jpg")],
        check=True,
    )
    return len(list(frames_dir.glob("frame_*.jpg")))


def get_video_duration(video_path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", video_path],
        capture_output=True, text=True,
    )
    fmt = json.loads(result.stdout).get("format", {})
    return float(fmt.get("duration", 0))


def generate_overlay(replacement: dict, output_path: Path, video_w: int = VIDEO_W, video_h: int = VIDEO_H) -> None:
    from PIL import Image, ImageDraw, ImageFont

    bbox = replacement.get("bbox_pct", {})
    x = int(video_w * bbox.get("x", 30) / 100)
    y = int(video_h * bbox.get("y", 30) / 100)
    w = int(video_w * bbox.get("w", 40) / 100)
    h = int(video_h * bbox.get("h", 20) / 100)

    img = Image.new("RGBA", (video_w, video_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([x, y, x + w, y + h], radius=8, fill=(20, 20, 30, 180))

    text = replacement.get("replace_text", "AURORA X")
    font_size = max(16, min(h // 3, 48))
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except (OSError, IOError):
        font = ImageFont.load_default()

    bbox_text = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox_text[2] - bbox_text[0], bbox_text[3] - bbox_text[1]
    tx = x + (w - tw) // 2
    ty = y + (h - th) // 2
    draw.text((tx, ty), text, fill=(255, 255, 255, 240), font=font)
    draw.rounded_rectangle([x, y, x + w, y + h], radius=8, outline=(100, 200, 255, 200), width=2)

    img.save(output_path, "PNG")


def composit_video(video_path: str, overlays: list[dict], output_path: Path) -> None:
    if not overlays:
        subprocess.run(["cp", video_path, str(output_path)], check=True)
        return

    inputs = ["-i", video_path]
    for ov in overlays:
        inputs.extend(["-i", ov["path"]])

    parts = []
    prev = "0:v"
    for i, ov in enumerate(overlays):
        tw = ov["time_window"]
        label = f"v{i + 1}"
        parts.append(f"[{prev}][{i + 1}:v]overlay=0:0:enable='between(t,{tw[0]},{tw[1]})'[{label}]")
        prev = label

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        *inputs,
        "-filter_complex", ";".join(parts),
        "-map", f"[{prev}]",
        "-map", "0:a?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "copy",
        str(output_path),
    ]
    subprocess.run(cmd, check=True)


def run_pipeline(task_id: str, params: dict) -> None:
    task_dir = WORK_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = task_dir / "frames"
    overlays_dir = task_dir / "overlays"
    output_path = task_dir / "output.mp4"

    # ── Phase 1: Decompose ──
    emit("decomposing", 10, "提取视频帧...")

    video_path = params.get("video_file") or params.get("video_url", "")
    if not video_path:
        emit("failed", 0, "未提供视频", error="No video source")
        return

    duration = get_video_duration(video_path)
    fps = min(1 / 5, 2.0)
    frames_count = extract_frames(video_path, frames_dir, fps)
    emit("decomposing", 20, f"提取了 {frames_count} 帧, 时长 {duration:.1f}s")

    # ── Phase 2: Analyze (模拟) ──
    emit("analyzing", 30, "分析帧内容...")
    time.sleep(0.5)  # 模拟分析时间

    replacements = params.get("replacements", [])
    findings = []
    for r in replacements:
        findings.append({
            "label": r.get("label", ""),
            "time_window": r.get("time_window", [0, 5]),
            "bbox_pct": r.get("bbox_pct", {}),
        })
    emit("analyzing", 50, f"发现 {len(findings)} 个品牌元素")

    # ── Phase 3: Modify ──
    emit("modifying", 60, "生成替换覆盖图...")
    overlays_dir.mkdir(parents=True, exist_ok=True)
    overlay_list = []
    for i, r in enumerate(replacements):
        tw = r.get("time_window", [0, 5])
        fname = f"overlay_{tw[0]}_to_{tw[1]}.png"
        out_path = overlays_dir / fname
        generate_overlay(r, out_path)
        overlay_list.append({"path": str(out_path), "time_window": tw})
        emit("modifying", 60 + int(20 * (i + 1) / max(len(replacements), 1)),
             f"生成覆盖图 {i + 1}/{len(replacements)}")

    # ── Phase 4: Reassemble ──
    emit("reassembling", 85, "合成最终视频...")
    composit_video(video_path, overlay_list, output_path)

    emit("completed", 100, "处理完成",
         result={
             "output": str(output_path),
             "overlays": [o["path"] for o in overlay_list],
             "findings": findings,
         })


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--params", required=True)
    args = parser.parse_args()

    try:
        params = json.loads(args.params)
    except json.JSONDecodeError as e:
        emit("failed", 0, f"参数解析失败: {e}", error=str(e))
        return

    try:
        run_pipeline(args.task_id, params)
    except Exception as e:
        emit("failed", 0, f"流水线异常: {e}", error=str(e))


if __name__ == "__main__":
    main()
