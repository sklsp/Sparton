"""The ecommerce job handlers drive their own sessions and Job rows.

``crawl_shop``, ``crawl_one_competitor``, ``generate_report_job`` and
``schedule_due_crawls`` open their own DB session (workers are separate
processes), so the tests seed committed rows through ``db_session``, fake the
crawler / catalogue read / report generation with monkeypatch, call the handler
directly, and then check the Job row and the shop/competitor rows afterwards --
including the failure path (``_fail``) and payloads whose job or shop does not
exist.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.database.domain_models import Job, JobStatus
from app.core.database.ecommerce_models import Competitor, Shop
from app.core.database.models import utcnow
from app.ecommerce import jobs
from app.ecommerce.crawl import CrawlOutcome


def _make_job(db_session, **fields) -> int:
    """Insert a committed Job row and return its id."""
    job = Job(type=fields.pop("type", "crawl_shop"), **fields)
    db_session.add(job)
    db_session.commit()
    return job.id


def _naive(dt):
    """SQLite hands back naive datetimes; utcnow() is aware. Normalize both sides."""
    if dt is not None and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def _record():
    return {"calls": [], "policies": [], "instances": 0, "closed": 0, "rematch_calls": 0}


def _fake_crawler(record):
    class FakeResponsibleCrawler:
        def __init__(self, policy=None):
            record["policies"].append(policy)
            record["instances"] += 1

        def close(self):
            record["closed"] += 1

    return FakeResponsibleCrawler


def _stub_crawl(monkeypatch, record, *, own_result=None, own_error=None,
                rematch_result=0, rematch_error=None, crawl_error=None,
                captures_written=3, changes=2):
    """Fake everything a crawl handler reaches for outside the Job/Shop rows."""
    monkeypatch.setattr("app.ecommerce.crawl.default_crawl_policy", lambda: "POLICY")
    monkeypatch.setattr("app.research.crawler.ResponsibleCrawler", _fake_crawler(record))

    def fake_read_own(db, shop):
        if own_error is not None:
            raise own_error
        return own_result or {"products": 5, "data_source": "feed"}

    def fake_rematch(db, shop):
        record["rematch_calls"] += 1
        if rematch_error is not None:
            raise rematch_error
        return rematch_result

    def fake_crawl(db, competitor, *, shop_id=None, crawler=None, crawl_id=None, **kwargs):
        record["calls"].append(
            {"competitor": competitor.id, "shop_id": shop_id, "crawler": crawler, "crawl_id": crawl_id}
        )
        if crawl_error is not None:
            raise crawl_error
        return CrawlOutcome(
            competitor_id=competitor.id, domain=competitor.domain, status="COMPLETED",
            captures_written=captures_written, changes=changes,
        )

    monkeypatch.setattr("app.ecommerce.matching.read_own_catalog", fake_read_own)
    monkeypatch.setattr("app.ecommerce.matching.rematch", fake_rematch)
    monkeypatch.setattr("app.ecommerce.crawl.crawl_competitor", fake_crawl)


def _seed_world(db_session, *, org=7):
    """A shop with two active competitors, plus rows that must never be crawled."""
    shop = Shop(organization_id=org, name="Acme", url="https://acme.example", domain="acme.example")
    db_session.add(shop)
    db_session.flush()
    a = Competitor(organization_id=org, shop_id=shop.id, domain="rival-a.example")
    b = Competitor(organization_id=org, shop_id=shop.id, domain="rival-b.example")
    inactive = Competitor(organization_id=org, shop_id=shop.id, domain="inactive.example", is_active=False)
    other_org = Competitor(organization_id=99, shop_id=None, domain="other-org.example")
    db_session.add_all([a, b, inactive, other_org])
    db_session.commit()
    return shop, a, b, other_org


def _crawl_jobs(db_session):
    return db_session.execute(select(Job).where(Job.type == "crawl_shop")).scalars().all()

# --- _load_job ------------------------------------------------------------------


def test_load_job_returns_none_without_a_job_id(db_session):
    assert jobs._load_job({}) == (None, None)


def test_load_job_returns_none_for_a_missing_job(db_session):
    assert jobs._load_job({"job_id": "999"}) == (None, None)


@pytest.mark.parametrize("status", [JobStatus.COMPLETED.value, JobStatus.CANCELLED.value])
def test_load_job_skips_finished_jobs(db_session, status):
    job_id = _make_job(db_session, type="crawl_shop", status=status)
    assert jobs._load_job({"job_id": str(job_id)}) == (None, None)


def test_load_job_returns_the_open_session_for_a_queued_job(db_session):
    job_id = _make_job(db_session, type="crawl_shop")
    db, job = jobs._load_job({"job_id": str(job_id)})
    try:
        assert job.id == job_id
        assert job.status == JobStatus.QUEUED.value
    finally:
        db.close()


# --- _fail ----------------------------------------------------------------------


def test_fail_marks_the_job_failed_with_error_and_timestamp(db_session):
    job_id = _make_job(db_session, type="crawl_shop", status=JobStatus.RUNNING.value)
    jobs._fail(db_session, db_session.get(Job, job_id), RuntimeError("boom"))

    db_session.expire_all()
    row = db_session.get(Job, job_id)
    assert row.status == JobStatus.FAILED.value
    assert row.error == "boom"
    assert row.completed_at is not None


def test_fail_truncates_long_errors_to_two_thousand_chars(db_session):
    job_id = _make_job(db_session, type="crawl_shop", status=JobStatus.RUNNING.value)
    jobs._fail(db_session, db_session.get(Job, job_id), RuntimeError("x" * 5000))

    db_session.expire_all()
    assert len(db_session.get(Job, job_id).error) == 2000


@pytest.mark.parametrize("job_id", [1, None])
def test_fail_never_raises_even_if_the_session_is_broken(job_id):
    class BrokenSession:
        def rollback(self):
            pass

        def get(self, model, pk):
            raise RuntimeError("db gone")

    jobs._fail(BrokenSession(), SimpleNamespace(id=job_id), ValueError("original"))


# --- crawl_shop -----------------------------------------------------------------


def test_crawl_shop_crawls_every_active_competitor_and_writes_totals(db_session, monkeypatch):
    shop, a, b, other_org = _seed_world(db_session)
    job_id = _make_job(db_session, type="crawl_shop", organization_id=7)
    record = _record()
    _stub_crawl(monkeypatch, record, rematch_result=4)

    jobs.crawl_shop({"job_id": str(job_id), "shop_id": shop.id})

    db_session.expire_all()
    row = db_session.get(Job, job_id)
    assert row.status == JobStatus.COMPLETED.value
    assert row.stage == "crawl complete"
    assert row.progress == 1.0
    assert row.started_at is not None and row.completed_at is not None
    assert row.result == {
        "own_products": 5, "matches": 4, "competitors": 2,
        "products": 6, "changes": 4, "failed": 0,
    }

    # One shared crawler for the whole shop, built from the shop policy, closed at the end.
    assert record["instances"] == 1 and record["closed"] == 1
    assert record["policies"] == ["POLICY"]
    assert len({id(c["crawler"]) for c in record["calls"]}) == 1
    # The inactive competitor and the other tenant's competitor are never crawled.
    assert sorted(c["competitor"] for c in record["calls"]) == sorted([a.id, b.id])
    assert all(c["shop_id"] == shop.id and c["crawl_id"] == job_id for c in record["calls"])

    # The shop is stamped and its schedule pushed forward by one week (default cadence).
    s = db_session.get(Shop, shop.id)
    assert s.last_crawled_at is not None
    delta = _naive(s.next_crawl_at) - _naive(s.last_crawled_at)
    assert abs(delta - timedelta(hours=168)) < timedelta(seconds=5)

    # A crawl that found changes queues a weekly report for the same tenant.
    reports = db_session.execute(select(Job).where(Job.type == "generate_report")).scalars().all()
    assert len(reports) == 1
    assert reports[0].payload == {"shop_id": shop.id, "days": 7, "kind": "weekly"}
    assert reports[0].idempotency_key.startswith(f"auto-report:{shop.id}:")
    assert reports[0].organization_id == 7



def test_crawl_shop_without_changes_does_not_queue_a_report(db_session, monkeypatch):
    shop, a, b, other_org = _seed_world(db_session)
    job_id = _make_job(db_session, type="crawl_shop", organization_id=7)
    record = _record()
    _stub_crawl(monkeypatch, record, captures_written=1, changes=0)

    jobs.crawl_shop({"job_id": str(job_id), "shop_id": shop.id})

    db_session.expire_all()
    row = db_session.get(Job, job_id)
    assert row.status == JobStatus.COMPLETED.value
    assert row.result["changes"] == 0 and row.result["products"] == 2
    reports = db_session.execute(select(Job).where(Job.type == "generate_report")).scalars().all()
    assert reports == []


def test_crawl_shop_scopes_explicit_competitor_ids_to_the_tenant(db_session, monkeypatch):
    shop, a, b, other_org = _seed_world(db_session)
    job_id = _make_job(db_session, type="crawl_shop", organization_id=7)
    record = _record()
    _stub_crawl(monkeypatch, record)

    # The payload is not trusted: the other tenant's id must be filtered out.
    jobs.crawl_shop({"job_id": str(job_id), "shop_id": shop.id,
                     "competitor_ids": [a.id, other_org.id]})

    assert [c["competitor"] for c in record["calls"]] == [a.id]


def test_crawl_shop_refuses_a_missing_shop(db_session, monkeypatch):
    job_id = _make_job(db_session, type="crawl_shop", organization_id=7)
    record = _record()
    _stub_crawl(monkeypatch, record)

    jobs.crawl_shop({"job_id": str(job_id), "shop_id": 999})

    assert record["calls"] == []
    row = db_session.get(Job, job_id)
    assert row.status == JobStatus.QUEUED.value and row.stage == "queued"


def test_crawl_shop_refuses_a_shop_without_an_organization(db_session, monkeypatch):
    shop = Shop(organization_id=None, name="Ghost", url="https://ghost.example", domain="ghost.example")
    db_session.add(shop)
    db_session.commit()
    job_id = _make_job(db_session, type="crawl_shop")
    record = _record()
    _stub_crawl(monkeypatch, record)

    jobs.crawl_shop({"job_id": str(job_id), "shop_id": shop.id})

    assert record["calls"] == []
    row = db_session.get(Job, job_id)
    assert row.status == JobStatus.QUEUED.value


def test_crawl_shop_ignores_a_payload_for_a_missing_job(db_session, monkeypatch):
    record = _record()
    _stub_crawl(monkeypatch, record)

    jobs.crawl_shop({"job_id": "999", "shop_id": 1})

    assert record["calls"] == []


def test_crawl_shop_failure_fails_the_job_and_reraises(db_session, monkeypatch):
    shop, a, b, other_org = _seed_world(db_session)
    job_id = _make_job(db_session, type="crawl_shop", organization_id=7)
    record = _record()
    _stub_crawl(monkeypatch, record, crawl_error=RuntimeError("site on fire"))

    with pytest.raises(RuntimeError, match="site on fire"):
        jobs.crawl_shop({"job_id": str(job_id), "shop_id": shop.id})

    db_session.expire_all()
    row = db_session.get(Job, job_id)
    assert row.status == JobStatus.FAILED.value
    assert "site on fire" in row.error
    assert row.completed_at is not None
    # The shared crawler is closed even when the crawl blows up.
    assert record["closed"] == 1


def test_crawl_shop_survives_a_broken_own_catalogue_read(db_session, monkeypatch):
    shop, a, b, other_org = _seed_world(db_session)
    job_id = _make_job(db_session, type="crawl_shop", organization_id=7)
    record = _record()
    _stub_crawl(monkeypatch, record, own_error=ValueError("no feed"))

    jobs.crawl_shop({"job_id": str(job_id), "shop_id": shop.id})

    db_session.expire_all()
    row = db_session.get(Job, job_id)
    assert row.status == JobStatus.COMPLETED.value
    assert row.result["own_products"] == 0


def test_crawl_shop_survives_a_broken_rematch(db_session, monkeypatch):
    shop, a, b, other_org = _seed_world(db_session)
    job_id = _make_job(db_session, type="crawl_shop", organization_id=7)
    record = _record()
    _stub_crawl(monkeypatch, record, rematch_error=ValueError("no pairs"))

    jobs.crawl_shop({"job_id": str(job_id), "shop_id": shop.id})

    db_session.expire_all()
    row = db_session.get(Job, job_id)
    assert row.status == JobStatus.COMPLETED.value
    assert row.result["matches"] == 0



# --- crawl_one_competitor --------------------------------------------------------


def test_crawl_one_competitor_completes_with_the_outcome_dict(db_session, monkeypatch):
    shop, a, b, other_org = _seed_world(db_session)
    job_id = _make_job(db_session, type="crawl_competitor", organization_id=7)
    record = _record()
    _stub_crawl(monkeypatch, record, rematch_result=3)

    jobs.crawl_one_competitor({"job_id": str(job_id), "competitor_id": a.id, "shop_id": shop.id})

    db_session.expire_all()
    row = db_session.get(Job, job_id)
    assert row.status == JobStatus.COMPLETED.value
    assert row.stage == "complete"
    assert row.progress == 1.0
    assert row.result["competitor_id"] == a.id
    assert row.result["domain"] == a.domain
    assert row.result["status"] == "COMPLETED"
    assert record["calls"][0]["shop_id"] == shop.id
    assert record["rematch_calls"] == 1


def test_crawl_one_competitor_for_an_org_level_competitor_skips_rematch(db_session, monkeypatch):
    org_comp = Competitor(organization_id=7, domain="org-wide.example")
    db_session.add(org_comp)
    db_session.commit()
    job_id = _make_job(db_session, type="crawl_competitor", organization_id=7)
    record = _record()
    _stub_crawl(monkeypatch, record)

    def boom_rematch(db, shop):
        raise AssertionError("rematch must not run without a shop")

    monkeypatch.setattr("app.ecommerce.matching.rematch", boom_rematch)

    jobs.crawl_one_competitor({"job_id": str(job_id), "competitor_id": org_comp.id})

    db_session.expire_all()
    assert db_session.get(Job, job_id).status == JobStatus.COMPLETED.value


def test_crawl_one_competitor_ignores_a_missing_competitor(db_session, monkeypatch):
    job_id = _make_job(db_session, type="crawl_competitor", organization_id=7)
    record = _record()
    _stub_crawl(monkeypatch, record)

    jobs.crawl_one_competitor({"job_id": str(job_id), "competitor_id": 999})

    assert record["calls"] == []
    row = db_session.get(Job, job_id)
    assert row.status == JobStatus.QUEUED.value


def test_crawl_one_competitor_refuses_a_competitor_without_an_organization(db_session, monkeypatch):
    comp = Competitor(organization_id=None, domain="unowned.example")
    db_session.add(comp)
    db_session.commit()
    job_id = _make_job(db_session, type="crawl_competitor")
    record = _record()
    _stub_crawl(monkeypatch, record)

    jobs.crawl_one_competitor({"job_id": str(job_id), "competitor_id": comp.id})

    assert record["calls"] == []
    row = db_session.get(Job, job_id)
    assert row.status == JobStatus.QUEUED.value


def test_crawl_one_competitor_failure_fails_the_job_and_reraises(db_session, monkeypatch):
    shop, a, b, other_org = _seed_world(db_session)
    job_id = _make_job(db_session, type="crawl_competitor", organization_id=7)
    record = _record()
    _stub_crawl(monkeypatch, record, crawl_error=RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        jobs.crawl_one_competitor({"job_id": str(job_id), "competitor_id": a.id})

    db_session.expire_all()
    row = db_session.get(Job, job_id)
    assert row.status == JobStatus.FAILED.value
    assert "boom" in row.error



def test_crawl_one_competitor_ignores_a_payload_for_a_missing_job(db_session, monkeypatch):
    record = _record()
    _stub_crawl(monkeypatch, record)

    jobs.crawl_one_competitor({"job_id": "999", "competitor_id": 1})

    assert record["calls"] == []


def test_generate_report_job_ignores_a_payload_without_a_job_id(db_session, monkeypatch):
    record = {"digests": []}
    _stub_reports(monkeypatch, record)

    jobs.generate_report_job({"shop_id": 1})

    assert "generate" not in record and record["digests"] == []



# --- generate_report_job ----------------------------------------------------------


def _stub_reports(monkeypatch, record, *, report=None, error=None):
    report = report or SimpleNamespace(id=55, status="ready")

    def fake_generate(db, *, organization_id, shop=None, competitors=None, days=7, kind="weekly", **kwargs):
        if error is not None:
            raise error
        record["generate"] = {
            "organization_id": organization_id,
            "shop": shop.id if shop is not None else None,
            "competitor_ids": [c.id for c in (competitors or [])],
            "days": days,
            "kind": kind,
        }
        return report

    def fake_digest(db, rep):
        record["digests"].append(rep.id)
        return True

    monkeypatch.setattr("app.ecommerce.reports.generate_report", fake_generate)
    monkeypatch.setattr("app.ecommerce.reports.send_weekly_digest", fake_digest)


def test_generate_report_job_writes_a_weekly_report_and_emails_the_digest(db_session, monkeypatch):
    shop, a, b, other_org = _seed_world(db_session)
    job_id = _make_job(db_session, type="generate_report", organization_id=7)
    record = {"digests": []}
    _stub_reports(monkeypatch, record)

    jobs.generate_report_job({"job_id": str(job_id), "shop_id": shop.id})

    gen = record["generate"]
    assert gen["organization_id"] == 7 and gen["shop"] == shop.id
    assert gen["days"] == 7 and gen["kind"] == "weekly"
    # The report covers every competitor of the tenant, active or not.
    inactive = db_session.execute(
        select(Competitor).where(Competitor.domain == "inactive.example")
    ).scalar_one()
    assert sorted(gen["competitor_ids"]) == sorted([a.id, b.id, inactive.id])
    assert record["digests"] == [55]

    db_session.expire_all()
    row = db_session.get(Job, job_id)
    assert row.status == JobStatus.COMPLETED.value
    assert row.stage == "report ready"
    assert row.progress == 1.0
    assert row.result == {"report_id": 55, "status": "ready"}


def test_generate_report_job_non_weekly_kind_skips_the_digest(db_session, monkeypatch):
    shop, a, b, other_org = _seed_world(db_session)
    job_id = _make_job(db_session, type="generate_report", organization_id=7)
    record = {"digests": []}
    _stub_reports(monkeypatch, record)

    jobs.generate_report_job({"job_id": str(job_id), "shop_id": shop.id, "days": 30, "kind": "monthly"})

    assert record["generate"]["days"] == 30 and record["generate"]["kind"] == "monthly"
    assert record["digests"] == []
    db_session.expire_all()
    assert db_session.get(Job, job_id).status == JobStatus.COMPLETED.value


def test_generate_report_job_without_a_shop_covers_the_whole_tenant(db_session, monkeypatch):
    shop, a, b, other_org = _seed_world(db_session)
    job_id = _make_job(db_session, type="generate_report", organization_id=7)
    record = {"digests": []}
    _stub_reports(monkeypatch, record)

    jobs.generate_report_job({"job_id": str(job_id), "organization_id": 7})

    # No shop: every competitor of the tenant is in scope (active or not), and none from elsewhere.
    assert record["generate"]["shop"] is None
    inactive = db_session.execute(
        select(Competitor).where(Competitor.domain == "inactive.example")
    ).scalar_one()
    assert sorted(record["generate"]["competitor_ids"]) == sorted([a.id, b.id, inactive.id])
    db_session.expire_all()
    assert db_session.get(Job, job_id).status == JobStatus.COMPLETED.value


def test_generate_report_job_failure_fails_the_job_and_reraises(db_session, monkeypatch):
    shop, a, b, other_org = _seed_world(db_session)
    job_id = _make_job(db_session, type="generate_report", organization_id=7)
    record = {"digests": []}
    _stub_reports(monkeypatch, record, error=RuntimeError("no data"))

    with pytest.raises(RuntimeError, match="no data"):
        jobs.generate_report_job({"job_id": str(job_id), "shop_id": shop.id})

    db_session.expire_all()
    row = db_session.get(Job, job_id)
    assert row.status == JobStatus.FAILED.value
    assert "no data" in row.error



# --- schedule_due_crawls -----------------------------------------------------------


def _seed_scheduled(db_session, *, org=7):
    """Two due shops (one with a custom cadence), one not yet due, one other tenant."""
    now = utcnow()
    s1 = Shop(organization_id=org, name="Due One", url="https://due-one.example", domain="due-one.example")
    s2 = Shop(organization_id=org, name="Due Two", url="https://due-two.example", domain="due-two.example",
              crawl_frequency_hours=48)
    not_due = Shop(organization_id=org, name="Not Due", url="https://not-due.example", domain="not-due.example")
    other = Shop(organization_id=99, name="Other Tenant", url="https://other.example", domain="other.example")
    # Due, but with no competitors left: the scheduler must skip it.
    orphan = Shop(organization_id=org, name="Orphan", url="https://orphan.example", domain="orphan.example")
    db_session.add_all([s1, s2, not_due, other, orphan])
    db_session.flush()
    for shop in (s1, s2, not_due, other, orphan):
        shop.next_crawl_at = now - timedelta(hours=1)
    not_due.next_crawl_at = now + timedelta(days=30)
    # The scheduler only queues shops that still have active competitors.
    db_session.add_all([
        Competitor(organization_id=org, shop_id=s1.id, domain="due-one-comp.example"),
        Competitor(organization_id=org, shop_id=s2.id, domain="due-two-comp.example"),
        Competitor(organization_id=99, shop_id=other.id, domain="other-tenant-comp.example"),
    ])
    db_session.commit()
    return s1, s2, not_due, other, orphan


def test_schedule_queues_one_job_per_due_shop_and_pushes_their_schedules(db_session):
    s1, s2, _, other, orphan = _seed_scheduled(db_session)

    jobs.schedule_due_crawls({})

    rows = db_session.execute(select(Job).where(Job.type == "crawl_shop")).scalars().all()
    # The scheduler is global: every due shop with active competitors gets a job.
    by_shop = {r.payload["shop_id"]: r for r in rows}
    assert set(by_shop) == {s1.id, s2.id, other.id}
    shops = {s.id: s for s in db_session.execute(select(Shop)).scalars().all()}
    for row in rows:
        assert row.status == JobStatus.QUEUED.value
        assert row.organization_id == shops[row.payload["shop_id"]].organization_id
        assert row.idempotency_key.startswith(f"crawl:{row.payload['shop_id']}:")

    db_session.expire_all()
    # The default cadence is one week; the custom shop keeps its own interval.
    d1 = _naive(db_session.get(Shop, s1.id).next_crawl_at) - _naive(utcnow())
    assert abs(d1 - timedelta(hours=168)) < timedelta(seconds=5)
    d2 = _naive(db_session.get(Shop, s2.id).next_crawl_at) - _naive(utcnow())
    assert abs(d2 - timedelta(hours=48)) < timedelta(seconds=5)
    # The orphan was skipped: no job for it and its schedule untouched.
    assert orphan.id not in by_shop
    assert _naive(db_session.get(Shop, orphan.id).next_crawl_at) - _naive(utcnow()) < timedelta(hours=1)


def test_schedule_leaves_not_yet_due_shops_alone(db_session):
    _, _, not_due, _, _ = _seed_scheduled(db_session)

    jobs.schedule_due_crawls({})

    db_session.expire_all()
    assert _naive(not_due.next_crawl_at) - _naive(utcnow()) > timedelta(days=29)


def test_schedule_is_idempotent_within_a_day(db_session):
    _seed_scheduled(db_session)

    jobs.schedule_due_crawls({})
    first_keys = {r.idempotency_key for r in db_session.execute(select(Job)).scalars().all()}
    assert len(first_keys) == 3  # the two due shops of org 7 and the due shop of org 99

    # Second run the same day: nothing new is queued.
    jobs.schedule_due_crawls({})
    all_jobs = db_session.execute(select(Job)).scalars().all()
    assert len(all_jobs) == 3
    assert {r.idempotency_key for r in all_jobs} == first_keys


def test_schedule_stamps_each_job_with_its_own_tenant(db_session):
    _seed_scheduled(db_session)

    jobs.schedule_due_crawls({})

    db_session.expire_all()
    rows = db_session.execute(select(Job)).scalars().all()
    # The scheduler serves every tenant, but each job carries the organization of
    # its own shop -- a crawl can never read another tenant's data.
    assert {r.organization_id for r in rows} == {7, 99}
    shops = {s.id: s for s in db_session.execute(select(Shop)).scalars().all()}
    for row in rows:
        assert row.organization_id == shops[row.payload["shop_id"]].organization_id


def test_schedule_tick_failure_rolls_back_and_stays_quiet(db_session, monkeypatch):
    _seed_scheduled(db_session)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(jobs, "enqueue", boom)

    jobs.schedule_due_crawls({})  # a broken tick must not take the worker down

    assert db_session.execute(select(Job)).scalars().all() == []
