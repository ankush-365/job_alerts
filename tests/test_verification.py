"""Verification tests: scam signals, board identity, classification, discovery, expiry, migration."""

from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from tests.conftest import fresh_db_url
from sqlalchemy import create_engine, text

from collectors.base_collector import BaseCollector, HttpFetcher
from core.ats_discovery import candidate_slugs, discover_board, yc_slug_from_url
from core.models import JobRecord, utcnow
from core.pipeline import run_pipeline
from core.preferences import load_preferences
from core.scam_signals import text_signals
from core.verifier import (
    NEEDS_REVIEW,
    SUSPICIOUS,
    VERIFIED,
    Evidence,
    SourceInfo,
    VerificationInput,
    board_identity_evidence,
    name_mentioned,
    verify,
)
from database.models import Company, Job
from database.repository import JobRepository, init_db

CONFIRMED = Evidence("board_identity", "pass", "'Acme' is named in 9 of 9 listings on the ashby:acme board.")
CLEAN = "Build computer vision models with PyTorch at Acme."


def inp(**kw) -> VerificationInput:
    data = dict(
        title="CV Engineer", company_name="Acme", description=CLEAN,
        apply_url="https://jobs.ashbyhq.com/acme/1/application",
        sources=[SourceInfo("ashby", "https://jobs.ashbyhq.com/acme/1", "ashby:acme")],
        board_identity={"ashby:acme": CONFIRMED},
    )
    data.update(kw)
    return VerificationInput(**data)


# --- scam signals --------------------------------------------------------------

@pytest.mark.parametrize("text,label", [
    ("Selected candidates must pay a registration fee of Rs 2,500 before joining.", "fee"),
    ("A refundable security deposit of ₹5000 is required for the laptop.", "fee"),
    ("WhatsApp your CV to +91 98765 43210 for an immediate interview.", "WhatsApp"),
    ("Send your resume on WhatsApp to our HR.", "WhatsApp"),
    ("Apply here: https://wa.me/919876543210", "WhatsApp"),
    ("Join our Telegram group for interview updates.", "WhatsApp"),
])
def test_strong_scam_signals(text, label):
    [signal] = text_signals(text)
    assert signal.strong and label in signal.label


@pytest.mark.parametrize("text", [
    "Email your resume to hr.techcorp.jobs@gmail.com",
    "100% job guarantee after training.",
    "Work from home and earn ₹50,000 per week!",
    "Direct joining, no interview required.",
])
def test_weak_scam_signals(text):
    [signal] = text_signals(text)
    assert not signal.strong


@pytest.mark.parametrize("text", [
    # Real sentences from legitimate listings in the stored data, and typical anti-fraud notices.
    "We will never ask you to pay a registration fee or deposit during hiring.",
    "Beware of fraudsters asking for a processing fee in our name.",
    "By submitting your application, you consent to being contacted by our Talent Acquisition team via phone "
    "call, email, SMS, WhatsApp, or other communication channels.",
    "Integrated Channels: Crafting engaging push notifications and WhatsApp campaigns.",
    "LlamaIndex is not responsible for any fees related to unsolicited resumes.",
    "Thorough understanding of global visa documentation, US MRV fee workflows.",
    "Negotiate budgets and fee arrangements with outside counsel.",
    "It is important to understand that as an independent contractor, continuous work is not guaranteed.",
    "Partner programs, including tiering, rebates and referral fees.",
])
def test_legitimate_text_not_flagged(text):
    assert text_signals(text) == []


# --- board identity ------------------------------------------------------------

def test_name_mentioned_is_case_sensitive():
    assert name_mentioned("Writer", "WRITER is where enterprises orchestrate AI.")
    assert name_mentioned("Writer", "At Writer, we build.")
    assert not name_mentioned("Writer", "You are a strong technical writer.")
    assert name_mentioned("Scale AI", "Scale is hiring.")
    assert not name_mentioned("Modal", "multimodal models")


def test_board_identity_by_own_domain():
    ev = board_identity_evidence("greenhouse:databricks", "Databricks",
                                 [("https://databricks.com/company/careers/x?gh_jid=1", "")])
    assert ev.outcome == "pass" and "databricks.com" in ev.detail


def test_board_identity_by_name_share():
    ok = board_identity_evidence("ashby:acme", "Acme", [("https://jobs.ashbyhq.com/acme/1", "Acme builds.")] * 3)
    weak = board_identity_evidence("ashby:acme", "Acme", [("https://jobs.ashbyhq.com/acme/1", "We build.")] * 3)
    assert ok.outcome == "pass" and weak.outcome == "warn"
    assert board_identity_evidence("ashby:acme", "Acme", []).outcome == "warn"


# --- classification ------------------------------------------------------------

def test_confirmed_company_board_is_verified():
    result = verify(inp())
    assert result.status == VERIFIED
    assert result.summary == "Listed on Acme's own Ashby job board."


def test_yc_only_needs_review_not_suspicious():
    result = verify(inp(sources=[SourceInfo("yc", "https://www.ycombinator.com/companies/acme/jobs/x")],
                        apply_url="https://account.ycombinator.com/authenticate?continue=x",
                        company_website="https://acme.ai"))
    assert result.status == NEEDS_REVIEW
    assert "YC-funded" in result.summary
    assert any(e.check == "company_website" and e.outcome == "pass" for e in result.evidence)


def test_yc_plus_confirmed_board_is_verified():
    result = verify(inp(sources=[SourceInfo("yc", "https://www.ycombinator.com/companies/acme/jobs/x"),
                                 SourceInfo("ashby", "https://jobs.ashbyhq.com/acme/1", "ashby:acme")]))
    assert result.status == VERIFIED
    assert any(e.check == "duplicates" for e in result.evidence)


def test_unconfirmed_board_needs_review():
    result = verify(inp(board_identity={}))
    assert result.status == NEEDS_REVIEW


def test_fee_request_is_suspicious_even_on_confirmed_board():
    result = verify(inp(description="Pay a registration fee of Rs 999 to confirm your slot."))
    assert result.status == SUSPICIOUS and "fee" in result.summary


def test_messaging_apply_link_is_suspicious():
    assert verify(inp(apply_url="https://wa.me/919999999999")).status == SUSPICIOUS
    assert verify(inp(apply_url="https://t.me/hiring_now")).status == SUSPICIOUS


def test_weak_signals_downgrade_to_review():
    assert verify(inp(description=CLEAN + " Send CVs to acme.hr.jobs@gmail.com")).status == NEEDS_REVIEW
    assert verify(inp(apply_url="https://bit.ly/abc")).status == NEEDS_REVIEW
    assert verify(inp(apply_url="http://jobs.ashbyhq.com/acme/1")).status == NEEDS_REVIEW
    assert verify(inp(apply_url="mailto:jobs@acme.ai")).status == NEEDS_REVIEW
    assert verify(inp(apply_url="https://random-careers-portal.xyz/apply")).status == NEEDS_REVIEW


def test_anti_fraud_notice_does_not_hurt():
    notice = CLEAN + " Acme will never ask candidates for any fee or deposit."
    assert verify(inp(description=notice)).status == VERIFIED


def test_apply_link_published_by_company_board():
    result = verify(inp(apply_url="https://databricks.com/careers/1",
                        sources=[SourceInfo("greenhouse", "https://databricks.com/careers/1", "greenhouse:databricks")],
                        board_identity={"greenhouse:databricks": CONFIRMED}))
    assert result.status == VERIFIED


def test_missing_website_is_not_a_red_flag():
    result = verify(inp(company_website=None))
    assert result.status == VERIFIED
    assert any(e.check == "company_website" and e.outcome == "info" for e in result.evidence)


def test_expired_listing_is_reported():
    result = verify(inp(expired_at=utcnow()))
    assert any(e.check == "expiry" for e in result.evidence)


# --- discovery -------------------------------------------------------------------

def test_yc_slug_and_candidate_slugs_are_sanitized():
    assert yc_slug_from_url("https://www.ycombinator.com/companies/knowtex/jobs/X6F-ml") == "knowtex"
    assert candidate_slugs("Acme AI Pvt Ltd", "acme-ai", "https://www.acme.ai") == ["acme-ai", "acmeai", "acme"]
    assert candidate_slugs("../../etc", None, None) == ["etc"]
    assert all("/" not in s and "." not in s for s in candidate_slugs("A.B/C", "x/../y", "https://evil.com/../"))


def ashby_payload(*titles, host="jobs.ashbyhq.com"):
    return {"jobs": [{"id": str(i), "title": t, "jobUrl": f"https://{host}/acme/{i}"} for i, t in enumerate(titles)]}


def make_fetcher(handler):
    return HttpFetcher(client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None,
                       clock=lambda: 0.0, min_interval=0, max_retries=0)


def test_discovery_accepts_board_with_same_opening():
    def handler(request):
        if request.url.host == "api.ashbyhq.com" and request.url.path.endswith("/knowtex"):
            return httpx.Response(200, json=ashby_payload("ML Engineer, LLM's", "Designer"))
        return httpx.Response(404)

    result = discover_board(make_fetcher(handler), "Knowtex", yc_slug="knowtex", website="http://www.knowtex.ai/",
                            known_titles=["ML Engineer, LLM's"])
    assert (result.board.ats, result.board.slug) == ("ashby", "knowtex")
    assert "Same opening" in result.evidence


def test_discovery_rejects_namesake_board():
    """A board under the same slug with none of the company's openings may belong to someone else."""
    def handler(request):
        if request.url.host == "api.ashbyhq.com":
            return httpx.Response(200, json=ashby_payload("Account Executive", "Nurse"))
        return httpx.Response(404)

    result = discover_board(make_fetcher(handler), "Modal", yc_slug="modal", website="https://modal.example",
                            known_titles=["ML Engineer"])
    assert result.board is None and "not accepted" in result.evidence


def test_discovery_accepts_board_hosted_on_company_domain():
    def handler(request):
        if request.url.host == "boards-api.greenhouse.io":
            return httpx.Response(200, json={"jobs": [{"id": 1, "title": "Other", "content": "",
                                                       "absolute_url": "https://careers.acme.ai/jobs/1"}]})
        return httpx.Response(404)

    result = discover_board(make_fetcher(handler), "Acme", yc_slug="acme", website="https://acme.ai",
                            known_titles=["ML Engineer"])
    assert result.board.ats == "greenhouse" and "hosted on acme.ai" in result.evidence


def test_discovery_accepts_board_citing_company_website_and_name():
    def handler(request):
        if request.url.host == "api.ashbyhq.com":
            return httpx.Response(200, json={"jobs": [
                {"id": "1", "title": "Product Engineer", "jobUrl": "https://jobs.ashbyhq.com/legionhealth/1",
                 "descriptionPlain": "At Legion Health https://legionhealth.com/, we believe in care."},
                {"id": "2", "title": "Psychiatrist", "jobUrl": "https://jobs.ashbyhq.com/legionhealth/2",
                 "descriptionPlain": "Legion Health is hiring clinicians."}]})
        return httpx.Response(404)

    result = discover_board(make_fetcher(handler), "Legion Health", yc_slug="legion-health",
                            website="https://www.legionhealth.com", known_titles=["Software Engineer"])
    assert result.board.slug == "legion-health" and "cite the company's website" in result.evidence


def test_domain_citation_is_whole_domain():
    from core.ats_discovery import _cites_domain

    assert _cites_domain("At Legion Health https://legionhealth.com/, we", "legionhealth.com")
    assert _cites_domain("visit www.acme.ai today", "acme.ai")
    assert not _cites_domain("At Legion Health https://legionhealth.com/", "health.com")
    assert not _cites_domain("see notacme.ai", "acme.ai")


def test_discovery_name_alone_is_not_enough():
    def handler(request):
        if request.url.host == "api.ashbyhq.com":
            return httpx.Response(200, json={"jobs": [{"id": "1", "title": "Nurse", "jobUrl": "https://x/1",
                                                       "descriptionPlain": "CTGT is hiring."}]})
        return httpx.Response(404)

    result = discover_board(make_fetcher(handler), "CTGT", yc_slug="ctgt", website=None, known_titles=["Intern"])
    assert result.board is None


def test_yc_opening_missing_from_confirmed_company_board():
    result = verify(inp(sources=[SourceInfo("yc", "https://www.ycombinator.com/companies/acme/jobs/x")],
                        apply_url="https://account.ycombinator.com/x", company_board="ashby:acme"))
    assert result.status == NEEDS_REVIEW
    assert "does not list this opening" in result.summary


def test_discovery_nothing_found():
    result = discover_board(make_fetcher(lambda r: httpx.Response(404)), "Acme", yc_slug="acme", website=None,
                            known_titles=["ML Engineer"])
    assert result.board is None and result.requests == len(result.tried) == 3  # one distinct slug x 3 ATS


# --- database: migration, expiry, end-to-end ------------------------------------

def test_missing_columns_are_added_to_an_existing_database(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = create_engine(url)
    with engine.begin() as conn:  # an older schema without the new columns, holding data
        conn.execute(text("CREATE TABLE companies (id INTEGER PRIMARY KEY, name VARCHAR(300), "
                          "normalized_name VARCHAR(300), website VARCHAR(500), domain VARCHAR(255), "
                          "ats_board VARCHAR(200), verification_status VARCHAR(20), verification_evidence JSON, "
                          "created_at DATETIME, updated_at DATETIME)"))
        conn.execute(text("INSERT INTO companies (id, name, normalized_name) VALUES (1, 'Acme', 'acme')"))
    factory = init_db(url)
    with factory() as s:
        company = s.get(Company, 1)
        assert company.name == "Acme" and company.ats_checked_at is None


def rec(source, sid, **kw):
    data = dict(source=source, source_job_id=sid, title="Computer Vision Engineer", company_name="Acme",
                location="Bengaluru", description="Acme builds YOLO object detection with PyTorch.",
                job_url=f"https://jobs.ashbyhq.com/acme/{sid}", ats_board="ashby:acme")
    data.update(kw)
    return JobRecord(**data)


class Static(BaseCollector):
    def __init__(self, source, records, complete_boards=()):
        super().__init__(fetcher=SimpleNamespace(requests_made=0))
        self.source, self.records, self._boards = source, records, list(complete_boards)

    def collect(self):
        self.complete_boards = list(self._boards)
        return list(self.records)


def test_job_missing_from_complete_board_is_expired_then_revived(tmp_path):
    factory = init_db(fresh_db_url(tmp_path, 'e'))
    prefs = load_preferences()
    run_pipeline(factory, {}, preferences=prefs, matcher=None,
                 collectors=[Static("ats", [rec("ashby", "1"), rec("ashby", "2", title="NLP Engineer")], ["ashby:acme"])])
    summary = run_pipeline(factory, {}, preferences=prefs, matcher=None,
                           collectors=[Static("ats", [rec("ashby", "1")], ["ashby:acme"])])
    assert summary.stats["ats"]["expired"] == 1
    with factory() as s:
        expired = {j.title: j.expired_at is not None for j in s.query(Job)}
        assert expired == {"Computer Vision Engineer": False, "NLP Engineer": True}
    run_pipeline(factory, {}, preferences=prefs, matcher=None,
                 collectors=[Static("ats", [rec("ashby", "1"), rec("ashby", "2", title="NLP Engineer")], ["ashby:acme"])])
    with factory() as s:
        assert all(j.expired_at is None for j in s.query(Job))


def test_failed_board_does_not_expire_its_jobs(tmp_path):
    factory = init_db(fresh_db_url(tmp_path, 'f'))
    prefs = load_preferences()
    run_pipeline(factory, {}, preferences=prefs, matcher=None,
                 collectors=[Static("ats", [rec("ashby", "1")], ["ashby:acme"])])
    run_pipeline(factory, {}, preferences=prefs, matcher=None, collectors=[Static("ats", [], [])])  # board failed
    with factory() as s:
        assert s.query(Job).one().expired_at is None


def test_pipeline_verifies_company_board_listing(tmp_path):
    factory = init_db(fresh_db_url(tmp_path, 'v'))
    summary = run_pipeline(factory, {}, preferences=load_preferences(), matcher=None,
                           collectors=[Static("ats", [rec("ashby", "1")], ["ashby:acme"])])
    assert summary.stats["verified"] == {VERIFIED: 1}
    with factory() as s:
        job = s.query(Job).one()
        assert job.verification_status == VERIFIED
        assert job.verification_evidence["summary"] == "Listed on Acme's own Ashby job board."
        assert job.company.verification_status == "board_confirmed"


def test_discovery_candidates_respect_limit_and_recheck(tmp_path):
    factory = init_db(fresh_db_url(tmp_path, 'd'))
    now = utcnow()
    with factory.begin() as s:
        repo = JobRepository(s)
        for name in ("Alpha", "Beta", "Gamma"):
            job, _ = repo.upsert_record(rec("yc", name, company_name=name, ats_board=None,
                                            job_url=f"https://www.ycombinator.com/companies/{name.lower()}/jobs/x"))
            job.filter_status = "passed"
    with factory.begin() as s:
        repo = JobRepository(s)
        found = repo.discovery_candidates(now, timedelta(days=30), limit=2)
        assert len(found) == 2 and found[0][1] in {"alpha", "beta", "gamma"}
        for company, _, _ in found:
            repo.record_discovery(company, None, "nothing", now)
    with factory() as s:
        left = JobRepository(s).discovery_candidates(now, timedelta(days=30), limit=5)
        assert [c.name for c, _, _ in left] == ["Gamma"]
        checked = s.query(Company).filter(Company.verification_status == "board_not_found").all()
        assert len(checked) == 2 and checked[0].verification_evidence["detail"] == "nothing"


def test_pipeline_discovers_yc_company_board_and_verifies_the_yc_listing(tmp_path):
    """End to end: YC listing stored -> next run discovers the company's Ashby board -> the same opening on
    the board merges with the YC listing -> the job becomes VERIFIED. Network is simulated."""
    factory = init_db(fresh_db_url(tmp_path, 'p'))
    prefs = load_preferences()
    yc_record = rec("yc", "117579", title="ML Engineer, LLM's", company_name="Knowtex", ats_board=None,
                    location="Bengaluru", job_url="https://www.ycombinator.com/companies/knowtex/jobs/X6F-ml")
    run_pipeline(factory, {}, preferences=prefs, matcher=None, collectors=[Static("yc", [yc_record])])
    with factory() as s:
        assert s.query(Job).one().verification_status == NEEDS_REVIEW

    def handler(request):
        if request.url.host == "api.ashbyhq.com" and request.url.path.endswith("/knowtex"):
            return httpx.Response(200, json={"jobs": [{
                "id": "abc", "title": "ML Engineer, LLM's", "location": "Bengaluru",
                "descriptionPlain": "Acme builds YOLO object detection with PyTorch.",
                "jobUrl": "https://jobs.ashbyhq.com/knowtex/abc",
                "applyUrl": "https://jobs.ashbyhq.com/knowtex/abc/application"}]})
        return httpx.Response(404)

    config = {"ats": {"enabled": True, "discover_for_yc": True, "max_discoveries_per_run": 5, "boards": []}}
    summary = run_pipeline(factory, config, fetcher=make_fetcher(handler), preferences=prefs, matcher=None)
    assert summary.stats["discovery"]["found"] == 1
    assert summary.stats["ats"]["merged"] == 1
    with factory() as s:
        job = s.query(Job).one()
        assert sorted(src.source for src in job.sources) == ["ashby", "yc"]
        assert job.company.ats_board == "ashby:knowtex"
        assert job.verification_status == VERIFIED, job.verification_evidence
    # The board is remembered: the next run polls it without discovering again.
    again = run_pipeline(factory, config, fetcher=make_fetcher(handler), preferences=prefs, matcher=None)
    assert again.stats["discovery"]["checked"] == 0 and again.stats["ats"]["updated"] == 1
