"""Local web dashboard (Phase 3): channel list with pause/resume/run-now, job/clip history,
and a manual-upload override — the GUI control surface promised in the plan for a user who
doesn't want to operate this via CLI commands.

Single-user, local-only tool: runs via `shorts-factory dashboard` and is meant to be opened
at http://127.0.0.1:8000 in a browser on the same machine. In-process state (the `_running`
set) is fine at this scale — there is exactly one server process, no multi-worker deployment.
"""

from __future__ import annotations

import datetime
import os
import shutil
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shorts_factory import config as config_module
from shorts_factory.integrations import youtube_api
from shorts_factory.pipeline.base import run_cycle
from shorts_factory.state import (
    AuditLevel,
    Channel,
    ChannelStatus,
    Clip,
    Job,
    JobStatus,
    UploadStatus,
    create_job,
    finish_job,
    get_channel_by_name,
    get_engine,
    log_audit,
)

BASE_DIR = Path(__file__).parent
app = FastAPI(title="쇼츠 팩토리")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

_running_channels: set[str] = set()


def _data_dir() -> Path:
    return Path(os.environ.get("SHORTS_FACTORY_DATA_DIR", "./data"))


def _db_path() -> Path:
    return _data_dir() / "shorts_factory.db"


def _get_session() -> Session:
    return Session(get_engine(_db_path()))


def _get_or_create_channel(session: Session, name: str, channel_config) -> Channel:
    channel = get_channel_by_name(session, name)
    if channel is None:
        channel = Channel(
            name=name,
            source_strategy=channel_config.source_strategy.value,
            format_template=channel_config.format_template,
            niche_keywords=channel_config.search_keywords,
            layout_config_ref=channel_config.layout_config_ref,
            gcp_project_ref=channel_config.gcp_project_ref,
        )
        session.add(channel)
        session.commit()
    return channel


STATUS_LABELS = {
    ChannelStatus.ACTIVE.value: ("가동 중", "ok"),
    ChannelStatus.PAUSED.value: ("일시정지", "muted"),
    ChannelStatus.NEEDS_REAUTH.value: ("재인증 필요", "danger"),
}
UPLOAD_STATUS_LABELS = {
    UploadStatus.PENDING.value: ("제작 중", "muted"),
    UploadStatus.PENDING_UPLOAD.value: ("업로드 대기(재고)", "info"),
    UploadStatus.UPLOADED.value: ("업로드 완료", "ok"),
    UploadStatus.FAILED.value: ("실패", "danger"),
}


@app.get("/")
def index(request: Request):
    with _get_session() as session:
        rows = []
        for name in config_module.list_channel_names():
            cfg = config_module.load_channel_config(name)
            channel = _get_or_create_channel(session, name, cfg)
            pending_count = (
                session.scalar(
                    select(func.count(Clip.id))
                    .join(Job)
                    .where(Job.channel_id == channel.id, Clip.upload_status == UploadStatus.PENDING_UPLOAD.value)
                )
                or 0
            )
            label, tone = STATUS_LABELS.get(channel.status, (channel.status, "muted"))
            rows.append(
                {
                    "name": channel.name,
                    "status_label": label,
                    "status_tone": tone,
                    "status_raw": channel.status,
                    "strategy": channel.source_strategy,
                    "niche": cfg.niche_description.strip(),
                    "last_run_at": channel.last_run_at,
                    "pending_count": pending_count,
                    "is_running": name in _running_channels,
                }
            )
    return templates.TemplateResponse(request, "index.html", {"channels": rows})


@app.post("/channels/{name}/pause")
def pause_channel(name: str):
    with _get_session() as session:
        channel = get_channel_by_name(session, name)
        if channel:
            channel.status = ChannelStatus.PAUSED.value
            session.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/channels/{name}/resume")
def resume_channel(name: str):
    with _get_session() as session:
        channel = get_channel_by_name(session, name)
        if channel:
            channel.status = ChannelStatus.ACTIVE.value
            session.commit()
    return RedirectResponse("/", status_code=303)


def _run_cycle_in_background(name: str) -> None:
    try:
        cfg = config_module.load_channel_config(name)
        with _get_session() as session:
            channel = _get_or_create_channel(session, name, cfg)
            run_cycle(session, channel, cfg)
    except Exception as exc:  # a bad cycle must not crash the server process
        with _get_session() as session:
            channel = get_channel_by_name(session, name)
            log_audit(session, None, "dashboard", f"run-now failed for '{name}': {exc}", AuditLevel.ERROR)
            session.commit()
    finally:
        _running_channels.discard(name)


@app.post("/channels/{name}/run-now")
def run_now(name: str):
    if name not in _running_channels:
        _running_channels.add(name)
        threading.Thread(target=_run_cycle_in_background, args=(name,), daemon=True).start()
    return RedirectResponse("/", status_code=303)


def _run_dub_reference_in_background(name: str, job_id: int, reference_path: Path, tone_hint: str | None) -> None:
    from shorts_factory.integrations import tts_client
    from shorts_factory.pipeline import dub_caption_render, viral_translate_dub

    with _get_session() as session:
        job = session.get(Job, job_id)
        channel = get_channel_by_name(session, name)
        try:
            font_cache_dir = _data_dir() / "cache"
            pretendard_path = dub_caption_render.ensure_pretendard_font(font_cache_dir)
            output_path = _data_dir() / "scratch" / "dub_reference" / str(job_id) / "final.mp4"
            tts_provider = os.environ.get("TTS_PROVIDER") or tts_client.DEFAULT_PROVIDER

            result = viral_translate_dub.run_manual(
                reference_video_path=reference_path,
                pretendard_font_path=pretendard_path,
                output_path=output_path,
                tone_hint=tone_hint,
                tts_provider=tts_provider,
            )

            hook_title_text = f"{result.hook_title.get('line1', '')} {result.hook_title.get('line2', '')}".strip()
            clip = Clip(
                job_id=job.id,
                hook_title=hook_title_text or None,
                format_template_used=channel.format_template if channel else None,
                rendered_path=str(result.output_path),
                upload_status=UploadStatus.PENDING_UPLOAD.value,
            )
            session.add(clip)
            log_audit(
                session, job, "dub_reference",
                f"레퍼런스 더빙 완성 ({result.final_duration_s:.1f}초, crop={result.crop_applied_px}px)"
                + (f" — {'; '.join(result.warnings)}" if result.warnings else ""),
            )
            finish_job(session, job, JobStatus.COMPLETED)
        except Exception as exc:
            log_audit(session, job, "dub_reference", f"레퍼런스 더빙 실패: {exc}", AuditLevel.ERROR)
            finish_job(session, job, JobStatus.FAILED, error=str(exc))
        finally:
            reference_path.unlink(missing_ok=True)
        session.commit()
        _running_channels.discard(name)


@app.post("/channels/{name}/dub-reference")
async def dub_reference(name: str, tone_hint: str = Form(""), reference_video: UploadFile = None):
    """Strategy 2 (viral_translate_dub): the user drops in ONE reference video and gets back
    a Korean-dubbed short built the same way the manual samples in samples/ were — crop
    detection, Gemini-authored script, TTS, gap-removal + speed-up, and reference-style
    captions. Runs in the background (this can take a couple of minutes); the finished clip
    shows up in 클립 이력 with 업로드 대기(재고) status, same as an automatic cycle's output —
    from there it goes out via the normal daily upload flow, or the user can also just watch
    for it and push it live sooner via manual review.
    """
    if name in _running_channels:
        return RedirectResponse(f"/channels/{name}", status_code=303)

    with _get_session() as session:
        channel = get_channel_by_name(session, name)
        job = create_job(session, channel)
        job_id = job.id
        session.commit()

    scratch_dir = _data_dir() / "scratch" / "dub_reference" / str(job_id)
    scratch_dir.mkdir(parents=True, exist_ok=True)
    reference_path = scratch_dir / (reference_video.filename or f"{uuid.uuid4()}.mp4")
    with reference_path.open("wb") as f:
        shutil.copyfileobj(reference_video.file, f)

    _running_channels.add(name)
    threading.Thread(
        target=_run_dub_reference_in_background,
        args=(name, job_id, reference_path, tone_hint.strip() or None),
        daemon=True,
    ).start()
    return RedirectResponse(f"/channels/{name}", status_code=303)


@app.get("/channels/{name}")
def channel_detail(request: Request, name: str):
    with _get_session() as session:
        channel = get_channel_by_name(session, name)
        if channel is None:
            cfg = config_module.load_channel_config(name)
            channel = _get_or_create_channel(session, name, cfg)

        jobs = list(
            session.scalars(select(Job).where(Job.channel_id == channel.id).order_by(Job.id.desc()).limit(20))
        )
        job_ids = [j.id for j in jobs]
        clips = (
            list(session.scalars(select(Clip).where(Clip.job_id.in_(job_ids)).order_by(Clip.id.desc())))
            if job_ids
            else []
        )
        clip_rows = [
            {
                "id": c.id,
                "hook_title": c.hook_title,
                "status_label": UPLOAD_STATUS_LABELS.get(c.upload_status, (c.upload_status, "muted"))[0],
                "status_tone": UPLOAD_STATUS_LABELS.get(c.upload_status, (c.upload_status, "muted"))[1],
                "youtube_url": (
                    f"https://youtube.com/shorts/{c.youtube_video_id_uploaded}"
                    if c.youtube_video_id_uploaded
                    else None
                ),
                "uploaded_at": c.uploaded_at,
            }
            for c in clips
        ]
        label, tone = STATUS_LABELS.get(channel.status, (channel.status, "muted"))
        channel_row = {
            "name": channel.name,
            "status_label": label,
            "status_tone": tone,
            "source_strategy": channel.source_strategy,
        }
        current_format = channel.format_template

    format_options = [
        {
            "key": key,
            "label": info["label"],
            "description": info["description"],
            "preview_url": f"/static/format_previews/{key}.jpg",
            "selected": key == current_format,
        }
        for key, info in config_module.FORMAT_TEMPLATE_INFO.items()
        if key in config_module.list_format_template_names()
    ]

    return templates.TemplateResponse(
        request,
        "channel_detail.html",
        {"channel": channel_row, "jobs": jobs, "clips": clip_rows, "format_options": format_options},
    )


@app.post("/channels/{name}/format")
def set_format(name: str, format_template: str = Form(...)):
    """Persist the user's chosen '숏폼 디자인' (format template) for this channel. Writes
    through to the channel's YAML config (the pipeline's actual source of truth each cycle —
    see longform_highlight_cut.run) and mirrors it into the DB row so the dashboard reflects
    the change immediately without waiting for the next run to re-register the channel."""
    if format_template not in config_module.list_format_template_names():
        return RedirectResponse(f"/channels/{name}", status_code=303)

    config_module.update_channel_format_template(name, format_template)

    with _get_session() as session:
        channel = get_channel_by_name(session, name)
        if channel:
            channel.format_template = format_template
            session.commit()
    return RedirectResponse(f"/channels/{name}", status_code=303)


@app.post("/channels/{name}/manual-upload")
async def manual_upload(name: str, title: str = Form(...), video: UploadFile = None):
    """The user's own produced video, uploaded straight to YouTube — bypasses the whole
    discover/render pipeline entirely (see plan: pause + manual override is a first-class flow)."""
    with _get_session() as session:
        channel = get_channel_by_name(session, name)
        job = create_job(session, channel)
        session.commit()

        try:
            scratch_dir = _data_dir() / "scratch" / "manual" / str(job.id)
            scratch_dir.mkdir(parents=True, exist_ok=True)
            dest_path = scratch_dir / (video.filename or f"{uuid.uuid4()}.mp4")
            with dest_path.open("wb") as f:
                shutil.copyfileobj(video.file, f)

            service = youtube_api.get_service(channel.name)
            video_id = youtube_api.upload_short(service, str(dest_path), title=title, description=title)

            clip = Clip(
                job_id=job.id,
                hook_title=title,
                rendered_path=str(dest_path),
                upload_status=UploadStatus.UPLOADED.value,
                youtube_video_id_uploaded=video_id,
                uploaded_at=datetime.datetime.now(datetime.timezone.utc),
            )
            session.add(clip)
            log_audit(session, job, "manual_upload", f"manually uploaded -> {video_id}")
            finish_job(session, job, JobStatus.COMPLETED)
        except Exception as exc:
            log_audit(session, job, "manual_upload", f"manual upload failed: {exc}", AuditLevel.ERROR)
            finish_job(session, job, JobStatus.FAILED, error=str(exc))
            if "invalid_grant" in str(exc).lower():
                channel.status = ChannelStatus.NEEDS_REAUTH.value
        session.commit()

    return RedirectResponse(f"/channels/{name}", status_code=303)


def run_dashboard(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port)
