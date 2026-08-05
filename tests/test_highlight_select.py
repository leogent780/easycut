from shorts_factory.integrations.whisper_client import TranscriptSegment, Word
from shorts_factory.pipeline.highlight_select import MAX_SEGMENT_S, MIN_SEGMENT_S, Segment, _snap_to_word_boundaries, _validate


def test_validate_accepts_well_formed_nonoverlapping_segments():
    segments = [Segment(10, 45, "hook1", 0.9), Segment(50, 90, "hook2", 0.8)]
    ok, reason = _validate(segments)
    assert ok is True
    assert reason == ""


def test_validate_rejects_empty_list():
    ok, reason = _validate([])
    assert ok is False


def test_validate_rejects_too_short_segment():
    ok, reason = _validate([Segment(10, 20, "hook", 0.5)])  # 10s, below MIN_SEGMENT_S
    assert ok is False
    assert "duration" in reason


def test_validate_rejects_too_long_segment():
    ok, reason = _validate([Segment(0, MAX_SEGMENT_S + 30, "hook", 0.5)])
    assert ok is False


def test_validate_rejects_overlapping_segments():
    ok, reason = _validate([Segment(10, 45, "hook1", 0.9), Segment(40, 80, "hook2", 0.8)])
    assert ok is False
    assert "overlap" in reason


def test_snap_to_word_boundaries_nudges_onto_nearest_word():
    words = [Word("a", 9.8, 10.2), Word("b", 44.9, 45.3)]
    transcript_segments = [TranscriptSegment("a b", 9.8, 45.3, words)]

    snapped = _snap_to_word_boundaries([Segment(10.05, 45.1, "h", 0.9)], transcript_segments)
    assert snapped[0].start_s == 9.8
    assert snapped[0].end_s == 45.3


def test_snap_to_word_boundaries_noop_when_no_words():
    segments = [Segment(10, 45, "h", 0.9)]
    assert _snap_to_word_boundaries(segments, []) == segments
