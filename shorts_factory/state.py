"""SQLAlchemy models and query helpers for the shorts-factory local state DB.

Single SQLite file shared by the CLI and the always-running scheduler daemon —
they never talk to each other directly, only through rows in this DB.
"""

from __future__ import annotations

import datetime
import enum
from pathlib import Path

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class ChannelStatus(str, enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    NEEDS_REAUTH = "needs_reauth"


class CredentialProvider(str, enum.Enum):
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"


class CredentialStatus(str, enum.Enum):
    VALID = "valid"
    EXPIRED = "expired"
    REVOKED = "revoked"


class SourceVideoStatus(str, enum.Enum):
    DISCOVERED = "discovered"
    DOWNLOADED = "downloaded"
    TRANSCRIBED = "transcribed"
    SEGMENTS_SELECTED = "segments_selected"
    RENDERED = "rendered"
    FAILED = "failed"
    SKIPPED = "skipped"


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class UploadStatus(str, enum.Enum):
    PENDING = "pending"
    PENDING_UPLOAD = "pending_upload"
    UPLOADED = "uploaded"
    FAILED = "failed"


class AuditLevel(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, index=True)
    source_strategy: Mapped[str] = mapped_column(String)
    format_template: Mapped[str] = mapped_column(String)
    niche_keywords: Mapped[list] = mapped_column(JSON, default=list)
    layout_config_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default=ChannelStatus.ACTIVE.value)
    gcp_project_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    last_run_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    credentials: Mapped[list["Credential"]] = relationship(back_populates="channel", cascade="all, delete-orphan")
    source_videos: Mapped[list["SourceVideo"]] = relationship(back_populates="channel", cascade="all, delete-orphan")
    jobs: Mapped[list["Job"]] = relationship(back_populates="channel", cascade="all, delete-orphan")


class Credential(Base):
    __tablename__ = "credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    provider: Mapped[str] = mapped_column(String)
    keyring_key_ref: Mapped[str] = mapped_column(String)
    expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String, default=CredentialStatus.VALID.value)

    channel: Mapped[Channel] = relationship(back_populates="credentials")

    __table_args__ = (UniqueConstraint("channel_id", "provider", name="uq_credential_channel_provider"),)


class SourceVideo(Base):
    __tablename__ = "source_videos"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String, unique=True, index=True)  # youtube video id
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    discovered_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    duration_s: Mapped[int | None] = mapped_column(nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    downloaded_path: Mapped[str | None] = mapped_column(String, nullable=True)
    processed_status: Mapped[str] = mapped_column(String, default=SourceVideoStatus.DISCOVERED.value)

    channel: Mapped[Channel] = relationship(back_populates="source_videos")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    cycle_date: Mapped[datetime.date] = mapped_column(default=lambda: _utcnow().date())
    status: Mapped[str] = mapped_column(String, default=JobStatus.PENDING.value)
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)

    channel: Mapped[Channel] = relationship(back_populates="jobs")
    clips: Mapped[list["Clip"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    audit_entries: Mapped[list["AuditLogEntry"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class Clip(Base):
    __tablename__ = "clips"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"))
    source_video_id: Mapped[int | None] = mapped_column(ForeignKey("source_videos.id"), nullable=True)
    segment_start_s: Mapped[float | None] = mapped_column(nullable=True)
    segment_end_s: Mapped[float | None] = mapped_column(nullable=True)
    hook_title: Mapped[str | None] = mapped_column(String, nullable=True)
    format_template_used: Mapped[str | None] = mapped_column(String, nullable=True)
    rendered_path: Mapped[str | None] = mapped_column(String, nullable=True)
    upload_status: Mapped[str] = mapped_column(String, default=UploadStatus.PENDING.value)
    youtube_video_id_uploaded: Mapped[str | None] = mapped_column(String, nullable=True)
    uploaded_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    job: Mapped[Job] = relationship(back_populates="clips")


class QuotaUsage(Base):
    __tablename__ = "quota_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    gcp_project_ref: Mapped[str] = mapped_column(String, index=True)
    date: Mapped[datetime.date] = mapped_column(default=lambda: _utcnow().date())
    units_used: Mapped[int] = mapped_column(default=0)

    __table_args__ = (UniqueConstraint("gcp_project_ref", "date", name="uq_quota_project_date"),)


class AuditLogEntry(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    step: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(String)
    level: Mapped[str] = mapped_column(String, default=AuditLevel.INFO.value)
    timestamp: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    job: Mapped[Job | None] = relationship(back_populates="audit_entries")


def get_engine(db_path: str | Path):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    return engine


# --- query helpers -----------------------------------------------------------


def get_channel_by_name(session: Session, name: str) -> Channel | None:
    return session.scalar(select(Channel).where(Channel.name == name))


def create_job(session: Session, channel: Channel) -> Job:
    job = Job(channel_id=channel.id, status=JobStatus.RUNNING.value, started_at=_utcnow())
    session.add(job)
    session.flush()
    return job


def finish_job(session: Session, job: Job, status: JobStatus, error: str | None = None) -> None:
    job.status = status.value
    job.finished_at = _utcnow()
    job.error = error


def log_audit(
    session: Session,
    job: Job | None,
    step: str,
    message: str,
    level: AuditLevel = AuditLevel.INFO,
) -> None:
    session.add(
        AuditLogEntry(
            job_id=job.id if job else None,
            step=step,
            message=message,
            level=level.value,
        )
    )


def is_source_already_processed(session: Session, source_id: str) -> bool:
    return session.scalar(select(SourceVideo).where(SourceVideo.source_id == source_id)) is not None


def get_quota_used_today(session: Session, gcp_project_ref: str) -> int:
    today = _utcnow().date()
    row = session.scalar(
        select(QuotaUsage).where(QuotaUsage.gcp_project_ref == gcp_project_ref, QuotaUsage.date == today)
    )
    return row.units_used if row else 0


def record_quota_usage(session: Session, gcp_project_ref: str, units: int) -> None:
    today = _utcnow().date()
    row = session.scalar(
        select(QuotaUsage).where(QuotaUsage.gcp_project_ref == gcp_project_ref, QuotaUsage.date == today)
    )
    if row is None:
        row = QuotaUsage(gcp_project_ref=gcp_project_ref, date=today, units_used=0)
        session.add(row)
        session.flush()
    row.units_used += units


def get_pending_upload_clips(session: Session, channel_id: int, limit: int) -> list[Clip]:
    return list(
        session.scalars(
            select(Clip)
            .join(Job)
            .where(Job.channel_id == channel_id, Clip.upload_status == UploadStatus.PENDING_UPLOAD.value)
            .order_by(Clip.created_at.asc())
            .limit(limit)
        )
    )
