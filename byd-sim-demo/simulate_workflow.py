#!/usr/bin/env python3
"""BYD视频产品替换 — 模拟工作流演示

模拟完整流水线: 分解→分析→修改→合成
用合成数据演示多进程并行处理架构，无需外部API。
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

# ── 配置 ──────────────────────────────────────────────────────────────────────

VIDEO = "/opt/claude-video/watch-dQw4w9WgXcQ/download/video.mp4"
WORK_DIR = Path("/opt/claude-video/byd-sim-demo")
FRAMES_DIR = WORK_DIR / "frames"
OVERLAY_DIR = WORK_DIR / "overlays"
OUTPUT_VIDEO = WORK_DIR / "byd_aurora_x_simulated.mp4"
VIDEO_W, VIDEO_H = 1280, 720

# 模拟分析结果: 时间窗口 → 品牌元素位置 (百分比bbox)
# 这些是模拟数据，实际工作流中由4个并行agent分析帧得出
MOCK_FINDINGS = [
    {
        "time_window": (5.0, 10.0),
        "label": "前脸BYD徽标",
        "bbox_pct": {"x": 38, "y": 42, "w": 24, "h": 16},
        "type": "logo",
    },
    {
        "time_window": (15.0, 25.0),
        "label": "中控台BYD UI",
        "bbox_pct": {"x": 30, "y": 30, "w": 40, "h": 35},
        "type": "screen",
    },
    {
        "time_window": (25.0, 30.0),
        "label": "前进气格栅BYD标",
        "bbox_pct": {"x": 35, "y": 40, "w": 30, "h": 20},
        "type": "logo",
    },
    {
        "time_window": (35.0, 45.0),
        "label": "尾门BYD徽标",
        "bbox_pct": {"x": 40, "y": 45, "w": 20, "h": 12},
        "type": "logo",
    },
    {
        "time_window": (70.0, 75.0),
        "label": "夜景前脸",
        "bbox_pct": {"x": 36, "y": 44, "w": 28, "h": 18},
        "type": "logo",
    },
    {
        "time_window": (90.0, 95.0),
        "label": "结尾卡片BYD SHARK",
        "bbox_pct": {"x": 25, "y": 35, "w": 50, "h": 30},
        "type": "text",
    },
]


def log(msg: str) -> None:
    print(f"[workflow] {msg}", file=sys.stderr, flush=True)


# ── 阶段1: 分解 ──────────────────────────────────────────────────────────────

def decompose() -> dict:
    """提取帧 + 音频（模拟转录）"""
    log("Phase 1: Decompose — 提取帧和音频")
    t0 = time.time()

    FRAMES_DIR.mkdir(parents=True, exist_ok=True)

    # 提取分析用帧 (1fps，用于模拟agent分析)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", VIDEO,
            "-vf", "fps=1/5,scale=640:-1",
            "-q:v", "3",
            str(FRAMES_DIR / "frame_%04d.jpg"),
        ],
        check=True,
    )

    # 提取音频信息 (模拟转录)
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", VIDEO],
        capture_output=True, text=True,
    )
    fmt = json.loads(result.stdout).get("format", {})
    duration = float(fmt.get("duration", 0))

    elapsed = time.time() - t0
    frames_count = len(list(FRAMES_DIR.glob("frame_*.jpg")))
    log(f"  提取了 {frames_count} 帧, 时长 {duration:.1f}s, 耗时 {elapsed:.1f}s")

    return {
        "duration": duration,
        "frames_count": frames_count,
        "frames_dir": str(FRAMES_DIR),
    }


# ── 阶段2: 分析 (多进程并行) ─────────────────────────────────────────────────

def _analyze_frame_batch(args: tuple) -> list[dict]:
    """模拟单个agent分析一批帧 (实际由LLM vision完成)"""
    batch_id, frame_indices, fps_interval = args
    findings = []
    for idx in frame_indices:
        timestamp = (idx - 1) * fps_interval
        # 检查是否有匹配的模拟发现
        for finding in MOCK_FINDINGS:
            t_start, t_end = finding["time_window"]
            if t_start <= timestamp <= t_end:
                findings.append({
                    "frame": f"frame_{idx:04d}.jpg",
                    "timestamp": timestamp,
                    "time_window": finding["time_window"],
                    "element": finding["label"],
                    "type": finding["type"],
                    "bbox_pct": finding["bbox_pct"],
                })
    log(f"  Agent {batch_id}: 分析了 {len(frame_indices)} 帧, 发现 {len(findings)} 个品牌元素")
    return findings


def analyze_parallel(decompose_result: dict) -> list[dict]:
    """4个进程并行分析帧"""
    log("Phase 2: Analyze — 4进程并行分析")
    t0 = time.time()

    total_frames = decompose_result["frames_count"]
    fps_interval = 5.0  # 每5秒一帧

    # 分成4批
    batch_size = math.ceil(total_frames / 4)
    batches = []
    for i in range(4):
        start = i * batch_size + 1
        end = min((i + 1) * batch_size, total_frames)
        frame_indices = list(range(start, end + 1))
        batches.append((i + 1, frame_indices, fps_interval))

    all_findings = []
    with ProcessPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_analyze_frame_batch, b): b[0] for b in batches}
        for future in as_completed(futures):
            batch_id = futures[future]
            try:
                result = future.result()
                all_findings.extend(result)
            except Exception as e:
                log(f"  Agent {batch_id} 失败: {e}")

    # 按时间排序并去重 (同一time_window只保留第一个)
    all_findings.sort(key=lambda x: x["timestamp"])
    seen_windows = set()
    deduped = []
    for f in all_findings:
        tw = f["time_window"]
        if tw not in seen_windows:
            seen_windows.add(tw)
            deduped.append(f)
    all_findings = deduped

    elapsed = time.time() - t0
    log(f"  共发现 {len(all_findings)} 个品牌元素, 耗时 {elapsed:.1f}s")
    return all_findings


# ── 阶段3: 修改 — 生成覆盖图 ────────────────────────────────────────────────

def _generate_overlay(args: tuple) -> str:
    """为单个时间窗口生成透明覆盖图"""
    from PIL import Image, ImageDraw, ImageFont

    finding, output_path = args
    bbox = finding["bbox_pct"]

    # 百分比 → 像素
    x = int(VIDEO_W * bbox["x"] / 100)
    y = int(VIDEO_H * bbox["y"] / 100)
    w = int(VIDEO_W * bbox["w"] / 100)
    h = int(VIDEO_H * bbox["h"] / 100)

    # 创建透明RGBA图
    img = Image.new("RGBA", (VIDEO_W, VIDEO_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 半透明深色遮罩
    draw.rounded_rectangle(
        [x, y, x + w, y + h],
        radius=8,
        fill=(20, 20, 30, 180),
    )

    # 替换文字
    text = "AURORA X"
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

    # 边框
    draw.rounded_rectangle(
        [x, y, x + w, y + h],
        radius=8,
        outline=(100, 200, 255, 200),
        width=2,
    )

    img.save(output_path, "PNG")
    return output_path


def generate_overlays(findings: list[dict]) -> list[dict]:
    """多进程生成覆盖图"""
    log("Phase 3: Modify — 并行生成覆盖图")
    t0 = time.time()

    OVERLAY_DIR.mkdir(parents=True, exist_ok=True)

    tasks = []
    for i, finding in enumerate(findings):
        t_start, t_end = finding["time_window"]
        fname = f"overlay_{t_start:.1f}_to_{t_end:.1f}.png"
        output_path = OVERLAY_DIR / fname
        tasks.append((finding, str(output_path)))

    overlay_paths = []
    # Pillow 不是进程安全的，用线程池 + 单进程生成（I/O密集型）
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_generate_overlay, t): t for t in tasks}
        for future in as_completed(futures):
            try:
                path = future.result()
                overlay_paths.append(path)
            except Exception as e:
                log(f"  覆盖图生成失败: {e}")

    overlay_paths.sort()
    elapsed = time.time() - t0
    log(f"  生成了 {len(overlay_paths)} 张覆盖图, 耗时 {elapsed:.1f}s")
    return [{"path": p, "finding": t[0]} for p, t in zip(overlay_paths, tasks)]


# ── 阶段4: 合成 ──────────────────────────────────────────────────────────────

def reassemble(overlay_results: list[dict]) -> str:
    """用ffmpeg filter_complex合成最终视频"""
    log("Phase 4: Reassemble — ffmpeg合成")
    t0 = time.time()

    if not overlay_results:
        log("  没有覆盖图，跳过合成")
        return ""

    WORK_DIR.mkdir(parents=True, exist_ok=True)

    # 构建filter_complex链
    # 输入: [0:v] = 源视频, [1:v] = overlay_1, [2:v] = overlay_2, ...
    inputs = ["-i", VIDEO]
    for ov in overlay_results:
        inputs.extend(["-i", ov["path"]])

    filter_parts = []
    prev_label = "0:v"

    for i, ov in enumerate(overlay_results):
        finding = ov["finding"]
        t_start, t_end = finding["time_window"]
        next_label = f"v{i + 1}"

        filter_parts.append(
            f"[{prev_label}][{i + 1}:v]overlay=0:0:enable='between(t,{t_start},{t_end})'[{next_label}]"
        )
        prev_label = next_label

    filter_complex = ";".join(filter_parts)

    # 写filter graph到文件 (调试用)
    filter_file = WORK_DIR / "filter_complex.txt"
    filter_file.write_text(filter_complex, encoding="utf-8")
    log(f"  Filter graph 已写入: {filter_file}")

    # 执行ffmpeg
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", f"[{prev_label}]",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-c:a", "copy",
        str(OUTPUT_VIDEO),
    ]

    log(f"  执行ffmpeg (输入: {len(overlay_results) + 1}个)...")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        log(f"  ffmpeg 失败: {result.stderr[:300]}")
        # 降级: 不用overlay，直接复制源视频
        log("  降级: 直接复制源视频作为输出")
        import shutil
        shutil.copy2(VIDEO, OUTPUT_VIDEO)
    else:
        elapsed = time.time() - t0
        size_mb = OUTPUT_VIDEO.stat().st_size / (1024 * 1024)
        log(f"  输出: {OUTPUT_VIDEO} ({size_mb:.1f}MB, 耗时 {elapsed:.1f}s)")

    return str(OUTPUT_VIDEO)


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 60)
    print("BYD视频产品替换 — 模拟工作流演示")
    print("=" * 60)
    print()

    t_total = time.time()

    # Phase 1: 分解
    decompose_result = decompose()
    print()

    # Phase 2: 分析 (4进程并行)
    findings = analyze_parallel(decompose_result)
    print()

    # 输出分析摘要
    print("## 分析结果摘要")
    for f in findings:
        t_start, t_end = f["time_window"]
        print(f"  [{t_start:.1f}s-{t_end:.1f}s] {f['element']} ({f['type']})")
    print()

    # Phase 3: 修改 (并行生成覆盖图)
    overlay_results = generate_overlays(findings)
    print()

    # Phase 4: 合成
    output = reassemble(overlay_results)
    print()

    # 总结
    total_time = time.time() - t_total
    print("=" * 60)
    print("工作流完成!")
    print(f"  总耗时: {total_time:.1f}s")
    print(f"  发现品牌元素: {len(findings)} 个")
    print(f"  生成覆盖图: {len(overlay_results)} 张")
    if output:
        print(f"  输出视频: {output}")
    print(f"  工作目录: {WORK_DIR}")
    print("=" * 60)


if __name__ == "__main__":
    main()
