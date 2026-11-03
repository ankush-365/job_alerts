"""Level 1 filter tests on synthetic listings, using the shipped preferences file."""

import pytest

from core.filters import filter_job, required_years_from_text
from core.models import JobRecord, WorkArrangement
from core.preferences import load_preferences, term_pattern


@pytest.fixture(scope="module")
def prefs():
    return load_preferences()


def job(title="Machine Learning Engineer", location="Bengaluru, India", description="", **kw) -> JobRecord:
    return JobRecord(source="ashby", source_job_id="1", title=title, company_name="Acme",
                     location=location, description=description, job_url="https://jobs.ashbyhq.com/acme/1", **kw)


AI_HEAVY = "You will build RAG pipelines with LangChain, embeddings and a vector database, fine-tuning LLMs in PyTorch."


# --- titles ----------------------------------------------------------------

@pytest.mark.parametrize("title", [
    "Machine Learning Engineer", "AI/ML Engineer", "Generative AI Engineer", "Computer Vision Engineer",
    "LLM Engineer", "Applied AI Engineer", "NLP Engineer", "Python AI Engineer", "AI Research Engineer",
    "Deep Learning Engineer", "ML Engineer Intern", "Backend Engineer (AI-Native)", "Software Engineer, ML",
])
def test_relevant_titles_pass(prefs, title):
    result = filter_job(job(title), prefs)
    assert result.passed, result.reasons


@pytest.mark.parametrize("title,why", [
    ("Frontend Developer", "frontend"),
    ("Frontend Engineer, AI Observability & Evals Platform", "frontend"),
    ("Backend Developer", "backend"),
    ("Sales Executive", "sales"),
    ("Senior Machine Learning Engineer", "senior"),
    ("Sr. AI Engineer", "sr"),
    ("Engineering Manager – AI", "manager"),
    ("Product Manager, Edge AI", "manager"),
    ("Accountant", "accountant"),
    ("Office Coordinator", "not an AI/ML role"),
    ("Customer Support Agent", "not an AI/ML role"),
])
def test_irrelevant_titles_rejected(prefs, title, why):
    result = filter_job(job(title), prefs)
    assert not result.passed
    assert why in result.reasons[0]


def test_ai_word_does_not_match_inside_city_names():
    assert not term_pattern(["ai"]).search("Data Analyst, Chennai")
    assert term_pattern(["gen ai"]).search("Gen-AI Engineer")


def test_generic_title_needs_ai_heavy_description(prefs):
    assert filter_job(job("Software Engineer", description=AI_HEAVY), prefs).passed
    weak = filter_job(job("Software Engineer", description="Build REST APIs in Go and React with LLM tooling."), prefs)
    assert not weak.passed and "generic" in weak.reasons[0]
    unknown = filter_job(job("Founding Engineer", description=""), prefs)
    assert unknown.passed and "description not available yet" in unknown.flags[0]


def test_ai_role_with_extra_swe_duties_is_kept(prefs):
    desc = "Build backend services, React dashboards and deploy ML models with PyTorch."
    assert filter_job(job("AI Engineer", description=desc), prefs).passed


# --- experience --------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("3+ years of experience with Python.", 3),
    ("2-4 years experience in ML. 5+ years preferred.", 2),
    ("Minimum of two years of hands-on experience.", 2),
    ("Freshers welcome! 0-1 years.", 0),
    ("We were founded 10 years ago.", None),
    ("", None),
])
def test_required_years(text, expected):
    assert required_years_from_text(text)[0] == expected


def test_preferred_only_years_are_soft():
    assert required_years_from_text("5+ years of experience preferred.") == (5, True)


def test_experience_filters(prefs):
    assert not filter_job(job(experience_text="3+ years"), prefs).passed
    assert filter_job(job(experience_text="1+ years"), prefs).passed
    fresher = filter_job(job(experience_text="Any (new grads ok)"), prefs)
    assert fresher.passed and "fresher / new grad friendly" in fresher.flags
    missing = filter_job(job(description="Work on CV models."), prefs)
    assert missing.passed and "experience not stated" in missing.flags
    from_desc = filter_job(job(description="Requires 5+ years of experience in deep learning."), prefs)
    assert not from_desc.passed and "description asks for 5+" in from_desc.reasons[0]


# --- location ----------------------------------------------------------------

@pytest.mark.parametrize("location,cls", [
    ("Bengaluru, KA, IN", "india"),
    ("Mumbai, MH, IN / Mumbai, Maharashtra, IN", "india"),
    ("Remote - India", "india"),
    ("San Francisco / Bangalore", "india"),
    ("Remote (Worldwide)", "remote_ok"),
    ("Remote, APAC", "remote_ok"),
])
def test_acceptable_locations(prefs, location, cls):
    result = filter_job(job(location=location), prefs)
    assert result.passed, result.reasons
    assert result.location_class == cls


def test_indiana_is_not_india(prefs):
    result = filter_job(job(location="Indianapolis, IN, US"), prefs)
    assert result.location_class == "abroad" and not result.passed


def test_remote_flag_with_foreign_city_is_not_open_remote(prefs):
    result = filter_job(job(location="London", work_arrangement=WorkArrangement.REMOTE), prefs)
    assert not result.passed and result.location_class == "abroad"
    result = filter_job(job(location="NY, SF or Remote"), prefs)
    assert not result.passed


def test_yc_us_visa_field_ignored_for_india_jobs(prefs):
    result = filter_job(job(location="South East Asia & India", visa_text="US citizen/visa only"), prefs)
    assert result.passed and "US work authorization required" not in result.flags


def test_level_suffix_is_seniority(prefs):
    assert not filter_job(job("Data Scientist III"), prefs).passed
    assert filter_job(job("Agent Engineer"), prefs).passed


def test_remote_restricted_to_us_rejected(prefs):
    result = filter_job(job(location="Remote (US)"), prefs)
    assert not result.passed and "remote limited to US" in result.reasons[0]


def test_remote_unspecified_kept_but_flagged(prefs):
    result = filter_job(job(location="Remote", work_arrangement=WorkArrangement.REMOTE), prefs)
    assert result.passed and "remote, region not stated" in result.flags


def test_remote_with_us_work_authorization_rejected(prefs):
    result = filter_job(job(location="Remote", description="Must be authorized to work in the United States."), prefs)
    assert not result.passed and "requires local work authorization" in result.reasons[0]


@pytest.mark.parametrize("location,description,status", [
    ("Berlin, Germany", "We offer visa sponsorship is available for this role.", "passed"),
    ("Berlin, Germany", "Join our ML team.", "review"),
    ("Berlin, Germany", "Unfortunately we are unable to sponsor visas.", "rejected"),
    ("Berlin, Germany", "You must have the right to work in Germany.", "rejected"),
    ("Remote (Worldwide)", "Join our ML team.", "passed"),
    ("Remote (US only)", "Join our ML team.", "rejected"),
])
def test_abroad_visa_matrix(prefs, location, description, status):
    assert filter_job(job(location=location, description=description), prefs).status == status


def test_yc_visa_field_abroad(prefs):
    sponsored = filter_job(job(location="San Francisco, CA, US", visa_text="Will sponsor"), prefs)
    assert sponsored.passed and any("visa sponsorship" in f for f in sponsored.flags)
    us_only = filter_job(job(location="San Francisco, CA, US", visa_text="US citizen/visa only"), prefs)
    assert us_only.status == "rejected"
    unknown = filter_job(job(location="San Francisco, CA, US"), prefs)
    assert unknown.status == "review" and "sponsorship not stated" in unknown.review_reasons[0]


def test_visa_not_required_is_not_a_restriction(prefs):
    result = filter_job(job(location="Remote", visa_text="US citizenship/visa not required"), prefs)
    assert result.passed
    assert "US work authorization required" not in result.flags


def test_missing_location_kept(prefs):
    result = filter_job(job(location=None), prefs)
    assert result.passed and "location not stated" in result.flags


def test_home_city_flag(prefs):
    assert "in Ahmedabad" in filter_job(job(location="Ahmedabad, Gujarat"), prefs).flags


# --- graduation / internship -------------------------------------------------

def test_graduation_year_flags(prefs):
    other = filter_job(job(description="Open to 2025 graduates and the class of 2026."), prefs)
    assert "targets 2025, 2026 graduates (you graduate 2027)" in other.flags
    match = filter_job(job(description="Hiring 2027 graduates for our program."), prefs)
    assert "mentions 2027 graduates" in match.flags


def test_internship_flag(prefs):
    assert "internship" in filter_job(job(employment_type="Intern"), prefs).flags
    assert "internship" in filter_job(job("Machine Learning Intern"), prefs).flags


@pytest.mark.parametrize("raw,kind,status", [
    ("FullTime", "full-time", "passed"),
    ("Full Time Employee", "full-time", "passed"),
    ("Internship", "internship", "passed"),
    ("Contract", "contract", "passed"),
    ("PartTime", "part-time", "rejected"),
    (None, None, "passed"),
])
def test_employment_types(prefs, raw, kind, status):
    result = filter_job(job(employment_type=raw), prefs)
    assert (result.employment_type, result.status) == (kind, status)


def test_old_postings_rejected_or_deprioritized_per_preference(prefs):
    from datetime import timedelta

    from core.models import utcnow

    def with_policy(policy):
        return type(prefs).model_validate({**prefs.model_dump(), "posting": {**prefs.posting.model_dump(),
                                                                           "when_older": policy}})

    old = job(posted_at=utcnow() - timedelta(days=120))
    rejected = filter_job(old, with_policy("reject"))
    assert rejected.status == "rejected" and "posting: 120 days old (max 30)" in rejected.reasons[0]
    kept = filter_job(old, with_policy("deprioritize"))
    assert kept.passed and kept.posting_age_days == 120 and "posted 120 days ago" in kept.flags
    assert filter_job(job(posted_at=utcnow() - timedelta(days=29)), with_policy("reject")).passed
    assert filter_job(job(posted_at=None), with_policy("reject")).passed  # unknown age is kept


def test_shipped_preferences_skip_old_postings(prefs):
    assert prefs.posting.when_older == "reject" and prefs.posting.max_age_days == 30


@pytest.mark.parametrize("title,description,passed", [
    ("CV Technologist", "Perform echocardiograms for cardiology patients.", False),
    ("CV Engineer", "", True),
    ("Research Engineer, Human Biomechanics", "Study gait and muscle physiology in the lab.", False),
    ("Research Engineer", AI_HEAVY, True),
    ("Data Scientist, Product", "Own SQL dashboards and A/B test analysis for growth.", False),
    ("Data Scientist", AI_HEAVY, True),
    ("LLM Evals Engineer", "", True),
    ("AI Test Engineer", "", True),
])
def test_ambiguous_titles_need_ai_evidence(prefs, title, description, passed):
    assert filter_job(job(title, description=description), prefs).passed is passed
