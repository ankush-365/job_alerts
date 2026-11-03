"""SQLAlchemy models. Works on SQLite (local/tests) and PostgreSQL (Neon, production).

A `Job` is one opening. Each place it was seen is a `JobSource` row, so the same
opening found on YC and on the company's Ashby board can later (Phase 4) share one
Job and one notification while keeping every source link.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class _CleanMixin:
    """Drops NUL characters (PostgreSQL rejects them) and clips to the column length (PostgreSQL
    enforces VARCHAR lengths, SQLite does not), so one odd scraped value can't fail a whole save."""

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        value = str(value).replace("\x00", "")
        length = getattr(self.impl, "length", None)
        return value[:length] if length else value


class CleanString(_CleanMixin, TypeDecorator):
    impl = String
    cache_ok = True  # SQLAlchemy only honours this when set on the class itself


class CleanText(_CleanMixin, TypeDecorator):
    impl = Text
    cache_ok = True


class UTCDateTime(TypeDecorator):
    """Stores UTC; always returns timezone-aware datetimes (SQLite otherwise drops tzinfo)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; use timezone-aware UTC")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(CleanString(300))
    normalized_name: Mapped[str] = mapped_column(CleanString(300), unique=True, index=True)
    website: Mapped[str | None] = mapped_column(CleanString(500))
    domain: Mapped[str | None] = mapped_column(CleanString(255), index=True)
    ats_board: Mapped[str | None] = mapped_column(CleanString(200))  # "<ats>:<slug>" if known
    ats_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)  # last board discovery attempt (YC companies)
    verification_status: Mapped[str | None] = mapped_column(CleanString(20))
    verification_evidence: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, onupdate=_now)

    jobs: Mapped[list[Job]] = relationship(back_populates="company")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    title: Mapped[str] = mapped_column(CleanString(500))
    normalized_title: Mapped[str] = mapped_column(CleanString(500), index=True)
    description: Mapped[str] = mapped_column(CleanText, default="")
    location: Mapped[str | None] = mapped_column(CleanString(500))
    work_arrangement: Mapped[str] = mapped_column(CleanString(20), default="unknown")
    experience_text: Mapped[str | None] = mapped_column(CleanString(200))
    employment_type: Mapped[str | None] = mapped_column(CleanString(100))
    salary_text: Mapped[str | None] = mapped_column(CleanString(300))
    salary_min: Mapped[float | None] = mapped_column(Float)
    salary_max: Mapped[float | None] = mapped_column(Float)
    salary_currency: Mapped[str | None] = mapped_column(CleanString(10))
    salary_period: Mapped[str | None] = mapped_column(CleanString(50))
    skills: Mapped[list] = mapped_column(JSON, default=list)
    visa_text: Mapped[str | None] = mapped_column(CleanString(300))
    apply_url: Mapped[str | None] = mapped_column(CleanString(2000))
    posted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)

    # Filled by later pipeline stages (Phases 4-6).
    filter_status: Mapped[str | None] = mapped_column(CleanString(20), index=True)  # passed / rejected
    filter_details: Mapped[dict | None] = mapped_column(JSON)  # reasons, flags, parsed experience/location
    relevance_score: Mapped[float | None] = mapped_column(Float, index=True)
    score_details: Mapped[dict | None] = mapped_column(JSON)
    verification_status: Mapped[str | None] = mapped_column(CleanString(20), index=True)
    verification_evidence: Mapped[dict | None] = mapped_column(JSON)  # {"summary": ..., "evidence": [...]}
    expired_at: Mapped[datetime | None] = mapped_column(UTCDateTime)  # gone from the company's own job board

    company: Mapped[Company] = relationship(back_populates="jobs")
    sources: Mapped[list[JobSource]] = relationship(back_populates="job", cascade="all, delete-orphan")
    notifications: Mapped[list[Notification]] = relationship(back_populates="job")


class JobSource(Base):
    __tablename__ = "job_sources"
    __table_args__ = (UniqueConstraint("source", "source_job_id", name="uq_source_job"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    source: Mapped[str] = mapped_column(CleanString(50))
    source_job_id: Mapped[str] = mapped_column(CleanString(200))
    job_url: Mapped[str] = mapped_column(CleanString(2000))
    canonical_url: Mapped[str] = mapped_column(CleanString(2000), index=True)
    apply_url: Mapped[str | None] = mapped_column(CleanString(2000))
    ats_board: Mapped[str | None] = mapped_column(CleanString(200))
    details_complete: Mapped[bool] = mapped_column(default=True)
    is_primary: Mapped[bool] = mapped_column(default=True)  # the source that owns the job's fields
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)

    job: Mapped[Job] = relationship(back_populates="sources")


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("job_id", "chat_id", "kind", name="uq_notification"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    chat_id: Mapped[str] = mapped_column(CleanString(50))
    kind: Mapped[str] = mapped_column(CleanString(20))  # "alert" or "digest"
    status: Mapped[str] = mapped_column(CleanString(20))  # "sent" or "failed"
    telegram_message_id: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(CleanText)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)

    job: Mapped[Job] = relationship(back_populates="notifications")


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(CleanString(20), default="running")  # running / ok / partial / failed
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    errors: Mapped[list] = mapped_column(JSON, default=list)


class CollectorRun(Base):
    __tablename__ = "collector_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("pipeline_runs.id"), index=True)
    source: Mapped[str] = mapped_column(CleanString(50))
    ok: Mapped[bool] = mapped_column(default=False)
    jobs_found: Mapped[int] = mapped_column(default=0)
    requests_made: Mapped[int] = mapped_column(default=0)
    errors: Mapped[list] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class KeyValue(Base):
    """Small persistent state, e.g. the Telegram update offset between runs."""

    __tablename__ = "kv_state"

    key: Mapped[str] = mapped_column(CleanString(100), primary_key=True)
    value: Mapped[str] = mapped_column(CleanText)
