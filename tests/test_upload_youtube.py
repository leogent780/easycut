from unittest.mock import patch

from shorts_factory.config import ChannelConfig, SourceStrategy
from shorts_factory.pipeline import quota
from shorts_factory.pipeline.upload_youtube import (
    _is_invalid_grant,
    _is_quota_exceeded,
    create_pending_clip,
    mark_clip_render_failed,
    mark_clip_rendered,
    upload_daily_batch,
)
from shorts_factory.state import Channel, ChannelStatus, UploadStatus, create_job


def test_is_invalid_grant_detects_oauth_revocation():
    assert _is_invalid_grant(Exception("400 Bad Request: invalid_grant")) is True
    assert _is_invalid_grant(Exception("some other error")) is False


def test_is_quota_exceeded_detects_quota_errors():
    assert _is_quota_exceeded(Exception("403 quotaExceeded: Daily Limit Exceeded")) is True
    assert _is_quota_exceeded(Exception("Rate Limit Exceeded")) is True
    assert _is_quota_exceeded(Exception("not related")) is False


def _make_channel(db_session, gcp_project_ref="proj-x") -> Channel:
    ch = Channel(
        name="test_ch", source_strategy="longform_highlight_cut", format_template="simple_hook_top",
        gcp_project_ref=gcp_project_ref,
    )
    db_session.add(ch)
    db_session.commit()
    return ch


def _make_config(daily_publish_count=1, gcp_project_ref="proj-x") -> ChannelConfig:
    return ChannelConfig(
        name="test_ch", source_strategy=SourceStrategy.LONGFORM_HIGHLIGHT_CUT,
        format_template="simple_hook_top", niche_description="test", search_keywords=["x"],
        gcp_project_ref=gcp_project_ref, daily_publish_count=daily_publish_count,
    )


def test_clip_state_machine(db_session):
    ch = _make_channel(db_session)
    job = create_job(db_session, ch)
    db_session.commit()

    clip = create_pending_clip(db_session, job, None, 10.0, 40.0, "hook1", "simple_hook_top")
    db_session.commit()
    assert clip.upload_status == UploadStatus.PENDING.value

    mark_clip_rendered(db_session, clip, "/tmp/clip1.mp4")
    assert clip.upload_status == UploadStatus.PENDING_UPLOAD.value
    assert clip.rendered_path == "/tmp/clip1.mp4"


def test_mark_clip_render_failed(db_session):
    ch = _make_channel(db_session)
    job = create_job(db_session, ch)
    db_session.commit()
    clip = create_pending_clip(db_session, job, None, 10.0, 40.0, "hook1", "simple_hook_top")
    db_session.commit()

    mark_clip_render_failed(db_session, clip)
    assert clip.upload_status == UploadStatus.FAILED.value


def test_daily_batch_respects_publish_count_and_leaves_rest_queued(db_session):
    ch = _make_channel(db_session)
    cfg = _make_config(daily_publish_count=1)
    job = create_job(db_session, ch)
    db_session.commit()

    c1 = create_pending_clip(db_session, job, None, 10.0, 40.0, "hook1", "simple_hook_top")
    c2 = create_pending_clip(db_session, job, None, 50.0, 80.0, "hook2", "simple_hook_top")
    mark_clip_rendered(db_session, c1, "/tmp/clip1.mp4")
    mark_clip_rendered(db_session, c2, "/tmp/clip2.mp4")
    db_session.commit()

    with patch("shorts_factory.integrations.youtube_api.get_service", return_value=object()), \
         patch("shorts_factory.integrations.youtube_api.upload_short", return_value="fake_id"):
        uploaded = upload_daily_batch(db_session, ch, cfg, job)
    db_session.commit()

    assert uploaded == ["fake_id"]
    assert c1.upload_status == UploadStatus.UPLOADED.value
    assert c2.upload_status == UploadStatus.PENDING_UPLOAD.value  # still queued for tomorrow
    assert quota.remaining_quota(db_session, "proj-x") == 10_000 - 1600


def test_daily_batch_stops_when_quota_exhausted(db_session):
    ch = _make_channel(db_session)
    cfg = _make_config(daily_publish_count=5)
    job = create_job(db_session, ch)
    db_session.commit()

    c1 = create_pending_clip(db_session, job, None, 10.0, 40.0, "hook1", "simple_hook_top")
    mark_clip_rendered(db_session, c1, "/tmp/clip1.mp4")
    db_session.commit()

    quota.spend(db_session, "proj-x", 9_000)  # leave only 1000 units — not enough for one 1600-unit upload
    db_session.commit()

    with patch("shorts_factory.integrations.youtube_api.get_service", return_value=object()), \
         patch("shorts_factory.integrations.youtube_api.upload_short", return_value="fake_id"):
        uploaded = upload_daily_batch(db_session, ch, cfg, job)
    db_session.commit()

    assert uploaded == []
    assert c1.upload_status == UploadStatus.PENDING_UPLOAD.value  # untouched, retried next cycle


def test_daily_batch_marks_needs_reauth_on_invalid_grant(db_session):
    ch = _make_channel(db_session)
    cfg = _make_config()
    job = create_job(db_session, ch)
    db_session.commit()

    # needs at least one pending_upload clip, otherwise upload_daily_batch short-circuits
    # before ever calling get_service (nothing to upload -> nothing to authenticate for).
    clip = create_pending_clip(db_session, job, None, 10.0, 40.0, "hook1", "simple_hook_top")
    mark_clip_rendered(db_session, clip, "/tmp/clip1.mp4")
    db_session.commit()

    with patch("shorts_factory.integrations.youtube_api.get_service", side_effect=Exception("invalid_grant")):
        uploaded = upload_daily_batch(db_session, ch, cfg, job)
    db_session.commit()

    assert uploaded == []
    assert ch.status == ChannelStatus.NEEDS_REAUTH.value
