"""Segment selection: pick the most viral-worthy 30-60s clips from a long-form transcript.

The riskiest, most-iterated prompt in the system (see plan §명시적 리스크/한계 — no automated
quality signal exists for "is this actually a good clip"; Phase 1 relies on human review).
"""

from __future__ import annotations

from dataclasses import dataclass

from shorts_factory.config import ChannelConfig
from shorts_factory.integrations.claude_client import MODEL_SONNET, call_structured
from shorts_factory.integrations.whisper_client import TranscriptSegment
from shorts_factory.pipeline.transcribe import TranscriptResult, to_plain_transcript

MIN_SEGMENT_S = 30
MAX_SEGMENT_S = 60

SEGMENT_SELECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_ts": {"type": "number", "description": "Segment start, seconds into the source video"},
                    "end_ts": {"type": "number", "description": "Segment end, seconds into the source video"},
                    "hook_title": {"type": "string", "description": "Punchy Korean hook title, under 30 characters"},
                    "confidence": {"type": "number", "description": "0.0-1.0, how confident this segment will perform well"},
                },
                "required": ["start_ts", "end_ts", "hook_title", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["segments"],
    "additionalProperties": False,
}


@dataclass
class Segment:
    start_s: float
    end_s: float
    hook_title: str
    confidence: float

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


def _base_system_prompt(niche_description: str, target_count: int) -> str:
    return (
        "You are selecting the most viral-worthy short clips from a long-form video transcript, "
        f"for a YouTube Shorts channel with this niche: {niche_description}\n\n"
        f"Find up to {target_count} distinct segments, each {MIN_SEGMENT_S}-{MAX_SEGMENT_S} seconds long, "
        "that a human editor would clip as the most surprising, funny, emotionally peak, or "
        "otherwise hook-worthy moments. Segments must not overlap. Each needs a punchy Korean "
        "hook title (under 30 characters) suitable as the on-screen banner text."
    )


def _validate(segments: list[Segment]) -> tuple[bool, str]:
    if not segments:
        return False, "no segments returned"
    for s in segments:
        if not (MIN_SEGMENT_S - 1 <= s.duration_s <= MAX_SEGMENT_S + 1):  # 1s tolerance for rounding
            return False, f"segment {s.start_s}-{s.end_s} duration {s.duration_s:.1f}s outside {MIN_SEGMENT_S}-{MAX_SEGMENT_S}s"
    ordered = sorted(segments, key=lambda s: s.start_s)
    for prev, cur in zip(ordered, ordered[1:]):
        if cur.start_s < prev.end_s:
            return False, f"overlapping segments: {prev.start_s}-{prev.end_s} and {cur.start_s}-{cur.end_s}"
    return True, ""


def _snap_to_word_boundaries(segments: list[Segment], transcript_segments: list[TranscriptSegment]) -> list[Segment]:
    """Nudge start/end onto the nearest real word boundary so clips don't cut mid-word."""
    all_words = [w for seg in transcript_segments for w in seg.words]
    if not all_words:
        return segments

    def nearest_boundary(ts: float, edge: str) -> float:
        candidates = [w.start_s if edge == "start" else w.end_s for w in all_words]
        return min(candidates, key=lambda c: abs(c - ts))

    return [
        Segment(
            start_s=nearest_boundary(s.start_s, "start"),
            end_s=nearest_boundary(s.end_s, "end"),
            hook_title=s.hook_title,
            confidence=s.confidence,
        )
        for s in segments
    ]


def _call_and_parse(system_prompt: str, transcript_text: str) -> list[Segment]:
    result = call_structured(
        system_prompt=system_prompt,
        user_content=f"전사 스크립트 (타임스탬프 포함):\n\n{transcript_text}",
        schema=SEGMENT_SELECTION_SCHEMA,
        tool_name="report_segments",
        model=MODEL_SONNET,
        max_tokens=4096,
    )
    return [
        Segment(start_s=s["start_ts"], end_s=s["end_ts"], hook_title=s["hook_title"], confidence=s["confidence"])
        for s in result.get("segments", [])
    ]


def select_segments(transcript: TranscriptResult, channel_config: ChannelConfig) -> list[Segment]:
    """Returns [] ("no output for this cycle") if the model can't produce valid segments even
    after one stricter retry — caller must not force clips out of a bad transcript."""
    transcript_text = to_plain_transcript(transcript)
    target_count = channel_config.target_clip_count

    system_prompt = _base_system_prompt(channel_config.niche_description, target_count)
    segments = _call_and_parse(system_prompt, transcript_text)
    ok, reason = _validate(segments)

    if not ok:
        stricter_prompt = (
            system_prompt
            + f"\n\nYour previous attempt was rejected: {reason}. "
            f"Every segment MUST be strictly between {MIN_SEGMENT_S} and {MAX_SEGMENT_S} seconds and "
            "segments MUST NOT overlap. Re-check your timestamps against these constraints before answering."
        )
        segments = _call_and_parse(stricter_prompt, transcript_text)
        ok, reason = _validate(segments)

    if not ok:
        return []

    return _snap_to_word_boundaries(segments, transcript.segments)
