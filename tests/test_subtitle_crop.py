import shutil
import subprocess

import pytest

from shorts_factory.pipeline import subtitle_crop


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _paint_text_like_band(pixels, width: int, y_start: int, y_end: int) -> None:
    """Alternating bright/dark stripes — bright enough and edgy enough to pass both the
    brightness and edge-density checks, standing in for real outlined caption text.
    """
    for y in range(y_start, y_end):
        for x in range(width):
            pixels[x, y] = 255 if (x // 4) % 2 == 0 else 0


def test_topmost_bright_band_y_finds_synthetic_band(tmp_path):
    from PIL import Image

    img = Image.new("L", (200, 400), color=0)  # all-black frame
    pixels = img.load()
    # paint a text-like band from y=300 to y=320 (in the bottom half of a 400px-tall frame)
    _paint_text_like_band(pixels, 200, 300, 320)
    path = tmp_path / "frame.png"
    img.save(path)

    band_top = subtitle_crop._topmost_bright_band_y(path)
    assert band_top == 300


def test_topmost_bright_band_y_ignores_plain_bright_background(tmp_path):
    """A uniformly bright region (e.g. a white countertop) has no edges and must NOT be
    mistaken for caption text — this is the false-positive this heuristic was specifically
    revised to avoid (see module docstring)."""
    from PIL import Image

    img = Image.new("L", (200, 400), color=0)
    pixels = img.load()
    for y in range(300, 400):
        for x in range(200):
            pixels[x, y] = 255  # plain bright fill, no internal edges
    path = tmp_path / "frame.png"
    img.save(path)

    assert subtitle_crop._topmost_bright_band_y(path) is None


def test_topmost_bright_band_y_returns_none_when_no_band(tmp_path):
    from PIL import Image

    img = Image.new("L", (200, 400), color=0)
    path = tmp_path / "frame.png"
    img.save(path)

    assert subtitle_crop._topmost_bright_band_y(path) is None


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg/ffprobe not available in this environment")
def test_detect_bottom_crop_px_on_synthetic_video(tmp_path):
    """Build a tiny synthetic video with a text-like (striped, edgy) band burned in near the
    bottom and confirm the heuristic finds it consistently across sampled frames.
    """
    video_path = tmp_path / "synthetic.mp4"
    # 200x400, 2s @ 5fps, black background with a vertical-striped (text-like) band y=300-320
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=200x400:d=2:r=5",
            "-vf",
            "geq=lum='if(between(Y,300,320), if(mod(floor(X/4),2),0,255), 0)':cb=128:cr=128",
            "-pix_fmt", "yuv420p", str(video_path),
        ],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr

    crop_px = subtitle_crop.detect_bottom_crop_px(video_path, sample_count=4)
    # band starts at y=300, safety margin is 20px -> expect ~280
    assert 260 <= crop_px <= 300


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg/ffprobe not available in this environment")
def test_detect_bottom_crop_px_returns_zero_for_plain_bright_background(tmp_path):
    """A plain bright (edge-free) background — like a white countertop — must not trigger a
    crop, even though it's easily bright enough to have tripped the old brightness-only check.
    """
    video_path = tmp_path / "plain_bright.mp4"
    result = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=white:s=200x400:d=2:r=5", "-pix_fmt", "yuv420p", str(video_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr

    assert subtitle_crop.detect_bottom_crop_px(video_path, sample_count=4) == 0


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg/ffprobe not available in this environment")
def test_detect_bottom_crop_px_returns_zero_when_no_band(tmp_path):
    video_path = tmp_path / "plain.mp4"
    result = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=200x400:d=2:r=5", "-pix_fmt", "yuv420p", str(video_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr

    assert subtitle_crop.detect_bottom_crop_px(video_path, sample_count=4) == 0
