from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
VIDEO = ROOT.parent / "download" / "video.mp4"
OUT = ROOT / "keyframe_image_poc"
SOURCE_FRAMES = OUT / "source_frames"
EDITED_FRAMES = OUT / "edited_keyframes"
CLIPS = OUT / "clips"
FINAL_VIDEO = OUT / "byd_shark_keyframe_image_poc.mp4"
CONTACT_SHEET = OUT / "keyframe_image_poc_sheet.jpg"

W, H = 1280, 720
FPS = 30
CLIP_SECONDS = 2.4


SHOTS = [
    {
        "name": "01_street_front",
        "time": "00:00:07.2",
        "title": "AURORA X",
        "subtitle": "street launch shot",
        "badge": (468, 286, 652, 336),
        "panel": (760, 82, 1188, 178),
    },
    {
        "name": "02_side_feature",
        "time": "00:00:18.8",
        "title": "AURORA X",
        "subtitle": "hybrid pickup concept",
        "badge": (94, 332, 205, 378),
        "panel": (760, 500, 1180, 590),
    },
    {
        "name": "03_front_hero",
        "time": "00:00:27.0",
        "title": "AURORA X",
        "subtitle": "new grille and badge treatment",
        "badge": (500, 286, 780, 354),
        "panel": (72, 78, 510, 172),
    },
    {
        "name": "04_rear_badge",
        "time": "00:00:39.2",
        "title": "AURORA X",
        "subtitle": "rear identity replacement",
        "badge": (560, 268, 950, 362),
        "panel": (76, 500, 514, 596),
    },
    {
        "name": "05_night_product",
        "time": "00:01:11.4",
        "title": "AURORA X",
        "subtitle": "night-driving product angle",
        "badge": (472, 312, 718, 372),
        "panel": (756, 84, 1188, 178),
    },
]


def run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.returncode != 0:
        raise SystemExit(
            f"Command failed:\n{' '.join(cmd)}\n\nSTDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}"
        )


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


def fit_frame(path: Path) -> Image.Image:
    image = Image.open(path).convert("RGB")
    scale = max(W / image.width, H / image.height)
    resized = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS)
    left = (resized.width - W) // 2
    top = (resized.height - H) // 2
    return resized.crop((left, top, left + W, top + H))


def rounded(draw: ImageDraw.ImageDraw, xy, fill, outline=None, width: int = 2, radius: int = 10) -> None:
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def centered_text(draw: ImageDraw.ImageDraw, xy, text: str, size: int, color, bold: bool = True) -> None:
    x1, y1, x2, y2 = xy
    text_font = font(size, bold)
    bbox = draw.textbbox((0, 0), text, font=text_font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text((x1 + (x2 - x1 - tw) / 2, y1 + (y2 - y1 - th) / 2 - 2), text, font=text_font, fill=color)


def edit_keyframe(source: Path, dest: Path, shot: dict) -> None:
    base = fit_frame(source).convert("RGBA")
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Rough product-area replacement: a mask-like badge/identity patch.
    rounded(draw, shot["badge"], fill=(8, 12, 18, 226), outline=(198, 230, 255, 210), width=3, radius=9)
    centered_text(draw, shot["badge"], shot["title"], 34, (246, 250, 255, 255), True)

    # Product-demo callout panel. This stands in for image-edit output in the POC.
    px1, py1, px2, py2 = shot["panel"]
    rounded(draw, shot["panel"], fill=(5, 18, 31, 214), outline=(106, 198, 255, 190), width=2, radius=8)
    draw.text((px1 + 24, py1 + 18), "KEYFRAME IMAGE EDIT", font=font(18, False), fill=(160, 218, 255, 235))
    draw.text((px1 + 24, py1 + 42), shot["title"], font=font(34, True), fill=(248, 251, 255, 255))
    draw.text((px1 + 24, py1 + 78), shot["subtitle"], font=font(22, False), fill=(218, 236, 248, 242))

    # Small color swatches make the replacement read as a different product identity.
    for i, color in enumerate([(18, 155, 224, 230), (255, 255, 255, 230), (36, 48, 64, 230)]):
        x = px2 - 86 + i * 24
        draw.rounded_rectangle((x, py1 + 23, x + 16, py1 + 39), radius=4, fill=color)

    edited = Image.alpha_composite(base, overlay).convert("RGB")
    edited.save(dest, quality=95)


def extract_source_frames() -> None:
    SOURCE_FRAMES.mkdir(parents=True, exist_ok=True)
    for shot in SHOTS:
        target = SOURCE_FRAMES / f"{shot['name']}.jpg"
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                shot["time"],
                "-i",
                str(VIDEO),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(target),
            ]
        )


def edit_frames() -> None:
    EDITED_FRAMES.mkdir(parents=True, exist_ok=True)
    for shot in SHOTS:
        edit_keyframe(SOURCE_FRAMES / f"{shot['name']}.jpg", EDITED_FRAMES / f"{shot['name']}.jpg", shot)


def build_contact_sheet() -> None:
    thumb_w, thumb_h = 384, 216
    sheet = Image.new("RGB", (thumb_w * len(SHOTS), thumb_h * 2 + 52), (245, 247, 250))
    draw = ImageDraw.Draw(sheet)
    for index, shot in enumerate(SHOTS):
        x = index * thumb_w
        src = fit_frame(SOURCE_FRAMES / f"{shot['name']}.jpg").resize((thumb_w, thumb_h), Image.Resampling.LANCZOS)
        edited = fit_frame(EDITED_FRAMES / f"{shot['name']}.jpg").resize((thumb_w, thumb_h), Image.Resampling.LANCZOS)
        sheet.paste(src, (x, 0))
        sheet.paste(edited, (x, thumb_h + 28))
        draw.text((x + 12, thumb_h + 7), f"{shot['time']}  source -> edited", font=font(16, False), fill=(32, 38, 46))
    sheet.save(CONTACT_SHEET, quality=92)


def build_image_clips() -> None:
    CLIPS.mkdir(parents=True, exist_ok=True)
    total_frames = int(CLIP_SECONDS * FPS)
    for index, shot in enumerate(SHOTS, start=1):
        image = EDITED_FRAMES / f"{shot['name']}.jpg"
        clip = CLIPS / f"clip_{index:02d}.mp4"
        zoom = "zoompan=z='min(zoom+0.0012,1.07)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        vf = f"scale={W}:{H},setsar=1,{zoom}:d={total_frames}:s={W}x{H}:fps={FPS},format=yuv420p"
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-loop",
                "1",
                "-i",
                str(image),
                "-t",
                str(CLIP_SECONDS),
                "-vf",
                vf,
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "18",
                "-movflags",
                "+faststart",
                str(clip),
            ]
        )


def concat_clips() -> None:
    list_file = CLIPS / "concat.txt"
    lines = [f"file '{(CLIPS / f'clip_{i:02d}.mp4').as_posix()}'" for i in range(1, len(SHOTS) + 1)]
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temp_video = OUT / "image_montage_no_audio.mp4"
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            str(temp_video),
        ]
    )
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(temp_video),
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-shortest",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(FINAL_VIDEO),
        ]
    )


def main() -> None:
    if not VIDEO.exists():
        raise SystemExit(f"Source video not found: {VIDEO}")
    OUT.mkdir(parents=True, exist_ok=True)
    extract_source_frames()
    edit_frames()
    build_contact_sheet()
    build_image_clips()
    concat_clips()
    print(f"Edited keyframes: {EDITED_FRAMES}")
    print(f"Contact sheet: {CONTACT_SHEET}")
    print(f"POC video: {FINAL_VIDEO}")


if __name__ == "__main__":
    main()
