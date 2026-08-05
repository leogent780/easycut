"""Strategy 1 orchestration: long-form YouTube video -> discover -> download -> transcribe ->
select segments -> render each -> queue for upload -> publish today's allotment from inventory.

This is Phase 1's target pipeline — the riskiest one (reframing quality, segment-selection
quality, OAuth unattended refresh, whisper throughput, quota math all get proven here first;
see plan §단계별 빌드 순서).
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy.orm import Session

from shorts_factory.config import ChannelConfig, load_format_template, load_layout_config
from shorts_factory.integrations import ytdlp_client
from shorts_factory.pipeline import discover, highlight_select, quota, render, transcribe, upload_youtube
from shorts_factory.pipeline.base import CycleResult
from shorts_factory.pipeline.overlay_render import OverlayContent, render_overlay_png
from shorts_factory.state import (
    AuditLevel,
    Channel,
    JobStatus,
    SourceVideo,
    SourceVideoStatus,
    create_job,
    finish_job,
    log_audit,
)


def _data_dir() -> Path:
    return Path(os.environ.get("SHORTS_FACTORY_DATA_DIR", "./data"))


def run(session: Session, channel: Channel, channel_config: ChannelConfig) -> CycleResult:
    job = create_job(session, channel)
    session.commit()

    format_template = load_format_template(channel_config.format_template)
    layout_config = (
        load_layout_config(channel_config.layout_config_ref) if channel_config.layout_config_ref else None
    )

    candidates = discover.discover_candidates(session, channel, channel_config, job=job)
    if not candidates:
        finish_job(session, job, JobStatus.FAILED, error="no discovery candidates")
        session.commit()
        return CycleResult(status="failed", message="no discovery candidates")

    scratch_dir = _data_dir() / "scratch" / str(job.id)
    output_dir = _data_dir() / "output" / channel.name / str(job.id)

    source_video: SourceVideo | None = None
    transcript_result = None
    segments = []

    for candidate in candidates:
        video = candidate.video
        try:
            local_path = ytdlp_client.download_video(video.video_id, scratch_dir)
        except Exception as exc:
            log_audit(session, job, "discover", f"download failed for {video.video_id}: {exc}", AuditLevel.WARNING)
            continue  # try next candidate — do not retry the same one (plan §실패 처리 원칙)

        transcript_result = transcribe.get_transcript(video.video_id, str(local_path), duration_s=video.duration_s)
        if transcript_result is None:
            log_audit(session, job, "transcribe", f"no usable transcript for {video.video_id}", AuditLevel.WARNING)
            continue

        segments = highlight_select.select_segments(transcript_result, channel_config)
        if not segments:
            log_audit(session, job, "highlight_select", f"no valid segments for {video.video_id}", AuditLevel.WARNING)
            continue

        source_video = SourceVideo(
            source_id=video.video_id,
            channel_id=channel.id,
            duration_s=video.duration_s,
            title=video.title,
            downloaded_path=str(local_path),
            processed_status=SourceVideoStatus.SEGMENTS_SELECTED.value,
        )
        session.add(source_video)
        session.flush()
        break  # found a candidate that worked end-to-end through segment selection

    if source_video is None or not segments:
        finish_job(session, job, JobStatus.FAILED, error="no candidate produced usable segments")
        session.commit()
        return CycleResult(status="no_output", message="all candidates failed discover/transcribe/select")

    clips_rendered = 0
    for segment in segments[: channel_config.target_clip_count]:
        clip = upload_youtube.create_pending_clip(
            session, job, source_video, segment.start_s, segment.end_s, segment.hook_title, format_template.name
        )
        session.flush()
        try:
            ass_path = scratch_dir / f"clip_{clip.id}.ass"
            png_path = scratch_dir / f"clip_{clip.id}_overlay.png"
            output_path = output_dir / f"clip_{clip.id}.mp4"

            from shorts_factory.pipeline.captions import generate_ass_file

            generate_ass_file(transcript_result.segments, segment.start_s, segment.end_s, ass_path)
            render_overlay_png(format_template, OverlayContent(hook_title_lines=[segment.hook_title]), png_path)
            render.render_clip(
                source_video_path=source_video.downloaded_path,
                segment=segment,
                ass_caption_path=ass_path,
                overlay_png_path=png_path,
                output_path=output_path,
                layout_config=layout_config,
            )
            upload_youtube.mark_clip_rendered(session, clip, str(output_path))
            clips_rendered += 1
        except Exception as exc:
            # Isolated per clip — one bad render must not abort the rest of the batch (plan §실패 처리 원칙).
            upload_youtube.mark_clip_render_failed(session, clip)
            log_audit(session, job, "render", f"clip {clip.id} render failed: {exc}", AuditLevel.ERROR)
        session.commit()

    uploaded_ids = upload_youtube.upload_daily_batch(session, channel, channel_config, job=job)

    finish_job(session, job, JobStatus.COMPLETED)
    session.commit()
    return CycleResult(
        status="completed",
        clips_rendered=clips_rendered,
        clips_uploaded=len(uploaded_ids),
        message=f"source={source_video.source_id} rendered={clips_rendered} uploaded={len(uploaded_ids)}",
    )
