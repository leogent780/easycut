"""ffmpeg orchestration: trim -> crop/scale to 1080x1920 -> burn ASS captions -> composite
format-template overlay PNG -> finished clip.

Uses ffmpeg via subprocess rather than moviepy — moviepy's Python-side frame processing adds
real throughput cost with no benefit here (crop/scale/subtitle-burn is exactly what ffmpeg's
native filter graph does, faster, without an extra Python decode/encode layer). A subprocess
crash is also easier to isolate/retry per-clip than an in-process library crash that could
take down the whole daemon (see plan §기술 스택).

NOTE ON UNVERIFIED PARTS: this sandbox cannot download real source footage or the mediapipe
face-detection model (no general internet access — see plan §실행 환경), so the crop/render
paths below are implemented to spec but NOT yet visually verified against real gaming/talking-
head footage. Phase 1's stated job is exactly to do that verification on the user's own machine
(see plan §단계별 빌드 순서, Phase 1) — treat this file as "correct per ffmpeg/mediapipe docs,
pending real-footage validation," not as already proven.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from shorts_factory.pipeline.highlight_select import Segment

CANVAS_WIDTH = 1080
CANVAS_HEIGHT = 1920

# Default vertical split for the gaming facecam+gameplay layout (see plan's honest limitation:
# every new streamer needs this ratio, and the underlying rects, calibrated once by hand).
DEFAULT_FACECAM_HEIGHT = 576  # ~30% of 1920
DEFAULT_GAMEPLAY_HEIGHT = CANVAS_HEIGHT - DEFAULT_FACECAM_HEIGHT


def _run_ffmpeg(args: list[str]) -> None:
    result = subprocess.run(["ffmpeg", "-y", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed (exit {result.returncode}):\n{result.stderr[-4000:]}")


def _gaming_crop_filter(layout_config: dict) -> str:
    """Facecam (top) + gameplay (bottom) composited into a single 1080x1920 canvas."""
    fx, fy, fw, fh = layout_config["facecam_rect"]
    gx, gy, gw, gh = layout_config["gameplay_crop_rect"]
    return (
        f"[0:v]crop={fw}:{fh}:{fx}:{fy},scale={CANVAS_WIDTH}:{DEFAULT_FACECAM_HEIGHT}[facecam];"
        f"[0:v]crop={gw}:{gh}:{gx}:{gy},scale={CANVAS_WIDTH}:{DEFAULT_GAMEPLAY_HEIGHT}[gameplay];"
        f"[facecam][gameplay]vstack=inputs=2[cropped]"
    )


def _detect_face_center_x(video_path: str | Path, sample_count: int = 5) -> int | None:
    """Best-effort: sample a few frames, run mediapipe face detection, return the median
    face-center x-coordinate (source resolution pixels). Returns None on any failure —
    caller falls back to a plain center crop, logging why. Not a per-frame dynamic pan
    (see plan: true tracking-with-smoothing is flagged as real tuning work, out of this
    pass's scope) — this computes ONE static crop position for the whole clip.
    """
    try:
        import cv2
        import mediapipe as mp
    except ImportError:
        return None

    try:
        cap = cv2.VideoCapture(str(video_path))
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total_frames <= 0:
            cap.release()
            return None

        centers = []
        with mp.solutions.face_detection.FaceDetection(min_detection_confidence=0.5) as detector:
            for i in range(sample_count):
                frame_idx = int(total_frames * (i + 1) / (sample_count + 1))
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
                ok, frame = cap.read()
                if not ok:
                    continue
                results = detector.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                if results.detections:
                    box = results.detections[0].location_data.relative_bounding_box
                    frame_w = frame.shape[1]
                    center_x = int((box.xmin + box.width / 2) * frame_w)
                    centers.append(center_x)
        cap.release()

        if not centers:
            return None
        centers.sort()
        return centers[len(centers) // 2]  # median, robust to one bad detection
    except Exception:
        return None


def _centered_crop_filter(source_w: int, source_h: int, face_center_x: int | None) -> str:
    """Static 9:16 crop from a landscape/other-aspect source, optionally centered on a detected face."""
    target_w = int(source_h * CANVAS_WIDTH / CANVAS_HEIGHT)
    target_w = min(target_w, source_w)
    if face_center_x is not None:
        crop_x = max(0, min(face_center_x - target_w // 2, source_w - target_w))
    else:
        crop_x = (source_w - target_w) // 2
    return f"[0:v]crop={target_w}:{source_h}:{crop_x}:0,scale={CANVAS_WIDTH}:{CANVAS_HEIGHT}[cropped]"


def _probe_dimensions(video_path: str | Path) -> tuple[int, int]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(video_path),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"ffprobe failed to read dimensions for {video_path}: {result.stderr}")
    w, h = result.stdout.strip().split("x")
    return int(w), int(h)


def render_clip(
    source_video_path: str | Path,
    segment: Segment,
    ass_caption_path: str | Path,
    overlay_png_path: str | Path,
    output_path: str | Path,
    layout_config: dict | None = None,
) -> Path:
    """Trim `segment` from the source, crop/scale to 1080x1920, burn captions + overlay.

    `layout_config` (from config/layouts/<ref>.yaml) selects the gaming facecam+gameplay
    composite crop; omit it for a plain face-centered/static crop (health/talking-head niche).
    """
    source_video_path = Path(source_video_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if layout_config is not None:
        crop_filter = _gaming_crop_filter(layout_config)
    else:
        source_w, source_h = _probe_dimensions(source_video_path)
        face_center_x = _detect_face_center_x(source_video_path)
        crop_filter = _centered_crop_filter(source_w, source_h, face_center_x)

    ass_path_escaped = str(ass_caption_path).replace("\\", "\\\\").replace(":", "\\:")
    filter_complex = (
        f"{crop_filter};"
        f"[cropped]subtitles={ass_path_escaped}[captioned];"
        f"[captioned][1:v]overlay=0:0[final]"
    )

    args = [
        "-ss", str(segment.start_s),
        "-to", str(segment.end_s),
        "-i", str(source_video_path),
        "-i", str(overlay_png_path),
        "-filter_complex", filter_complex,
        "-map", "[final]",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-c:a", "aac",
        "-movflags", "+faststart",
        str(output_path),
    ]
    _run_ffmpeg(args)
    return output_path
