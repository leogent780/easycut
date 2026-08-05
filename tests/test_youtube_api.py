import datetime

from shorts_factory.integrations.youtube_api import VideoCandidate, parse_iso8601_duration


def test_parse_iso8601_duration_various():
    assert parse_iso8601_duration("PT15M33S") == 933
    assert parse_iso8601_duration("PT1H2M3S") == 3723
    assert parse_iso8601_duration("PT45S") == 45
    assert parse_iso8601_duration("PT0S") == 0
    assert parse_iso8601_duration("garbage") == 0


def test_velocity_score_favors_recent_fast_growth():
    now = datetime.datetime.now(datetime.timezone.utc)

    recent_fast = VideoCandidate(
        video_id="a", title="t", description="d", channel_title="c",
        published_at=now - datetime.timedelta(hours=12), view_count=500_000,
    )
    old_evergreen = VideoCandidate(
        video_id="b", title="t", description="d", channel_title="c",
        published_at=now - datetime.timedelta(days=730), view_count=50_000_000,
    )

    # cumulative-view ranking would put old_evergreen first; velocity ranking must not.
    assert recent_fast.velocity_score > old_evergreen.velocity_score


def test_velocity_score_same_day_upload_does_not_divide_by_zero():
    now = datetime.datetime.now(datetime.timezone.utc)
    brand_new = VideoCandidate(
        video_id="a", title="t", description="d", channel_title="c",
        published_at=now, view_count=1000,
    )
    assert brand_new.velocity_score > 0  # must not raise ZeroDivisionError or return inf
