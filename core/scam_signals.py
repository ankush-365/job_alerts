"""Text and link signals associated with fraudulent job postings.

These are indicators, not proof. Strong signals (asking candidates for money, applying
through a messaging app) make a listing SUSPICIOUS; weaker ones only add warnings.
Sentences that *deny* a signal ("we will never ask you for a fee") are ignored, because
legitimate employers often include exactly such anti-fraud notices.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from core.filters import sentence_around

_NEGATION = re.compile(
    r"\b(never|not|no|don't|do not|doesn't|does not|won't|will not|without|free of charge|at no cost|beware|fraud|"
    r"scam|phishing|impersonat\w*|fake|report)\b",
    re.I,
)

# (label, strong?, pattern). Signals in _NEGATION_IS_PART_OF_SIGNAL contain "no"/"without" themselves.
_TEXT_SIGNALS: list[tuple[str, bool, re.Pattern]] = [
    ("asks candidates to pay a fee or deposit", True, re.compile(
        r"\b(registration|application|processing|training|joining|onboarding|security|refundable|kit|"
        r"certification|verification)\s+(fee|fees|deposit|charges?|amount)\b"
        r"|\bpay\s+(?:a|an|the)?\s*(?:one[\s-]time\s+)?(fee|deposit|amount)\b"
        r"|\b(deposit|fee)\s+(?:of\s+)?(?:rs\.?|inr|₹|\$)\s?\d", re.I)),
    # Needs an actual instruction to use the app (a number/handle, "send your CV on WhatsApp", a wa.me link);
    # merely listing WhatsApp among contact channels or product features is normal.
    ("asks candidates to apply or chat via WhatsApp/Telegram", True, re.compile(
        r"\b(whats\s?app|telegram)\b[^.\n]{0,40}?(\+?\d[\d\s()-]{8,}\d|@[a-z0-9_]{4,})"
        r"|\b(send|share|forward|submit|drop)\s+(?:us\s+)?(?:your\s+)?(cv|resume|details|biodata)\b[^.\n]{0,40}\b(whats\s?app|telegram)\b"
        r"|\b(whats\s?app|telegram)\s+(?:us\s+)?(?:your\s+)?(cv|resume|details)\b"
        r"|\bjoin\s+(?:our|the)\s+telegram\s+(group|channel)\b"
        r"|\bwa\.me/|\bt\.me/", re.I)),
    ("guarantees a job or placement", False, re.compile(
        r"\b(guaranteed|100%)\s+(job|placement|selection|offer)\b|\bjob\s+guarantee\b", re.I)),
    ("unrealistic earnings claim", False, re.compile(
        r"\bearn\s+(?:up\s+to\s+)?(?:rs\.?|inr|₹|\$)\s?[\d,]+\s*(?:k\b)?\s*(?:per|a|/)\s*(day|week)\b"
        r"|\b(no\s+experience|work\s+from\s+home)\b[^.\n]{0,40}\b(high|huge|unlimited)\s+(income|salary|earnings)\b",
        re.I)),
    ("hiring without any interview", False, re.compile(r"\b(no|without)\s+(an?\s+)?interviews?\s+(required|needed|process)\b|\bdirect\s+joining\b", re.I)),
]
_NEGATION_IS_PART_OF_SIGNAL = {"hiring without any interview"}

_PERSONAL_EMAIL = re.compile(
    r"\b[\w.+-]+@(gmail|yahoo|ymail|hotmail|outlook|live|rediffmail|aol|proton|protonmail|icloud)\.(com|in|co\.in|me)\b",
    re.I,
)

# Application links on these hosts are normal for real companies.
RECOGNIZED_ATS_HOSTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "workday.com", "smartrecruiters.com",
    "icims.com", "jobvite.com", "bamboohr.com", "workable.com", "recruitee.com", "teamtailor.com",
    "breezy.hr", "jazzhr.com", "applytojob.com", "personio.de", "personio.com", "rippling.com", "dover.com",
    "keka.com", "darwinbox.in", "zohorecruit.com", "zohorecruit.in", "freshteam.com", "successfactors.com",
    "taleo.net", "oraclecloud.com", "ycombinator.com", "workatastartup.com", "wellfound.com", "linkedin.com",
)
# Application links on these hosts are a strong fraud signal for an employer listing.
SUSPICIOUS_LINK_HOSTS = (
    "wa.me", "whatsapp.com", "t.me", "telegram.me", "telegram.org",
)
# Link shorteners and generic form builders hide the destination; worth a warning, not proof.
OPAQUE_LINK_HOSTS = (
    "bit.ly", "tinyurl.com", "goo.gl", "cutt.ly", "rb.gy", "is.gd", "forms.gle", "docs.google.com",
    "typeform.com", "jotform.com", "forms.office.com",
)


@dataclass(frozen=True)
class Signal:
    label: str
    strong: bool
    excerpt: str


def text_signals(text: str) -> list[Signal]:
    """Fraud indicators in a description, skipping sentences that negate them."""
    found: list[Signal] = []
    for label, strong, pattern in _TEXT_SIGNALS:
        for match in pattern.finditer(text):
            sentence = sentence_around(text, match.start(), match.end())
            if label not in _NEGATION_IS_PART_OF_SIGNAL and _NEGATION.search(sentence):
                continue
            found.append(Signal(label, strong, sentence.strip()[:200]))
            break
    emails = {m.group(0).lower() for m in _PERSONAL_EMAIL.finditer(text)}
    if emails:
        found.append(Signal("contact address is a personal email account", False, ", ".join(sorted(emails))[:200]))
    return found


def host_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host.lower().removeprefix("www.") if host else None


def host_matches(host: str, domains: tuple[str, ...] | list[str]) -> str | None:
    """The listed domain that `host` equals or is a subdomain of."""
    for domain in domains:
        if host == domain or host.endswith("." + domain):
            return domain
    return None
