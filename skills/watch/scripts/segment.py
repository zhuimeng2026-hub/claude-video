#!/usr/bin/env python3
"""Video object segmentation via Replicate-hosted SAM 2.

Uploads a video to Replicate, runs the meta/sam-2-video model to produce
per-frame binary masks, and downloads them locally.  Pure stdlib — no
``pip install replicate`` needed.

Usage::

    python3 segment.py <video> --points "320,240" --labels "1"
    python3 segment.py <video> --points "320,240 100,100" --labels "1 0"
"""
from __future__ import annotations

import io
import json
import mimetypes
import os
import ssl
import subprocess
import sys
import time
import uuid
import urllib.error
from pathlib import Path
from urllib.request import Request, urlopen


# ── Replicate endpoints ──────────────────────────────────────────────────────

REPLICATE_API = "https://api.replicate.com/v1"
FILES_ENDPOINT = f"{REPLICATE_API}/files"
PREDICTIONS_ENDPOINT = f"{REPLICATE_API}/predictions"

DEFAULT_MODEL = (
    "meta/sam-2-video"
    ":8493227172126379a30e46d88442159788e60f14b7e5a09125f0a32323c67539"
)

# ── Retry / polling constants ────────────────────────────────────────────────

MAX_ATTEMPTS = 4
MAX_429_RETRIES = 2
RETRY_BASE_DELAY = 2.0

POLL_INTERVAL = 2.0        # initial polling interval (seconds)
MAX_POLL_INTERVAL = 30.0   # cap for exponential back-off
POLL_TIMEOUT = 600.0       # give up after 10 minutes


# ── Token loading ────────────────────────────────────────────────────────────

def load_replicate_token() -> str | None:
    """Return the Replicate API token from env or ~/.config/watch/.env."""
    def _from_env(name: str) -> str | None:
        value = os.environ.get(name)
        return value.strip() if value else None

    def _from_dotenv(path: Path, name: str) -> str | None:
        if not path.exists():
            return None
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() != name:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
                    value = value[1:-1]
                return value or None
        except OSError:
            return None
        return None

    dotenv_paths = [
        Path.home() / ".config" / "watch" / ".env",
        Path.cwd() / ".env",
    ]

    value = _from_env("REPLICATE_API_TOKEN")
    if value:
        return value
    for p in dotenv_paths:
        value = _from_dotenv(p, "REPLICATE_API_TOKEN")
        if value:
            return value
    return None


# ── HTTP helpers ─────────────────────────────────────────────────────────────

_CTX = ssl.create_default_context()
_USER_AGENT = "watch-skill/1.0 (+claude-code; python-urllib)"


def _headers(token: str, extra: dict | None = None) -> dict:
    h = {
        "Authorization": f"Bearer {token}",
        "User-Agent": _USER_AGENT,
    }
    if extra:
        h.update(extra)
    return h


def _read_error(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read()
        if body:
            return f" — {body.decode('utf-8', errors='replace')[:400]}"
    except Exception:
        pass
    return ""


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    header = exc.headers.get("Retry-After") if getattr(exc, "headers", None) else None
    if not header:
        return None
    try:
        return float(header)
    except ValueError:
        return None


# ── Multipart upload ─────────────────────────────────────────────────────────

def _build_multipart(fields: dict[str, str], file_path: Path) -> tuple[bytes, str]:
    """Hand-rolled multipart/form-data — avoids ``requests`` / SDK deps."""
    boundary = f"----WatchBoundary{uuid.uuid4().hex}"
    eol = b"\r\n"
    buf = io.BytesIO()

    for name, value in fields.items():
        buf.write(f"--{boundary}".encode()); buf.write(eol)
        buf.write(f'Content-Disposition: form-data; name="{name}"'.encode()); buf.write(eol)
        buf.write(eol)
        buf.write(str(value).encode()); buf.write(eol)

    mimetype = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    buf.write(f"--{boundary}".encode()); buf.write(eol)
    buf.write(
        f'Content-Disposition: form-data; name="content"; filename="{file_path.name}"'.encode()
    )
    buf.write(eol)
    buf.write(f"Content-Type: {mimetype}".encode()); buf.write(eol)
    buf.write(eol)
    buf.write(file_path.read_bytes())
    buf.write(eol)
    buf.write(f"--{boundary}--".encode()); buf.write(eol)

    return buf.getvalue(), boundary


# ── Replicate API operations ─────────────────────────────────────────────────

def upload_file(video_path: Path, token: str) -> str:
    """Upload a local file to Replicate's file storage. Returns the hosted URL."""
    body, boundary = _build_multipart({"content_type": "video/mp4"}, video_path)
    headers = _headers(token, {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    })

    request = Request(FILES_ENDPOINT, data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=120, context=_CTX) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Replicate file upload failed: {exc}{_read_error(exc)}")

    url = data.get("urls", {}).get("get")
    if not url:
        raise SystemExit(f"Replicate file upload returned no URL: {json.dumps(data)[:300]}")
    print(f"[watch] uploaded {video_path.name} → {url[:80]}…", file=sys.stderr)
    return url


def create_prediction(
    token: str,
    model_version: str,
    input_dict: dict,
) -> dict:
    """Create a prediction and wait up to 60 s for it to finish (Prefer: wait)."""
    payload = json.dumps({
        "version": model_version,
        "input": input_dict,
    }).encode()

    headers = _headers(token, {
        "Content-Type": "application/json",
        "Prefer": "wait",
    })

    request = Request(PREDICTIONS_ENDPOINT, data=payload, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=120, context=_CTX) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Replicate prediction create failed: {exc}{_read_error(exc)}")


def poll_prediction(token: str, prediction_id: str) -> dict:
    """Poll until the prediction reaches a terminal state."""
    url = f"{PREDICTIONS_ENDPOINT}/{prediction_id}"
    headers = _headers(token)
    interval = POLL_INTERVAL
    elapsed = 0.0

    while elapsed < POLL_TIMEOUT:
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=30, context=_CTX) as resp:
                data = json.loads(resp.read().decode())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"[watch] poll network error ({exc}) — retrying", file=sys.stderr)
            time.sleep(interval)
            elapsed += interval
            interval = min(interval * 2, MAX_POLL_INTERVAL)
            continue

        status = data.get("status", "")
        if status == "succeeded":
            return data
        if status in ("failed", "canceled"):
            error = data.get("error") or "unknown error"
            raise SystemExit(f"Replicate prediction {status}: {error}")

        print(f"[watch] SAM 2 status: {status} ({elapsed:.0f}s elapsed)", file=sys.stderr)
        time.sleep(interval)
        elapsed += interval
        interval = min(interval * 1.5, MAX_POLL_INTERVAL)

    raise SystemExit(f"Replicate prediction timed out after {POLL_TIMEOUT}s")


def download_file(url: str, out_path: Path, token: str) -> Path:
    """Download a file from Replicate's authenticated URL."""
    headers = _headers(token)
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=60, context=_CTX) as resp:
            out_path.write_bytes(resp.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Download failed ({url[:60]}…): {exc}{_read_error(exc)}")
    return out_path


# ── High-level orchestration ─────────────────────────────────────────────────

def segment_video(
    video_path: str | Path,
    points: list[list[int]],
    labels: list[int],
    output_dir: str | Path,
    token: str | None = None,
    model_version: str = DEFAULT_MODEL,
) -> dict:
    """Run SAM 2 video segmentation end-to-end.

    Returns ``{"masks": [{"path": ..., "index": ...}], "raw_output": ...}``.
    """
    video_path = Path(video_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if token is None:
        token = load_replicate_token()
    if not token:
        raise SystemExit(
            "No Replicate API token. Set REPLICATE_API_TOKEN in the environment "
            "or in ~/.config/watch/.env"
        )

    # 1. Upload
    print(f"[watch] uploading video to Replicate…", file=sys.stderr)
    video_url = upload_file(video_path, token)

    # 2. Create prediction
    input_dict = {
        "video": video_url,
        "points_per_side": 32,
    }
    if points:
        input_dict["point_coords"] = points
        input_dict["point_labels"] = labels

    print(f"[watch] creating SAM 2 prediction…", file=sys.stderr)
    pred = create_prediction(token, model_version, input_dict)

    # 3. Poll if not yet complete
    status = pred.get("status", "")
    if status != "succeeded":
        pred = poll_prediction(token, pred["id"])

    # 4. Download mask frames
    output = pred.get("output") or []
    if not output:
        raise SystemExit("SAM 2 returned no output masks")

    # output may be a list of URLs (video frames with masks) or a single video URL
    masks: list[dict] = []
    if isinstance(output, list):
        for i, item in enumerate(output):
            if isinstance(item, str) and item.startswith("http"):
                mask_path = output_dir / f"mask_{i + 1:04d}.png"
                download_file(item, mask_path, token)
                masks.append({"path": str(mask_path), "index": i + 1})
                print(f"[watch] downloaded mask {i + 1}/{len(output)}", file=sys.stderr)
    elif isinstance(output, str) and output.startswith("http"):
        # Single video output — download as-is
        mask_path = output_dir / "mask_output.mp4"
        download_file(output, mask_path, token)
        masks.append({"path": str(mask_path), "index": 0})

    print(f"[watch] segmentation complete — {len(masks)} mask(s) saved", file=sys.stderr)
    return {
        "masks": masks,
        "prediction_id": pred.get("id"),
        "model": model_version,
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def _parse_points(raw: str) -> list[list[int]]:
    """Parse ``'320,240 100,100'`` into ``[[320,240],[100,100]]``."""
    points = []
    for pair in raw.strip().split():
        x, y = pair.split(",")
        points.append([int(x), int(y)])
    return points


def _parse_labels(raw: str) -> list[int]:
    """Parse ``'1 0'`` into ``[1, 0]``."""
    return [int(l) for l in raw.strip().split()]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="SAM 2 video segmentation via Replicate"
    )
    parser.add_argument("video", help="Path to video file")
    parser.add_argument(
        "--points", required=True,
        help='Prompt points as "x,y x,y ..." (e.g. "320,240")',
    )
    parser.add_argument(
        "--labels", required=True,
        help='Point labels: 1=foreground 0=background (e.g. "1 0")',
    )
    parser.add_argument(
        "--output-dir", default=None,
        help="Directory for mask outputs (default: <video>.masks/)",
    )
    parser.add_argument(
        "--model-version", default=DEFAULT_MODEL,
        help="Replicate model version string",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output results as JSON to stdout",
    )
    args = parser.parse_args()

    video = Path(args.video)
    if not video.exists():
        raise SystemExit(f"Video not found: {video}")

    output_dir = Path(args.output_dir) if args.output_dir else video.with_suffix("") / "masks"
    points = _parse_points(args.points)
    labels = _parse_labels(args.labels)

    if len(points) != len(labels):
        raise SystemExit(
            f"Mismatch: {len(points)} points but {len(labels)} labels"
        )

    result = segment_video(
        video_path=video,
        points=points,
        labels=labels,
        output_dir=output_dir,
        model_version=args.model_version,
    )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"\n# SAM 2 Segmentation Results\n")
        print(f"Video: {video}")
        print(f"Masks: {len(result['masks'])} frame(s)")
        print(f"\n## Mask Frames\n")
        for m in result["masks"]:
            print(f"- {m['path']} (frame {m['index']})")


if __name__ == "__main__":
    main()
