"""Database access. All SQL lives here so pipeline stages stay storage-agnostic."""

from __future__ import annotations

import os
from collections import Counter
from itertools import batched
from datetime import datetime, timedelta

from sqlalchemy import create_engine, func, inspect, or_, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import Session, aliased, joinedload, selectinload, sessionmaker

from core.deduplicator import descriptions_compatible, locations_compatible
from core.filters import FilterResult, filter_job
from core.models import CollectionResult, JobRecord, WorkArrangement, utcnow
from core.preferences import Preferences
from collectors.ats_collector import AtsBoard
from core.ats_discovery import yc_slug_from_url
from core.relevance_scorer import score_job
from core.verifier import Evidence, SourceInfo, VerificationInput, board_identity_evidence, verify
from core.semantic import SemanticMatcher
from core.normalizer import canonicalize_url, domain_of, normalize_company, normalize_title
from database.models import Base, CollectorRun, Company, Job, JobSource, KeyValue, Notification, PipelineRun

DUPLICATE_LOOKBACK = timedelta(days=60)  # only openings seen this recently can absorb a new listing
LIVE_WINDOW = timedelta(days=2)  # a listing seen this recently is still live on its board
STALE_AFTER = timedelta(days=30)  # rejected listings unseen this long lose their stored description
ALERT_RETRY_WINDOW = timedelta(hours=24)  # failed alert sends are retried on later runs for this long
BOARD_IDENTITY_MAX_AGE = timedelta(days=7)  # how long a listings-based board ownership check is reused

# Fields copied from a fresh JobRecord onto an existing Job when the record has a value.
_UPDATABLE_FIELDS = (
    "title", "location", "work_arrangement", "experience_text", "employment_type", "salary_text",
    "salary_min", "salary_max", "salary_currency", "salary_period", "visa_text", "apply_url",
)


def _new_source(record: JobRecord, now: datetime, *, primary: bool) -> JobSource:
    return JobSource(
        source=record.source,
        source_job_id=record.source_job_id,
        job_url=record.job_url,
        canonical_url=canonicalize_url(record.job_url),
        apply_url=record.apply_url,
        ats_board=record.ats_board,
        details_complete=record.details_complete,
        is_primary=primary,
        first_seen_at=now,
        last_seen_at=now,
    )


def _apply_primary(job: Job, record: JobRecord, filter_result: FilterResult | None) -> None:
    for field in _UPDATABLE_FIELDS:
        value = getattr(record, field)
        if value not in (None, ""):
            setattr(job, field, value.value if field == "work_arrangement" else value)
    job.normalized_title = normalize_title(job.title)
    if record.skills:
        job.skills = record.skills
    if record.posted_at and not job.posted_at:
        job.posted_at = record.posted_at
    if filter_result is not None:
        _set_filter_result(job, filter_result)
    if record.description:
        job.description = record.description
    elif job.description is None:
        job.description = ""


def _fill_missing(job: Job, record: JobRecord) -> None:
    for field in _UPDATABLE_FIELDS:
        value = getattr(record, field)
        if value not in (None, "") and getattr(job, field) in (None, "", "unknown"):
            setattr(job, field, value.value if field == "work_arrangement" else value)
    if record.skills and not job.skills:
        job.skills = record.skills
    if record.posted_at and not job.posted_at:
        job.posted_at = record.posted_at
    if record.description and not job.description:
        job.description = record.description


def _set_filter_result(job: Job, result: FilterResult) -> None:
    job.filter_status = result.status
    job.filter_details = result.as_dict()


def job_to_record(job: Job) -> JobRecord:
    """Rebuild a JobRecord from stored data, for re-evaluation without collecting again."""
    source = next((s for s in job.sources if s.is_primary), job.sources[0])
    return JobRecord(
        source=source.source,
        source_job_id=source.source_job_id,
        title=job.title,
        company_name=job.company.name,
        description=job.description or "",
        location=job.location,
        work_arrangement=WorkArrangement(job.work_arrangement),
        experience_text=job.experience_text,
        employment_type=job.employment_type,
        salary_text=job.salary_text,
        salary_min=job.salary_min,
        salary_max=job.salary_max,
        salary_currency=job.salary_currency,
        salary_period=job.salary_period,
        skills=job.skills or [],
        visa_text=job.visa_text,
        job_url=source.job_url,
        apply_url=job.apply_url,
        company_website=job.company.website,
        ats_board=source.ats_board,
        posted_at=job.posted_at,
        details_complete=source.details_complete,
        first_seen_at=job.first_seen_at,
    )


def normalize_database_url(url: str) -> str:
    """Neon and others hand out 'postgres://' or 'postgresql://'; SQLAlchemy needs the psycopg driver named."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def make_engine(url: str) -> Engine:
    url = normalize_database_url(url)
    kwargs: dict = {}
    if url.startswith("postgresql"):
        kwargs["pool_pre_ping"] = True  # managed Postgres (Neon) closes idle connections
        if os.environ.get("DB_NULLPOOL") == "1":
            kwargs["poolclass"] = NullPool
    return create_engine(url, **kwargs)


def _add_missing_columns(engine: Engine) -> list[str]:
    """Additive schema migration: create columns that exist in the models but not yet in the database.

    Only adds nullable columns, never drops or alters, so existing data is untouched. Covers the
    columns added during development; larger schema changes would need a real migration tool.
    """
    inspector = inspect(engine)
    added: list[str] = []
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                if not column.nullable and column.default is None and column.server_default is None:
                    raise RuntimeError(f"cannot auto-add NOT NULL column {table.name}.{column.name}")
                col_type = column.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {column.name} {col_type}'))
                added.append(f"{table.name}.{column.name}")
    return added


def init_db(url: str) -> sessionmaker[Session]:
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    return sessionmaker(engine, expire_on_commit=False)


class JobRepository:
    def __init__(self, session: Session):
        self.session = session
        # Per-session caches so storing thousands of listings needs a handful of queries, not one per
        # listing (each query is a network round trip to a managed database).
        self._companies: dict[str, Company] | None = None
        self._sources: dict[tuple[str, str], JobSource] = {}
        self._preloaded: set[tuple[str, str]] = set()

    def preload(self, records: list[JobRecord]) -> None:
        """Fetch the stored source rows (with their jobs) for these records in a few batched queries."""
        wanted: dict[str, list[str]] = {}
        for record in records:
            key = (record.source, record.source_job_id)
            if key not in self._preloaded:
                self._preloaded.add(key)
                wanted.setdefault(record.source, []).append(record.source_job_id)
        for source, ids in wanted.items():
            for chunk in batched(ids, 500):
                rows = self.session.scalars(
                    select(JobSource)
                    .where(JobSource.source == source, JobSource.source_job_id.in_(chunk))
                    .options(joinedload(JobSource.job))
                )
                for row in rows:
                    self._sources[(row.source, row.source_job_id)] = row

    # --- companies -------------------------------------------------------

    def get_or_create_company(self, name: str, website: str | None = None, ats_board: str | None = None) -> Company:
        key = normalize_company(name)
        if self._companies is None:
            self._companies = {c.normalized_name: c for c in self.session.scalars(select(Company))}
        company = self._companies.get(key)
        if company is None:
            company = Company(name=name, normalized_name=key)
            self.session.add(company)
            self._companies[key] = company
        if website and not company.website:
            company.website = website
            company.domain = domain_of(website)
        if ats_board and not company.ats_board:
            company.ats_board = ats_board
        return company

    # --- jobs ------------------------------------------------------------

    def find_source(self, source: str, source_job_id: str) -> JobSource | None:
        key = (source, source_job_id)
        if key in self._sources:
            return self._sources[key]
        if key in self._preloaded:
            return None  # preloaded and not stored
        return self.session.scalar(
            select(JobSource).where(JobSource.source == source, JobSource.source_job_id == source_job_id)
        )

    def find_duplicate(self, record: JobRecord, company: Company, now: datetime) -> Job | None:
        """Stored job that is the same opening as `record` (see core/deduplicator.py), or None."""
        same_url = self.session.scalar(
            select(JobSource).where(JobSource.canonical_url == canonicalize_url(record.job_url))
        )
        if same_url is not None:
            return same_url.job

        company_match = Job.company_id == company.id
        domain = domain_of(record.company_website)
        if domain:
            company_match = or_(company_match, Company.domain == domain)
        candidates = self.session.scalars(
            select(Job)
            .join(Company)
            .where(
                Job.normalized_title == normalize_title(record.title),
                Job.last_seen_at >= now - DUPLICATE_LOOKBACK,
                company_match,
            )
            .order_by(Job.id)
        )
        for job in candidates:
            # Two live listings on the same board with the same title are separate openings;
            # a same-board match only counts once the older listing has gone stale (a repost).
            if any(s.source == record.source and s.last_seen_at >= now - LIVE_WINDOW for s in job.sources):
                continue
            if locations_compatible(job.location, record.location) and descriptions_compatible(
                job.description, record.description
            ):
                return job
        return None

    def upsert_record(
        self, record: JobRecord, filter_result: FilterResult | None = None, now: datetime | None = None
    ) -> tuple[Job, str]:
        """Store one listing. Returns (job, outcome) with outcome "new", "updated" or "merged".

        "merged" means the listing is another source for an opening already stored.
        Only a job's primary source (where it was first seen) changes its fields and
        filter status; other sources just fill gaps, so fields don't flip between runs.
        Every job keeps its description (even rejected ones) so preference changes can
        be re-evaluated from the database; see `prune_stale_descriptions`.
        """
        now = now or utcnow()
        company = self.get_or_create_company(record.company_name, record.company_website, record.ats_board)
        existing = self.find_source(record.source, record.source_job_id)

        if existing is not None:
            existing.last_seen_at = now
            existing.job_url = record.job_url
            existing.canonical_url = canonicalize_url(record.job_url)
            existing.apply_url = record.apply_url or existing.apply_url
            existing.details_complete = existing.details_complete or record.details_complete
            job = existing.job
            job.last_seen_at = now
            if record.ats_board:
                job.expired_at = None  # listed on the company's board again
            if existing.is_primary:
                _apply_primary(job, record, filter_result)
            else:
                _fill_missing(job, record)
            return job, "updated"

        self.session.flush()  # make earlier inserts in this run visible to duplicate matching
        duplicate = self.find_duplicate(record, company, now)
        if duplicate is not None:
            new_source = _new_source(record, now, primary=False)
            duplicate.sources.append(new_source)
            self._sources[(record.source, record.source_job_id)] = new_source
            duplicate.last_seen_at = now
            _fill_missing(duplicate, record)
            return duplicate, "merged"

        job = Job(
            company=company,
            title=record.title,
            normalized_title=normalize_title(record.title),
            work_arrangement=record.work_arrangement.value,
            skills=[],
            first_seen_at=record.first_seen_at,
            last_seen_at=now,
        )
        _apply_primary(job, record, filter_result)
        new_source = _new_source(record, now, primary=True)
        job.sources.append(new_source)
        self._sources[(record.source, record.source_job_id)] = new_source
        self.session.add(job)
        self.session.flush()  # assign job.id
        return job, "new"

    def stored_description(self, source: str, source_job_id: str) -> str:
        found = self.find_source(source, source_job_id)
        return found.job.description if found is not None else ""

    def reevaluate_all(self, preferences: Preferences, now: datetime | None = None) -> dict[str, int]:
        """Re-run the filters on every stored job with the current preferences. Returns status counts."""
        counts: Counter = Counter()
        jobs = self.session.scalars(select(Job).options(selectinload(Job.sources), selectinload(Job.company)))
        for job in jobs:
            result = filter_job(job_to_record(job), preferences, now=now)
            _set_filter_result(job, result)
            counts[result.status] += 1
        return dict(counts)

    def score_active_jobs(
        self, preferences: Preferences, matcher: SemanticMatcher | None, now: datetime | None = None
    ) -> dict[str, int]:
        """Score every non-rejected job seen in the last 30 days; clear scores of rejected ones.

        Returns counts per tier plus "semantic" = 1 if the embedding model was used.
        """
        now = now or utcnow()
        for job in self.session.scalars(select(Job).where(Job.filter_status == "rejected", Job.relevance_score.is_not(None))):
            job.relevance_score = None
            job.score_details = None
        jobs = self.session.scalars(
            select(Job)
            .where(Job.filter_status != "rejected", Job.last_seen_at >= now - STALE_AFTER)
            .options(selectinload(Job.sources), selectinload(Job.company))
        ).all()
        records = [job_to_record(job) for job in jobs]
        similarities = matcher.similarities([f"{r.title}\n{r.description}" for r in records]) if matcher else None
        counts: Counter = Counter()
        for i, (job, record) in enumerate(zip(jobs, records)):
            result = score_job(
                record, job.filter_details or {}, preferences,
                semantic_similarity=similarities[i] if similarities else None, now=now,
            )
            job.relevance_score = result.score
            job.score_details = result.as_dict()
            counts[result.tier] += 1
        counts["semantic"] = int(similarities is not None and bool(jobs))
        return dict(counts)

    # --- expiry, board identity, discovery, verification ---------------------

    def mark_unlisted_as_expired(self, board: str, seen_at: datetime) -> int:
        """`board` was fetched completely at `seen_at`; its listings not seen then are no longer open."""
        sources = self.session.scalars(
            select(JobSource).where(JobSource.ats_board == board, JobSource.last_seen_at < seen_at)
        ).all()
        count = 0
        for source in sources:
            if source.job.expired_at is None:
                source.job.expired_at = seen_at
                count += 1
        return count

    def refresh_board_identities(self, now: datetime, max_age: timedelta = BOARD_IDENTITY_MAX_AGE) -> int:
        """(Re)check who owns each configured board from its stored listings. Discovery results are kept."""
        companies = self.session.scalars(select(Company).where(Company.ats_board.is_not(None))).all()
        checked = 0
        for company in companies:
            info = company.verification_evidence or {}
            if info.get("method") == "discovery":
                continue
            checked_at = info.get("checked_at")
            if checked_at and datetime.fromisoformat(checked_at) > now - max_age:
                continue
            listings = self.session.execute(
                select(JobSource.job_url, Job.description).join(Job).where(JobSource.ats_board == company.ats_board)
            ).all()
            evidence = board_identity_evidence(company.ats_board, company.name, [(u, d) for u, d in listings])
            company.verification_status = "board_confirmed" if evidence.outcome == "pass" else "board_unconfirmed"
            company.verification_evidence = {"method": "listings", "board": company.ats_board,
                                             "checked_at": now.isoformat(), **evidence.as_dict()}
            checked += 1
        return checked

    def board_identities(self) -> dict[str, Evidence]:
        result: dict[str, Evidence] = {}
        for company in self.session.scalars(select(Company).where(Company.ats_board.is_not(None))):
            info = company.verification_evidence or {}
            if info.get("board") == company.ats_board and "outcome" in info:
                result[company.ats_board] = Evidence(info["check"], info["outcome"], info["detail"])
        return result

    def discovery_candidates(self, now: datetime, recheck_after: timedelta, limit: int) -> list[tuple[Company, str | None, list[str]]]:
        """YC companies with relevant jobs whose own job board is unknown. Returns (company, yc_slug, yc titles)."""
        rows = self.session.execute(
            select(Company, JobSource.job_url, Job.title)
            .join(Job, Job.company_id == Company.id)
            .join(JobSource, JobSource.job_id == Job.id)
            .where(
                JobSource.source == "yc",
                Job.filter_status != "rejected",
                Company.ats_board.is_(None),
                or_(Company.ats_checked_at.is_(None), Company.ats_checked_at < now - recheck_after),
            )
            .order_by(Job.relevance_score.desc().nulls_last())
        ).all()
        found: dict[int, tuple[Company, str | None, list[str]]] = {}
        for company, job_url, title in rows:
            if company.id not in found:
                if len(found) >= limit:
                    continue
                found[company.id] = (company, yc_slug_from_url(job_url), [])
            found[company.id][2].append(title)
        return list(found.values())

    def record_discovery(
        self, company: Company, board: str | None, evidence: str, now: datetime, tried: list[str] | None = None
    ) -> None:
        """Keep the outcome either way, so it is visible why a company has (or lacks) a confirmed board."""
        company.ats_checked_at = now
        company.verification_evidence = {"method": "discovery", "board": board, "checked_at": now.isoformat(),
                                         "check": "board_identity", "outcome": "pass" if board else "info",
                                         "detail": evidence, "tried": tried or []}
        if board:
            company.ats_board = board
            company.verification_status = "board_confirmed"
        else:
            company.verification_status = "board_not_found"

    def discovered_boards(self) -> list[AtsBoard]:
        boards = []
        for company in self.session.scalars(select(Company).where(Company.ats_board.is_not(None))):
            if (company.verification_evidence or {}).get("method") == "discovery":
                ats, _, slug = company.ats_board.partition(":")
                boards.append(AtsBoard(ats, slug, company_name=company.name, website=company.website))
        return boards

    def verify_active_jobs(self, now: datetime) -> dict[str, int]:
        """Classify every non-rejected job seen in the last 30 days; clear rejected jobs' results."""
        for job in self.session.scalars(
            select(Job).where(Job.filter_status == "rejected", Job.verification_status.is_not(None))
        ):
            job.verification_status = None
            job.verification_evidence = None
        identities = self.board_identities()
        jobs = self.session.scalars(
            select(Job)
            .where(Job.filter_status != "rejected", Job.last_seen_at >= now - STALE_AFTER)
            .options(selectinload(Job.sources), selectinload(Job.company))
        ).all()
        counts: Counter = Counter()
        for job in jobs:
            result = verify(VerificationInput(
                title=job.title,
                company_name=job.company.name,
                description=job.description or "",
                apply_url=job.apply_url,
                sources=[SourceInfo(s.source, s.job_url, s.ats_board) for s in job.sources],
                company_website=job.company.website,
                board_identity=identities,
                company_board=job.company.ats_board,
                expired_at=job.expired_at,
            ))
            job.verification_status = result.status
            job.verification_evidence = result.as_dict()
            counts[result.status] += 1
        return dict(counts)

    # --- notifications -----------------------------------------------------

    def _never_sent(self, chat_id: str):
        """True if neither this job nor a same-role copy (same company and normalized title, e.g. another
        city) was ever sent to the chat, as an alert or in a digest. This keeps every message unique."""
        sent_job = aliased(Job)
        return ~select(Notification.id).join(sent_job, sent_job.id == Notification.job_id).where(
            Notification.chat_id == chat_id,
            Notification.status == "sent",
            sent_job.company_id == Job.company_id,
            sent_job.normalized_title == Job.normalized_title,
        ).exists()

    def alert_candidates(self, preferences: Preferences, chat_id: str, now: datetime) -> list[Job]:
        """Jobs that may be alerted now, best first (grouping and the per-run cap happen in the notifier).

        Rules: passed the filters (or in review, if configured), score >= threshold, not expired, still
        listed recently, details fetched, verification allowed by preferences, and never sent before in
        any form, including same-role copies (failed sends are retried for 24 hours).
        """
        n = preferences.notifications
        statuses = ["passed"] + (["review"] if n.location_review_jobs == "alert" else [])
        allowed = ["VERIFIED"] + (["NEEDS_REVIEW"] if n.notify_needs_review else []) + (
            ["SUSPICIOUS"] if n.notify_suspicious else [])
        gave_up = select(Notification.id).where(
            Notification.job_id == Job.id, Notification.chat_id == chat_id, Notification.kind == "alert",
            Notification.status == "failed", Notification.created_at < now - ALERT_RETRY_WINDOW,
        ).exists()
        details = select(JobSource.id).where(JobSource.job_id == Job.id, JobSource.details_complete.is_(True)).exists()
        return list(self.session.scalars(
            select(Job)
            .where(
                Job.filter_status.in_(statuses),
                Job.relevance_score >= n.minimum_relevance_score,
                Job.verification_status.in_(allowed),
                Job.expired_at.is_(None),
                Job.last_seen_at >= now - LIVE_WINDOW,
                details,
                self._never_sent(chat_id),
                ~gave_up,
            )
            .options(selectinload(Job.sources), selectinload(Job.company))
            .order_by(Job.relevance_score.desc(), Job.first_seen_at.desc())
            .limit(n.max_alerts_per_run * 5)  # headroom: several same-role copies may collapse into one alert
        ))

    def record_notification(self, job_id: int, chat_id: str, kind: str, status: str, now: datetime,
                            message_id: int | None = None, error: str | None = None) -> None:
        """One row per (job, chat, kind); a retry updates the row but keeps the first-attempt time."""
        row = self.session.scalar(select(Notification).where(
            Notification.job_id == job_id, Notification.chat_id == chat_id, Notification.kind == kind))
        if row is None:
            row = Notification(job_id=job_id, chat_id=chat_id, kind=kind, created_at=now)
            self.session.add(row)
        row.status = status
        row.telegram_message_id = message_id
        row.error = error[:1000] if error else None

    def digest_jobs(self, chat_id: str, since: datetime) -> tuple[list[Job], dict[str, int]]:
        """Relevant jobs first seen since `since` that were never sent in any form (best first), plus totals."""
        recent = Job.first_seen_at >= since
        jobs = list(self.session.scalars(
            select(Job)
            .where(recent, Job.filter_status.in_(["passed", "review"]), Job.relevance_score.is_not(None),
                   Job.expired_at.is_(None), Job.verification_status != "SUSPICIOUS", self._never_sent(chat_id))
            .options(selectinload(Job.sources), selectinload(Job.company))
            .order_by(Job.relevance_score.desc())
        ))
        totals = {
            "new": self.session.scalar(select(func.count(Job.id)).where(recent)) or 0,
            "relevant": self.session.scalar(select(func.count(Job.id)).where(recent, Job.filter_status == "passed")) or 0,
            "alerted": self.session.scalar(select(func.count(Notification.id)).where(
                Notification.chat_id == chat_id, Notification.kind == "alert", Notification.status == "sent",
                Notification.created_at >= since)) or 0,
        }
        return jobs, totals

    def latest_jobs(self, limit: int) -> list[Job]:
        return list(self.session.scalars(
            select(Job)
            .where(Job.filter_status == "passed", Job.relevance_score.is_not(None), Job.expired_at.is_(None),
                   Job.verification_status != "SUSPICIOUS")
            .options(selectinload(Job.sources), selectinload(Job.company))
            .order_by(Job.first_seen_at.desc(), Job.relevance_score.desc())
            .limit(limit)
        ))

    def stats(self) -> dict:
        def count(*where) -> int:
            return self.session.scalar(select(func.count(Job.id)).where(*where)) or 0

        last = self.session.scalar(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(1))
        return {
            "discovered": count(),
            "relevant": count(Job.filter_status == "passed"),
            "review": count(Job.filter_status == "review"),
            "verified": count(Job.verification_status == "VERIFIED"),
            "needs_review": count(Job.verification_status == "NEEDS_REVIEW"),
            "suspicious": count(Job.verification_status == "SUSPICIOUS"),
            "notified": self.session.scalar(select(func.count(Notification.id)).where(
                Notification.kind == "alert", Notification.status == "sent")) or 0,
            "last_run": f"{last.started_at:%Y-%m-%d %H:%M} UTC ({last.status})" if last else None,
        }

    def prune_stale_descriptions(self, now: datetime, older_than: timedelta = STALE_AFTER) -> int:
        """Drop descriptions of rejected jobs no longer listed anywhere; they can't come back to matter."""
        jobs = self.session.scalars(
            select(Job).where(Job.filter_status == "rejected", Job.last_seen_at < now - older_than, Job.description != "")
        ).all()
        for job in jobs:
            job.description = ""
        return len(jobs)

    def known_source_ids(self, source: str, *, require_details: bool = False) -> set[str]:
        query = select(JobSource.source_job_id).where(JobSource.source == source)
        if require_details:
            query = query.where(JobSource.details_complete.is_(True))
        return set(self.session.scalars(query))

    def count_jobs(self) -> int:
        return self.session.scalar(select(func.count(Job.id))) or 0

    # --- run logs --------------------------------------------------------

    def start_run(self) -> PipelineRun:
        run = PipelineRun(started_at=utcnow(), status="running", stats={}, errors=[])
        self.session.add(run)
        self.session.flush()
        return run

    def get_run(self, run_id: int) -> PipelineRun:
        run = self.session.get(PipelineRun, run_id)
        if run is None:
            raise LookupError(f"pipeline run {run_id} not found")
        return run

    def record_collector_run(self, run: PipelineRun, result: CollectionResult) -> CollectorRun:
        row = CollectorRun(
            run_id=run.id,
            source=result.source,
            ok=result.ok,
            jobs_found=len(result.jobs),
            requests_made=result.requests_made,
            errors=result.errors[:50],
            started_at=result.started_at,
            finished_at=result.finished_at,
        )
        self.session.add(row)
        return row

    # --- key/value state -------------------------------------------------

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.session.get(KeyValue, key)
        return row.value if row else default

    def set_state(self, key: str, value: str) -> None:
        row = self.session.get(KeyValue, key)
        if row is None:
            self.session.add(KeyValue(key=key, value=value))
        else:
            row.value = value
