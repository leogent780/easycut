"""Word-timestamp transcript -> ASS subtitle file (karaoke-style word highlight when
word-level timing is available, plain whole-line display otherwise).

ffmpeg's `subtitles` filter burns this in — chosen over drawtext because \\k karaoke tags
and declarative styling handle the "current word highlighted" look natively (see plan).
"""

from __future__ import annotations

from pathlib import Path

from shorts_factory.integrations.whisper_client import TranscriptSegment

DEFAULT_STYLE = {
    "fontname": "Arial",
    "fontsize": 64,
    "primary_colour": "&H00FFFFFF",  # unspoken/upcoming text — white (ASS is &HAABBGGRR)
    "highlight_colour": "&H0000D7FF",  # spoken/highlighted word — gold-ish (BGR order: FF D7 00 -> gold)
    "outline_colour": "&H00000000",
    "back_colour": "&H00000000",
    "alignment": 2,  # bottom-center
    "margin_v": 140,
}


def _format_ass_time(seconds: float) -> str:
    seconds = max(seconds, 0.0)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    centiseconds = round((s - int(s)) * 100)
    if centiseconds == 100:  # rounding edge case: 59.996s -> 60.00cs, roll into the next second
        centiseconds = 0
        s += 1
    return f"{h}:{m:02d}:{int(s):02d}.{centiseconds:02d}"


def _build_header(style: dict) -> str:
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{style['fontname']},{style['fontsize']},{style['primary_colour']},"
        f"{style['highlight_colour']},{style['outline_colour']},{style['back_colour']},"
        f"-1,0,0,0,100,100,0,0,1,3,0,{style['alignment']},50,50,{style['margin_v']},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


def _karaoke_line_for_segment(segment: TranscriptSegment, clip_start_s: float) -> str | None:
    """One \\k-tagged dialogue line per transcript segment, words highlighted in sequence.
    Returns None if the segment falls entirely outside [0, clip duration] after rebasing."""
    words = segment.words
    if not words:
        return None

    rebased_words = [(w.text, w.start_s - clip_start_s, w.end_s - clip_start_s) for w in words]
    rebased_words = [w for w in rebased_words if w[2] > 0]  # drop anything ending before the clip starts
    if not rebased_words:
        return None

    line_start = max(rebased_words[0][1], 0.0)
    line_end = rebased_words[-1][2]

    k_tags = []
    for i, (text, start, _end) in enumerate(rebased_words):
        next_start = rebased_words[i + 1][1] if i + 1 < len(rebased_words) else line_end
        duration_cs = max(round((next_start - start) * 100), 1)
        k_tags.append(f"{{\\k{duration_cs}}}{text}")

    text = " ".join(k_tags)
    return f"Dialogue: 0,{_format_ass_time(line_start)},{_format_ass_time(line_end)},Default,,0,0,0,,{text}"


def generate_ass_file(
    segments: list[TranscriptSegment],
    clip_start_s: float,
    clip_end_s: float,
    output_path: str | Path,
    style: dict | None = None,
) -> Path:
    """Build an ASS caption file covering [clip_start_s, clip_end_s) of the source transcript,
    with all timestamps rebased so the clip's own start is t=0."""
    style = {**DEFAULT_STYLE, **(style or {})}
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    for seg in segments:
        if seg.end_s <= clip_start_s or seg.start_s >= clip_end_s:
            continue
        line = _karaoke_line_for_segment(seg, clip_start_s)
        if line:
            lines.append(line)

    content = _build_header(style) + "\n".join(lines) + "\n"
    output_path.write_text(content, encoding="utf-8")
    return output_path
