"""Transcript acquisition: YouTube auto-captions first (fast, free, no download needed),
faster-whisper fallback when captions are missing or look too sparse to be usable
(common for gaming VODs — crowd noise/game audio, little clear speech).
"""

from __future__ import annotations

from dataclasses import dataclass

from shorts_factory.integrations import ytdlp_client, whisper_client
from shorts_factory.integrations.whisper_client import TranscriptSegment, Word

# Below this average chars-per-minute-of-video, auto-captions are treated as too sparse to trust
# (heuristic, not a hard science — tune after Phase 1 real-data review per the plan).
MIN_CAPTION_DENSITY_CHARS_PER_MIN = 40


@dataclass
class TranscriptResult:
    segments: list[TranscriptSegment]
    source: str  # "youtube_captions" | "whisper"
    has_word_level_timing: bool


def _captions_to_segments(cues) -> list[TranscriptSegment]:
    segments = []
    for cue in cues:
        # No true per-word timing from a caption cue — treat the whole cue as one "word"
        # spanning its full duration. captions.py falls back to whole-line display (not
        # karaoke-style highlight) whenever has_word_level_timing is False.
        word = Word(text=cue.text, start_s=cue.start_s, end_s=cue.end_s)
        segments.append(TranscriptSegment(text=cue.text, start_s=cue.start_s, end_s=cue.end_s, words=[word]))
    return segments


def _caption_density(cues, duration_s: float) -> float:
    if duration_s <= 0:
        return 0.0
    total_chars = sum(len(c.text) for c in cues)
    return total_chars / (duration_s / 60.0)


def get_transcript(
    video_id: str,
    local_media_path: str,
    duration_s: float,
    caption_lang: str = "ko",
    whisper_language: str | None = None,
) -> TranscriptResult | None:
    """Returns None only when both caption fetch and whisper fail entirely — caller should
    skip this source video and fall back to the next discovery candidate (see plan §실패
    처리 원칙), not retry the same video."""
    cues = ytdlp_client.fetch_auto_captions(video_id, lang=caption_lang)
    if cues and _caption_density(cues, duration_s) >= MIN_CAPTION_DENSITY_CHARS_PER_MIN:
        return TranscriptResult(
            segments=_captions_to_segments(cues), source="youtube_captions", has_word_level_timing=False
        )

    try:
        segments = whisper_client.transcribe(local_media_path, language=whisper_language)
    except Exception:
        return None

    if not segments:
        return None
    return TranscriptResult(segments=segments, source="whisper", has_word_level_timing=True)


def to_plain_transcript(result: TranscriptResult) -> str:
    return whisper_client.segments_to_plain_transcript(result.segments)
