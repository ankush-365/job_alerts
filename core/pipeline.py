"""One pipeline run: discover YC company boards -> collect -> filter/dedupe/store -> score -> verify.

Each collector's results are stored in their own transaction, so a failure while
collecting or storing one source never loses another source's jobs.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.orm import Session, sessionmaker

from collectors.base_collector import BaseCollector, HttpFetcher
from collectors.registry import build_collectors, make_fetcher
from core.ats_discovery import discover_board
from core.filters import filter_job
from core.models import CollectionResult, utcnow
from core.preferences import Preferences, preferences_fingerprint
from core.semantic import SemanticMatcher
from notifications.notifier import TelegramNotifier
from database.repository import JobRepository

logger = logging.getLogger(__name__)

PREFERENCES_KEY = "preferences_fingerprint"


def _discover_yc_boards(session_factory: sessionmaker[Session], fetcher: HttpFetcher, ats_config: dict) -> dict:
    """Look for the own job boards of YC companies with relevant jobs (bounded per run)."""
    stats = {"checked": 0, "found": 0, "requests": 0}
    now = utcnow()
    with session_factory.begin() as session:
        repo = JobRepository(session)
        candidates = repo.discovery_candidates(
            now,
            recheck_after=timedelta(days=ats_config.get("rediscover_after_days", 30)),
            limit=ats_config.get("max_discoveries_per_run", 5),
        )
        for company, yc_slug, titles in candidates:
            try:
                found = discover_board(fetcher, company.name, yc_slug=yc_slug, website=company.website,
                                       known_titles=titles)
            except Exception:  # noqa: BLE001 - discovery is best effort
                logger.exception("Board discovery for %s failed", company.name)
                continue
            board = f"{found.board.ats}:{found.board.slug}" if found.board else None
            repo.record_discovery(company, board, found.evidence, now, tried=found.tried)
            stats["checked"] += 1
            stats["found"] += bool(board)
            stats["requests"] += found.requests
            logger.info("Board discovery %s: %s", company.name, found.evidence)
    return stats


def make_matcher(preferences: Preferences) -> SemanticMatcher | None:
    semantic = preferences.scoring.semantic
    if not semantic.enabled or not preferences.profile.summary.strip():
        return None
    return SemanticMatcher(semantic.model, preferences.profile.summary)


@dataclass
class RunSummary:
    run_id: int
    status: str = "running"
    new_job_ids: list[int] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def _store_result(
    session_factory: sessionmaker[Session], run_id: int, result: CollectionResult, preferences: Preferences | None
) -> tuple[list[int], dict]:
    """Filter and save one collector's jobs in a single transaction. Returns (new job ids, counts)."""
    new_ids: list[int] = []
    counts: Counter = Counter()
    with session_factory.begin() as session:
        repo = JobRepository(session)
        repo.preload(result.jobs)
        now = utcnow()
        for record in result.jobs:
            if not record.description:
                # e.g. YC sends details only once; filter on the description stored earlier.
                stored = repo.stored_description(record.source, record.source_job_id)
                if stored:
                    record = record.model_copy(update={"description": stored})
            verdict = filter_job(record, preferences) if preferences else None
            job, outcome = repo.upsert_record(record, verdict, now)
            counts[outcome] += 1
            if verdict is not None:
                counts[verdict.status] += 1
                for reason in verdict.reasons[:1]:
                    counts["rejected_" + reason.split(":", 1)[0]] += 1
            if outcome == "new":
                new_ids.append(job.id)
        for board in result.complete_boards:
            counts["expired"] += repo.mark_unlisted_as_expired(board, now)
        repo.record_collector_run(repo.get_run(run_id), result)
    return new_ids, dict(counts)


def run_pipeline(
    session_factory: sessionmaker[Session],
    sources_config: dict,
    *,
    fetcher: HttpFetcher | None = None,
    collectors: list[BaseCollector] | None = None,
    preferences: Preferences | None = None,
    matcher: SemanticMatcher | None | bool = True,
    notifier: TelegramNotifier | None = None,
) -> RunSummary:
    """Run every collector, then filter, store and score. Without `preferences`, jobs are stored unfiltered.

    `matcher=True` builds the semantic matcher from the preferences; pass None to score with rules only.
    With a `notifier`, new qualifying jobs are sent to Telegram at the end of the run.
    """
    own_fetcher = fetcher is None and collectors is None
    fetcher = fetcher or make_fetcher(sources_config)

    with session_factory.begin() as session:
        repo = JobRepository(session)
        summary = RunSummary(run_id=repo.start_run().id)
        # YC detail pages are fetched only for jobs we have never fully stored.
        known = {"yc": repo.known_source_ids("yc", require_details=True)}

    if collectors is None:
        ats_config = sources_config.get("ats") or {}
        if ats_config.get("enabled") and ats_config.get("discover_for_yc"):
            summary.stats["discovery"] = _discover_yc_boards(session_factory, fetcher, ats_config)
        with session_factory() as session:
            extra_boards = JobRepository(session).discovered_boards()
        collectors = build_collectors(sources_config, fetcher, known_job_ids=known, extra_boards=extra_boards)

    sources_ok = 0
    try:
        for collector in collectors:
            result = collector.run()
            stats = {"ok": result.ok, "found": len(result.jobs), "requests": result.requests_made,
                     "errors": len(result.errors)}
            try:
                new_ids, counts = _store_result(session_factory, summary.run_id, result, preferences)
            except Exception as exc:  # noqa: BLE001 - the transaction was rolled back
                logger.exception("Storing %s results failed", collector.source)
                result.ok = stats["ok"] = False
                summary.errors.append(f"{collector.source}: storage failed: {type(exc).__name__}: {exc}")
            else:
                stats.update(counts)
                summary.new_job_ids.extend(new_ids)

            summary.errors.extend(f"{collector.source}: {e}" for e in result.errors[:20])
            summary.stats[collector.source] = stats
            sources_ok += bool(result.ok)
    finally:
        if own_fetcher:
            fetcher.close()

    if sources_ok == len(collectors):
        summary.status = "ok"
    elif sources_ok:
        summary.status = "partial"
    else:
        summary.status = "failed"

    with session_factory.begin() as session:
        repo = JobRepository(session)
        now = utcnow()
        if preferences is not None:
            fingerprint = preferences_fingerprint(preferences)
            if repo.get_state(PREFERENCES_KEY) != fingerprint:
                # Preferences changed: re-judge jobs that this run's collectors did not return.
                summary.stats["reevaluated"] = repo.reevaluate_all(preferences, now)
                repo.set_state(PREFERENCES_KEY, fingerprint)
        if preferences is not None:
            if matcher is True:
                matcher = make_matcher(preferences)
            summary.stats["scored"] = repo.score_active_jobs(preferences, matcher or None, now)
        if preferences is not None:
            repo.refresh_board_identities(now)
            summary.stats["verified"] = repo.verify_active_jobs(now)
        summary.stats["descriptions_pruned"] = repo.prune_stale_descriptions(now)

    if notifier is not None and preferences is not None:
        summary.stats["telegram"] = notifier.run(session_factory, preferences, utcnow())

    with session_factory.begin() as session:
        run = JobRepository(session).get_run(summary.run_id)
        run.status = summary.status
        run.finished_at = utcnow()
        run.stats = {**summary.stats, "new_jobs": len(summary.new_job_ids)}
        run.errors = summary.errors[:100]

    logger.info("Run %d finished: %s, %d new jobs", summary.run_id, summary.status, len(summary.new_job_ids))
    return summary
