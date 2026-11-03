"""Decide whether a newly seen listing is the same opening as a stored job.

Matching order (first hit wins):
  1. Same canonical URL on any source.
  2. Same company (normalized name or website domain) + same normalized title
     + compatible location, seen recently. When both sides have descriptions,
     they must also be textually similar, so two different openings that share a
     generic title ("Software Engineer") at one company are not merged.
"""

from __future__ import annotations

import re

_LOCATION_NOISE = {
    "remote", "hybrid", "onsite", "on", "site", "office", "in", "us", "usa", "the", "and", "or", "any",
    "location", "locations", "multiple", "of", "within", "only",
}
# Same city, different spellings.
_CITY_ALIASES = {"bangalore": "bengaluru", "gurgaon": "gurugram", "bombay": "mumbai", "sf": "san francisco"}

MIN_DESCRIPTION_SIMILARITY = 0.5


def _location_tokens(location: str | None) -> set[str]:
    text = (location or "").lower()
    for alias, canonical in _CITY_ALIASES.items():
        text = re.sub(rf"\b{alias}\b", canonical, text)
    return {w for w in re.findall(r"[a-z]{3,}", text) if w not in _LOCATION_NOISE}


def locations_compatible(a: str | None, b: str | None) -> bool:
    """Unknown on either side is compatible; otherwise the places must overlap."""
    ta, tb = _location_tokens(a), _location_tokens(b)
    if not ta or not tb:
        return True
    return bool(ta & tb)


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9+#]{3,}", text.lower()))


def description_similarity(a: str, b: str) -> float:
    """Jaccard similarity of word sets: 1.0 identical vocabulary, 0.0 disjoint."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def descriptions_compatible(a: str | None, b: str | None) -> bool:
    """Missing text on either side can't disprove a match."""
    if not a or not b:
        return True
    return description_similarity(a, b) >= MIN_DESCRIPTION_SIMILARITY
