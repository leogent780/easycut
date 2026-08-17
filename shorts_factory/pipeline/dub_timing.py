"""Pure timing/text logic for the viral_translate_dub pipeline — silence-gap parsing, the
gapless timeline remap after cutting silences out, and splitting a sentence into short
caption chunks. Kept dependency-free (no ffmpeg/network calls) so it's cheaply unit-testable;
`pipeline/viral_translate_dub.py` does the actual subprocess/API orchestration around these.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


def parse_silence_intervals(ffmpeg_stderr: str) -> list[tuple[float, float]]:
    """Parse `silencedetect` filter output into a list of (start, end) tuples."""
    starts = [float(m) for m in re.findall(r"silence_start:\s*([\d.]+)", ffmpeg_stderr)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([\d.]+)", ffmpeg_stderr)]
    return list(zip(starts, ends))


def compute_keep_segments(
    silences: list[tuple[float, float]], total_duration: float, min_segment_s: float = 0.01
) -> list[tuple[float, float]]:
    """Invert a list of silence intervals into the "keep" (non-silent) segments spanning
    [0, total_duration]. A trailing silence that runs to `total_duration` (or a leading one
    starting at 0) is simply not included as a keep segment — no special-casing needed.
    """
    keep: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in silences:
        if start > cursor:
            keep.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < total_duration:
        keep.append((cursor, total_duration))
    return [(a, b) for a, b in keep if b - a > min_segment_s]


def map_time_through_keep_segments(t: float, keep_segments: list[tuple[float, float]]) -> float:
    """Map a timestamp `t` in the ORIGINAL timeline to its position in the gapless timeline
    produced by concatenating `keep_segments` back-to-back (silence removed). A `t` that falls
    inside a removed gap clamps to the cumulative time at that gap's start.
    """
    cum = 0.0
    for a, b in keep_segments:
        if t <= a:
            return cum
        if t <= b:
            return cum + (t - a)
        cum += b - a
    return cum


def compute_gapless_speed_mapping(
    original_timestamps: dict[str, float],
    keep_segments: list[tuple[float, float]],
    speed_factor: float,
) -> dict[str, float]:
    """Convenience wrapper: map a dict of named original-timeline timestamps through
    gap-removal, then divide by `speed_factor` to land in the final (sped-up) timeline.
    """
    return {name: map_time_through_keep_segments(t, keep_segments) / speed_factor for name, t in original_timestamps.items()}


def snap_boundary_to_silences(
    t: float, silences: list[tuple[float, float]], tolerance_s: float = 1.2
) -> float:
    """Gemini's audio-timestamp estimates for sentence boundaries are approximate (it tends
    to round). Snap `t` to the midpoint of the nearest real `silencedetect` interval if one
    exists within `tolerance_s` — real silence data is ground truth for "a pause exists here",
    Gemini's number is only used to say WHICH pause corresponds to which sentence break.
    Falls back to the unmodified estimate if no silence is close enough.
    """
    best = None
    best_dist = tolerance_s
    for start, end in silences:
        midpoint = (start + end) / 2
        dist = abs(midpoint - t)
        if dist <= best_dist:
            best = midpoint
            best_dist = dist
    return best if best is not None else t


@dataclass
class CaptionChunk:
    text: str
    start_s: float
    end_s: float


# A trailing word this short at a chunk boundary is almost always a bare counter/determiner
# (e.g. "두" in "두 배는", "닭" in "닭 뼈부터") modifying whatever comes right after it, rather
# than a complete standalone thought — splitting it onto the next caption/scene reads as a
# broken mid-word cut even though they're technically separate whitespace-tokens. Defer it to
# start the NEXT chunk instead of letting it end this one.
SHORT_TRAILING_WORD_MAX_CHARS = 2


def chunk_sentence(text: str, min_chunk_chars: int = 6, max_chunk_chars: int = 16) -> list[str]:
    """Split a Korean sentence into short caption-sized chunks (2-4 어절 each), matching the
    "짧게 짧게 한덩이씩" cadence observed in high-performing reference shorts (see
    samples/xhs_dish_brush_ko_dub_v2/README.md): greedily accumulate space-separated words
    until the running chunk reaches `min_chunk_chars`, then start a new chunk. A short leftover
    tail merges into the previous chunk rather than standing alone. A chunk never closes on a
    bare 1-2 character trailing word (see SHORT_TRAILING_WORD_MAX_CHARS) — that word is popped
    back off and deferred to open the next chunk instead, so it stays attached to whatever
    follows it rather than to whatever happened to precede it. This can make an individual
    chunk read shorter than `min_chunk_chars`; that's an acceptable trade for never breaking a
    word pair across two chunks/scenes.
    """
    words = text.split()
    if not words:
        return []
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for idx, word in enumerate(words):
        current.append(word)
        current_len += len(word) + 1
        if current_len >= min_chunk_chars:
            is_last_word = idx == len(words) - 1
            if len(word) <= SHORT_TRAILING_WORD_MAX_CHARS and not is_last_word and len(current) > 1:
                current.pop()
                current_len -= len(word) + 1
                chunks.append(" ".join(current))
                current = [word]
                current_len = len(word) + 1
            else:
                chunks.append(" ".join(current))
                current = []
                current_len = 0
    if current:
        leftover = " ".join(current)
        if chunks and current_len < min_chunk_chars:
            chunks[-1] = chunks[-1] + " " + leftover
        else:
            chunks.append(leftover)
    return chunks


def layout_caption_chunks(sentences: list[str], sentence_windows: list[tuple[float, float]]) -> list[CaptionChunk]:
    """For each (sentence, time window) pair, split the sentence into short chunks and
    allocate each chunk a slice of the window proportional to its character count.
    """
    if len(sentences) != len(sentence_windows):
        raise ValueError("sentences and sentence_windows must be the same length")

    result: list[CaptionChunk] = []
    for sentence, (w_start, w_end) in zip(sentences, sentence_windows):
        pieces = chunk_sentence(sentence)
        if not pieces:
            continue
        total_chars = sum(len(p) for p in pieces)
        duration = w_end - w_start
        cursor = w_start
        for piece in pieces:
            share = (len(piece) / total_chars) * duration if total_chars else duration / len(pieces)
            result.append(CaptionChunk(text=piece, start_s=cursor, end_s=cursor + share))
            cursor += share
    return result
