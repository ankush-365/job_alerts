"""Sends job alerts, the daily digest and command replies on every pipeline run.

Alerts are pushed automatically: each run sends every newly qualifying job that has not been
sent before. Every message is unique: a job (or the same role in another city) that was
already sent as an alert or in a digest is never sent again, and same-role copies found
together are combined into one message listing all locations. Bot commands are read with getUpdates during the run (there is no always-on
server), so a reply arrives on the next run. Only the configured chat is ever answered.
Telegram failures are recorded and never stop the pipeline.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, sessionmaker

from core.preferences import Preferences
from database.models import Job
from database.repository import JobRepository
from notifications.formatter import (
    HELP_TEXT,
    START_TEXT,
    JobView,
    format_alert,
    format_digest,
    format_latest,
    format_preferences,
    format_stats,
)
from notifications.telegram_bot import TelegramClient, TelegramError, next_offset, parse_commands

logger = logging.getLogger(__name__)

OFFSET_KEY = "telegram_update_offset"
DIGEST_DATE_KEY = "last_digest_date"
DIGEST_AT_KEY = "last_digest_at"


def experience_label(job: Job) -> str:
    details = job.filter_details or {}
    years = details.get("required_years")
    if job.experience_text:
        return job.experience_text
    if years == 0:
        return "Freshers / new grads welcome"
    if years is not None:
        preferred = any("preferred, not required" in f for f in details.get("flags") or [])
        return f"{years}+ years {'preferred' if preferred else 'required'}"
    return "Not stated"


def group_openings(jobs: list[Job]) -> list[list[Job]]:
    """Group same-role copies (same company + normalized title, e.g. different cities), keeping order."""
    groups: dict[tuple[int, str], list[Job]] = {}
    for job in jobs:
        groups.setdefault((job.company_id, job.normalized_title), []).append(job)
    return list(groups.values())


def to_view(jobs: Job | list[Job]) -> JobView:
    """View of one opening; for a group of same-role copies, the best-scored first plus all locations."""
    group = jobs if isinstance(jobs, list) else [jobs]
    job = group[0]
    details = job.filter_details or {}
    score = job.score_details or {}
    verification = job.verification_evidence or {}
    primary_first = sorted(job.sources, key=lambda s: not s.is_primary)
    employment = details.get("employment_type") or job.employment_type
    locations = list(dict.fromkeys(j.location for j in group if j.location))
    location = " / ".join(locations[:5]) + (f" (+{len(locations) - 5} more)" if len(locations) > 5 else "")
    sources = list(dict.fromkeys((s.source, s.job_url) for j in group
                                 for s in sorted(j.sources, key=lambda s: not s.is_primary)))
    return JobView(
        id=job.id,
        title=job.title,
        company=job.company.name,
        location=location or None,
        experience=experience_label(job),
        employment=employment.replace("-", " ").capitalize() if employment else None,
        salary=job.salary_text,
        score=int(job.relevance_score) if job.relevance_score is not None else None,
        tier=score.get("tier"),
        verification=job.verification_status,
        verification_summary=verification.get("summary"),
        matched_skills=score.get("matched_skills") or [],
        missing_skills=score.get("missing_skills") or [],
        reason=score.get("reason"),
        apply_url=job.apply_url or (primary_first[0].job_url if primary_first else None),
        sources=sources[:4],
        flags=details.get("flags") or [],
        first_seen_at=job.first_seen_at,
    )


class TelegramNotifier:
    def __init__(self, client: TelegramClient, chat_id: int, *, send_delay: float = 0.5, sleep=time.sleep):
        self.client = client
        self.chat_id = chat_id
        self.chat_key = str(chat_id)
        self.send_delay = send_delay
        self._sleep = sleep

    def run(self, session_factory: sessionmaker[Session], prefs: Preferences, now: datetime) -> dict:
        stats: dict = {}
        for name, step in (("commands", self.handle_commands), ("alerts", self.send_alerts),
                           ("digest", self.send_digest)):
            try:
                stats[name] = step(session_factory, prefs, now)
            except Exception as exc:  # noqa: BLE001 - notification problems must not fail the run
                logger.exception("Telegram %s step failed", name)
                stats[name] = {"error": f"{type(exc).__name__}: {exc}"}
        return stats

    # --- alerts ----------------------------------------------------------

    def send_alerts(self, session_factory: sessionmaker[Session], prefs: Preferences, now: datetime) -> dict:
        sent = failed = 0
        with session_factory.begin() as session:
            repo = JobRepository(session)
            groups = group_openings(repo.alert_candidates(prefs, self.chat_key, now))
            for group in groups[: prefs.notifications.max_alerts_per_run]:
                try:
                    message = self.client.send_message(self.chat_id, format_alert(to_view(group)))
                except TelegramError as exc:
                    failed += 1
                    for job in group:
                        repo.record_notification(job.id, self.chat_key, "alert", "failed", now, error=str(exc))
                    if exc.status_code in (400, 401, 403, 404) and "parse" not in str(exc).lower():
                        logger.error("Telegram rejected alerts (%s); stopping this run", exc)
                        break  # bad token, blocked bot or wrong chat: every further send would fail too
                    continue
                sent += 1
                for job in group:  # every copy counts as sent, so none is alerted again later
                    repo.record_notification(job.id, self.chat_key, "alert", "sent", now,
                                             message_id=message.get("message_id"))
                self._sleep(self.send_delay)
        return {"sent": sent, "failed": failed}

    # --- daily digest ------------------------------------------------------

    def send_digest(self, session_factory: sessionmaker[Session], prefs: Preferences, now: datetime) -> dict:
        n = prefs.notifications
        if not n.daily_digest:
            return {"sent": False, "reason": "disabled"}
        local = now.astimezone(ZoneInfo(n.timezone))
        today = local.date().isoformat()
        with session_factory.begin() as session:
            repo = JobRepository(session)
            if local.hour < n.digest_hour or repo.get_state(DIGEST_DATE_KEY) == today:
                return {"sent": False}
            last = repo.get_state(DIGEST_AT_KEY)
            since = datetime.fromisoformat(last) if last else now - timedelta(days=1)
            jobs, totals = repo.digest_jobs(self.chat_key, since)
            groups = group_openings(jobs)[: n.digest_max_jobs]
            messages = format_digest(local.strftime("%d %b %Y"), [to_view(g) for g in groups], totals)
            for text in messages:
                self.client.send_message(self.chat_id, text)  # a failure aborts; the digest is retried next run
                self._sleep(self.send_delay)
            for group in groups:
                for job in group:
                    repo.record_notification(job.id, self.chat_key, "digest", "sent", now)
            repo.set_state(DIGEST_DATE_KEY, today)
            repo.set_state(DIGEST_AT_KEY, now.isoformat())
        return {"sent": True, "jobs": len(groups), "messages": len(messages)}

    # --- bot commands ------------------------------------------------------

    def handle_commands(self, session_factory: sessionmaker[Session], prefs: Preferences, now: datetime) -> dict:
        with session_factory.begin() as session:
            repo = JobRepository(session)
            offset = repo.get_state(OFFSET_KEY)
            updates = self.client.get_updates(int(offset) if offset else None)
            answered = ignored = 0
            for command in parse_commands(updates):
                if command.chat_id != self.chat_id:
                    ignored += 1  # private bot: never answer other chats
                    logger.info("Ignoring /%s from unregistered chat", command.command)
                    continue
                self.client.send_message(self.chat_id, self._reply(command.command, command.args, repo, prefs))
                answered += 1
            new_offset = next_offset(updates)
            if new_offset is not None:
                repo.set_state(OFFSET_KEY, str(new_offset))
        return {"answered": answered, "ignored": ignored}

    def _reply(self, command: str, args: str, repo: JobRepository, prefs: Preferences) -> str:
        if command == "start":
            return START_TEXT
        if command == "latest":
            limit = min(int(args), 20) if args.strip().isdigit() and int(args) > 0 else 10
            return format_latest([to_view(j) for j in repo.latest_jobs(limit)])
        if command == "stats":
            return format_stats(repo.stats())
        if command == "preferences":
            return format_preferences(prefs)
        return HELP_TEXT
