"""The worker's liveness signal.

The worker serves no HTTP, so it cannot answer `/live`. Its healthcheck is a
separate process reading a beat the worker writes to Redis, and the whole
arrangement rests on three things being true:

- the worker actually writes the beat while idle, not only while busy;
- a missing beat and a stale beat are both treated as failure, and told apart
  in the message, because they are different bugs;
- the healthcheck exits non-zero rather than raising, since a crashing probe
  can read as a passing one.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.core.jobs import heartbeat as hb


class FakeRedis:
    """Just enough Redis. No fakeredis in the dependency set, deliberately:
    the product requirements must stay small, so the test double is here."""

    def __init__(self) -> None:
        self.store: dict[str, tuple[str, float | None]] = {}
        self.ping_raises = False
        self.set_raises = False
        self.get_raises = False

    def set(self, key, value, ex=None):
        if self.set_raises:
            raise ConnectionError("redis is down")
        self.store[key] = (value, None if ex is None else time.time() + ex)
        return True

    def get(self, key):
        if self.get_raises:
            raise ConnectionError("redis is down")
        entry = self.store.get(key)
        return None if entry is None else entry[0]

    def ping(self):
        if self.ping_raises:
            raise ConnectionError("redis is down")
        return True


class TestWritingABeat:
    def test_a_beat_is_readable_under_the_expected_key(self):
        client = FakeRedis()
        assert hb.write_heartbeat(client, "host-a", now=1000.0) is True
        assert client.get(hb.heartbeat_key("host-a")) == "1000.0"

    def test_the_key_carries_a_ttl(self):
        """A beat with no expiry outlives the process that wrote it, so a dead
        worker would look alive forever."""
        client = FakeRedis()
        hb.write_heartbeat(client, "host-a", ttl_seconds=90, now=1000.0)
        _value, expires = client.store[hb.heartbeat_key("host-a")]
        assert expires is not None, "the beat has no TTL"

    def test_two_hosts_do_not_share_a_beat(self):
        client = FakeRedis()
        hb.write_heartbeat(client, "host-a", now=1000.0)
        assert hb.heartbeat_age(client, "host-b") is None

    def test_a_redis_outage_does_not_raise_into_the_worker(self):
        """The worker must keep draining the queue; the healthcheck reports the
        outage by the absence of a beat."""
        client = FakeRedis()
        client.set_raises = True
        assert hb.write_heartbeat(client, "host-a") is False

    def test_no_client_is_not_an_error(self):
        assert hb.write_heartbeat(None) is False


class TestReadingABeat:
    def test_a_fresh_beat_is_fresh(self):
        client = FakeRedis()
        hb.write_heartbeat(client, "host-a", now=1000.0)
        assert hb.heartbeat_age(client, "host-a", now=1005.0) == 5.0
        assert hb.heartbeat_is_fresh(client, "host-a", now=1005.0) is True

    def test_a_missing_beat_is_none_not_zero(self):
        """Zero would read as "beat right now", which is the one answer that
        must not be confused with "we have no idea"."""
        assert hb.heartbeat_age(FakeRedis(), "host-a") is None

    def test_a_stale_beat_is_not_fresh(self):
        client = FakeRedis()
        hb.write_heartbeat(client, "host-a", now=1000.0)
        assert hb.heartbeat_is_fresh(client, "host-a", now=1000.0 + 600, max_age_seconds=90) is False

    def test_a_beat_from_the_future_is_treated_as_fresh(self):
        """Clock skew between containers must not fail a deploy."""
        client = FakeRedis()
        hb.write_heartbeat(client, "host-a", now=2000.0)
        assert hb.heartbeat_age(client, "host-a", now=1000.0) == 0.0
        assert hb.heartbeat_is_fresh(client, "host-a", now=1000.0) is True

    def test_a_corrupt_value_is_refused_rather_than_crashing(self):
        client = FakeRedis()
        client.store[hb.heartbeat_key("host-a")] = ("not-a-number", None)
        assert hb.heartbeat_age(client, "host-a") is None
        assert hb.heartbeat_is_fresh(client, "host-a") is False

    def test_a_redis_outage_reads_as_no_beat(self):
        client = FakeRedis()
        client.get_raises = True
        assert hb.heartbeat_age(client, "host-a") is None


class TestTheHealthcheckCommand:
    """`python -m workers.healthcheck` is what compose runs. These tests are
    about the exit code, because that is the only thing Docker reads."""

    def test_a_healthy_worker_exits_zero(self, monkeypatch):
        import workers.healthcheck as hc

        client = FakeRedis()
        hb.write_heartbeat(client, "host-a", now=time.time())
        monkeypatch.setattr(hc, "check_database", lambda: (True, "database ok"))
        monkeypatch.setattr(hc, "check_redis", lambda *a, **k: (True, "redis ok"))
        monkeypatch.setattr(
            hc, "check_heartbeat", lambda *a, **k: (True, "worker heartbeat 1s old")
        )
        assert hc.main() == 0

    def test_a_missing_beat_exits_non_zero(self, monkeypatch):
        """The failure this whole change exists for: the loop is not running."""
        import workers.healthcheck as hc

        monkeypatch.setattr(hc, "check_database", lambda: (True, "database ok"))
        monkeypatch.setattr(hc, "check_redis", lambda *a, **k: (True, "redis ok"))
        monkeypatch.setattr(
            hc, "check_heartbeat", lambda *a, **k: (False, "no worker heartbeat")
        )
        assert hc.main() == 1

    def test_a_raising_check_fails_rather_than_passing(self, monkeypatch):
        """A probe that crashes can read as a passing one in some
        orchestrators, which is worse than no probe at all."""
        import workers.healthcheck as hc

        def boom():
            raise RuntimeError("engine is on fire")

        monkeypatch.setattr(hc, "check_database", boom)
        assert hc.main() == 1

    def test_the_heartbeat_check_names_the_stale_case_distinctly(self, monkeypatch):
        """A missing beat and a wedged worker are different bugs, and an
        operator reading `docker inspect` should be able to tell them apart."""
        import workers.healthcheck as hc

        client = FakeRedis()
        client.store[hb.heartbeat_key("host-a")] = (str(time.time() - 600), None)
        ok, message = hc.check_heartbeat(client, hostname="host-a", max_age_seconds=90)
        assert ok is False
        assert "wedged" in message, message
        assert "old" in message, message

    def test_redis_being_absent_is_reported_not_crashed(self):
        import workers.healthcheck as hc

        ok, message = hc.check_redis(client=None)
        assert ok is False
        assert "redis" in message.lower()


class TestTheWorkerBeatsWhileIdle:
    def test_the_loop_beats_outside_job_processing(self):
        """Read from the source, because this is a structural property of the
        loop: a beat written only after a job completes means a worker with an
        empty queue never looks alive, and that is the common case."""
        import inspect

        from workers.worker import Worker

        source = inspect.getsource(Worker.run)
        beat = source.index("write_heartbeat")
        claim = source.index("self._work_once")
        assert beat < claim, (
            "the beat is written after the job, so an idle worker never looks alive"
        )

    def test_the_beat_interval_is_well_under_the_ttl(self):
        """A missed beat must not look like a dead worker, and the heartbeat
        key's TTL is what the healthcheck trusts."""
        from workers.worker import HEARTBEAT_INTERVAL_SECONDS

        assert HEARTBEAT_INTERVAL_SECONDS * 2 < hb.DEFAULT_TTL_SECONDS

    def test_the_healthcheck_max_age_exceeds_the_ttl(self):
        """A beat older than the TTL has already been evicted, so refusing it at
        the TTL would be the honest number; the extra headroom is for the
        orchestrator's own interval."""
        from workers.healthcheck import MAX_AGE_SECONDS

        assert MAX_AGE_SECONDS >= hb.DEFAULT_TTL_SECONDS


class TestComposeGivesTheWorkerItsOwnProbe:
    """The original defect: the worker inherited the image's HTTP probe."""

    @pytest.fixture(scope="class")
    def compose(self) -> dict:
        yaml = pytest.importorskip("yaml")
        return yaml.safe_load(
            (Path(__file__).resolve().parent.parent / "docker-compose.yml")
            .read_text(encoding="utf-8")
        )

    def test_the_worker_does_not_inherit_the_api_probe(self, compose):
        """`docker compose config` resolves the image HEALTHCHECK into the
        service, so a service with no `healthcheck` block has none -- unless the
        image supplies one, which is exactly what happened."""
        worker = compose["services"]["worker"]
        assert worker.get("healthcheck"), (
            "the worker has no healthcheck of its own, so it inherits the "
            "image's curl of an HTTP endpoint it does not serve"
        )

    def test_the_worker_probe_does_not_curl_http(self, compose):
        test = compose["services"]["worker"]["healthcheck"]["test"]
        joined = " ".join(str(part) for part in test)
        assert "/live" not in joined, "the worker still probes the API's /live"
        assert "curl" not in joined, "the worker does not serve HTTP"
        assert "workers.healthcheck" in joined

    def test_the_api_keeps_its_http_probe(self, compose):
        """Only the worker needed changing; the API is serving HTTP and its
        probe is correct."""
        api = compose["services"]["api"]
        assert "healthcheck" not in api, (
            "the api no longer overrides the image probe; it should inherit it"
        )

    def test_the_start_period_outlasts_worker_startup(self, compose):
        """A short start period reports a booting worker as unhealthy, which is
        how a correct probe becomes a permanently red dashboard."""
        health = compose["services"]["worker"]["healthcheck"]
        assert int(health["start_period"].rstrip("s")) >= 30, health
        assert int(health["start_period"].rstrip("s")) > int(
            health["interval"].rstrip("s")
        ), "start_period must exceed the interval or a booting worker trips it"
