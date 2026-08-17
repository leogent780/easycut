"""Best-effort automatic detection of a burned-in subtitle/watermark band near the bottom of
a source video, so it can be cropped out before re-encoding for a dubbed short.

Approach (matches the manual technique validated by hand on a real source video — see
samples/xhs_dish_brush_ko_dub/README.md): sample several frames spread across the video,
grayscale-scan each frame's bottom half row-by-row for a sustained band of bright pixels
(the anti-aliased white-with-dark-outline look of most burned-in captions), and return the
topmost such band's y-position (with a safety margin) as a "keep the video above this line"
crop height. This is a heuristic, not OCR/text-detection — see `Limitations` below.

Limitations (be upfront about these, don't oversell the automation):
- Assumes the caption/watermark sits in a horizontal band near the bottom and is close to
  white/bright text — captions that are dark-on-light, positioned elsewhere, or absent
  entirely will not be detected reliably. Callers should treat the return value as a
  starting point a human should glance at before trusting it for every future video, exactly
  like the gaming-layout crop rects already require one-time manual calibration per streamer
  (see pipeline/render.py's module docstring).
- Only ever recommends a bottom crop (cropping from the top-down to exclude a bottom band).
  A subtitle baked in elsewhere on-frame is out of scope for this heuristic.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

BRIGHT_THRESHOLD = 200
MIN_BRIGHT_PIXEL_COUNT = 25  # per-row, out of a full-width scan
SAFETY_MARGIN_PX = 20


def _extract_frame(video_path: str | Path, timestamp_s: float, out_path: Path) -> bool:
    result = subprocess.run(
        ["ffmpeg", "-y", "-ss", str(timestamp_s), "-i", str(video_path), "-frames:v", "1", "-update", "1", str(out_path)],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and out_path.exists()


def _topmost_bright_band_y(image_path: Path) -> int | None:
    """Return the topmost y (in the bottom half of the frame) where a sustained bright
    horizontal band starts, or None if no such band is found.
    """
    from PIL import Image
    import numpy as np

    img = Image.open(image_path).convert("L")
    arr = np.array(img)
    h, _ = arr.shape
    scan_start = h // 2  # only look at the bottom half — captions/watermarks live there

    band_top: int | None = None
    for y in range(scan_start, h):
        bright_count = int((arr[y] > BRIGHT_THRESHOLD).sum())
        if bright_count >= MIN_BRIGHT_PIXEL_COUNT:
            band_top = y
            break
    return band_top


def detect_bottom_crop_px(video_path: str | Path, sample_count: int = 6) -> int:
    """Sample `sample_count` frames spread across the video's duration and return a safe
    "keep video from y=0 to this height" crop value. Returns 0 (no crop) if fewer than half
    the sampled frames show a detectable bright band — a single stray frame (e.g. a bright
    highlight in the footage itself) shouldn't force a crop on the whole video.
    """
    duration = _probe_duration(video_path)
    if duration <= 0:
        return 0

    timestamps = [duration * (i + 1) / (sample_count + 1) for i in range(sample_count)]
    band_tops: list[int] = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for i, ts in enumerate(timestamps):
            frame_path = tmp_dir / f"frame_{i}.png"
            if not _extract_frame(video_path, ts, frame_path):
                continue
            band_top = _topmost_bright_band_y(frame_path)
            if band_top is not None:
                band_tops.append(band_top)

    if len(band_tops) < (sample_count + 1) // 2:
        return 0  # not consistent enough across samples — don't force a crop

    # take the MINIMUM (highest on screen) observed band top across samples — the crop must
    # be tall enough to exclude the caption in its highest-observed position in any sample.
    safest_band_top = min(band_tops)
    return max(0, safest_band_top - SAFETY_MARGIN_PX)


def _probe_duration(video_path: str | Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0
