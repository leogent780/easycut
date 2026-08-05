"""Daily upload of rendered clips sitting in inventory (`clips.upload_status = pending_upload`)
to the channel's real YouTube channel, throttled to `daily_publish_count` per cycle and gated
on remaining API quota — see plan's batch-generate-but-publish-gradually design.
"""

from __future__ import annotations

import datetime
import time

from sqlalchemy.orm import Session

from shorts_factory.config import ChannelConfig
from shorts_factory.integrations import youtube_api
from shorts_factory.pipeline import quota
from shorts_factory.state import (
    AuditLevel,
    Channel,
    ChannelStatus,
    Clip,
    Job,
    SourceVideo,
    UploadStatus,
    get_pending_upload_clips,
    log_audit,
)

RETRY_BACKOFFS_S = [2, 4, 8]


def create_pending_clip(
    session: Session,
    job: Job,
    source_video: SourceVideo | None,
    segment_start_s: float | None,
    segment_end_s: float | None,
    hook_title: str,
    format_template_name: str,
) -> Clip:
    clip = Clip(
        job_id=job.id,
        source_video_id=source_video.id if source_video else None,
        segment_start_s=segment_start_s,
        segment_end_s=segment_end_s,
        hook_title=hook_title,
        format_template_used=format_template_name,
        upload_status=UploadStatus.PENDING.value,
    )
    session.add(clip)
    session.flush()
    return clip


def mark_clip_rendered(session: Session, clip: Clip, rendered_path: str) -> None:
    clip.rendered_path = rendered_path
    clip.upload_status = UploadStatus.PENDING_UPLOAD.value


def mark_clip_render_failed(session: Session, clip: Clip) -> None:
    clip.upload_status = UploadStatus.FAILED.value


def _is_invalid_grant(exc: Exception) -> bool:
    return "invalid_grant" in str(exc).lower()


def _is_quota_exceeded(exc: Exception) -> bool:
    text = str(exc).lower()
    return "quotaexceeded" in text.replace(" ", "") or "rate limit exceeded" in text


def upload_daily_batch(
    session: Session, channel: Channel, channel_config: ChannelConfig, job: Job | None = None
) -> list[str]:
    """Publish up to `daily_publish_count` clips from inventory. Returns the uploaded video ids.

    Anything not uploaded this call (quota exhausted, transient failures after retry) stays
    `pending_upload` and is retried on a later cycle — never re-rendered, never dropped.
    """
    pending_clips = get_pending_upload_clips(session, channel.id, limit=channel_config.daily_publish_count)
    if not pending_clips:
        return []

    try:
        service = youtube_api.get_service(channel.name)
    except Exception as exc:
        if _is_invalid_grant(exc):
            channel.status = ChannelStatus.NEEDS_REAUTH.value
            log_audit(session, job, "upload", f"OAuth refresh failed for '{channel.name}': {exc}", AuditLevel.ERROR)
        else:
            log_audit(session, job, "upload", f"could not build YouTube service: {exc}", AuditLevel.ERROR)
        return []

    uploaded_ids: list[str] = []
    for clip in pending_clips:
        if not quota.has_budget_for_upload(session, channel_config.gcp_project_ref):
            log_audit(session, job, "upload", "daily quota exhausted — remaining clips stay queued", AuditLevel.WARNING)
            break

        video_id = _upload_with_retry(session, service, clip, channel, channel_config, job)
        if video_id:
            uploaded_ids.append(video_id)
            if channel.status == ChannelStatus.NEEDS_REAUTH.value:
                break  # _upload_with_retry already flipped status — stop this channel's batch

    return uploaded_ids


def _upload_with_retry(
    session: Session, service, clip: Clip, channel: Channel, channel_config: ChannelConfig, job: Job | None
) -> str | None:
    last_error: Exception | None = None
    for attempt, backoff in enumerate([0, *RETRY_BACKOFFS_S]):
        if backoff:
            time.sleep(backoff)
        try:
            video_id = youtube_api.upload_short(
                service,
                file_path=clip.rendered_path,
                title=clip.hook_title or "쇼츠",
                description=clip.hook_title or "",
            )
            quota.spend_videos_insert(session, channel_config.gcp_project_ref)
            clip.upload_status = UploadStatus.UPLOADED.value
            clip.youtube_video_id_uploaded = video_id
            clip.uploaded_at = datetime.datetime.now(datetime.timezone.utc)
            log_audit(session, job, "upload", f"uploaded clip {clip.id} -> {video_id}")
            return video_id
        except Exception as exc:
            last_error = exc
            if _is_invalid_grant(exc):
                channel.status = ChannelStatus.NEEDS_REAUTH.value
                log_audit(session, job, "upload", f"OAuth revoked mid-batch for '{channel.name}': {exc}", AuditLevel.ERROR)
                return None  # do not retry — reauth is required, not a transient failure
            if _is_quota_exceeded(exc):
                log_audit(session, job, "upload", f"quota exceeded on upload: {exc}", AuditLevel.WARNING)
                return None  # clip stays pending_upload, retried next cycle — do not burn retries here
            log_audit(session, job, "upload", f"upload attempt {attempt + 1} failed: {exc}", AuditLevel.WARNING)

    log_audit(session, job, "upload", f"clip {clip.id} failed after retries: {last_error}", AuditLevel.ERROR)
    return None  # stays pending_upload — plan says never silently drop a rendered clip
