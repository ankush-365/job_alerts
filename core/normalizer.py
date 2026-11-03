"""Canonical forms used for storage keys and (in Phase 4) duplicate detection."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that only track where a click came from; dropping them does not change the page.
_TRACKING_PARAMS = re.compile(r"^(utm_.*|gh_src|ref|referrer|source|src|lever-source|lever-origin|fbclid|gclid)$", re.I)

_COMPANY_SUFFIXES = re.compile(
    r"\b(inc|incorporated|llc|ltd|limited|pvt|private|corp|corporation|co|company|gmbh|technologies|labs)\b\.?", re.I
)


def canonicalize_url(url: str) -> str:
    """Lowercase scheme/host, drop fragments, tracking params and trailing slashes; sort the rest."""
    parts = urlsplit(url.strip())
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING_PARAMS.match(k))
    path = parts.path.rstrip("/") or "/"
    host = parts.netloc.lower().removeprefix("www.")
    return urlunsplit((parts.scheme.lower() or "https", host, path, urlencode(query), ""))


def _ascii_words(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9+#]+", " ", text.lower()).strip()


def normalize_company(name: str) -> str:
    """'Acme Technologies Pvt. Ltd.' -> 'acme'. Used as a matching key, never displayed."""
    words = _ascii_words(_COMPANY_SUFFIXES.sub(" ", name))
    return re.sub(r"\s+", " ", words) or _ascii_words(name)


def normalize_title(title: str) -> str:
    """'Sr. ML Engineer (Remote)' -> 'senior ml engineer'. Used as a matching key."""
    text = re.sub(r"\([^)]*\)", " ", title)  # drop parentheticals such as (Remote), (m/f/d)
    text = _ascii_words(text)
    text = re.sub(r"\bsr\b", "senior", text)
    text = re.sub(r"\bjr\b", "junior", text)
    return re.sub(r"\s+", " ", text).strip()


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    host = urlsplit(url if "//" in url else f"https://{url}").hostname
    return host.removeprefix("www.") if host else None
