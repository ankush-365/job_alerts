"""Level 2 relevance scoring (0-100) without an LLM.

score = weighted components (skills, semantic similarity, title, experience, location,
salary, recency) - penalties (PhD required, other graduation years, senior pay band,
more experience preferred) + small bonuses. Every number in the result comes from the
job text or the preferences; nothing is invented. If semantic similarity is unavailable,
its weight is spread over the other components.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from core.filters import SOFT_CONTEXT, sentence_around  # same notion of "preferred" as the filters
from core.models import JobRecord, utcnow
from core.preferences import Preferences, TextIndex
from core.salary import format_inr_lpa, parse_salary, to_inr_per_year

_PHD = re.compile(r"\bph\.?\s?d\b|\bdoctorate\b|\bdoctoral\b", re.I)
_EARLY_CAREER_TITLE = re.compile(r"\b(intern|internship|new grad|graduate|junior|entry[\s-]level|fresher|trainee)\b", re.I)


@dataclass
class ScoreResult:
    score: int
    tier: str
    matched_skills: list[str]
    missing_skills: list[str]
    experience_match: bool
    location_match: bool
    reason: str
    components: dict[str, float] = field(default_factory=dict)  # 0-1 per component
    adjustments: dict[str, float] = field(default_factory=dict)  # penalties (negative) and bonuses
    semantic_similarity: float | None = None
    salary_inr_per_year: list[float] | None = None

    def as_dict(self) -> dict:
        return {
            "score": self.score,
            "tier": self.tier,
            "matched_skills": self.matched_skills,
            "missing_skills": self.missing_skills,
            "experience_match": self.experience_match,
            "location_match": self.location_match,
            "reason": self.reason,
            "components": self.components,
            "adjustments": self.adjustments,
            "semantic_similarity": self.semantic_similarity,
            "salary_inr_per_year": self.salary_inr_per_year,
        }


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _phd_required(record: JobRecord) -> bool:
    if _PHD.search(record.title):
        return True
    for match in _PHD.finditer(record.description):
        sentence = sentence_around(record.description, match.start(), match.end())
        if not SOFT_CONTEXT.search(sentence) and not re.search(r"\bor\b|equivalent", sentence, re.I):
            return True
    return False


def score_job(
    record: JobRecord,
    filter_details: dict,
    prefs: Preferences,
    *,
    semantic_similarity: float | None = None,
    now: datetime | None = None,
) -> ScoreResult:
    now = now or utcnow()
    scoring = prefs.scoring
    text = f"{record.title}\n{record.description}\n{' '.join(record.skills)}"
    flags = filter_details.get("flags") or []
    internship = filter_details.get("employment_type") == "internship" or "internship" in flags
    components: dict[str, float] = {}
    adjustments: dict[str, float] = {}
    positives: list[str] = []
    negatives: list[str] = []

    # Skills: weighted matches against the profile.
    index = TextIndex(text)
    matched = [s for s in prefs.profile.skills if s.terms.found(index)]
    matched.sort(key=lambda s: -s.weight)
    components["skills"] = _clamp(sum(s.weight for s in matched) / scoring.skills_for_full_score)
    core = [s.name for s in matched if s.weight >= 3]
    if matched:
        names = ", ".join(s.name for s in matched[:5])
        if core:
            positives.append(f"{len(core)} core skill{'s' * (len(core) > 1)} ({', '.join(core[:4])})")
        else:
            positives.append(f"{len(matched)} of your skill{'s' * (len(matched) > 1)} ({names})")
    missing = sorted(prefs.profile.missing_terms.found(index))

    # Semantic similarity to the profile summary.
    if semantic_similarity is not None:
        sem = scoring.semantic
        components["semantic"] = _clamp(
            (semantic_similarity - sem.similarity_floor) / (sem.similarity_ceiling - sem.similarity_floor)
        )
        if components["semantic"] >= 0.7:
            positives.append("description closely resembles your work")

    # Title.
    if prefs.titles.ai.search(record.title):
        components["title"] = 1.0
    elif prefs.titles.generic_engineering.search(record.title):
        components["title"] = 0.5
    else:
        components["title"] = 0.3

    # Experience.
    years = filter_details.get("required_years")
    early_title = bool(_EARLY_CAREER_TITLE.search(record.title))
    if years == 0 or early_title or internship:
        components["experience"] = 1.0
        positives.append("internship" if internship else "open to freshers / new grads")
    elif years is None:
        components["experience"] = 0.55
    elif years <= 1:
        components["experience"] = 0.85
    elif years <= prefs.experience.max_required_years:
        components["experience"] = 0.7
    else:  # only "preferred", otherwise the filter would have rejected it
        components["experience"] = 0.3
    experience_match = years is None or years <= prefs.experience.max_required_years
    if any("preferred, not required" in f for f in flags):
        adjustments["experience_preferred_above_limit"] = -scoring.penalties.experience_preferred_above_limit
        negatives.append(f"{years}+ years preferred")

    # Location.
    location_class = filter_details.get("location_class", "unknown")
    home = any(f.startswith("in ") and prefs.candidate.home_city and prefs.candidate.home_city in f for f in flags)
    status = filter_details.get("status", "passed")
    location_scores = {"india": 0.9, "remote_ok": 0.85, "remote_unspecified": 0.6, "unknown": 0.5}
    if home:
        components["location"] = 1.0
        positives.append(f"in {prefs.candidate.home_city}")
    elif location_class == "abroad":
        components["location"] = 0.45 if status == "passed" else 0.25
    else:
        components["location"] = location_scores.get(location_class, 0.5)
        if location_class == "india":
            positives.append(f"in India ({record.location})")
        elif location_class == "remote_ok":
            positives.append("remote, open to India")
    location_match = location_class in {"india", "remote_ok", "remote_unspecified"} or (
        location_class == "abroad" and status == "passed"
    )

    # Salary.
    salary = parse_salary(record.salary_text, min_value=record.salary_min, max_value=record.salary_max,
                          currency=record.salary_currency, period=record.salary_period)
    inr = to_inr_per_year(salary, scoring.inr_per_unit) if salary else None
    preferred_min = prefs.salary.preferred_min_inr_per_year
    if inr is None:
        components["salary"] = 0.6  # undisclosed is neutral, never a rejection
    elif internship:
        components["salary"] = 0.8
    elif preferred_min and inr[1] < preferred_min:
        components["salary"] = 0.0
        negatives.append(f"pay below your minimum (~{format_inr_lpa(inr[1])})")
    else:
        components["salary"] = 1.0
    senior_band = scoring.senior_pay_band_min_inr_per_year
    if inr and senior_band and not internship and years != 0 and not early_title and inr[0] >= senior_band:
        adjustments["senior_pay_band"] = -scoring.penalties.senior_pay_band
        negatives.append(f"pay band suggests a senior role (~{format_inr_lpa(inr[0])}+)")

    # Recency.
    age = filter_details.get("posting_age_days")
    if not prefs.posting.prioritize_new_jobs or age is None:
        components["recency"] = 0.5
    elif age <= 7:
        components["recency"] = 1.0
        positives.append("posted this week")
    elif age <= (prefs.posting.max_age_days or 30):
        components["recency"] = 0.6
    else:
        components["recency"] = 0.2

    # Penalties and bonuses.
    if _phd_required(record):
        adjustments["phd_required"] = -scoring.penalties.phd_required
        negatives.append("asks for a PhD")
    if any(f.startswith("targets ") and "graduates" in f for f in flags):
        adjustments["other_graduation_years"] = -scoring.penalties.other_graduation_years
        negatives.append(next(f for f in flags if f.startswith("targets ")))
    if any(f.startswith("mentions ") and "graduates" in f for f in flags):
        adjustments["graduation_year_match"] = 5
        positives.append(f"mentions {prefs.candidate.graduation_year} graduates")

    weights = {k: w for k, w in scoring.weights.items() if k in components}
    total_weight = sum(weights.values()) or 1
    base = sum(components[k] * w for k, w in weights.items()) * 100 / total_weight
    score = int(round(_clamp(base + sum(adjustments.values()), 0, 100)))
    tier = "high" if score >= scoring.tiers.high else "moderate" if score >= scoring.tiers.moderate else "low"

    reason = "; ".join(positives[:4]) or "few direct matches with your profile"
    if negatives:
        reason += ". Watch out: " + "; ".join(negatives)
    return ScoreResult(
        score=score,
        tier=tier,
        matched_skills=[s.name for s in matched],
        missing_skills=missing,
        experience_match=experience_match,
        location_match=location_match,
        reason=reason + ".",
        components={k: round(v, 3) for k, v in components.items()},
        adjustments=adjustments,
        semantic_similarity=None if semantic_similarity is None else round(semantic_similarity, 4),
        salary_inr_per_year=[round(inr[0]), round(inr[1])] if inr else None,
    )
