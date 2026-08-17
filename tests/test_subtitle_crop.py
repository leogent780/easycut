import shutil
import subprocess

import pytest

from shorts_factory.pipeline import subtitle_crop


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def test_topmost_bright_band_y_finds_synthetic_band(tmp_path):
    from PIL import Image

    img = Image.new("L", (200, 400), color=0)  # all-black frame
    pixels = img.load()
    # paint a bright band from y=300 to y=320 (in the bottom half of a 400px-tall frame)
    for y in range(300, 320):
        for x in range(200):
            pixels[x, y] = 255
    path = tmp_path / "frame.png"
    img.save(path)

    band_top = subtitle_crop._topmost_bright_band_y(path)
    assert band_top == 300


def test_topmost_bright_band_y_returns_none_when_no_band(tmp_path):
    from PIL import Image

    img = Image.new("L", (200, 400), color=0)
    path = tmp_path / "frame.png"
    img.save(path)

    assert subtitle_crop._topmost_bright_band_y(path) is None


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg/ffprobe not available in this environment")
def test_detect_bottom_crop_px_on_synthetic_video(tmp_path):
    """Build a tiny synthetic video with a bright band burned in near the bottom (like a
    subtitle) and confirm the heuristic finds it consistently across sampled frames.
    """
    video_path = tmp_path / "synthetic.mp4"
    # 200x400, 2s @ 5fps, black background with a white box from y=300-320 for the whole clip
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=200x400:d=2:r=5",
            "-vf", "drawbox=x=0:y=300:w=200:h=20:color=white:t=fill",
            "-pix_fmt", "yuv420p", str(video_path),
        ],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr

    crop_px = subtitle_crop.detect_bottom_crop_px(video_path, sample_count=4)
    # band starts at y=300, safety margin is 20px -> expect ~280
    assert 260 <= crop_px <= 300


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg/ffprobe not available in this environment")
def test_detect_bottom_crop_px_returns_zero_when_no_band(tmp_path):
    video_path = tmp_path / "plain.mp4"
    result = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=200x400:d=2:r=5", "-pix_fmt", "yuv420p", str(video_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr

    assert subtitle_crop.detect_bottom_crop_px(video_path, sample_count=4) == 0
