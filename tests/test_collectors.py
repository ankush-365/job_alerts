"""Collector tests with synthetic payloads shaped like the real responses (no network)."""

import html
import json

import httpx
import pytest

from collectors.ats_collector import (
    AtsBoard,
    AtsCollector,
    discover_board,
    parse_ashby,
    parse_greenhouse,
    parse_lever,
)
from collectors.base_collector import FetchError, HttpFetcher
from collectors.registry import build_collectors, load_sources_config
from collectors.yc_collector import YCCollector, extract_page_props
from core.models import WorkArrangement
from core.text import html_to_text


def make_fetcher(handler, **kwargs):
    sleeps = []
    fetcher = HttpFetcher(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
        clock=lambda: 0.0,
        min_interval=0,
        **kwargs,
    )
    return fetcher, sleeps


def yc_page(props: dict, ld: dict | None = None) -> str:
    attr = html.escape(json.dumps({"component": "x", "props": props}), quote=True)
    ld_block = f'<script type="application/ld+json">{json.dumps(ld)}</script>' if ld else ""
    return f'<html><head>{ld_block}</head><body><div id="app" data-page="{attr}"></div></body></html>'


YC_POSTING = {
    "id": 101,
    "title": "Computer Vision Engineer",
    "url": "/companies/acme/jobs/abc-cv-engineer",
    "applyUrl": "https://account.ycombinator.com/authenticate?continue=x",
    "location": "Bengaluru, KA, IN / Remote (IN)",
    "type": "Full-time",
    "salaryRange": "₹8L - ₹15L",
    "minExperience": "Any (new grads ok)",
    "visa": "Will sponsor",
    "skills": ["Python", "PyTorch"],
    "companyName": "Acme AI",
}


# --- HttpFetcher -----------------------------------------------------------

def test_fetcher_retries_5xx_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, text="ok")

    fetcher, sleeps = make_fetcher(handler, backoff_base=1.0)
    assert fetcher.get("https://example.com/x").text == "ok"
    assert sleeps == [1.0, 2.0]
    assert fetcher.requests_made == 3


def test_fetcher_honours_retry_after_and_does_not_retry_404():
    seq = [httpx.Response(429, headers={"Retry-After": "5"}), httpx.Response(404)]

    fetcher, sleeps = make_fetcher(lambda r: seq.pop(0))
    with pytest.raises(FetchError) as exc:
        fetcher.get("https://example.com/x")
    assert exc.value.status_code == 404
    assert sleeps == [5.0]


def test_fetcher_rate_limits_per_host():
    now = [0.0]
    sleeps = []
    fetcher = HttpFetcher(
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        sleep=sleeps.append, clock=lambda: now[0], min_interval=2.0,
    )
    fetcher.get("https://a.com/1")
    now[0] = 0.5
    fetcher.get("https://a.com/2")
    fetcher.get("https://b.com/1")
    assert sleeps == [1.5]


# --- YC --------------------------------------------------------------------

def test_yc_list_and_detail_enrichment():
    detail = yc_page(
        {"job": {"description": "Build **YOLO** pipelines."}, "company": {"website": "https://acme.ai"}},
        ld={"@type": "JobPosting", "datePosted": "2026-10-19T23:35:57Z"},
    )

    def handler(request):
        if request.url.path == "/jobs/role/software-engineer/india":
            return httpx.Response(200, text=yc_page({"jobPostings": [YC_POSTING]}))
        if request.url.path == "/companies/acme/jobs/abc-cv-engineer":
            return httpx.Response(200, text=detail)
        return httpx.Response(404)

    fetcher, _ = make_fetcher(handler)
    result = YCCollector(fetcher, role_paths=["/jobs/role/software-engineer/india"]).run()
    assert result.ok and result.errors == []
    job = result.jobs[0]
    assert job.source_job_id == "101"
    assert job.job_url == "https://www.ycombinator.com/companies/acme/jobs/abc-cv-engineer"
    assert job.work_arrangement == WorkArrangement.REMOTE
    assert job.salary_text == "₹8L - ₹15L"
    assert job.experience_text == "Any (new grads ok)"
    assert job.description == "Build **YOLO** pipelines."
    assert job.company_website == "https://acme.ai"
    assert job.posted_at.isoformat() == "2026-10-19T23:35:57+00:00"


def test_yc_skips_detail_for_known_jobs_and_dedupes_across_pages():
    requested = []

    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(200, text=yc_page({"jobPostings": [YC_POSTING]}))

    fetcher, _ = make_fetcher(handler)
    collector = YCCollector(fetcher, role_paths=["/p1", "/p2"], known_job_ids={"101"})
    result = collector.run()
    assert len(result.jobs) == 1
    assert requested == ["/p1", "/p2"]  # no detail fetch
    assert result.jobs[0].description == ""


def test_yc_one_failed_page_is_reported_not_fatal():
    def handler(request):
        if request.url.path == "/bad":
            return httpx.Response(403)
        return httpx.Response(200, text=yc_page({"jobPostings": []}))

    fetcher, _ = make_fetcher(handler)
    result = YCCollector(fetcher, role_paths=["/bad", "/good"]).run()
    assert result.ok
    assert any("/bad" in e and "403" in e for e in result.errors)


def test_yc_all_pages_failing_marks_result_failed():
    fetcher, _ = make_fetcher(lambda r: httpx.Response(200, text="<html>layout changed</html>"))
    result = YCCollector(fetcher, role_paths=["/a"]).run()
    assert not result.ok and result.jobs == []
    assert "data-page" in result.errors[-1]


def test_extract_page_props_missing_raises():
    with pytest.raises(ValueError):
        extract_page_props("<html></html>")


# --- ATS parsers -------------------------------------------------------------

def test_parse_greenhouse_unescapes_html_content():
    data = {"jobs": [{
        "id": 55, "title": " ML Engineer ", "company_name": "Observe.AI",
        "content": "&lt;p&gt;Work on &lt;b&gt;NLP&lt;/b&gt;&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;",
        "location": {"name": "Bangalore, India"},
        "absolute_url": "https://job-boards.greenhouse.io/observeai/jobs/55",
        "first_published": "2026-10-01T10:00:00-04:00",
    }]}
    [job] = parse_greenhouse(data, "observeai", "Observe.AI")
    assert job.title == "ML Engineer"
    assert job.description == "Work on NLP\n\n- Python"
    assert job.ats_board == "greenhouse:observeai"
    assert job.posted_at.utcoffset().total_seconds() == -4 * 3600


def test_parse_lever_combines_sections_and_salary():
    data = [{
        "id": "abc", "text": "AI Engineer", "descriptionPlain": "Intro.",
        "lists": [{"text": "Requirements", "content": "<li>PyTorch</li><li>RAG</li>"}],
        "additionalPlain": "Benefits.", "categories": {"commitment": "Intern", "location": "Remote - India"},
        "workplaceType": "remote", "salaryRange": {"min": 30000, "max": 50000, "currency": "INR", "interval": "per-month-salary"},
        "createdAt": 1760000000000, "hostedUrl": "https://jobs.lever.co/cred/abc",
        "applyUrl": "https://jobs.lever.co/cred/abc/apply",
    }]
    [job] = parse_lever(data, "cred", "CRED")
    assert "Requirements\n- PyTorch\n- RAG" in job.description
    assert job.employment_type == "Intern"
    assert job.work_arrangement == WorkArrangement.REMOTE
    assert (job.salary_min, job.salary_max, job.salary_currency) == (30000, 50000, "INR")
    assert job.posted_at.year == 2025


def test_parse_ashby_skips_unlisted_and_merges_locations():
    data = {"jobs": [
        {"id": "1", "title": "LLM Engineer", "isListed": True, "location": "Bengaluru",
         "secondaryLocations": [{"location": "Remote - India"}], "workplaceType": "Hybrid",
         "employmentType": "FullTime", "descriptionPlain": "LangGraph agents",
         "compensation": {"compensationTierSummary": "₹20L – ₹30L"},
         "jobUrl": "https://jobs.ashbyhq.com/sarvam/1", "applyUrl": "https://jobs.ashbyhq.com/sarvam/1/application",
         "publishedAt": "2026-10-30T00:00:00.000+00:00"},
        {"id": "2", "title": "Hidden", "isListed": False, "jobUrl": "https://jobs.ashbyhq.com/sarvam/2"},
    ]}
    jobs = parse_ashby(data, "sarvam", "Sarvam AI")
    assert [j.title for j in jobs] == ["LLM Engineer"]
    assert jobs[0].location == "Bengaluru / Remote - India"
    assert jobs[0].work_arrangement == WorkArrangement.HYBRID
    assert jobs[0].salary_text == "₹20L – ₹30L"


def test_ats_collector_isolates_failing_board():
    def handler(request):
        if "greenhouse" in request.url.host:
            return httpx.Response(500)
        return httpx.Response(200, json={"jobs": [{"id": "1", "title": "ML", "jobUrl": "https://jobs.ashbyhq.com/x/1"}]})

    fetcher, _ = make_fetcher(handler, max_retries=0)
    result = AtsCollector(fetcher, [AtsBoard("greenhouse", "bad"), AtsBoard("ashby", "good", "Good Co")]).run()
    assert result.ok
    assert [j.company_name for j in result.jobs] == ["Good Co"]
    assert any("greenhouse:bad" in e for e in result.errors)


def test_discover_board_uses_404_as_absent():
    def handler(request):
        return httpx.Response(200, json=[]) if "lever" in request.url.host else httpx.Response(404)

    fetcher, _ = make_fetcher(handler)
    assert discover_board(fetcher, "acme") == "lever"
    fetcher, _ = make_fetcher(lambda r: httpx.Response(404))
    assert discover_board(fetcher, "nobody") is None


def test_per_host_interval_override():
    now = [0.0]
    sleeps = []
    fetcher = HttpFetcher(
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        sleep=sleeps.append, clock=lambda: now[0], min_interval=2.0, host_intervals={"fast.com": 0.5},
    )
    fetcher.get("https://fast.com/1")
    fetcher.get("https://fast.com/2")
    fetcher.get("https://slow.com/1")
    fetcher.get("https://slow.com/2")
    assert sleeps == [0.5, 2.0]


def test_ats_boards_interleaved_by_provider():
    boards = [AtsBoard("ashby", "a1"), AtsBoard("ashby", "a2"), AtsBoard("ashby", "a3"),
              AtsBoard("greenhouse", "g1"), AtsBoard("lever", "l1")]
    order = [f"{b.ats}:{b.slug}" for b in AtsCollector(None, boards)._interleaved()]
    assert order == ["ashby:a1", "greenhouse:g1", "lever:l1", "ashby:a2", "ashby:a3"]


def test_unsupported_ats_rejected():
    with pytest.raises(ValueError):
        AtsBoard("workday", "x")


# --- text & config -----------------------------------------------------------

def test_html_to_text_drops_scripts():
    assert html_to_text("<p>Hi</p><script>alert(1)</script><p>there</p>") == "Hi\n\nthere"


def test_shipped_sources_config_builds_collectors():
    config = load_sources_config()
    fetcher, _ = make_fetcher(lambda r: httpx.Response(200))
    collectors = build_collectors(config, fetcher)
    assert [c.source for c in collectors] == ["yc", "ats"]
    assert len(collectors[1].boards) >= 30
