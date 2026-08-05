from shorts_factory.integrations.whisper_client import TranscriptSegment, Word
from shorts_factory.pipeline.captions import _format_ass_time, _karaoke_line_for_segment, generate_ass_file


def test_format_ass_time_basic():
    assert _format_ass_time(0.0) == "0:00:00.00"
    assert _format_ass_time(65.5) == "0:01:05.50"
    assert _format_ass_time(3725.996) == "1:02:06.00"  # rounds up cleanly across the second boundary


def test_karaoke_line_rebases_time_to_clip_start():
    words = [Word("안녕", 12.0, 12.4), Word("하세요", 12.4, 12.9)]
    segment = TranscriptSegment("안녕 하세요", 12.0, 12.9, words)

    line = _karaoke_line_for_segment(segment, clip_start_s=10.0)
    assert line is not None
    assert "0:00:02.00" in line  # 12.0 - 10.0 = 2.0s, rebased
    assert "\\k40" in line  # (12.4-12.0)*100 = 40 centiseconds
    assert "안녕" in line and "하세요" in line


def test_karaoke_line_returns_none_for_segment_entirely_before_clip():
    words = [Word("과거", 1.0, 1.5)]
    segment = TranscriptSegment("과거", 1.0, 1.5, words)
    assert _karaoke_line_for_segment(segment, clip_start_s=10.0) is None


def test_generate_ass_file_filters_segments_outside_clip_window(tmp_path):
    words_in = [Word("in", 12.0, 12.5)]
    words_out = [Word("out", 100.0, 100.5)]
    segments = [
        TranscriptSegment("in", 12.0, 12.5, words_in),
        TranscriptSegment("out", 100.0, 100.5, words_out),
    ]
    output = generate_ass_file(segments, clip_start_s=10.0, clip_end_s=40.0, output_path=tmp_path / "test.ass")
    content = output.read_text()
    assert "in" in content
    assert "out" not in content
    assert "[V4+ Styles]" in content
    assert "[Events]" in content
