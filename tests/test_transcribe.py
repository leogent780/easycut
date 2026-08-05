from shorts_factory.integrations.ytdlp_client import CaptionCue
from shorts_factory.pipeline.transcribe import MIN_CAPTION_DENSITY_CHARS_PER_MIN, _caption_density, _captions_to_segments


def test_captions_to_segments_preserves_timing_and_text():
    cues = [CaptionCue(0.0, 2.0, "안녕하세요"), CaptionCue(2.0, 5.0, "반갑습니다")]
    segments = _captions_to_segments(cues)
    assert len(segments) == 2
    assert segments[0].start_s == 0.0 and segments[0].end_s == 2.0
    assert segments[0].words[0].text == "안녕하세요"
    assert segments[0].words[0].start_s == 0.0
    assert segments[0].words[0].end_s == 2.0  # no true word-level timing — spans whole cue


def test_caption_density_dense_transcript_passes_threshold():
    cues = [CaptionCue(0.0, 10.0, "가" * 100)]
    density = _caption_density(cues, duration_s=10.0)
    assert density >= MIN_CAPTION_DENSITY_CHARS_PER_MIN


def test_caption_density_sparse_transcript_fails_threshold():
    # A single short word over a full minute of gaming-VOD noise — should read as too sparse.
    cues = [CaptionCue(0.0, 60.0, "어")]
    density = _caption_density(cues, duration_s=60.0)
    assert density < MIN_CAPTION_DENSITY_CHARS_PER_MIN


def test_caption_density_zero_duration_is_zero_not_error():
    assert _caption_density([CaptionCue(0.0, 1.0, "x")], duration_s=0.0) == 0.0
