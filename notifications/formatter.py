"""Telegram messages (HTML parse mode). All job data is untrusted: every value is escaped and
only http(s) links are ever turned into anchors."""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime

from notifications.telegram_bot import MAX_MESSAGE_LENGTH

SOURCE_LABELS = {"yc": "Y Combinator", "ashby": "Company board (Ashby)", "greenhouse": "Company board (Greenhouse)",
                 "lever": "Company board (Lever)"}
VERIFICATION_LABELS = {"VERIFIED": "✅ VERIFIED", "NEEDS_REVIEW": "🟡 NEEDS REVIEW", "SUSPICIOUS": "⛔ SUSPICIOUS"}
TIER_LABELS = {"high": "High", "moderate": "Moderate", "low": "Low"}


@dataclass
class JobView:
    """Plain data for one job, decoupled from the database models."""

    id: int
    title: str
    company: str
    location: str | None
    experience: str
    employment: str | None
    salary: str | None
    score: int | None
    tier: str | None
    verification: str | None
    verification_summary: str | None
    matched_skills: list[str]
    missing_skills: list[str]
    reason: str | None
    apply_url: str | None
    sources: list[tuple[str, str]]  # (source, url)
    flags: list[str]
    first_seen_at: datetime | None = None


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def link(url: str | None, text: str) -> str:
    if not url or not url.lower().startswith(("https://", "http://")):
        return esc(text)
    return f'<a href="{html.escape(url, quote=True)}">{esc(text)}</a>'


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def format_alert(job: JobView) -> str:
    lines = ["🆕 <b>NEW AI/ML JOB FOUND</b>", ""]
    lines.append(f"<b>Position:</b> {esc(_cut(job.title, 150))}")
    lines.append(f"<b>Company:</b> {esc(_cut(job.company, 100))}")
    lines.append(f"<b>Location:</b> {esc(_cut(job.location or 'Not stated', 150))}")
    lines.append(f"<b>Experience:</b> {esc(job.experience)}")
    if job.employment:
        lines.append(f"<b>Employment:</b> {esc(job.employment)}")
    if job.salary:
        lines.append(f"<b>Salary:</b> {esc(_cut(job.salary, 120))}")
    lines.append("")
    if job.score is not None:
        tier = TIER_LABELS.get(job.tier or "", "")
        lines.append(f"<b>Relevance:</b> {job.score}/100" + (f" ({tier})" if tier else ""))
    if job.verification:
        label = VERIFICATION_LABELS.get(job.verification, job.verification)
        lines.append(f"<b>Verification:</b> {label}")
        if job.verification_summary:
            lines.append(f"<i>{esc(_cut(job.verification_summary, 250))}</i>")
    if job.matched_skills:
        lines += ["", "<b>Matched skills:</b>", esc(", ".join(job.matched_skills[:12]))]
    if job.missing_skills:
        lines.append(f"<b>Not in your profile:</b> {esc(', '.join(job.missing_skills[:8]))}")
    if job.reason:
        lines += ["", "<b>Why this job matches:</b>", esc(_cut(job.reason, 600))]
    notes = [f for f in job.flags if f.startswith(("targets ", "mentions ", "internship", "posted ", "remote,"))]
    if notes:
        lines.append(f"<i>{esc(_cut('; '.join(notes), 200))}</i>")
    lines.append("")
    lines.append(f"<b>Apply:</b> {link(job.apply_url, 'Application link')}")
    sources = ", ".join(link(url, SOURCE_LABELS.get(src, src)) for src, url in job.sources)
    lines.append(f"<b>Source:</b> {sources}")
    return _fit("\n".join(lines))


def format_digest_line(job: JobView) -> str:
    score = f"{job.score}" if job.score is not None else "–"
    mark = {"VERIFIED": "✅", "NEEDS_REVIEW": "🟡"}.get(job.verification or "", "")
    return (f"• <b>{score}</b> {mark} {link(job.apply_url or (job.sources[0][1] if job.sources else None), _cut(job.title, 80))}"
            f" — {esc(_cut(job.company, 40))}, {esc(_cut(job.location or 'location not stated', 50))}")


def format_digest(day: str, jobs: list[JobView], totals: dict[str, int]) -> list[str]:
    """One or more messages (split to respect Telegram's length limit). Only jobs never sent before."""
    header = [f"📋 <b>Daily AI/ML job digest — {esc(day)}</b>", "",
              f"New today: {totals.get('new', 0)} listings checked, {totals.get('relevant', 0)} relevant; "
              f"{totals.get('alerted', 0)} already sent to you as alerts (not repeated here)."]
    if jobs:
        blocks = ["<b>Worth a look</b> (abroad with visa unknown, or below the alert score)\n"
                  + "\n".join(format_digest_line(j) for j in jobs)]
    else:
        blocks = ["No other new relevant jobs today."]
    return _split("\n".join(header), blocks)


def format_latest(jobs: list[JobView]) -> str:
    if not jobs:
        return "No relevant jobs found yet."
    return _fit("🗂 <b>Latest relevant jobs</b>\n\n" + "\n".join(format_digest_line(j) for j in jobs))


def format_stats(stats: dict) -> str:
    lines = ["📊 <b>AI Job Radar stats</b>", ""]
    labels = [("discovered", "Jobs discovered"), ("relevant", "Passed filters"), ("review", "Needs a look (abroad)"),
              ("verified", "Verified"), ("needs_review", "Needs review"), ("suspicious", "Suspicious"),
              ("notified", "Alerts sent")]
    for key, label in labels:
        lines.append(f"{label}: <b>{stats.get(key, 0)}</b>")
    if stats.get("last_run"):
        lines += ["", f"Last run: {esc(stats['last_run'])}"]
    return "\n".join(lines)


def format_preferences(prefs) -> str:
    n = prefs.notifications
    lines = ["⚙️ <b>Current preferences</b>", "",
             f"<b>Experience:</b> up to {prefs.experience.max_required_years} years required",
             f"<b>Graduation year:</b> {esc(prefs.candidate.graduation_year)}",
             f"<b>Home city:</b> {esc(prefs.candidate.home_city)}",
             f"<b>Jobs abroad:</b> {esc(prefs.location.onsite_abroad.replace('_', ' '))}",
             f"<b>Employment:</b> {esc(', '.join(prefs.employment.allowed_types))}",
             f"<b>Min salary:</b> ₹{(prefs.salary.preferred_min_inr_per_year or 0) / 1e5:.1f} LPA (undisclosed is fine)",
             f"<b>Alert threshold:</b> {n.minimum_relevance_score}/100",
             f"<b>Daily digest:</b> {'on, after ' + str(n.digest_hour) + ':00 ' + esc(n.timezone) if n.daily_digest else 'off'}",
             "",
             "<b>Top skills:</b> " + esc(", ".join(s.name for s in sorted(prefs.profile.skills, key=lambda s: -s.weight)[:12])),
             "",
             "<i>Edit config/job_preferences.yaml to change these.</i>"]
    return _fit("\n".join(lines))


HELP_TEXT = (
    "🤖 <b>AI Job Radar</b>\n\n"
    "Job alerts are sent automatically on every scheduled run (hourly); you don't need to do anything.\n\n"
    "/latest – recently found relevant jobs\n"
    "/stats – how many jobs were found, filtered, verified and sent\n"
    "/preferences – your current search preferences\n"
    "/help – this message\n\n"
    "<i>Commands are answered on the next scheduled run, so replies can take up to an hour.</i>"
)

START_TEXT = (
    "✅ <b>AI Job Radar is active for this chat.</b>\n\n"
    "New AI/ML jobs matching your profile are sent here automatically every hour, plus a daily digest.\n\n"
) + HELP_TEXT.split("\n\n", 1)[1]


def _fit(text: str) -> str:
    return text if len(text) <= MAX_MESSAGE_LENGTH else text[: MAX_MESSAGE_LENGTH - 20].rsplit("\n", 1)[0] + "\n…"


def _split(header: str, blocks: list[str]) -> list[str]:
    """Pack header + blocks into messages under the limit. Every line is self-contained HTML,
    so splitting between lines never breaks a tag."""
    lines = [header]
    for block in blocks:
        lines += ["", *block.split("\n")]
    messages: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > MAX_MESSAGE_LENGTH and current:
            messages.append(current)
            current = line
        else:
            current = candidate
    messages.append(current)
    return messages
