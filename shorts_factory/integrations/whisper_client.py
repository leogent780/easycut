"""faster-whisper wrapper — fallback transcription when yt-dlp auto-captions are missing/unusable.

Word-level timestamps are required here (not just segment-level) because captions.py needs
per-word timing to build karaoke-style ASS captions. This is the single most CPU/GPU-intensive
step in the pipeline (see plan §1a) — Phase 1 must measure real wall-clock time on the actual
target hardware before assuming a given channel's daily cycle time budget.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass
class Word:
    text: str
    start_s: float
    end_s: float


@dataclass
class TranscriptSegment:
    text: str
    start_s: float
    end_s: float
    words: list[Word]


def _pick_compute_config() -> tuple[str, str, str]:
    """Return (model_size, device, compute_type) — GPU if available, else CPU int8 for speed."""
    try:
        import torch

        if torch.cuda.is_available():
            return "large-v3", "cuda", "float16"
    except ImportError:
        pass
    return "medium", "cpu", "int8"


@lru_cache(maxsize=1)
def _get_model():
    from faster_whisper import WhisperModel

    model_size, device, compute_type = _pick_compute_config()
    return WhisperModel(model_size, device=device, compute_type=compute_type)


def transcribe(audio_path: str, language: str | None = None) -> list[TranscriptSegment]:
    """Transcribe an audio/video file with word-level timestamps.

    `language` should match the source content's spoken language (None = auto-detect,
    which costs an extra pass — pass it explicitly when known, e.g. "en" for English
    gaming-stream audio being localized into Korean).
    """
    model = _get_model()
    segments_iter, _info = model.transcribe(audio_path, language=language, word_timestamps=True)

    segments: list[TranscriptSegment] = []
    for seg in segments_iter:
        words = [Word(text=w.word.strip(), start_s=w.start, end_s=w.end) for w in (seg.words or [])]
        segments.append(TranscriptSegment(text=seg.text.strip(), start_s=seg.start, end_s=seg.end, words=words))
    return segments


def segments_to_plain_transcript(segments: list[TranscriptSegment]) -> str:
    """Flatten to plain text with inline timestamps, for feeding to the LLM segment-selection prompt."""
    lines = [f"[{s.start_s:.1f}-{s.end_s:.1f}] {s.text}" for s in segments]
    return "\n".join(lines)
