"""Tests for app.core.security.rate_limit: local and distributed limiters."""

from __future__ import annotations

import ipaddress
import types
from collections import defaultdict

import pytest
from fastapi import HTTPException, status
from starlette.requests import Request

from app.core.config import settings
from app.core.security import rate_limit as rl


@pytest.fixture(autouse=True)
def _pin_settings(monkeypatch):
    """Deterministic environment: local limiter, loopback-only proxy trust."""
    monkeypatch.setattr(settings, "redis_url", None)
    monkeypatch.setattr(settings, "rate_limit_disabled", False)
    monkeypatch.setattr(settings, "forwarded_allow_ips", "127.0.0.1")


def make_request(client=("198.51.100.7", 44321), xff=None, path="/things", route_path=None):
    """A real starlette Request built from a scope we fully control."""
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    scope = {"type": "http", "method": "GET", "path": path, "query_string": b"", "headers": headers}
    if client is not None:
        scope["client"] = client
    if route_path is not None:
        scope["route"] = types.SimpleNamespace(path=route_path)
    return Request(scope)


class FakeClock:
    """Stands in for the `time` module the limiter reads, so windows move without sleeping."""

    def __init__(self):
        self.monotonic_now = 1000.0
        self.wall_now = 120.0

    def monotonic(self):
        return self.monotonic_now

    def time(self):
        return self.wall_now


@pytest.fixture()
def fake_clock(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(rl, "time", clock)
    return clock


class TestLocalLimiter:
    def test_under_limit_passes_and_over_raises_429(self):
        for _ in range(3):
            rl.check_rate_limit("k", limit=3)
        with pytest.raises(HTTPException) as excinfo:
            rl.check_rate_limit("k", limit=3)
        assert excinfo.value.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        assert "Rate limit exceeded" in excinfo.value.detail

    def test_window_resets_when_old_hits_expire(self, fake_clock):
        for _ in range(3):
            rl.check_rate_limit("k", limit=3, window_seconds=60)
        with pytest.raises(HTTPException):
            rl.check_rate_limit("k", limit=3, window_seconds=60)

        fake_clock.monotonic_now += 61  # whole bucket now outside the window
        rl.check_rate_limit("k", limit=3, window_seconds=60)  # passes again

    def test_separate_keys_have_separate_buckets(self):
        for _ in range(2):
            rl.check_rate_limit("a", limit=2)
        with pytest.raises(HTTPException):
            rl.check_rate_limit("a", limit=2)
        rl.check_rate_limit("b", limit=2)  # untouched bucket

    def test_reset_limits_clears_buckets(self):
        for _ in range(2):
            rl.check_rate_limit("k", limit=2)
        with pytest.raises(HTTPException):
            rl.check_rate_limit("k", limit=2)
        rl.reset_limits()
        rl.check_rate_limit("k", limit=2)  # fresh bucket


class TestRedisClient:
    def test_returns_none_without_redis_url(self):
        assert rl._redis_client() is None

    def test_unreachable_redis_returns_none(self, monkeypatch):
        import redis as real_redis

        def boom(*_args, **_kwargs):
            raise ConnectionError("no route to host")

        monkeypatch.setattr(settings, "redis_url", "redis://localhost:9/0")
        monkeypatch.setattr(real_redis.Redis, "from_url", staticmethod(boom))
        assert rl._redis_client() is None

    def test_missing_redis_package_returns_none(self, monkeypatch):
        import sys

        monkeypatch.setattr(settings, "redis_url", "redis://cache.local/0")
        # A None entry in sys.modules makes `import redis` raise ImportError.
        monkeypatch.setitem(sys.modules, "redis", None)
        assert rl._redis_client() is None

    def test_reachable_redis_returns_a_pingable_client(self, monkeypatch):
        import redis as real_redis

        fake = types.SimpleNamespace(ping=lambda: True)
        captured = {}

        def from_url(url, **kwargs):
            captured["url"] = url
            return fake

        monkeypatch.setattr(settings, "redis_url", "redis://cache.local/0")
        monkeypatch.setattr(real_redis.Redis, "from_url", staticmethod(from_url))
        assert rl._redis_client() is fake
        assert captured["url"] == "redis://cache.local/0"


class FakeRedis:
    """Just enough of the redis client surface for _hit_distributed."""

    def __init__(self):
        self.counters = defaultdict(int)
        self.expires = []
        self.fail_incr = False

    def incr(self, key):
        if self.fail_incr:
            raise ConnectionError("redis went away")
        self.counters[key] += 1
        return self.counters[key]

    def expire(self, key, ttl):
        self.expires.append((key, ttl))


class TestDistributedLimiter:
    @pytest.fixture()
    def fake_redis(self, monkeypatch):
        client = FakeRedis()
        monkeypatch.setattr(rl, "_redis_client", lambda: client)
        return client

    def test_counts_per_window_and_expires_first_hit(self, fake_clock, fake_redis):
        for _ in range(2):
            rl.check_rate_limit("k", limit=2, window_seconds=60)
        with pytest.raises(HTTPException) as excinfo:
            rl.check_rate_limit("k", limit=2, window_seconds=60)
        assert excinfo.value.status_code == status.HTTP_429_TOO_MANY_REQUESTS

        # Fixed window derived from the wall clock: 120.0 // 60 -> window 2.
        assert list(fake_redis.counters) == ["ratelimit:k:2"]
        # EXPIRE is set exactly once, on the first hit of the window.
        assert fake_redis.expires == [("ratelimit:k:2", 61)]

    def test_new_window_starts_a_fresh_counter(self, fake_clock, fake_redis):
        for _ in range(2):
            rl.check_rate_limit("k", limit=2, window_seconds=60)
        with pytest.raises(HTTPException):
            rl.check_rate_limit("k", limit=2, window_seconds=60)

        fake_clock.wall_now = 180.5  # next fixed window (3)
        rl.check_rate_limit("k", limit=2, window_seconds=60)  # passes again
        assert "ratelimit:k:3" in fake_redis.counters

    def test_redis_failure_falls_back_to_local(self, fake_clock, fake_redis):
        fake_redis.fail_incr = True
        rl.check_rate_limit("k", limit=2, window_seconds=60)  # local hit 1
        rl.check_rate_limit("k", limit=2, window_seconds=60)  # local hit 2
        with pytest.raises(HTTPException):
            rl.check_rate_limit("k", limit=2, window_seconds=60)  # local 429


class TestTrustedProxyNetworks:
    def test_parses_commas_semicolons_and_bare_addresses(self, monkeypatch):
        monkeypatch.setattr(settings, "forwarded_allow_ips", "10.0.0.0/8; ,192.168.1.5, 172.16.0.0/12")
        assert rl._trusted_proxy_networks() == [
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("192.168.1.5/32"),
            ipaddress.ip_network("172.16.0.0/12"),
        ]

    def test_unparseable_entries_are_skipped(self, monkeypatch):
        monkeypatch.setattr(settings, "forwarded_allow_ips", "garbage, 10.0.0.0/8")
        assert rl._trusted_proxy_networks() == [ipaddress.ip_network("10.0.0.0/8")]

    def test_empty_setting_falls_back_to_loopback(self, monkeypatch):
        monkeypatch.setattr(settings, "forwarded_allow_ips", None)
        assert rl._trusted_proxy_networks() == [ipaddress.ip_network("127.0.0.1/32")]


class TestPeerTrust:
    def test_peer_in_configured_network_is_trusted(self, monkeypatch):
        monkeypatch.setattr(settings, "forwarded_allow_ips", "10.0.0.0/8")
        assert rl._peer_is_trusted_proxy(make_request(client=("10.1.2.3", 5))) is True

    def test_peer_outside_configured_networks_is_not(self):
        assert rl._peer_is_trusted_proxy(make_request(client=("198.51.100.7", 5))) is False

    def test_unparseable_peer_address_is_not_trusted(self, monkeypatch):
        monkeypatch.setattr(settings, "forwarded_allow_ips", "0.0.0.0/0")
        assert rl._peer_is_trusted_proxy(make_request(client=("not-an-ip", 5))) is False


class TestClientKey:
    def test_socket_address_when_peer_is_not_a_trusted_proxy(self):
        request = make_request(client=("198.51.100.7", 44321), xff="203.0.113.9")
        assert rl.client_key(request) == "198.51.100.7"

    def test_forwarded_for_is_honored_from_a_trusted_proxy(self):
        request = make_request(client=("127.0.0.1", 50000), xff="203.0.113.9, 10.0.0.4")
        assert rl.client_key(request) == "203.0.113.9"

    def test_invalid_forwarded_for_falls_back_to_socket(self):
        request = make_request(client=("127.0.0.1", 50000), xff="not-an-ip")
        assert rl.client_key(request) == "127.0.0.1"

    def test_missing_client_reports_unknown(self):
        assert rl.client_key(make_request(client=None, xff="203.0.113.9")) == "unknown"


class TestRateLimitDependency:
    def test_disabled_setting_short_circuits(self, monkeypatch):
        monkeypatch.setattr(settings, "rate_limit_disabled", True)
        dependency = rl.rate_limit(limit=1)
        # Exhaust the bucket directly; the disabled dependency must not see it.
        rl.check_rate_limit("203.0.113.5:/things", limit=1)
        dependency(make_request(client=("203.0.113.5", 9)))

    def test_counts_per_identity_and_route_path(self):
        dependency = rl.rate_limit(limit=2, window_seconds=60)
        alice = make_request(client=("203.0.113.5", 9), route_path="/things/{id}")
        bob = make_request(client=("203.0.113.6", 9), route_path="/things/{id}")
        dependency(alice)
        dependency(alice)
        with pytest.raises(HTTPException):
            dependency(alice)
        dependency(bob)  # separate identity, fresh bucket

    def test_key_by_overrides_the_default_identity(self):
        dependency = rl.rate_limit(limit=1, key_by=lambda request: "tenant-42")
        dependency(make_request(client=("203.0.113.5", 9)))
        with pytest.raises(HTTPException):
            dependency(make_request(client=("203.0.113.6", 9)))  # same tenant bucket

    def test_falls_back_to_url_path_without_route(self):
        dependency = rl.rate_limit(limit=1)
        dependency(make_request(client=("203.0.113.7", 9), path="/no-route"))
        with pytest.raises(HTTPException):
            dependency(make_request(client=("203.0.113.7", 9), path="/no-route"))

