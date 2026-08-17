from shorts_factory.pipeline import dub_timing


def test_parse_silence_intervals():
    stderr = """
    [silencedetect] silence_start: 1.86112
    [silencedetect] silence_end: 2.18929 | silence_duration: 0.32817
    [silencedetect] silence_start: 4.97846
    [silencedetect] silence_end: 5.5585 | silence_duration: 0.58004
    """
    result = dub_timing.parse_silence_intervals(stderr)
    assert result == [(1.86112, 2.18929), (4.97846, 5.5585)]


def test_compute_keep_segments_basic():
    silences = [(0.0, 0.5), (2.0, 2.5), (5.0, 5.3)]
    keep = dub_timing.compute_keep_segments(silences, total_duration=6.0)
    assert keep == [(0.5, 2.0), (2.5, 5.0), (5.3, 6.0)]


def test_compute_keep_segments_drops_tiny_fragments():
    silences = [(0.0, 1.0), (1.005, 2.0)]  # leaves a 0.005s sliver between them
    keep = dub_timing.compute_keep_segments(silences, total_duration=3.0, min_segment_s=0.01)
    assert keep == [(2.0, 3.0)]


def test_map_time_through_keep_segments_before_first_gap():
    keep = [(0.5, 2.0), (2.5, 5.0)]
    assert dub_timing.map_time_through_keep_segments(0.5, keep) == 0.0


def test_map_time_through_keep_segments_inside_segment():
    keep = [(0.5, 2.0), (2.5, 5.0)]
    # 1.0 is 0.5s into the first kept segment -> maps to 0.5
    assert dub_timing.map_time_through_keep_segments(1.0, keep) == 0.5


def test_map_time_through_keep_segments_inside_removed_gap_clamps():
    keep = [(0.5, 2.0), (2.5, 5.0)]
    # 2.2 falls inside the removed gap (2.0-2.5) -> clamps to cumulative time at gap start
    assert dub_timing.map_time_through_keep_segments(2.2, keep) == 1.5


def test_map_time_through_keep_segments_after_last_segment():
    keep = [(0.5, 2.0), (2.5, 5.0)]
    total_kept = (2.0 - 0.5) + (5.0 - 2.5)
    assert dub_timing.map_time_through_keep_segments(10.0, keep) == total_kept


def test_compute_gapless_speed_mapping():
    keep = [(0.0, 2.0), (2.5, 5.0)]  # 2.0 + 2.5 = 4.5s kept
    original = {"a": 0.0, "b": 2.0, "c": 4.0}
    mapped = dub_timing.compute_gapless_speed_mapping(original, keep, speed_factor=1.5)
    # gapless(a)=0.0, gapless(b)=2.0, gapless(c)=2.0+(4.0-2.5)=3.5
    assert mapped["a"] == 0.0
    assert mapped["b"] == 2.0 / 1.5
    assert mapped["c"] == 3.5 / 1.5


def test_snap_boundary_to_silences_within_tolerance():
    silences = [(10.0, 10.8)]  # midpoint 10.4
    assert dub_timing.snap_boundary_to_silences(10.5, silences, tolerance_s=1.0) == 10.4


def test_snap_boundary_to_silences_out_of_tolerance_returns_original():
    silences = [(10.0, 10.8)]
    assert dub_timing.snap_boundary_to_silences(50.0, silences, tolerance_s=1.0) == 50.0


def test_chunk_sentence_splits_into_short_pieces():
    chunks = dub_timing.chunk_sentence("이거 디자인 진짜 대박이죠 세제가 주르륵 나오는 회전식 만능 브러시예요")
    assert len(chunks) > 1
    assert all(len(c) <= 20 for c in chunks)  # no single chunk should be a whole long sentence
    assert " ".join(chunks) == "이거 디자인 진짜 대박이죠 세제가 주르륵 나오는 회전식 만능 브러시예요"


def test_chunk_sentence_merges_short_leftover_tail():
    # last word alone would be under min_chunk_chars — should merge into previous chunk
    chunks = dub_timing.chunk_sentence("가나다라 마바사", min_chunk_chars=6)
    assert chunks == ["가나다라 마바사"]


def test_chunk_sentence_does_not_strand_short_trailing_word():
    # "두" (a bare counter/determiner) must never be left dangling at the end of a chunk with
    # "배는" pushed into the next one — reported bug: captions showed "속도가 두" / "배는 빨라진다고"
    chunks = dub_timing.chunk_sentence("이거 하나면 주방 작업 속도가 두 배는 빨라진다고")
    assert " ".join(chunks) == "이거 하나면 주방 작업 속도가 두 배는 빨라진다고"
    for chunk in chunks:
        words = chunk.split()
        assert not (len(words[-1]) <= dub_timing.SHORT_TRAILING_WORD_MAX_CHARS and chunk != chunks[-1])


def test_layout_caption_chunks_allocates_proportional_time():
    sentences = ["가나다라 마바사아자차"]  # one sentence, will split into 2 chunks by length
    windows = [(0.0, 10.0)]
    chunks = dub_timing.layout_caption_chunks(sentences, windows)
    assert len(chunks) >= 1
    assert chunks[0].start_s == 0.0
    assert chunks[-1].end_s == 10.0
    # chunks should be contiguous (no gaps/overlaps)
    for a, b in zip(chunks, chunks[1:]):
        assert a.end_s == b.start_s


def test_layout_caption_chunks_requires_matching_lengths():
    import pytest

    with pytest.raises(ValueError):
        dub_timing.layout_caption_chunks(["a", "b"], [(0.0, 1.0)])
