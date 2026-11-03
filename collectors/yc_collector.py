"""Y Combinator job listings from public ycombinator.com/jobs pages.

Access method (checked 2026-11-03): role pages such as /jobs/role/software-engineer/india
are public, allowed by robots.txt, and embed the listing data as JSON in a
`data-page` attribute. Each page holds at most ~40 postings, so several role/location
pages are polled. Job detail pages add the full description, an exact posting
date (schema.org JobPosting) and the company website. Applying requires a YC account.
"""

from __future__ import annotations

import html
import json
import logging
import re
from collections.abc import Iterable
from datetime import datetime
from urllib.parse import urljoin

from collectors.base_collector import BaseCollector, HttpFetcher
from core.models import JobRecord, WorkArrangement
from core.text import clean_whitespace

logger = logging.getLogger(__name__)

BASE_URL = "https://www.ycombinator.com"
_DATA_PAGE_RE = re.compile(r'data-page="([^"]+)"')
_LD_JSON_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def extract_page_props(page_html: str) -> dict:
    match = _DATA_PAGE_RE.search(page_html)
    if not match:
        raise ValueError("data-page JSON not found; YC page layout may have changed")
    return json.loads(html.unescape(match.group(1))).get("props", {})


def extract_job_posting_ld(page_html: str) -> dict:
    for block in _LD_JSON_RE.findall(page_html):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            return data
    return {}


def _work_arrangement(location: str | None) -> WorkArrangement:
    if location and "remote" in location.lower():
        return WorkArrangement.REMOTE
    return WorkArrangement.UNKNOWN


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def posting_to_record(posting: dict) -> JobRecord:
    """Map one entry of a role page's `jobPostings` list to a JobRecord."""
    location = posting.get("location") or None
    return JobRecord(
        source="yc",
        source_job_id=str(posting["id"]),
        title=posting.get("title", "").strip(),
        company_name=(posting.get("companyName") or "").strip(),
        location=location,
        work_arrangement=_work_arrangement(location),
        experience_text=posting.get("minExperience") or None,
        employment_type=posting.get("type") or None,
        salary_text=posting.get("salaryRange") or None,
        skills=[s for s in (posting.get("skills") or []) if isinstance(s, str)],
        visa_text=posting.get("visa") or None,
        job_url=urljoin(BASE_URL, posting["url"]),
        apply_url=posting.get("applyUrl") or None,
        details_complete=False,
    )


class YCCollector(BaseCollector):
    source = "yc"

    def __init__(
        self,
        fetcher: HttpFetcher,
        *,
        role_paths: Iterable[str],
        known_job_ids: set[str] | None = None,
        max_detail_fetches: int = 30,
    ):
        super().__init__(fetcher)
        self.role_paths = list(role_paths)
        self.known_job_ids = known_job_ids or set()
        self.max_detail_fetches = max_detail_fetches

    def collect(self) -> list[JobRecord]:
        records: dict[str, JobRecord] = {}
        pages_ok = 0
        for path in self.role_paths:
            url = urljoin(BASE_URL, path)
            try:
                props = extract_page_props(self.fetcher.get(url).text)
            except Exception as exc:  # noqa: BLE001 - one bad page should not lose the others
                self.errors.append(f"{url}: {exc}")
                continue
            pages_ok += 1
            for posting in props.get("jobPostings") or []:
                try:
                    record = posting_to_record(posting)
                except (KeyError, ValueError) as exc:
                    self.errors.append(f"{url}: skipped malformed posting: {exc}")
                    continue
                records.setdefault(record.source_job_id, record)

        if self.role_paths and pages_ok == 0:
            raise RuntimeError("all YC role pages failed: " + "; ".join(self.errors[:3]))

        new_ids = [job_id for job_id in records if job_id not in self.known_job_ids]
        for job_id in new_ids[: self.max_detail_fetches]:
            self._enrich(records[job_id])
        if len(new_ids) > self.max_detail_fetches:
            logger.info("YC: %d new jobs left without details until next run", len(new_ids) - self.max_detail_fetches)
        return list(records.values())

    def _enrich(self, record: JobRecord) -> None:
        """Add description, posting date and company website from the job detail page."""
        try:
            page = self.fetcher.get(record.job_url).text
            props = extract_page_props(page)
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"{record.job_url}: detail fetch failed: {exc}")
            return
        job = props.get("job") or {}
        company = props.get("company") or {}
        record.description = clean_whitespace(job.get("description") or "")
        record.company_website = company.get("website") or None
        record.posted_at = _parse_datetime(extract_job_posting_ld(page).get("datePosted"))
        record.details_complete = True
