"""Load and validate config/job_preferences.yaml."""

from __future__ import annotations

import hashlib
import re
from functools import cached_property
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from config.settings import PROJECT_ROOT

DEFAULT_PREFERENCES_FILE = PROJECT_ROOT / "config" / "job_preferences.yaml"


def term_pattern(terms: list[str]) -> re.Pattern:
    """Whole-word, case-insensitive alternation; longer terms first so 'gen ai' wins over 'ai'."""
    escaped = sorted((re.escape(t.lower()).replace(r"\ ", r"[\s\-_/]+") for t in terms if t), key=len, reverse=True)
    if not escaped:
        return re.compile(r"(?!x)x")  # matches nothing
    return re.compile(r"(?<![a-z0-9])(" + "|".join(escaped) + r")(?![a-z0-9])", re.I)


_TOKEN = re.compile(r"[a-z0-9+#]+")


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens; any other character separates words (so 'rt-detr' == 'rt detr')."""
    return _TOKEN.findall(text.lower())


class TextIndex:
    """A text normalized to ' word word ... ', so whole-word term lookup is a fast substring check."""

    def __init__(self, text: str):
        self.normalized = " " + " ".join(tokenize(text)) + " "

    def contains(self, words: tuple[str, ...]) -> bool:
        return " " + " ".join(words) + " " in self.normalized


class TermSet:
    """Same matching rules as `term_pattern`, but word lookups instead of a regex scan."""

    def __init__(self, terms: list[str]):
        self.terms = {tuple(tokenize(t)): t for t in terms if tokenize(t)}

    def found(self, index: TextIndex) -> list[str]:
        return [term for key, term in self.terms.items() if index.contains(key)]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", ignored_types=(cached_property,))


class Candidate(_Section):
    graduation_year: int | None = None
    home_city: str | None = None


class Titles(_Section):
    ai_terms: list[str]
    reject_terms: list[str] = Field(default_factory=list)
    non_ai_engineering_terms: list[str] = Field(default_factory=list)
    generic_engineering_terms: list[str] = Field(default_factory=list)
    min_ai_terms_in_description_for_generic_titles: int = 4

    @cached_property
    def ai(self) -> re.Pattern:
        return term_pattern(self.ai_terms)

    @cached_property
    def reject(self) -> re.Pattern:
        return term_pattern(self.reject_terms)

    @cached_property
    def non_ai_engineering(self) -> re.Pattern:
        return term_pattern(self.non_ai_engineering_terms)

    @cached_property
    def generic_engineering(self) -> re.Pattern:
        return term_pattern(self.generic_engineering_terms)


class Experience(_Section):
    max_required_years: int = 2
    reject_if_unknown: bool = False


class Location(_Section):
    india_terms: list[str]
    remote_ok_terms: list[str] = Field(default_factory=list)
    remote_restricted_terms: list[str] = Field(default_factory=list)
    onsite_abroad: Literal["reject", "allow_if_visa_sponsored", "allow"] = "allow_if_visa_sponsored"
    abroad_sponsorship_unknown: Literal["review", "reject"] = "review"
    reject_if_unknown: bool = False

    @cached_property
    def india(self) -> re.Pattern:
        return term_pattern(self.india_terms)

    @cached_property
    def remote_ok(self) -> re.Pattern:
        return term_pattern(self.remote_ok_terms)

    @cached_property
    def remote_restricted(self) -> re.Pattern:
        return term_pattern(self.remote_restricted_terms)


class Salary(_Section):
    preferred_min_inr_per_year: float | None = None


class Employment(_Section):
    allowed_types: list[Literal["full-time", "internship", "contract", "part-time", "temporary"]] = Field(
        default_factory=lambda: ["full-time", "internship", "contract"]
    )


class Posting(_Section):
    max_age_days: int | None = 30
    when_older: Literal["deprioritize", "reject"] = "deprioritize"
    prioritize_new_jobs: bool = True


class Notifications(_Section):
    minimum_relevance_score: int = 60
    notify_needs_review: bool = True
    notify_suspicious: bool = False
    location_review_jobs: Literal["digest", "alert", "never"] = "digest"
    daily_digest: bool = True
    digest_hour: int = Field(default=20, ge=0, le=23)  # local hour (timezone below) after which the digest is sent
    timezone: str = "Asia/Kolkata"
    max_alerts_per_run: int = 10  # safety cap so a first run or a preference change can't flood the chat
    digest_max_jobs: int = 15


class Skill(_Section):
    name: str
    weight: float = 1
    aliases: list[str] = Field(default_factory=list)

    @cached_property
    def pattern(self) -> re.Pattern:
        return term_pattern(self.aliases or [self.name])

    @cached_property
    def terms(self) -> TermSet:
        return TermSet(self.aliases or [self.name])


class Profile(_Section):
    summary: str = ""
    skills: list[Skill] = Field(default_factory=list)
    watch_for_missing: list[str] = Field(default_factory=list)

    @cached_property
    def missing_terms(self) -> TermSet:
        return TermSet(self.watch_for_missing)


class SemanticSettings(_Section):
    enabled: bool = True
    model: str = "minishlab/potion-base-8M"
    similarity_floor: float = 0.25
    similarity_ceiling: float = 0.65


class Penalties(_Section):
    phd_required: float = 15
    other_graduation_years: float = 10
    senior_pay_band: float = 10
    experience_preferred_above_limit: float = 5


class Tiers(_Section):
    high: int = 80
    moderate: int = 60


class Scoring(_Section):
    weights: dict[str, float] = Field(
        default_factory=lambda: {"skills": 30, "semantic": 20, "title": 15, "experience": 15,
                                 "location": 10, "salary": 5, "recency": 5}
    )
    skills_for_full_score: float = 15
    semantic: SemanticSettings = Field(default_factory=SemanticSettings)
    penalties: Penalties = Field(default_factory=Penalties)
    senior_pay_band_min_inr_per_year: float | None = 15_000_000
    tiers: Tiers = Field(default_factory=Tiers)
    inr_per_unit: dict[str, float] = Field(default_factory=lambda: {"INR": 1, "USD": 85})


class Preferences(_Section):
    candidate: Candidate = Field(default_factory=Candidate)
    titles: Titles
    description_ai_terms: list[str] = Field(default_factory=list)
    experience: Experience = Field(default_factory=Experience)
    location: Location
    salary: Salary = Field(default_factory=Salary)
    employment: Employment = Field(default_factory=Employment)
    posting: Posting = Field(default_factory=Posting)
    notifications: Notifications = Field(default_factory=Notifications)
    profile: Profile = Field(default_factory=Profile)
    scoring: Scoring = Field(default_factory=Scoring)

    @cached_property
    def description_ai(self) -> TermSet:
        return TermSet(self.description_ai_terms)


def load_preferences(path: Path = DEFAULT_PREFERENCES_FILE) -> Preferences:
    with open(path, encoding="utf-8") as fh:
        return Preferences.model_validate(yaml.safe_load(fh) or {})


def preferences_fingerprint(prefs: Preferences) -> str:
    """Changes whenever any preference changes; used to trigger re-evaluation of stored jobs."""
    return hashlib.sha256(prefs.model_dump_json().encode()).hexdigest()[:16]
