"""Normalized data shapes shared by collectors and the rest of the pipeline."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WorkArrangement(str, Enum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class JobRecord(BaseModel):
    """One job as seen on one source. Missing information stays None/empty, never guessed."""

    source: str  # e.g. "yc", "greenhouse", "lever", "ashby"
    source_job_id: str
    title: str
    company_name: str
    description: str = ""  # plain text; untrusted
    location: str | None = None
    work_arrangement: WorkArrangement = WorkArrangement.UNKNOWN
    experience_text: str | None = None  # as stated by the source, e.g. "Any (new grads ok)"
    employment_type: str | None = None  # as stated, e.g. "Full-time", "Intern"
    salary_text: str | None = None  # original wording, original currency
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None  # "year", "month", "hour", ... as stated
    skills: list[str] = Field(default_factory=list)  # only skills the source lists explicitly
    visa_text: str | None = None
    job_url: str
    apply_url: str | None = None
    company_website: str | None = None
    ats_board: str | None = None  # "<ats>:<slug>" when the listing is on a company ATS board
    posted_at: datetime | None = None
    details_complete: bool = True  # False when only a summary was fetched (e.g. YC list page)
    first_seen_at: datetime = Field(default_factory=utcnow)


class CollectionResult(BaseModel):
    """Outcome of one collector run. Collectors report failure here instead of raising."""

    source: str
    ok: bool
    jobs: list[JobRecord] = Field(default_factory=list)
    requests_made: int = 0
    errors: list[str] = Field(default_factory=list)
    # ATS boards fetched completely this run; a job missing from one of these is no longer listed.
    complete_boards: list[str] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
