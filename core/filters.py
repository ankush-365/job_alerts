"""Level 1 rule-based filtering: title, experience, location, employment type and posting age.

Each job ends up "passed", "review" (worth a look but uncertain, e.g. onsite abroad
with unknown visa sponsorship) or "rejected". Every decision is explained: `reasons`
say why a job was rejected, `review_reasons` why it needs review, and `flags` record
things worth knowing (e.g. "experience not stated"). Missing information never
rejects a job unless the preferences say so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache

from core.models import JobRecord, WorkArrangement, utcnow
from core.preferences import Preferences, TextIndex

_WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_NUM = r"\b(\d{1,2}|" + "|".join(_WORD_NUMBERS) + r")"
_YEARS = r"(?:years?|yrs?)"
_EXPERIENCE_PATTERNS = [
    re.compile(_NUM + r"\s*\+?\s*(?:-|–|—|to)\s*" + _NUM + r"\s*\+?\s*" + _YEARS, re.I),  # 2-4 years
    re.compile(r"(?:at least|minimum(?: of)?|min\.?|over)\s+" + _NUM + r"\s*\+?\s*" + _YEARS, re.I),
    re.compile(_NUM + r"\s*(?:\+|plus)\s*" + _YEARS, re.I),  # 3+ years
    re.compile(_NUM + r"\s*" + _YEARS + r"\s+(?:of\s+)?(?:[\w/-]+\s+){0,5}?(?:experience|exp)\b", re.I),
]
SOFT_CONTEXT = re.compile(r"\b(prefer|preferred|preferably|nice to have|bonus|ideally|a plus)\b", re.I)
_EXPERIENCE_WORD = re.compile(r"\bexperience|\bexp\b|\bbackground\b", re.I)
_FRESHER = re.compile(
    r"\b(new grads?|new graduates?|fresher|freshers|entry[\s-]level|no experience required|0\s*(?:-|–|to)\s*[12]\s*years?)\b",
    re.I,
)
# Explicit statements that the employer will not sponsor / needs existing work rights (any country).
_SPONSOR_NO = re.compile(
    r"(unable to sponsor|(?:not|cannot|can't|won't|will not|do not|don't|are not able to) (?:be able to )?(?:provide |offer )?"
    r"(?:visa |immigration )?sponsor|no (?:visa |immigration )?sponsorship|sponsorship (?:is )?not (?:available|provided|offered)"
    r"|(?:must|should) (?:already )?(?:have|hold|possess) (?:the |a )?(?:valid )?(?:right|authori[sz]ation|eligibility|permit) to work"
    r"|(?:must|should) be (?:legally )?(?:authori[sz]ed|eligible|permitted) to work"
    r"|citizens?(?:hip)? (?:only|required)|security clearance)",
    re.I,
)
# Explicit statements that visa sponsorship is offered.
_SPONSOR_YES = re.compile(
    r"(visa sponsorship (?:is )?(?:available|provided|offered|possible)|(?:we|will|can|happy to) (?:provide |offer )?"
    r"(?:visa |immigration )sponsorship|(?:we|will|can) sponsor (?:work )?visas?|sponsor(?:ing)? (?:work )?visas"
    r"|(?:relocation|visa) (?:and|&) (?:visa|relocation) (?:support|assistance))",
    re.I,
)
# Cheap triggers (plain substring search): the expensive patterns only run on short windows around them.
_YEARS_TRIGGER = ("year", "yr")
_SPONSOR_TRIGGER = ("sponsor", "authoriz", "authoris", "right to work", "citizen", "clearance", "eligib", "permit",
                    "relocation")
_GRAD_TRIGGER = ("graduat", "class of", "batch", "grads", "passout")

_EMPLOYMENT_TYPES = [
    ("internship", re.compile(r"intern", re.I)),
    ("part-time", re.compile(r"part[\s-]?time", re.I)),
    ("contract", re.compile(r"contract|freelance|consult", re.I)),
    ("temporary", re.compile(r"temp(?:orary)?\b", re.I)),
    ("full-time", re.compile(r"full[\s-]?time|permanent|regular", re.I)),
]
_GRAD_YEAR = re.compile(r"(?:class of|graduat\w*(?:\s+\w+){0,3}?|batch)\s*(?:of\s+|in\s+|by\s+)?(20\d\d)|(20\d\d)\s+(?:graduates?|grads|batch|passouts?)", re.I)


@dataclass
class FilterResult:
    reasons: list[str] = field(default_factory=list)
    review_reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    required_years: int | None = None
    location_class: str = "unknown"  # india | remote_ok | remote_unspecified | abroad | unknown
    sponsorship: str = "unknown"  # yes | no | unknown
    employment_type: str | None = None  # normalized: full-time | internship | contract | part-time | temporary
    posting_age_days: int | None = None
    description_ai_terms: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.reasons:
            return "rejected"
        return "review" if self.review_reasons else "passed"

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "reasons": self.reasons,
            "review_reasons": self.review_reasons,
            "flags": self.flags,
            "required_years": self.required_years,
            "location_class": self.location_class,
            "sponsorship": self.sponsorship,
            "employment_type": self.employment_type,
            "posting_age_days": self.posting_age_days,
            "description_ai_terms": self.description_ai_terms,
        }


@lru_cache(maxsize=4)
def _lower(text: str) -> str:
    return text.lower()


def _windows(text: str, trigger: tuple[str, ...], before: int, after: int) -> list[tuple[int, str]]:
    """Merged (offset, snippet) windows around each occurrence of the trigger substrings."""
    lower = _lower(text)
    hits: list[tuple[int, int]] = []
    for word in trigger:
        pos = lower.find(word)
        while pos != -1:
            hits.append((max(0, pos - before), min(len(text), pos + len(word) + after)))
            pos = lower.find(word, pos + 1)
    spans: list[list[int]] = []
    for start, end in sorted(hits):
        if spans and start <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], end)
        else:
            spans.append([start, end])
    return [(start, text[start:end]) for start, end in spans]


def _search_windows(pattern: re.Pattern, text: str, trigger: tuple[str, ...], before: int = 150, after: int = 150) -> bool:
    return any(pattern.search(snippet) for _, snippet in _windows(text, trigger, before, after))


def _num(token: str) -> int:
    return int(token) if token.isdigit() else _WORD_NUMBERS[token.lower()]


def sentence_around(text: str, start: int, end: int) -> str:
    left = max(text.rfind(".", 0, start), text.rfind("\n", 0, start)) + 1
    right_candidates = [i for i in (text.find(".", end), text.find("\n", end)) if i != -1]
    right = min(right_candidates) if right_candidates else len(text)
    return text[left:right]


def required_years_from_text(text: str, *, structured: bool = False) -> tuple[int | None, bool]:
    """Years of experience a text demands. Returns (years, soft) where soft means only 'preferred'.

    `structured` is for short source fields like YC's "3+ years", which need no "experience" context word.
    """
    if not text:
        return None, False
    fresher_hit = bool(_FRESHER.search(text))
    required: list[int] = []
    preferred: list[int] = []
    taken: list[tuple[int, int]] = []  # spans already used, so "2-4 years" is not re-read as "4 years"
    windows = [(0, text)] if structured else _windows(text, _YEARS_TRIGGER, before=60, after=120)
    for pattern in _EXPERIENCE_PATTERNS:
        for offset, snippet in windows:
            for match in pattern.finditer(snippet):
                start, end = match.start() + offset, match.end() + offset
                if any(start < e and s < end for s, e in taken):
                    continue
                taken.append((start, end))
                sentence = sentence_around(text, start, end)
                if not structured and not _EXPERIENCE_WORD.search(sentence):
                    continue
                years = _num(match.group(1))
                if years > 20:
                    continue
                (preferred if SOFT_CONTEXT.search(sentence) else required).append(years)
    if required:
        return max(required), False
    if fresher_hit:
        return 0, False
    if preferred:
        return min(preferred), True
    return None, False


def _title_check(record: JobRecord, prefs: Preferences, result: FilterResult) -> None:
    title = record.title
    t = prefs.titles
    if m := t.reject.search(title):
        result.reasons.append(f"title: '{m.group(1).lower()}' is excluded")
        return
    ai_in_title = t.ai.search(title)
    if ai_in_title:
        return
    if m := t.non_ai_engineering.search(title):
        result.reasons.append(f"title: '{m.group(1).lower()}' role without an AI/ML term")
        return
    if t.generic_engineering.search(title):
        needed = t.min_ai_terms_in_description_for_generic_titles
        if len(result.description_ai_terms) >= needed:
            result.flags.append(f"generic title, AI-heavy description ({len(result.description_ai_terms)} AI terms)")
            return
        if not record.description:
            result.flags.append("generic title, description not available yet")
            return
        result.reasons.append(
            f"title: generic and description has only {len(result.description_ai_terms)} AI terms (needs {needed})"
        )
        return
    result.reasons.append("title: not an AI/ML role")


def _experience_check(record: JobRecord, prefs: Preferences, result: FilterResult) -> None:
    years, soft = required_years_from_text(record.experience_text or "", structured=True)
    source = "listing"
    if years is None:
        years, soft = required_years_from_text(record.description)
        source = "description"
    result.required_years = years
    limit = prefs.experience.max_required_years
    if years is None:
        result.flags.append("experience not stated")
        if prefs.experience.reject_if_unknown:
            result.reasons.append("experience: not stated")
    elif years > limit and not soft:
        result.reasons.append(f"experience: {source} asks for {years}+ years (max {limit})")
    elif years > limit:
        result.flags.append(f"{years}+ years preferred, not required")
    elif years == 0:
        result.flags.append("fresher / new grad friendly")


def sponsorship_status(record: JobRecord) -> str:
    """'yes', 'no' or 'unknown', from YC's visa field or explicit statements in the description."""
    # YC visa values seen: "US citizen/visa only", "US citizenship/visa not required", "Will sponsor".
    visa = record.visa_text or ""
    if re.search(r"will sponsor|sponsorship available|not required", visa, re.I):
        return "yes"
    if re.search(r"\bonly\b", visa, re.I):
        return "no"
    if _search_windows(_SPONSOR_NO, record.description, _SPONSOR_TRIGGER):
        return "no"
    if _search_windows(_SPONSOR_YES, record.description, _SPONSOR_TRIGGER):
        return "yes"
    return "unknown"


def _location_check(record: JobRecord, prefs: Preferences, result: FilterResult) -> None:
    """Rules:
    India (any city, or remote India)        -> keep
    Remote worldwide / APAC                   -> keep, unless local work rights are required
    Remote limited to US/EU/...               -> reject
    Abroad + sponsorship stated               -> keep
    Abroad + sponsorship unknown              -> review (or reject, per preferences)
    Abroad + no sponsorship / work rights req -> reject
    """
    loc = prefs.location
    text = record.location or ""
    lower = text.lower()
    # YC writes "Bengaluru, KA, IN"; treat a trailing country code IN as India.
    india = bool(loc.india.search(text)) or bool(re.search(r",\s*[A-Z]{2},\s*IN\b|\(IN\)", text))
    remote = record.work_arrangement == WorkArrangement.REMOTE or "remote" in lower or "anywhere" in lower
    if india:
        result.location_class = "india"
        if prefs.candidate.home_city and prefs.candidate.home_city.lower() in lower:
            result.flags.append(f"in {prefs.candidate.home_city}")
        return

    result.sponsorship = sponsorship_status(record)
    if result.sponsorship == "no":
        result.flags.append("needs existing work authorization / no sponsorship")
    elif result.sponsorship == "yes":
        result.flags.append("visa sponsorship offered")

    # "Remote" next to a named foreign place (e.g. "London" with Ashby's isRemote) means remote in that country.
    place = re.sub(r"\b(remote|hybrid|onsite|on-site|anywhere|or|and|in|office)\b|[^a-z]+", " ", lower).strip()
    if remote and (not place or loc.remote_ok.search(text) or loc.remote_restricted.search(text)):
        if loc.remote_ok.search(text):
            result.location_class = "remote_ok"
        elif m := loc.remote_restricted.search(text):
            result.location_class = "abroad"
            result.reasons.append(f"location: remote limited to {m.group(1).upper()}")
            return
        else:
            result.location_class = "remote_unspecified"
            result.flags.append("remote, region not stated")
        if result.sponsorship == "no":
            result.reasons.append("location: remote but requires local work authorization")
        return
    if not text.strip():
        result.flags.append("location not stated")
        if loc.reject_if_unknown:
            result.reasons.append("location: not stated")
        return

    result.location_class = "abroad"
    where = text[:60]
    if loc.onsite_abroad == "allow":
        result.flags.append(f"abroad: {where}")
    elif loc.onsite_abroad == "reject":
        result.reasons.append(f"location: outside India ({where})")
    elif result.sponsorship == "yes":
        result.flags.append(f"abroad with visa sponsorship: {where}")
    elif result.sponsorship == "no":
        result.reasons.append(f"location: outside India without sponsorship ({where})")
    elif loc.abroad_sponsorship_unknown == "review":
        result.review_reasons.append(f"outside India, visa sponsorship not stated ({where})")
    else:
        result.reasons.append(f"location: outside India, sponsorship not stated ({where})")


def normalize_employment_type(value: str | None) -> str | None:
    if not value:
        return None
    for name, pattern in _EMPLOYMENT_TYPES:
        if pattern.search(value):
            return name
    return None


def _employment_check(record: JobRecord, prefs: Preferences, result: FilterResult) -> None:
    kind = normalize_employment_type(record.employment_type)
    if kind is None and re.search(r"\bintern(ship)?\b", record.title, re.I):
        kind = "internship"
    result.employment_type = kind
    if kind == "internship":
        result.flags.append("internship")
    if kind and kind not in prefs.employment.allowed_types:
        result.reasons.append(f"employment: {kind} not in allowed types")


def _posting_age_check(record: JobRecord, prefs: Preferences, result: FilterResult, now: datetime) -> None:
    if record.posted_at is None:
        return
    age = max((now - record.posted_at).days, 0)
    result.posting_age_days = age
    limit = prefs.posting.max_age_days
    if limit is None or age <= limit:
        return
    if prefs.posting.when_older == "reject":
        result.reasons.append(f"posting: {age} days old (max {limit})")
    else:
        result.flags.append(f"posted {age} days ago")


def _graduation_flags(record: JobRecord, prefs: Preferences, result: FilterResult) -> None:
    grad_year = prefs.candidate.graduation_year
    if not grad_year or not record.description:
        return
    years = {int(a or b) for _, snippet in _windows(record.description, _GRAD_TRIGGER, 60, 60)
             for a, b in _GRAD_YEAR.findall(snippet)}
    years = {y for y in years if 2020 <= y <= 2035}
    if not years:
        return
    if grad_year in years:
        result.flags.append(f"mentions {grad_year} graduates")
    else:
        result.flags.append(f"targets {', '.join(map(str, sorted(years)))} graduates (you graduate {grad_year})")


def filter_job(record: JobRecord, prefs: Preferences, *, now: datetime | None = None) -> FilterResult:
    result = FilterResult()
    result.description_ai_terms = sorted(prefs.description_ai.found(TextIndex(record.description)))
    _title_check(record, prefs, result)
    _experience_check(record, prefs, result)
    _location_check(record, prefs, result)
    _employment_check(record, prefs, result)
    _posting_age_check(record, prefs, result, now or utcnow())
    _graduation_flags(record, prefs, result)
    return result
