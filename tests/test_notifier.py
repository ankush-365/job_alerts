"""Alerts, digest and bot commands with a fake Telegram client (nothing is really sent)."""

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import fresh_db_url

from core.models import JobRecord, utcnow
from core.pipeline import run_pipeline
from core.preferences import load_preferences
from database.models import Job, Notification
from database.repository import JobRepository, init_db
from notifications.formatter import JobView, format_alert, format_digest, link
from notifications.notifier import OFFSET_KEY, TelegramNotifier
from notifications.telegram_bot import MAX_MESSAGE_LENGTH, TelegramError

CHAT = 4242


class FakeClient:
    def __init__(self, fail_with: list[TelegramError | None] | None = None):
        self.sent: list[tuple[int, str]] = []
        self.updates: list[dict] = []
        self.offsets: list[int | None] = []
        self.fail_with = list(fail_with or [])

    def send_message(self, chat_id, text, **kwargs):
        if self.fail_with:
            error = self.fail_with.pop(0)
            if error is not None:
                raise error
        assert len(text) <= MAX_MESSAGE_LENGTH
        self.sent.append((chat_id, text))
        return {"message_id": len(self.sent)}

    def get_updates(self, offset=None, **kwargs):
        self.offsets.append(offset)
        return [u for u in self.updates if offset is None or u["update_id"] >= offset]


@pytest.fixture
def prefs():
    return load_preferences()


@pytest.fixture
def factory(tmp_path):
    return init_db(fresh_db_url(tmp_path, 'n'))


def add_job(factory, sid, *, title="Computer Vision Engineer", score=80.0, status="passed", verification="VERIFIED",
            expired=False, details=True, seen=None, first_seen=None):
    now = seen or utcnow()
    with factory.begin() as s:
        record = JobRecord(source="ashby", source_job_id=sid, title=title, company_name="Acme", location="Bengaluru",
                           job_url=f"https://jobs.ashbyhq.com/acme/{sid}", details_complete=details,
                           apply_url=f"https://jobs.ashbyhq.com/acme/{sid}/application",
                           first_seen_at=first_seen or now)
        job, _ = JobRepository(s).upsert_record(record, now=now)
        job.filter_status = status
        job.filter_details = {"status": status, "flags": [], "required_years": 0}
        job.relevance_score = score
        job.score_details = {"tier": "high", "matched_skills": ["Python"], "missing_skills": [], "reason": "Good fit."}
        job.verification_status = verification
        job.verification_evidence = {"summary": "Listed on Acme's own Ashby job board.", "evidence": []}
        job.expired_at = now if expired else None
        return job.id


def notifier(client):
    return TelegramNotifier(client, CHAT, sleep=lambda s: None)


def at_hour(hour):  # a UTC time that is `hour`:30 in India (UTC+5:30)
    return datetime(2026, 11, 3, hour, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))).astimezone(timezone.utc)


# --- formatting ----------------------------------------------------------------

def view(**kw):
    data = dict(id=1, title="CV Engineer", company="Acme", location="Pune", experience="Not stated",
                employment="Full time", salary=None, score=88, tier="high", verification="VERIFIED",
                verification_summary="Listed on Acme's own Ashby job board.", matched_skills=["Python", "YOLO"],
                missing_skills=[], reason="Strong match.", apply_url="https://jobs.ashbyhq.com/acme/1",
                sources=[("ashby", "https://jobs.ashbyhq.com/acme/1")], flags=[])
    data.update(kw)
    return JobView(**data)


def test_alert_contains_required_fields():
    text = format_alert(view(salary="₹8L - ₹15L"))
    for part in ("NEW AI/ML JOB FOUND", "<b>Position:</b> CV Engineer", "<b>Company:</b> Acme", "<b>Location:</b> Pune",
                 "<b>Experience:</b>", "<b>Salary:</b> ₹8L - ₹15L", "<b>Relevance:</b> 88/100 (High)",
                 "✅ VERIFIED", "Python, YOLO", "Strong match.", '<a href="https://jobs.ashbyhq.com/acme/1">',
                 "Company board (Ashby)"):
        assert part in text


def test_untrusted_text_is_escaped():
    text = format_alert(view(title='<script>alert(1)</script> & "Co"', company="<b>Evil</b>",
                             reason="<a href='https://evil'>click</a>"))
    assert "<script>" not in text and "&lt;script&gt;" in text
    assert "<b>Evil</b>" not in text and "&amp;" in text
    assert "<a href='https://evil'>" not in text and "&lt;a href='https://evil'&gt;" in text


def test_only_http_links_become_anchors():
    assert link("javascript:alert(1)", "x") == "x"
    assert link('https://a.com/?q="><b>', "x") == '<a href="https://a.com/?q=&quot;&gt;&lt;b&gt;">x</a>'


def test_huge_fields_still_fit_one_message():
    text = format_alert(view(title="T" * 5000, reason="R" * 5000, matched_skills=["S" * 300] * 50))
    assert len(text) <= MAX_MESSAGE_LENGTH


def test_digest_splits_into_messages_under_limit():
    jobs = [view(id=i, title=f"Machine Learning Engineer {i} " + "x" * 60) for i in range(200)]
    messages = format_digest("03 Nov 2026", jobs, {"new": 300, "relevant": 200, "alerted": 100})
    assert len(messages) > 1 and all(len(m) <= MAX_MESSAGE_LENGTH for m in messages)
    assert sum(m.count("Machine Learning Engineer") for m in messages) == 200


# --- alerts ----------------------------------------------------------------------

def test_alerts_sent_once_and_never_repeated(factory, prefs):
    add_job(factory, "1")
    client = FakeClient()
    assert notifier(client).send_alerts(factory, prefs, utcnow()) == {"sent": 1, "failed": 0}
    assert notifier(client).send_alerts(factory, prefs, utcnow()) == {"sent": 0, "failed": 0}
    assert len(client.sent) == 1 and client.sent[0][0] == CHAT


@pytest.mark.parametrize("kwargs", [
    dict(score=40.0),                      # below threshold
    dict(status="rejected"),
    dict(status="review"),                 # abroad, sponsorship unknown -> digest only
    dict(verification="SUSPICIOUS"),
    dict(expired=True),
    dict(details=False),                   # YC listing whose details aren't fetched yet
    dict(seen=utcnow() - timedelta(days=5)),  # no longer listed
])
def test_jobs_that_must_not_be_alerted(factory, prefs, kwargs):
    add_job(factory, "1", **kwargs)
    assert notifier(FakeClient()).send_alerts(factory, prefs, utcnow())["sent"] == 0


def test_needs_review_alerted_only_if_enabled(factory, prefs):
    add_job(factory, "1", verification="NEEDS_REVIEW")
    assert notifier(FakeClient()).send_alerts(factory, prefs, utcnow())["sent"] == 1
    factory2 = factory
    with factory2.begin() as s:
        s.query(Notification).delete()
    strict = type(prefs).model_validate({**prefs.model_dump(), "notifications": {
        **prefs.notifications.model_dump(), "notify_needs_review": False}})
    assert notifier(FakeClient()).send_alerts(factory2, strict, utcnow())["sent"] == 0


def test_best_scores_first_and_capped_per_run(factory, prefs):
    for i in range(15):
        add_job(factory, str(i), title=f"ML Engineer {i}", score=60.0 + i)
    client = FakeClient()
    assert notifier(client).send_alerts(factory, prefs, utcnow())["sent"] == 10
    assert "ML Engineer 14" in client.sent[0][1]
    assert notifier(client).send_alerts(factory, prefs, utcnow())["sent"] == 5  # the rest next run


def test_failed_send_is_retried_then_given_up(factory, prefs):
    add_job(factory, "1")
    first = utcnow()
    client = FakeClient(fail_with=[TelegramError("server", status_code=500, retryable=True)])
    assert notifier(client).send_alerts(factory, prefs, first) == {"sent": 0, "failed": 1}
    assert notifier(client).send_alerts(factory, prefs, first + timedelta(hours=1)) == {"sent": 1, "failed": 0}
    with factory() as s:
        row = s.query(Notification).one()
        assert row.status == "sent" and row.created_at == first

    add_job(factory, "2", title="NLP Engineer")
    always_failing = FakeClient(fail_with=[TelegramError("server", status_code=500)] * 5)
    notifier(always_failing).send_alerts(factory, prefs, first)
    assert notifier(FakeClient()).send_alerts(factory, prefs, first + timedelta(hours=25))["sent"] == 0


def test_blocked_bot_stops_the_run(factory, prefs):
    add_job(factory, "1", title="A ML Engineer")
    add_job(factory, "2", title="B ML Engineer")
    client = FakeClient(fail_with=[TelegramError("Forbidden: bot was blocked by the user", status_code=403)])
    assert notifier(client).send_alerts(factory, prefs, utcnow()) == {"sent": 0, "failed": 1}


# --- digest ------------------------------------------------------------------------

def test_digest_once_per_day_after_configured_hour(factory, prefs):
    now = at_hour(20)
    add_job(factory, "1", seen=now, first_seen=now - timedelta(hours=3))
    add_job(factory, "2", title="ML Engineer abroad", status="review", seen=now, first_seen=now - timedelta(hours=2))
    client = FakeClient()
    n = notifier(client)
    n.send_alerts(factory, prefs, now)
    assert n.send_digest(factory, prefs, at_hour(19))["sent"] is False
    result = n.send_digest(factory, prefs, now)
    assert result["sent"] is True and result["jobs"] == 1  # the alerted job is not repeated
    digest = client.sent[-1][1]
    assert "Worth a look" in digest and "ML Engineer abroad" in digest
    assert "Computer Vision Engineer" not in digest and "1 already sent to you as alerts" in digest
    assert n.send_digest(factory, prefs, now + timedelta(hours=1))["sent"] is False  # same day


def test_digest_failure_is_retried_next_run(factory, prefs):
    now = at_hour(21)
    add_job(factory, "1", seen=now, first_seen=now - timedelta(hours=1))
    failing = FakeClient(fail_with=[TelegramError("down", status_code=502)])
    with pytest.raises(TelegramError):
        notifier(failing).send_digest(factory, prefs, now)
    assert notifier(FakeClient()).send_digest(factory, prefs, now + timedelta(hours=1))["sent"] is True


# --- commands ------------------------------------------------------------------------

def update(uid, chat, text):
    return {"update_id": uid, "message": {"text": text, "chat": {"id": chat, "type": "private"}}}


def test_commands_answered_only_for_registered_chat(factory, prefs):
    add_job(factory, "1")
    client = FakeClient()
    client.updates = [update(10, CHAT, "/stats"), update(11, 999, "/latest"), update(12, CHAT, "/latest 3"),
                      update(13, CHAT, "/preferences"), update(14, CHAT, "/whatever")]
    result = notifier(client).handle_commands(factory, prefs, utcnow())
    assert result == {"answered": 4, "ignored": 1}
    assert all(chat == CHAT for chat, _ in client.sent)
    stats, latest, preferences, help_text = (t for _, t in client.sent)
    assert "Jobs discovered: <b>1</b>" in stats
    assert "Computer Vision Engineer" in latest
    assert "Alert threshold" in preferences
    assert "/latest" in help_text and "automatically" in help_text
    with factory() as s:
        assert JobRepository(s).get_state(OFFSET_KEY) == "15"


def test_updates_are_acknowledged_so_commands_are_not_answered_twice(factory, prefs):
    client = FakeClient()
    client.updates = [update(20, CHAT, "/help")]
    n = notifier(client)
    n.handle_commands(factory, prefs, utcnow())
    n.handle_commands(factory, prefs, utcnow())
    assert len(client.sent) == 1 and client.offsets == [None, 21]


# --- pipeline -------------------------------------------------------------------------

def test_pipeline_sends_alerts_automatically_and_survives_telegram_errors(factory, prefs):
    add_job(factory, "1")

    class Broken(FakeClient):
        def get_updates(self, *a, **k):
            raise RuntimeError("network down")

    client = Broken()
    summary = run_pipeline(factory, {}, collectors=[], preferences=prefs, matcher=None,
                           notifier=TelegramNotifier(client, CHAT, sleep=lambda s: None))
    assert summary.status == "ok"
    assert "error" in summary.stats["telegram"]["commands"]
    with factory() as s:
        assert s.query(Job).one().relevance_score is not None


# --- uniqueness ------------------------------------------------------------------------

def add_copy(factory, sid, location, **kw):
    job_id = add_job(factory, sid, title="Internship - Search Machine Learning Engineer", **kw)
    with factory.begin() as s:
        s.get(Job, job_id).location = location
    return job_id


def test_same_role_in_several_cities_is_one_alert(factory, prefs):
    add_copy(factory, "london", "London", score=82.0)
    add_copy(factory, "belgrade", "Belgrade", score=80.0)
    client = FakeClient()
    assert notifier(client).send_alerts(factory, prefs, utcnow())["sent"] == 1
    text = client.sent[0][1]
    assert "London / Belgrade" in text and text.count("Company board (Ashby)") == 2
    with factory() as s:
        assert s.query(Notification).filter_by(kind="alert", status="sent").count() == 2  # both copies recorded


def test_new_city_copy_of_an_already_sent_role_is_not_sent(factory, prefs):
    add_copy(factory, "london", "London")
    notifier(FakeClient()).send_alerts(factory, prefs, utcnow())
    add_copy(factory, "berlin", "Berlin")  # appears in a later run
    client = FakeClient()
    assert notifier(client).send_alerts(factory, prefs, utcnow())["sent"] == 0


def test_job_sent_in_a_digest_is_never_alerted_later(factory, prefs):
    now = at_hour(20)
    job_id = add_job(factory, "1", status="review", seen=now, first_seen=now - timedelta(hours=1))
    n = notifier(FakeClient())
    assert n.send_digest(factory, prefs, now)["jobs"] == 1
    with factory.begin() as s:
        s.get(Job, job_id).filter_status = "passed"  # e.g. preferences changed and it now qualifies
    assert n.send_alerts(factory, prefs, now + timedelta(hours=1))["sent"] == 0


def test_digest_groups_same_role_copies(factory, prefs):
    now = at_hour(20)
    add_copy(factory, "london", "London", status="review", seen=now, first_seen=now - timedelta(hours=1))
    add_copy(factory, "belgrade", "Belgrade", status="review", seen=now, first_seen=now - timedelta(hours=1))
    client = FakeClient()
    assert notifier(client).send_digest(factory, prefs, now)["jobs"] == 1
    assert client.sent[0][1].count("Internship - Search Machine Learning Engineer") == 1
    assert "London / Belgrade" in client.sent[0][1]


def test_different_roles_at_same_company_are_separate(factory, prefs):
    add_job(factory, "1", title="ML Ops Engineer")
    add_job(factory, "2", title="Data Scientist - Evaluations")
    assert notifier(FakeClient()).send_alerts(factory, prefs, utcnow())["sent"] == 2
