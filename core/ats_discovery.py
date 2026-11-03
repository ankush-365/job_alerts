"""Find the Greenhouse/Lever/Ashby board of a company first seen on YC, and confirm it is theirs.

A board slug can belong to a different company with a similar name, so a board is only
accepted with positive evidence: the same opening appears on YC and on the board; or the
board's job pages are hosted on the company's website domain; or the board's listings
mention the company's website domain (as recorded by YC) and mostly name the company.
A company's name alone is not enough here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from collectors.ats_collector import ENDPOINTS, AtsBoard, fetch_board
from collectors.base_collector import FetchError, HttpFetcher
from core.normalizer import normalize_company, normalize_title
from core.scam_signals import host_of
from core.verifier import BOARD_NAME_MIN_SHARE, name_mentioned

logger = logging.getLogger(__name__)

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")  # only these ever reach a URL
_YC_COMPANY_PATH = re.compile(r"ycombinator\.com/companies/([a-z0-9-]+)/", re.I)


@dataclass
class DiscoveryResult:
    board: AtsBoard | None
    evidence: str
    tried: list[str] = field(default_factory=list)
    requests: int = 0


def _cites_domain(text: str, domain: str) -> bool:
    """Whole-domain mention: 'legionhealth.com' does not count as a mention of 'health.com'."""
    return re.search(r"(?<![a-z0-9.-])(?:www\.)?" + re.escape(domain) + r"(?![a-z0-9-])", text.lower()) is not None


def yc_slug_from_url(job_url: str) -> str | None:
    m = _YC_COMPANY_PATH.search(job_url)
    return m.group(1).lower() if m else None


def candidate_slugs(company_name: str, yc_slug: str | None, website: str | None) -> list[str]:
    raw = [yc_slug, (yc_slug or "").replace("-", ""), normalize_company(company_name).replace(" ", ""),
           normalize_company(company_name).replace(" ", "-")]
    host = host_of(website) if website else None
    if host:
        raw.append(host.split(".")[0])
    slugs: list[str] = []
    for s in raw:
        if s and _SLUG.match(s) and s not in slugs:
            slugs.append(s)
    return slugs


def discover_board(
    fetcher: HttpFetcher, company_name: str, *, yc_slug: str | None, website: str | None, known_titles: list[str]
) -> DiscoveryResult:
    before = fetcher.requests_made
    titles = {normalize_title(t) for t in known_titles if t}
    company_host = host_of(website) if website else None
    result = DiscoveryResult(board=None, evidence="No Greenhouse/Lever/Ashby board found.")
    unconfirmed: list[str] = []
    for slug in candidate_slugs(company_name, yc_slug, website):
        for ats in ENDPOINTS:
            board = AtsBoard(ats, slug, company_name=company_name, website=website)
            result.tried.append(f"{ats}:{slug}")
            try:
                records = fetch_board(fetcher, board)
            except FetchError as exc:
                if exc.status_code != 404:
                    logger.warning("Board discovery %s:%s failed: %s", ats, slug, exc)
                continue
            except Exception as exc:  # noqa: BLE001 - malformed board data; try the next candidate
                logger.warning("Board discovery %s:%s unreadable: %s", ats, slug, exc)
                continue
            shared = sorted({r.title for r in records if normalize_title(r.title) in titles})
            hosted = company_host and any(
                (h := host_of(r.job_url)) and (h == company_host or h.endswith("." + company_host)) for r in records
            )
            described = [r.description for r in records if r.description]
            site_cited = bool(company_host) and any(_cites_domain(d, company_host) for d in described)
            named_share = sum(name_mentioned(company_name, d) for d in described) / len(described) if described else 0
            if shared:
                result.board = board
                result.evidence = f"Same opening '{shared[0]}' is listed on YC and on the {ats}:{slug} board."
            elif hosted:
                result.board = board
                result.evidence = f"The {ats}:{slug} board's job pages are hosted on {company_host}."
            elif site_cited and named_share >= BOARD_NAME_MIN_SHARE:
                result.board = board
                result.evidence = (f"The {ats}:{slug} board's listings cite the company's website ({company_host}) "
                                   f"and name '{company_name}' in {named_share:.0%} of them.")
            else:
                unconfirmed.append(f"{ats}:{slug} ({len(records)} jobs)")
                continue
            result.requests = fetcher.requests_made - before
            return result
    if unconfirmed:
        result.evidence = ("Found board(s) with no matching opening and no link to the company's website, so not "
                           f"accepted as theirs: {', '.join(unconfirmed)}.")
    result.requests = fetcher.requests_made - before
    return result
