from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
OVERLAYS = ROOT / "overlays"
OVERLAYS.mkdir(parents=True, exist_ok=True)

W, H = 1280, 720


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


def rounded_box(draw: ImageDraw.ImageDraw, xy, fill, outline=None, width=2, radius=8):
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def label(draw: ImageDraw.ImageDraw, xy, text, size=32, color=(246, 250, 255, 255), bold=True):
    draw.text(xy, text, font=font(size, bold), fill=color)


def plate(draw: ImageDraw.ImageDraw, xy, text, size=28, fill=(18, 22, 30, 220)):
    x1, y1, x2, y2 = xy
    rounded_box(draw, xy, fill=fill, outline=(210, 230, 255, 190), width=2, radius=6)
    bbox = draw.textbbox((0, 0), text, font=font(size, True))
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((x1 + (x2 - x1 - tw) / 2, y1 + (y2 - y1 - th) / 2 - 1), text, font=font(size, True), fill=(245, 250, 255, 255))


def save_overlay(name: str, builder):
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(im)
    builder(draw)
    path = OVERLAYS / f"{name}.png"
    im.save(path)
    return path


overlays: list[tuple[str, float, float]] = []


def add(name: str, start: float, end: float, builder):
    save_overlay(name, builder)
    overlays.append((name, start, end))


def intro(draw):
    # Building sign and front grille: a rough mask + replacement brand layer.
    rounded_box(draw, (353, 38, 742, 95), fill=(8, 12, 18, 205), outline=(154, 214, 255, 190), radius=10)
    label(draw, (380, 49), "AURORA X", 38)
    label(draw, (591, 57), "export", 20, color=(190, 220, 250, 230), bold=False)
    plate(draw, (442, 315, 575, 352), "AURORA", 23)


def street_front(draw):
    plate(draw, (470, 288, 642, 334), "AURORA", 32)
    plate(draw, (523, 405, 704, 442), "AURORA X", 24)


def side_close(draw):
    plate(draw, (98, 335, 194, 374), "AX", 26)
    rounded_box(draw, (780, 502, 1080, 548), fill=(13, 17, 24, 190), outline=(110, 190, 255, 170), radius=6)
    label(draw, (801, 511), "Hybrid pickup demo", 23, color=(234, 247, 255, 250), bold=False)


def hero_front(draw):
    plate(draw, (505, 290, 775, 352), "AURORA", 44)
    plate(draw, (535, 428, 745, 468), "AURORA X", 27)


def rear_badge(draw):
    rounded_box(draw, (560, 270, 948, 361), fill=(78, 105, 126, 214), outline=(210, 232, 245, 155), radius=10)
    label(draw, (600, 292), "AURORA", 48)
    label(draw, (808, 310), "X", 34, color=(218, 235, 250, 240))
    plate(draw, (584, 646, 745, 683), "AURORA X", 23)


def night_front(draw):
    plate(draw, (475, 315, 715, 370), "AURORA", 40, fill=(8, 10, 16, 210))
    plate(draw, (528, 428, 720, 468), "AURORA X", 25, fill=(8, 10, 16, 210))


def end_card(draw):
    draw.rectangle((0, 0, W, H), fill=(4, 84, 164, 255))
    label(draw, (410, 286), "AURORA X", 62)
    label(draw, (409, 356), "GLOBAL PRODUCT DEMO", 30, color=(230, 245, 255, 245), bold=False)
    rounded_box(draw, (410, 410, 870, 454), fill=(8, 22, 42, 130), outline=(198, 226, 255, 150), radius=8)
    label(draw, (444, 417), "export-ready pickup concept", 24, color=(232, 244, 255, 245), bold=False)


add("overlay_01_intro", 0.0, 0.2, intro)
add("overlay_02_street_front", 6.7, 9.6, street_front)
add("overlay_03_side_close", 18.0, 20.5, side_close)
add("overlay_04_hero_front", 26.5, 28.2, hero_front)
add("overlay_05_rear_badge", 38.4, 41.5, rear_badge)
add("overlay_06_night_front", 70.0, 74.5, night_front)
add("overlay_07_end_card", 90.8, 93.9, end_card)

lines = []
current = "[0:v]"
for i, (name, start, end) in enumerate(overlays, start=1):
    nxt = f"[v{i}]"
    lines.append(f"{current}[{i}:v]overlay=0:0:enable='between(t,{start},{end})'{nxt}")
    current = nxt

(ROOT / "filter_complex.txt").write_text(";\n".join(lines), encoding="utf-8")

summary = [
    "# Product Replacement POC Assets",
    "",
    "Generated transparent replacement overlays and an ffmpeg filter graph.",
    "",
    "## Overlays",
    "",
]
for name, start, end in overlays:
    summary.append(f"- `{OVERLAYS / (name + '.png')}`: {start:.1f}s to {end:.1f}s")
summary.extend(
    [
        "",
        "## Filter Graph",
        "",
        f"`{ROOT / 'filter_complex.txt'}`",
        "",
        "This is a lightweight stand-in for detection/segmentation + inpainting + tracking:",
        "",
        "- Detection/segmentation: hand-authored product/logo boxes on selected shots.",
        "- Inpainting: masked boxes use local-looking dark or body-color fills.",
        "- Tracking: time-windowed overlays follow stable product shots.",
        "- Composition: ffmpeg overlays render a new video while preserving the source audio.",
    ]
)
(ROOT / "PRODUCT_REPLACEMENT_POC_ASSETS.md").write_text("\n".join(summary) + "\n", encoding="utf-8")

print(f"Generated {len(overlays)} overlays under {OVERLAYS}")
print(ROOT / "filter_complex.txt")
