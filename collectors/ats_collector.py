"""Company job boards hosted on Greenhouse, Lever and Ashby.

These are the companies' own official listings, published through documented
public read APIs that need no authentication (checked 2026-11-03):
  Greenhouse: https://developers.greenhouse.io/job-board.html
  Lever:      https://github.com/lever/postings-api
  Ashby:      https://developers.ashbyhq.com/docs/public-job-posting-api
An unknown board slug returns HTTP 404, which `discover_board` uses to find a
company's board from its name.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import datetime, timezone

from collectors.base_collector import BaseCollector, FetchError, HttpFetcher
from core.models import JobRecord, WorkArrangement
from core.text import clean_whitespace, html_to_text

logger = logging.getLogger(__name__)

ENDPOINTS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
}
PARAMS = {
    "greenhouse": {"content": "true"},
    "lever": {"mode": "json"},
    "ashby": {"includeCompensation": "true"},
}
# Smallest responses that still prove a board exists.
DISCOVERY_PARAMS = {"greenhouse": None, "lever": {"mode": "json", "limit": "1"}, "ashby": None}


def _iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _arrangement(value: str | None, *, is_remote: bool | None = None, location: str | None = None) -> WorkArrangement:
    v = (value or "").lower().replace("-", "").replace("_", "")
    if v == "remote" or is_remote:
        return WorkArrangement.REMOTE
    if v == "hybrid":
        return WorkArrangement.HYBRID
    if v in {"onsite", "inoffice"}:
        return WorkArrangement.ONSITE
    if location and "remote" in location.lower():
        return WorkArrangement.REMOTE
    return WorkArrangement.UNKNOWN


def parse_greenhouse(data: dict, slug: str, company_name: str) -> list[JobRecord]:
    records = []
    for job in data.get("jobs") or []:
        location = (job.get("location") or {}).get("name") or None
        records.append(
            JobRecord(
                source="greenhouse",
                source_job_id=str(job["id"]),
                title=job.get("title", "").strip(),
                company_name=job.get("company_name") or company_name,
                description=html_to_text(job.get("content")),
                location=location,
                work_arrangement=_arrangement(None, location=location),
                job_url=job["absolute_url"],
                apply_url=job["absolute_url"],
                ats_board=f"greenhouse:{slug}",
                posted_at=_iso(job.get("first_published") or job.get("updated_at")),
            )
        )
    return records


def parse_lever(data: list, slug: str, company_name: str) -> list[JobRecord]:
    records = []
    for job in data or []:
        categories = job.get("categories") or {}
        location = categories.get("location") or None
        parts = [job.get("descriptionPlain") or ""]
        for section in job.get("lists") or []:
            parts.append(f"{section.get('text', '')}\n{html_to_text(section.get('content'))}")
        parts.append(job.get("additionalPlain") or "")
        salary = job.get("salaryRange") or {}
        created_ms = job.get("createdAt")
        records.append(
            JobRecord(
                source="lever",
                source_job_id=str(job["id"]),
                title=job.get("text", "").strip(),
                company_name=company_name,
                description=clean_whitespace("\n\n".join(p for p in parts if p)),
                location=location,
                work_arrangement=_arrangement(job.get("workplaceType"), location=location),
                employment_type=categories.get("commitment") or None,
                salary_min=salary.get("min"),
                salary_max=salary.get("max"),
                salary_currency=salary.get("currency"),
                salary_period=salary.get("interval"),
                job_url=job["hostedUrl"],
                apply_url=job.get("applyUrl") or job["hostedUrl"],
                ats_board=f"lever:{slug}",
                posted_at=datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc) if created_ms else None,
            )
        )
    return records


def parse_ashby(data: dict, slug: str, company_name: str) -> list[JobRecord]:
    records = []
    for job in data.get("jobs") or []:
        if job.get("isListed") is False:
            continue
        location = job.get("location") or None
        secondary = [s.get("location") for s in job.get("secondaryLocations") or [] if s.get("location")]
        if location and secondary:
            location = " / ".join([location, *secondary])
        compensation = job.get("compensation") or {}
        records.append(
            JobRecord(
                source="ashby",
                source_job_id=str(job["id"]),
                title=job.get("title", "").strip(),
                company_name=company_name,
                description=clean_whitespace(job.get("descriptionPlain") or html_to_text(job.get("descriptionHtml"))),
                location=location,
                work_arrangement=_arrangement(job.get("workplaceType"), is_remote=job.get("isRemote"), location=location),
                employment_type=job.get("employmentType") or None,
                salary_text=compensation.get("compensationTierSummary") or None,
                job_url=job["jobUrl"],
                apply_url=job.get("applyUrl") or job["jobUrl"],
                ats_board=f"ashby:{slug}",
                posted_at=_iso(job.get("publishedAt")),
            )
        )
    return records


PARSERS: dict[str, Callable[..., list[JobRecord]]] = {
    "greenhouse": parse_greenhouse,
    "lever": parse_lever,
    "ashby": parse_ashby,
}


class AtsBoard:
    def __init__(self, ats: str, slug: str, company_name: str | None = None, website: str | None = None):
        if ats not in ENDPOINTS:
            raise ValueError(f"unsupported ATS {ats!r}; expected one of {sorted(ENDPOINTS)}")
        self.ats = ats
        self.slug = slug
        self.company_name = company_name or slug
        self.website = website

    def __repr__(self) -> str:
        return f"AtsBoard({self.ats}:{self.slug})"


def fetch_board(fetcher: HttpFetcher, board: AtsBoard) -> list[JobRecord]:
    data = fetcher.get_json(ENDPOINTS[board.ats].format(slug=board.slug), params=PARAMS[board.ats])
    records = PARSERS[board.ats](data, board.slug, board.company_name)
    if board.website:
        for record in records:
            record.company_website = board.website
    return records


def discover_board(fetcher: HttpFetcher, slug: str) -> str | None:
    """Return the first ATS that hosts a board under `slug`, or None. 404 means 'not here'."""
    for ats in ENDPOINTS:
        try:
            fetcher.get(ENDPOINTS[ats].format(slug=slug), params=DISCOVERY_PARAMS[ats])
            return ats
        except FetchError as exc:
            if exc.status_code != 404:
                logger.warning("discover_board %s on %s: %s", slug, ats, exc)
    return None


class AtsCollector(BaseCollector):
    """Polls a configured list of company boards. One failing board does not stop the others."""

    source = "ats"

    def __init__(self, fetcher: HttpFetcher, boards: Iterable[AtsBoard]):
        super().__init__(fetcher)
        self.boards = list(boards)

    def _interleaved(self) -> list[AtsBoard]:
        """Alternate providers (ashby, greenhouse, lever, ashby, ...) so per-host waits overlap."""
        queues: dict[str, list[AtsBoard]] = {}
        for board in self.boards:
            queues.setdefault(board.ats, []).append(board)
        order: list[AtsBoard] = []
        while any(queues.values()):
            for queue in queues.values():
                if queue:
                    order.append(queue.pop(0))
        return order

    def collect(self) -> list[JobRecord]:
        records: list[JobRecord] = []
        failed = 0
        for board in self._interleaved():
            try:
                records.extend(fetch_board(self.fetcher, board))
                self.complete_boards.append(f"{board.ats}:{board.slug}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                self.errors.append(f"{board.ats}:{board.slug}: {exc}")
        if self.boards and failed == len(self.boards):
            raise RuntimeError("all ATS boards failed: " + "; ".join(self.errors[:3]))
        return records
