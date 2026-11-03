"""Storage and pipeline tests on a temporary SQLite file (persistence across runs is the point)."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from tests.conftest import fresh_db_url

from collectors.base_collector import BaseCollector
from core.models import JobRecord, WorkArrangement
from core.normalizer import canonicalize_url, domain_of, normalize_company, normalize_title
from core.pipeline import run_pipeline
from database.models import CollectorRun, Job, JobSource, PipelineRun
from database.repository import JobRepository, init_db, normalize_database_url


def record(source_job_id="1", **overrides) -> JobRecord:
    data = dict(
        source="ashby", source_job_id=source_job_id, title="ML Engineer", company_name="Acme AI Pvt Ltd",
        job_url=f"https://jobs.ashbyhq.com/acme/{source_job_id}?utm_source=x",
        apply_url=f"https://jobs.ashbyhq.com/acme/{source_job_id}/application",
        location="Bengaluru", work_arrangement=WorkArrangement.HYBRID, ats_board="ashby:acme",
        company_website="https://www.acme.ai",
    )
    data.update(overrides)
    return JobRecord(**data)


class StaticCollector(BaseCollector):
    def __init__(self, source, records=None, fail=False):
        super().__init__(fetcher=SimpleNamespace(requests_made=0))
        self.source, self.records, self.fail = source, records or [], fail

    def collect(self):
        if self.fail:
            raise RuntimeError("source down")
        return list(self.records)


@pytest.fixture
def db_url(tmp_path):
    return fresh_db_url(tmp_path)


# --- normalizer --------------------------------------------------------------

def test_canonicalize_url():
    assert canonicalize_url("HTTPS://WWW.Example.com/jobs/1/?utm_source=li&b=2&a=1#apply") == \
        "https://example.com/jobs/1?a=1&b=2"
    assert canonicalize_url("https://x.com/?gh_jid=5&gh_src=abc") == "https://x.com/?gh_jid=5"


def test_normalize_company_and_title():
    assert normalize_company("Acme Technologies Pvt. Ltd.") == normalize_company("ACME") == "acme"
    assert normalize_title("Sr. ML Engineer (Remote, India)") == "senior ml engineer"
    assert normalize_title("C++ / Python Engineer") == "c++ python engineer"


def test_domain_of():
    assert domain_of("https://www.acme.ai/careers") == "acme.ai"
    assert domain_of("acme.ai") == "acme.ai"
    assert domain_of(None) is None


def test_normalize_database_url():
    assert normalize_database_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_database_url("sqlite:///x.db") == "sqlite:///x.db"


# --- repository --------------------------------------------------------------

def test_upsert_insert_then_update(db_url):
    factory = init_db(db_url)
    with factory.begin() as s:
        job, outcome = JobRepository(s).upsert_record(record(salary_text=None))
        assert outcome == "new"
    with factory.begin() as s:
        job, outcome = JobRepository(s).upsert_record(record(salary_text="₹20L", description="Now with details"))
        assert outcome == "updated"
    with factory() as s:
        job = s.query(Job).one()
        assert job.salary_text == "₹20L" and job.description == "Now with details"
        assert job.company.normalized_name == "acme ai"
        assert job.company.domain == "acme.ai"
        [src] = job.sources
        assert src.canonical_url == "https://jobs.ashbyhq.com/acme/1"
        assert job.first_seen_at.tzinfo is not None


def test_update_never_blanks_existing_values(db_url):
    factory = init_db(db_url)
    with factory.begin() as s:
        JobRepository(s).upsert_record(record(salary_text="₹20L", description="Full text"))
    with factory.begin() as s:
        JobRepository(s).upsert_record(record(salary_text=None, description=""))
    with factory() as s:
        job = s.query(Job).one()
        assert (job.salary_text, job.description) == ("₹20L", "Full text")


def test_known_source_ids_requires_details(db_url):
    factory = init_db(db_url)
    with factory.begin() as s:
        repo = JobRepository(s)
        repo.upsert_record(record("a", source="yc", title="A", details_complete=False))
        repo.upsert_record(record("b", source="yc", title="B", details_complete=True))
    with factory() as s:
        repo = JobRepository(s)
        assert repo.known_source_ids("yc") == {"a", "b"}
        assert repo.known_source_ids("yc", require_details=True) == {"b"}


def test_state_roundtrip(db_url):
    factory = init_db(db_url)
    with factory.begin() as s:
        JobRepository(s).set_state("telegram_offset", "10")
    with factory.begin() as s:
        repo = JobRepository(s)
        repo.set_state("telegram_offset", "11")
    with factory() as s:
        assert JobRepository(s).get_state("telegram_offset") == "11"
        assert JobRepository(s).get_state("missing", "d") == "d"


def test_naive_datetime_rejected(db_url):
    factory = init_db(db_url)
    with pytest.raises(Exception):
        with factory.begin() as s:
            JobRepository(s).upsert_record(record(posted_at=datetime(2026, 1, 1)))


# --- pipeline ----------------------------------------------------------------

def test_pipeline_persists_between_runs_and_reports_new_only_once(db_url):
    collectors = lambda: [StaticCollector("ats", [record("1"), record("2", title="NLP Engineer")])]  # noqa: E731
    first = run_pipeline(init_db(db_url), {}, collectors=collectors())
    # A brand-new engine on the same file simulates the next scheduled run.
    second = run_pipeline(init_db(db_url), {}, collectors=collectors())
    assert first.status == second.status == "ok"
    assert len(first.new_job_ids) == 2
    assert second.new_job_ids == []
    assert second.stats["ats"]["updated"] == 2
    with init_db(db_url)() as s:
        assert s.query(Job).count() == 2
        assert s.query(PipelineRun).filter_by(status="ok").count() == 2


def test_pipeline_one_source_failing_is_partial(db_url):
    factory = init_db(db_url)
    summary = run_pipeline(factory, {}, collectors=[
        StaticCollector("yc", fail=True),
        StaticCollector("ats", [record("1")]),
    ])
    assert summary.status == "partial"
    assert len(summary.new_job_ids) == 1
    assert any("source down" in e for e in summary.errors)
    with factory() as s:
        runs = {r.source: r for r in s.query(CollectorRun)}
        assert runs["yc"].ok is False and runs["ats"].ok is True
        run = s.query(PipelineRun).one()
        assert run.status == "partial" and run.finished_at is not None


def test_pipeline_storage_failure_rolls_back_only_that_source(db_url):
    factory = init_db(db_url)
    bad = record("x", posted_at=datetime(2026, 1, 1))  # naive datetime -> DB layer raises
    summary = run_pipeline(factory, {}, collectors=[
        StaticCollector("yc", [record("y1", source="yc"), bad.model_copy(update={"source": "yc"})]),
        StaticCollector("ats", [record("1")]),
    ])
    assert summary.status == "partial"
    assert summary.stats["yc"]["ok"] is False and summary.stats["yc"].get("new", 0) == 0
    with factory() as s:
        assert {src.source for src in s.query(JobSource)} == {"ashby"}
        assert len(summary.new_job_ids) == 1 and s.get(Job, summary.new_job_ids[0]) is not None


def test_later_runs_filter_on_stored_description(db_url):
    """YC sends descriptions only once; a later run must still see them when filtering."""
    from core.preferences import load_preferences

    prefs = load_preferences()
    detailed = record("y1", source="yc", title="Software Engineer", location="Bengaluru",
                      description="Requires 5+ years of experience with PyTorch, LLM and RAG systems.")
    listing_only = detailed.model_copy(update={"description": "", "details_complete": False})

    class YC(StaticCollector):
        pass

    factory = init_db(db_url)
    run_pipeline(factory, {}, collectors=[YC("yc", [detailed])], preferences=prefs, matcher=None)
    second = run_pipeline(factory, {}, collectors=[YC("yc", [listing_only])], preferences=prefs, matcher=None)
    assert second.stats["yc"]["rejected"] == 1
    with factory() as s:
        job = s.query(Job).one()
        assert job.filter_status == "rejected"
        assert "5+" in job.filter_details["reasons"][0]
        assert job.description  # kept, since YC will not send it again


def test_preference_change_reevaluates_jobs_not_collected_again(db_url):
    from core.preferences import load_preferences

    prefs = load_preferences()
    factory = init_db(db_url)
    run_pipeline(factory, {}, preferences=prefs, matcher=None, collectors=[
        StaticCollector("ats", [record("1", title="ML Engineer", location="Berlin, Germany", description="ML team.")])])
    with factory() as s:
        assert s.query(Job).one().filter_status == "review"

    stricter = prefs.model_copy(deep=True)
    stricter.location.abroad_sponsorship_unknown = "reject"
    summary = run_pipeline(factory, {}, preferences=stricter, matcher=None, collectors=[])  # nothing collected
    assert summary.stats["reevaluated"] == {"rejected": 1}
    with factory() as s:
        assert s.query(Job).one().filter_status == "rejected"
    # Same preferences again: no re-evaluation needed.
    assert "reevaluated" not in run_pipeline(factory, {}, preferences=stricter, matcher=None, collectors=[]).stats


def test_rejected_descriptions_kept_until_stale(db_url):
    from datetime import timedelta

    from core.models import utcnow

    factory = init_db(db_url)
    with factory.begin() as s:
        repo = JobRepository(s)
        job, _ = repo.upsert_record(record("1", title="Sales Executive", description="Sell things."))
        job.filter_status = "rejected"
    with factory.begin() as s:
        assert JobRepository(s).prune_stale_descriptions(utcnow()) == 0
        assert JobRepository(s).prune_stale_descriptions(utcnow() + timedelta(days=31)) == 1
    with factory() as s:
        assert s.query(Job).one().description == ""


def test_pipeline_all_failed(db_url):
    summary = run_pipeline(init_db(db_url), {}, collectors=[StaticCollector("yc", fail=True)])
    assert summary.status == "failed"


def test_pipeline_scores_kept_jobs_and_clears_rejected(db_url):
    from core.preferences import load_preferences

    prefs = load_preferences()
    factory = init_db(db_url)
    good = record("1", title="Computer Vision Engineer", description="YOLO object detection with PyTorch.")
    summary = run_pipeline(factory, {}, preferences=prefs, matcher=None, collectors=[StaticCollector("ats", [good])])
    assert sum(v for k, v in summary.stats["scored"].items() if k != "semantic") == 1
    with factory() as s:
        job = s.query(Job).one()
        assert job.relevance_score is not None and "Object Detection" in job.score_details["matched_skills"]

    data = prefs.model_dump()
    data["titles"]["reject_terms"].append("computer vision")
    stricter = type(prefs).model_validate(data)  # fresh object: compiled term patterns are cached per instance
    run_pipeline(factory, {}, preferences=stricter, matcher=None, collectors=[])
    with factory() as s:
        job = s.query(Job).one()
        assert job.filter_status == "rejected" and job.relevance_score is None


def test_overlong_and_nul_text_is_stored_safely(db_url):
    """Postgres enforces VARCHAR lengths and rejects NUL characters; scraped text may contain both."""
    factory = init_db(db_url)
    nasty = record("x", title="ML Engineer " + "y" * 700, location="Bengaluru " * 80,
                   salary_text="$1\x00" * 200, description="Uses PyTorch\x00 daily.")
    with factory.begin() as s:
        JobRepository(s).upsert_record(nasty)
    with factory() as s:
        job = s.query(Job).one()
        assert len(job.title) == 500 and job.title.startswith("ML Engineer y")
        assert len(job.location) <= 500 and "\x00" not in job.salary_text
        assert job.description == "Uses PyTorch daily."


def test_migration_copies_everything_and_new_rows_still_get_fresh_ids(tmp_path):
    from scripts.migrate_database import migrate

    source = f"sqlite:///{tmp_path / 'source.db'}"
    factory = init_db(source)
    with factory.begin() as s:
        repo = JobRepository(s)
        for i in range(3):
            repo.upsert_record(record(str(i), title=f"Role {i}"))
        repo.set_state("telegram_update_offset", "77")

    target = fresh_db_url(tmp_path, "target")
    counts = migrate(source, target)
    assert counts["jobs"] == (3, 3) and counts["job_sources"] == (3, 3) and counts["kv_state"] == (1, 1)

    target_factory = init_db(target)
    with target_factory.begin() as s:
        job, outcome = JobRepository(s).upsert_record(record("new", title="Brand New Role"))
        assert outcome == "new" and job.id == 4  # the id sequence continues after the copied rows
    with target_factory() as s:
        assert JobRepository(s).get_state("telegram_update_offset") == "77"
    with pytest.raises(SystemExit):
        migrate(source, target)  # refuses a target that already has jobs
