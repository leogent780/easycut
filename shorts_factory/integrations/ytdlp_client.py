"""yt-dlp wrapper: source video download + YouTube auto-caption extraction.

Search/candidate discovery goes through the YouTube Data API (youtube_api.py) instead —
yt-dlp is only used here for (a) downloading the actual media and (b) pulling the
public auto-caption track, which the Data API's captions.* endpoints cannot do for
videos the app doesn't own (see plan §유튜브 API 핵심 사항).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CaptionCue:
    start_s: float
    end_s: float
    text: str


def _ydl_opts(extra: dict | None = None) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noprogress": True}
    if extra:
        opts.update(extra)
    return opts


def download_video(video_id: str, output_dir: str | Path) -> Path:
    """Download best video+audio for a YouTube video id. Returns the local file path."""
    import yt_dlp

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={video_id}"
    outtmpl = str(output_dir / f"{video_id}.%(ext)s")

    opts = _ydl_opts({"format": "bestvideo+bestaudio/best", "outtmpl": outtmpl, "merge_output_format": "mp4"})
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return Path(ydl.prepare_filename(info)).with_suffix(".mp4")


def fetch_auto_captions(video_id: str, lang: str = "ko") -> list[CaptionCue] | None:
    """Fetch YouTube's own auto-generated (or uploader-provided) captions as VTT, parsed into cues.

    Returns None if no caption track is available in the requested language — caller
    should fall back to whisper transcription (see pipeline/transcribe.py).
    """
    import tempfile

    import yt_dlp

    with tempfile.TemporaryDirectory() as tmp:
        outtmpl = str(Path(tmp) / f"{video_id}.%(ext)s")
        opts = _ydl_opts(
            {
                "skip_download": True,
                "writeautomaticsub": True,
                "writesubtitles": True,
                "subtitleslangs": [lang],
                "subtitlesformat": "vtt",
                "outtmpl": outtmpl,
            }
        )
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])

        vtt_files = list(Path(tmp).glob(f"{video_id}*.vtt"))
        if not vtt_files:
            return None
        return _parse_vtt(vtt_files[0].read_text(encoding="utf-8"))


_VTT_TIME_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})\.(\d{3})"
)


def _vtt_timestamp_to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _parse_vtt(content: str) -> list[CaptionCue]:
    """Parse a WebVTT file into (start, end, text) cues, stripping inline styling tags."""
    cues: list[CaptionCue] = []
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        match = _VTT_TIME_RE.search(lines[i])
        if match:
            start = _vtt_timestamp_to_seconds(*match.group(1, 2, 3, 4))
            end = _vtt_timestamp_to_seconds(*match.group(5, 6, 7, 8))
            i += 1
            text_lines = []
            while i < len(lines) and lines[i].strip():
                text_lines.append(lines[i])
                i += 1
            text = " ".join(text_lines)
            text = re.sub(r"<[^>]+>", "", text).strip()  # strip <c>, <00:00:00.000> word-timing tags
            if text and (not cues or cues[-1].text != text):  # yt-dlp auto-subs often repeat the same
                cues.append(CaptionCue(start_s=start, end_s=end, text=text))  # cue with growing text —
        else:  # keep only the final (complete) occurrence would need look-ahead;
            i += 1  # simple de-dup on identical text is a reasonable approximation.
    return cues
