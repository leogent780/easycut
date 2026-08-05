from shorts_factory.state import (
    Channel,
    JobStatus,
    create_job,
    finish_job,
    get_channel_by_name,
    get_quota_used_today,
    is_source_already_processed,
    log_audit,
    record_quota_usage,
)
from shorts_factory.state import SourceVideo


def test_channel_roundtrip(db_session):
    ch = Channel(
        name="ch1", source_strategy="longform_highlight_cut", format_template="simple_hook_top",
        gcp_project_ref="proj1",
    )
    db_session.add(ch)
    db_session.commit()

    fetched = get_channel_by_name(db_session, "ch1")
    assert fetched is not None
    assert fetched.status == "active"


def test_job_lifecycle(db_session):
    ch = Channel(name="ch1", source_strategy="s", format_template="f", gcp_project_ref="p")
    db_session.add(ch)
    db_session.commit()

    job = create_job(db_session, ch)
    db_session.commit()
    assert job.status == JobStatus.RUNNING.value

    finish_job(db_session, job, JobStatus.COMPLETED)
    db_session.commit()
    assert job.status == JobStatus.COMPLETED.value
    assert job.finished_at is not None


def test_quota_accumulates_per_project_per_day(db_session):
    assert get_quota_used_today(db_session, "proj1") == 0
    record_quota_usage(db_session, "proj1", 100)
    record_quota_usage(db_session, "proj1", 250)
    db_session.commit()
    assert get_quota_used_today(db_session, "proj1") == 350
    # a different project's quota is tracked independently
    assert get_quota_used_today(db_session, "proj2") == 0


def test_source_video_dedupe(db_session):
    ch = Channel(name="ch1", source_strategy="s", format_template="f", gcp_project_ref="p")
    db_session.add(ch)
    db_session.commit()

    assert is_source_already_processed(db_session, "yt123") is False
    db_session.add(SourceVideo(source_id="yt123", channel_id=ch.id))
    db_session.commit()
    assert is_source_already_processed(db_session, "yt123") is True


def test_audit_log_without_job(db_session):
    log_audit(db_session, None, "discover", "test message")
    db_session.commit()  # should not raise even with job=None
