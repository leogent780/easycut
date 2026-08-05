from shorts_factory.pipeline.discover import MAX_KEYWORDS_PER_CYCLE, _pick_keywords_for_cycle, _video_duration_bucket


def test_pick_keywords_rotates_across_cycles():
    keywords = ["a", "b", "c", "d"]
    day0 = _pick_keywords_for_cycle(keywords, cycle_seed=0)
    day1 = _pick_keywords_for_cycle(keywords, cycle_seed=1)
    assert day0 == ["a", "b"]
    assert day1 == ["b", "c"]
    assert day0 != day1  # different keyword subset each cycle, not the same top-N every time


def test_pick_keywords_returns_all_when_fewer_than_cap():
    keywords = ["a"]
    assert _pick_keywords_for_cycle(keywords, cycle_seed=5) == ["a"]


def test_pick_keywords_respects_cap_size():
    keywords = ["a", "b", "c", "d", "e"]
    result = _pick_keywords_for_cycle(keywords, cycle_seed=0)
    assert len(result) == MAX_KEYWORDS_PER_CYCLE


def test_video_duration_bucket_long_for_20min_plus():
    assert _video_duration_bucket(1200) == "long"
    assert _video_duration_bucket(3600) == "long"


def test_video_duration_bucket_none_for_short_min_duration():
    assert _video_duration_bucket(180) is None
    assert _video_duration_bucket(0) is None
