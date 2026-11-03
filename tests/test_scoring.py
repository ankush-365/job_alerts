"""Relevance scoring tests (no network: the semantic model is never loaded here)."""

from datetime import timedelta

import pytest

from core.filters import filter_job
from core.models import JobRecord, utcnow
from core.preferences import load_preferences
from core.relevance_scorer import score_job
from core.salary import Salary, parse_salary, parse_salary_text, to_inr_per_year
from core.semantic import SemanticMatcher


@pytest.fixture(scope="module")
def prefs():
    return load_preferences()


def job(title="Computer Vision Engineer", description="", location="Bengaluru, India", **kw) -> JobRecord:
    return JobRecord(source="ashby", source_job_id="1", title=title, company_name="Acme", description=description,
                     location=location, job_url="https://jobs.ashbyhq.com/acme/1", **kw)


def score(record, prefs, **kw):
    return score_job(record, filter_job(record, prefs).as_dict(), prefs, **kw)


CV_DESC = ("Build real-time object detection with YOLO and multi-object tracking (ByteTrack) on RTSP video "
           "streams for CCTV analytics. PyTorch, OpenCV, Python. Freshers welcome.")
RAG_DESC = "Build RAG pipelines with LangChain, embeddings and vector databases (FAISS) on top of LLMs. Python."


# --- salary ------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("$215K – $250K • Offers Equity", Salary(215_000, 250_000, "USD", "year")),
    ("CA$215K – CA$295K • Offers Equity", Salary(215_000, 295_000, "CAD", "year")),
    ("₹2M - ₹3.5M INR", Salary(2_000_000, 3_500_000, "INR", "year")),
    ("₹8L - ₹15L", Salary(800_000, 1_500_000, "INR", "year")),
    ("$108,300 – $148,950 • Offers Bonus", Salary(108_300, 148_950, "USD", "year")),
    ("$6K - $7.5K / monthly", Salary(6_000, 7_500, "USD", "month")),
    ("$40 – $50 per hour", Salary(40, 50, "USD", "hour")),
    ("SGD 335K – SGD 405K", Salary(335_000, 405_000, "SGD", "year")),
    ("$100K - $150K CAD", Salary(100_000, 150_000, "CAD", "year")),
    ("A$265K – A$340K", Salary(265_000, 340_000, "AUD", "year")),
    ("$180K", Salary(180_000, 180_000, "USD", "year")),
    ("₹6 LPA", Salary(600_000, 600_000, "INR", "year")),
])
def test_parse_salary_text(text, expected):
    assert parse_salary_text(text) == expected


@pytest.mark.parametrize("text", ["$1", "$220 - $285", "Competitive", "", None])
def test_ambiguous_salary_is_none(text):
    assert parse_salary_text(text) is None


def test_structured_salary_and_conversion():
    salary = parse_salary(None, min_value=30000, max_value=50000, currency="inr", period="per-month-salary")
    assert salary == Salary(30000, 50000, "INR", "month")
    assert to_inr_per_year(salary, {"INR": 1}) == (360_000, 600_000)
    assert to_inr_per_year(Salary(1, 2, "XYZ", "year"), {"INR": 1}) is None


# --- scorer ------------------------------------------------------------------

def test_strong_cv_match_in_india_scores_high(prefs):
    result = score(job(description=CV_DESC), prefs)
    assert result.tier == "high", result
    assert {"Object Detection", "Object Tracking", "Video Streams", "Computer Vision"} <= set(result.matched_skills)
    assert result.experience_match and result.location_match
    assert "open to freshers" in result.reason


def test_matched_skills_come_from_the_text_only(prefs):
    result = score(job(title="ML Engineer", description="We use scikit-learn."), prefs)
    assert result.matched_skills == ["Scikit-learn"]


def test_missing_skills_reported(prefs):
    result = score(job(description=RAG_DESC + " Deploy on Kubernetes and AWS."), prefs)
    assert result.missing_skills == ["aws", "kubernetes"]


def test_senior_pay_band_and_phd_penalties(prefs):
    base = score(job(description=RAG_DESC), prefs)
    senior = score(job(description=RAG_DESC, salary_text="$230K – $450K"), prefs)
    phd = score(job(description=RAG_DESC + " A PhD in machine learning is required."), prefs)
    assert senior.adjustments["senior_pay_band"] == -10 and senior.score < base.score
    assert phd.adjustments["phd_required"] == -15 and phd.score < base.score
    assert "Watch out" in phd.reason


def test_phd_preferred_is_not_penalized(prefs):
    result = score(job(description=RAG_DESC + " PhD preferred."), prefs)
    assert "phd_required" not in result.adjustments


def test_low_inr_salary_scores_zero_on_salary(prefs):
    result = score(job(description=RAG_DESC, salary_text="₹3L - ₹4L"), prefs)
    assert result.components["salary"] == 0 and "below your minimum" in result.reason


def test_undisclosed_salary_is_neutral(prefs):
    assert score(job(description=RAG_DESC), prefs).components["salary"] == 0.6


def test_internship_pay_not_judged_against_full_time_minimum(prefs):
    result = score(job("ML Intern", description=RAG_DESC, salary_text="₹20K / month", employment_type="Intern"), prefs)
    assert result.components["salary"] == 0.8 and result.components["experience"] == 1.0


def test_home_city_beats_other_indian_city(prefs):
    home = score(job(description=RAG_DESC, location="Ahmedabad, Gujarat"), prefs)
    other = score(job(description=RAG_DESC, location="Pune"), prefs)
    assert home.components["location"] > other.components["location"]


def test_recent_postings_rank_higher(prefs):
    new = score(job(description=RAG_DESC, posted_at=utcnow() - timedelta(days=2)), prefs)
    old = score(job(description=RAG_DESC, posted_at=utcnow() - timedelta(days=200)), prefs)
    assert new.score > old.score


def test_graduation_year_bonus_and_penalty(prefs):
    match = score(job(description=RAG_DESC + " Open to 2027 graduates."), prefs)
    other = score(job(description=RAG_DESC + " Open to 2025 graduates only."), prefs)
    assert match.adjustments.get("graduation_year_match") == 5
    assert other.adjustments.get("other_graduation_years") == -10


def test_semantic_similarity_used_when_available(prefs):
    without = score(job(description=RAG_DESC), prefs)
    close = score(job(description=RAG_DESC), prefs, semantic_similarity=0.65)
    far = score(job(description=RAG_DESC), prefs, semantic_similarity=0.1)
    assert "semantic" not in without.components
    assert close.components["semantic"] == 1.0 and far.components["semantic"] == 0.0
    assert close.score > far.score


def test_score_bounds_and_output_shape(prefs):
    result = score(job(), prefs).as_dict()
    assert 0 <= result["score"] <= 100
    assert {"score", "matched_skills", "missing_skills", "experience_match", "location_match", "reason"} <= set(result)


# --- semantic fallback --------------------------------------------------------

def test_semantic_matcher_unavailable_returns_none():
    matcher = SemanticMatcher("this-model/does-not-exist-xyz", "profile text")
    matcher._failed = True  # as after a failed load; avoids a network call in tests
    assert matcher.similarities(["some job"]) is None


def test_semantic_matcher_empty_profile_returns_none():
    assert SemanticMatcher("any", "  ").similarities(["job"]) is None
