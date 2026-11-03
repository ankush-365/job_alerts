"""Duplicate detection: same opening across platforms becomes one job with several sources."""

from datetime import timedelta

import pytest

from tests.conftest import fresh_db_url

from core.deduplicator import description_similarity, locations_compatible
from core.models import JobRecord, utcnow
from database.models import Job, JobSource
from database.repository import JobRepository, init_db

DESC = "We are hiring a computer vision engineer to build YOLO detection and tracking pipelines on RTSP streams."


def rec(source, sid, *, title="Computer Vision Engineer", company="Acme AI", location="Bengaluru", url=None,
        description=DESC, website=None) -> JobRecord:
    return JobRecord(source=source, source_job_id=sid, title=title, company_name=company, location=location,
                     description=description, company_website=website,
                     job_url=url or f"https://{source}.example.com/jobs/{sid}")


@pytest.fixture
def repo(tmp_path):
    factory = init_db(fresh_db_url(tmp_path, 'd'))
    session = factory()
    yield JobRepository(session)
    session.close()


def test_location_compatibility():
    assert locations_compatible("Bangalore, India", "Bengaluru, KA, IN")
    assert locations_compatible(None, "Pune")
    assert locations_compatible("Remote", "Hyderabad")  # 'remote' alone carries no place
    assert not locations_compatible("Hyderabad", "Pune")


def test_description_similarity():
    assert description_similarity(DESC, DESC) == 1.0
    assert description_similarity(DESC, "Sell our product to enterprise accounts across EMEA.") < 0.1


def test_same_opening_on_two_platforms_merges(repo):
    _, first = repo.upsert_record(rec("yc", "1", company="Acme AI"))
    _, second = repo.upsert_record(rec("ashby", "a1", company="ACME AI Pvt. Ltd.", location="Bangalore, India"))
    assert (first, second) == ("new", "merged")
    job = repo.session.query(Job).one()
    assert sorted(s.source for s in job.sources) == ["ashby", "yc"]
    assert [s.is_primary for s in sorted(job.sources, key=lambda s: s.source)] == [False, True]


def test_merge_by_company_domain_when_names_differ(repo):
    repo.upsert_record(rec("yc", "1", company="Acme", website="https://acme.ai"))
    _, outcome = repo.upsert_record(rec("greenhouse", "g1", company="Acme Robotics", website="https://www.acme.ai/"))
    assert outcome == "merged"


def test_same_canonical_url_merges(repo):
    repo.upsert_record(rec("yc", "1", url="https://jobs.ashbyhq.com/acme/xyz?utm_source=yc"))
    _, outcome = repo.upsert_record(rec("ashby", "xyz", title="CV Engineer II", url="https://jobs.ashbyhq.com/acme/xyz"))
    assert outcome == "merged"


def test_different_city_is_a_different_opening(repo):
    repo.upsert_record(rec("yc", "1", location="Hyderabad"))
    _, outcome = repo.upsert_record(rec("ashby", "a1", location="Pune"))
    assert outcome == "new"


def test_different_description_same_title_not_merged(repo):
    repo.upsert_record(rec("yc", "1", title="Software Engineer", description=DESC))
    _, outcome = repo.upsert_record(rec("ashby", "a1", title="Software Engineer",
                                        description="Own billing microservices in Go, Kafka and Postgres for payments."))
    assert outcome == "new"


def test_two_live_listings_on_same_board_stay_separate(repo):
    repo.upsert_record(rec("ashby", "a1"))
    _, outcome = repo.upsert_record(rec("ashby", "a2"))
    assert outcome == "new"


def test_repost_after_old_listing_went_stale_merges(repo):
    old = utcnow() - timedelta(days=10)
    repo.upsert_record(rec("ashby", "a1"), now=old)
    _, outcome = repo.upsert_record(rec("ashby", "a2"))
    assert outcome == "merged"


def test_old_jobs_outside_lookback_not_merged(repo):
    repo.upsert_record(rec("yc", "1"), now=utcnow() - timedelta(days=90))
    _, outcome = repo.upsert_record(rec("ashby", "a1"))
    assert outcome == "new"


def test_secondary_source_fills_gaps_but_does_not_overwrite(repo):
    first = rec("yc", "1", description="")
    first.salary_text = "₹10L - ₹15L"
    repo.upsert_record(first)
    second = rec("ashby", "a1")
    second.salary_text = "$1"
    repo.upsert_record(second)
    job = repo.session.query(Job).one()
    assert job.salary_text == "₹10L - ₹15L"
    assert job.description == DESC
    # The secondary source seen again is just an update of its source row.
    _, outcome = repo.upsert_record(second)
    assert outcome == "updated"
    assert repo.session.query(JobSource).count() == 2
