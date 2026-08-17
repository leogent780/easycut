"""Best-effort automatic detection of a burned-in subtitle/watermark band near the bottom of
a source video, so it can be cropped out before re-encoding for a dubbed short.

Approach: sample several frames spread across the video and look, in each frame's bottom
half, for the topmost row where BOTH:
  (a) a sustained run of bright pixels exists (the white fill of most burned-in captions), and
  (b) that row has a high density of sharp brightness jumps (the black outline stroke most
      burned-in captions use creates many white<->black edges close together in a single row)
Requiring both signals — not just (a) — is what makes this usable on real footage: a plain
bright background (a white countertop, a reflective steel sink, a sunlit wall) easily produces
a tall run of bright pixels with almost no edges, and would otherwise cause a false positive
(this was confirmed empirically on a real TikTok source video during development — brightness-
only detection locked onto a reflective faucet instead of the actual caption text). The
combined test reliably separated true caption rows from background brightness on that same
video's frames.

Across multiple sampled frames, the MEDIAN detected band-top is used rather than the minimum
— matching the same "median, robust to one bad detection" approach already used for face-crop
centering in pipeline/render.py — so one outlier frame (e.g. an intro frame that isn't part of
the actual captioned content) doesn't skew the result.

This is a heuristic, not OCR/text-detection — see `Limitations` below.

Limitations (be upfront about these, don't oversell the automation):
- Assumes the caption/watermark sits in a horizontal band near the bottom and uses a bright
  fill with a dark outline (the overwhelmingly common style) — captions styled otherwise, or
  positioned elsewhere, may not be detected reliably. Callers should treat the return value as
  a starting point a human should glance at before trusting it for every future video, exactly
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
MIN_BRIGHT_PIXEL_COUNT = 50  # per-row, out of a full-width scan
EDGE_JUMP_THRESHOLD = 50  # minimum brightness delta between adjacent pixels to count as an "edge"
MIN_EDGE_COUNT = 20  # per-row
SUSTAIN_ROWS = 3  # require the row to look like text for this many consecutive rows
SAFETY_MARGIN_PX = 20


def _extract_frame(video_path: str | Path, timestamp_s: float, out_path: Path) -> bool:
    result = subprocess.run(
        ["ffmpeg", "-y", "-ss", str(timestamp_s), "-i", str(video_path), "-frames:v", "1", "-update", "1", str(out_path)],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and out_path.exists()


def _row_looks_like_text(row) -> bool:
    import numpy as np

    bright_count = int((row > BRIGHT_THRESHOLD).sum())
    if bright_count < MIN_BRIGHT_PIXEL_COUNT:
        return False
    edge_count = int((np.abs(np.diff(row.astype(int))) > EDGE_JUMP_THRESHOLD).sum())
    return edge_count >= MIN_EDGE_COUNT


def _topmost_bright_band_y(image_path: Path) -> int | None:
    """Return the topmost y (in the bottom half of the frame) where a sustained
    caption-like row run starts, or None if no such run is found.
    """
    from PIL import Image
    import numpy as np

    img = Image.open(image_path).convert("L")
    arr = np.array(img)
    h, _ = arr.shape
    scan_start = h // 2  # only look at the bottom half — captions/watermarks live there

    for y in range(scan_start, h - SUSTAIN_ROWS):
        if all(_row_looks_like_text(arr[y + k]) for k in range(SUSTAIN_ROWS)):
            return y
    return None


def detect_bottom_crop_px(video_path: str | Path, sample_count: int = 8) -> int:
    """Sample `sample_count` frames spread across the video's duration and return a safe
    "keep video from y=0 to this height" crop value. Returns 0 (no crop) if fewer than half
    the sampled frames show a detectable caption-like band — a single stray frame (e.g. a
    bright highlight, or a non-representative intro frame) shouldn't force a crop on the
    whole video.
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

    band_tops.sort()
    median_band_top = band_tops[len(band_tops) // 2]
    return max(0, median_band_top - SAFETY_MARGIN_PX)


def _probe_duration(video_path: str | Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0
