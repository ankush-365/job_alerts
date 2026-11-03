"""Company and job credibility screening: VERIFIED / NEEDS_REVIEW / SUSPICIOUS, with evidence.

What each status means here:
  VERIFIED      The opening is on the company's own job board (Greenhouse/Lever/Ashby) and that
                board is confirmed to belong to the company, with no fraud warnings.
  SUSPICIOUS    At least one strong fraud signal (asks for money, apply via WhatsApp/Telegram).
  NEEDS_REVIEW  Everything else: not enough evidence either way. This is not an accusation;
                e.g. most YC listings land here until found on the company's own board.

These are heuristics over public data. They cannot prove a job is real or fake; every
result keeps the evidence it was based on so the user can judge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from core.scam_signals import (
    OPAQUE_LINK_HOSTS,
    RECOGNIZED_ATS_HOSTS,
    SUSPICIOUS_LINK_HOSTS,
    host_matches,
    host_of,
    text_signals,
)

VERIFIED = "VERIFIED"
NEEDS_REVIEW = "NEEDS_REVIEW"
SUSPICIOUS = "SUSPICIOUS"

ATS_SOURCES = {"greenhouse", "lever", "ashby"}
ATS_LABELS = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby"}
_ATS_HOST = re.compile(r"(greenhouse\.io|lever\.co|ashbyhq\.com)$")
BOARD_NAME_MIN_SHARE = 0.5  # share of a board's listings that must name the company


@dataclass(frozen=True)
class Evidence:
    check: str
    outcome: str  # pass | fail | warn | info
    detail: str

    def as_dict(self) -> dict:
        return {"check": self.check, "outcome": self.outcome, "detail": self.detail}


@dataclass(frozen=True)
class SourceInfo:
    source: str
    job_url: str
    ats_board: str | None = None


@dataclass
class VerificationInput:
    title: str
    company_name: str
    description: str
    apply_url: str | None
    sources: list[SourceInfo]
    company_website: str | None = None
    # ats_board -> Evidence confirming (outcome "pass") or not confirming who owns the board
    board_identity: dict[str, Evidence] = field(default_factory=dict)
    company_board: str | None = None  # the company's confirmed own board, if known
    expired_at: datetime | None = None


@dataclass
class VerificationResult:
    status: str
    summary: str
    evidence: list[Evidence]

    def as_dict(self) -> dict:
        return {"status": self.status, "summary": self.summary, "evidence": [e.as_dict() for e in self.evidence]}


# --- company / board identity ------------------------------------------------

def _name_forms(company_name: str) -> set[str]:
    """'Scale AI' -> {'Scale AI', 'SCALE AI', 'Scale', 'SCALE'}; case-sensitive so the word 'writer' isn't 'Writer'."""
    forms = {company_name.strip()}
    core = re.sub(r"[\s,]+(AI|Labs|Inc\.?|Technologies|Pvt\.? Ltd\.?|Ltd\.?)$", "", company_name.strip())
    if len(core) >= 3:
        forms.add(core)
    return forms | {f.upper() for f in forms}


def name_mentioned(company_name: str, text: str) -> bool:
    return any(re.search(r"(?<![A-Za-z0-9])" + re.escape(form) + r"(?![A-Za-z0-9])", text) for form in _name_forms(company_name))


def board_identity_evidence(board: str, company_name: str, listings: list[tuple[str, str]]) -> Evidence:
    """Does this ATS board belong to `company_name`? `listings` = (job_url, description) of the board's jobs."""
    if not listings:
        return Evidence("board_identity", "warn", f"No listings available to confirm who owns the {board} board.")
    own_domain = sorted({h for url, _ in listings if (h := host_of(url)) and not _ATS_HOST.search(h)})
    if own_domain:
        return Evidence("board_identity", "pass",
                        f"Board listings are hosted on the company's own site ({', '.join(own_domain[:2])}).")
    described = [d for _, d in listings if d]
    named = sum(name_mentioned(company_name, d) for d in described)
    if described and named / len(described) >= BOARD_NAME_MIN_SHARE:
        return Evidence("board_identity", "pass",
                        f"'{company_name}' is named in {named} of {len(described)} listings on the {board} board.")
    return Evidence("board_identity", "warn",
                    f"Only {named} of {len(described)} listings on the {board} board name '{company_name}'; "
                    "board ownership not confirmed.")


# --- job checks --------------------------------------------------------------

def _possessive(name: str) -> str:
    return f"{name}'" if name.endswith("s") else f"{name}'s"


def _official_listing(inp: VerificationInput) -> tuple[list[Evidence], bool]:
    evidence: list[Evidence] = []
    confirmed = False
    ats_sources = [s for s in inp.sources if s.source in ATS_SOURCES and s.ats_board]
    for src in ats_sources:
        label = ATS_LABELS.get(src.source, src.source)
        identity = inp.board_identity.get(src.ats_board)
        if identity is not None and identity.outcome == "pass":
            confirmed = True
            evidence.append(Evidence("official_listing", "pass",
                                     f"Listed on {_possessive(inp.company_name)} own {label} job board ({src.job_url}). {identity.detail}"))
        else:
            reason = identity.detail if identity else "Board ownership not checked yet."
            evidence.append(Evidence("official_listing", "warn", f"Listed on a {label} board ({src.ats_board}). {reason}"))
    board = inp.company_board
    if board and inp.board_identity.get(board, Evidence("", "", "")).outcome == "pass" and not any(
        s.ats_board == board for s in ats_sources
    ):
        evidence.append(Evidence("official_listing", "warn",
                                 f"The company's own job board ({board}) does not list this opening; it may be "
                                 "closed or posted only here."))
    elif not ats_sources:
        evidence.append(Evidence("official_listing", "info",
                                 "Not found on the company's own job board (Greenhouse/Lever/Ashby)."))
    if any(s.source == "yc" for s in inp.sources):
        evidence.append(Evidence("company_identity", "pass",
                                 "Posted on Y Combinator's Work at a Startup, which lists YC-funded companies."))
    return evidence, confirmed


def _company_website(inp: VerificationInput) -> Evidence:
    host = host_of(inp.company_website) if inp.company_website else None
    if host:
        return Evidence("company_website", "pass", f"Official website on record: {host}.")
    return Evidence("company_website", "info", "No official website on record (not a red flag by itself).")


def _apply_link(inp: VerificationInput) -> Evidence:
    url = (inp.apply_url or "").strip()
    if not url:
        return Evidence("apply_link", "info", "No application link given.")
    if url.lower().startswith("mailto:"):
        return Evidence("apply_link", "warn", f"Applications go to an email address ({url[7:][:80]}).")
    host = host_of(url)
    if host is None or not url.lower().startswith(("https://", "http://")):
        return Evidence("apply_link", "warn", "Application link is not a valid web address.")
    if domain := host_matches(host, SUSPICIOUS_LINK_HOSTS):
        return Evidence("apply_link", "fail", f"Application link goes to a messaging app ({domain}).")
    if domain := host_matches(host, OPAQUE_LINK_HOSTS):
        return Evidence("apply_link", "warn", f"Application link hides its destination ({domain}).")
    if not url.lower().startswith("https://"):
        return Evidence("apply_link", "warn", f"Application link is not encrypted (http) on {host}.")
    if domain := host_matches(host, RECOGNIZED_ATS_HOSTS):
        return Evidence("apply_link", "pass", f"Application link is on a recognized hiring platform ({domain}).")
    company_host = host_of(inp.company_website) if inp.company_website else None
    if company_host and (host == company_host or host.endswith("." + company_host)):
        return Evidence("apply_link", "pass", f"Application link is on the company's own domain ({host}).")
    if any(host_of(s.job_url) == host for s in inp.sources if s.source in ATS_SOURCES):
        return Evidence("apply_link", "pass", f"Application link ({host}) was published by the company's job board itself.")
    return Evidence("apply_link", "warn", f"Application link is on an unrecognized domain ({host}).")


def verify(inp: VerificationInput) -> VerificationResult:
    evidence, official = _official_listing(inp)
    evidence.append(_company_website(inp))
    link = _apply_link(inp)
    evidence.append(link)

    signals = text_signals(inp.description)
    for signal in signals:
        evidence.append(Evidence("description", "fail" if signal.strong else "warn",
                                 f"{signal.label[0].upper()}{signal.label[1:]}: \"{signal.excerpt}\""))
    if not signals and inp.description:
        evidence.append(Evidence("description", "pass", "No payment requests or other common fraud phrases found."))

    if len(inp.sources) > 1:
        names = ", ".join(sorted({s.source for s in inp.sources}))
        evidence.append(Evidence("duplicates", "info", f"Same opening found on {len(inp.sources)} sources ({names})."))
    if inp.expired_at is not None:
        evidence.append(Evidence("expiry", "warn",
                                 f"No longer listed on the company's job board since {inp.expired_at:%Y-%m-%d}."))

    failures = [e for e in evidence if e.outcome == "fail"]
    warnings = [e for e in evidence if e.outcome == "warn" and e.check in {"description", "apply_link"}]
    if failures:
        status, summary = SUSPICIOUS, failures[0].detail
    elif official and not warnings:
        status = VERIFIED
        summary = next(e.detail for e in evidence if e.check == "official_listing" and e.outcome == "pass").split(" (")[0] + "."
    else:
        status = NEEDS_REVIEW
        if warnings:
            summary = warnings[0].detail
        elif any(e.check == "official_listing" and e.outcome == "warn" for e in evidence):
            summary = next(e.detail for e in evidence if e.check == "official_listing" and e.outcome == "warn")
        elif any(e.check == "company_identity" for e in evidence):
            summary = "YC-funded company; this opening is not yet confirmed on the company's own job board."
        else:
            summary = "Could not confirm this listing on a job board owned by the company."
    return VerificationResult(status, summary, evidence)
