"""Queue transport (app.core.jobs.transport): handler registry, inline backend and Redis structures."""

import fnmatch
import json
import math

import pytest
import redis as redis_module

from app.core.config import settings
from app.core.jobs import transport
from app.core.jobs.transport import (
    DEAD_LETTER_KEY,
    DELAYED_KEY,
    PROCESSING_KEY,
    QUEUE_KEY,
    InlineQueueBackend,
    RedisQueueBackend,
    get_backend,
    get_handler,
    register_handler,
    set_backend,
)

FAKE_EPOCH = 1_000_000.0


class FakeClock:
    """Deterministic stand-in for the time module used by transport.py."""

    def __init__(self):
        self.now = FAKE_EPOCH

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeRedis:
    """In-memory Redis implementing only the methods app.core.jobs.transport calls.

    Lists keep insertion order (lpush prepends), sorted sets map member to score.
    brpoplpush never blocks: it returns immediately like rpoplpush does.
    """

    def __init__(self):
        self.lists = {}
        self.zsets = {}

    # -- lists -----------------------------------------------------------

    def lpush(self, key, value):
        items = self.lists.setdefault(key, [])
        items.insert(0, value)
        return len(items)

    def rpoplpush(self, src, dst):
        items = self.lists.get(src) or []
        if not items:
            return None
        value = items.pop()
        self.lists.setdefault(dst, []).insert(0, value)
        return value

    def brpoplpush(self, src, dst, timeout=None):
        return self.rpoplpush(src, dst)

    def lrem(self, key, count, value):
        items = self.lists.get(key, [])
        removed = 0
        for i, item in enumerate(items):
            if item == value:
                del items[i]
                removed += 1
                if count == 0 or removed >= abs(count):
                    break
        return removed

    def llen(self, key):
        return len(self.lists.get(key, []))

    def lrange(self, key, start, stop):
        items = self.lists.get(key, [])
        if stop == -1:
            stop = len(items)
        return items[start:stop]

    # -- sorted sets -------------------------------------------------------

    def zadd(self, key, mapping):
        target = self.zsets.setdefault(key, {})
        added = 0
        for member, score in mapping.items():
            if member not in target:
                added += 1
            target[member] = score
        return added

    def zrangebyscore(self, key, min_score, max_score):
        members = self.zsets.get(key, {})
        lo = -math.inf if min_score == "-inf" else float(min_score)
        hi = math.inf if max_score == "+inf" else float(max_score)
        due = [(score, member) for member, score in members.items() if lo <= score <= hi]
        return [member for _, member in sorted(due)]

    def zrem(self, key, *members):
        target = self.zsets.get(key, {})
        removed = 0
        for member in members:
            if member in target:
                del target[member]
                removed += 1
        return removed

    def zcard(self, key):
        return len(self.zsets.get(key, {}))

    # -- misc ---------------------------------------------------------------

    def keys(self, pattern="*"):
        names = set(self.lists) | set(self.zsets)
        return fnmatch.filter(names, pattern)

    def ping(self):
        return True


class UnreachableFakeRedis(FakeRedis):
    """A client whose connection is down: every ping raises."""

    def ping(self):
        raise ConnectionError("no redis here")


@pytest.fixture()
def handlers(monkeypatch):
    """A fresh handler registry so registrations never leak between tests."""
    registry = {}
    monkeypatch.setattr(transport, "_handlers", registry)
    return registry


@pytest.fixture()
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(transport, "time", fake)
    return fake


@pytest.fixture()
def redis_backend(clock):
    return RedisQueueBackend(FakeRedis())


@pytest.fixture()
def process_backend():
    """Clear the cached process-wide backend before and after each test."""
    set_backend(None)
    yield
    set_backend(None)



# --- handler registry ---------------------------------------------------------


def test_get_handler_returns_none_for_unregistered_type(handlers):
    assert get_handler("crawl_shop") is None


def test_register_handler_makes_get_handler_return_it(handlers):
    def crawl(payload):
        return payload

    register_handler("crawl_shop", crawl)

    assert get_handler("crawl_shop") is crawl


def test_register_handler_overwrites_an_existing_type(handlers):
    first = lambda p: 1  # noqa: E731
    second = lambda p: 2  # noqa: E731

    register_handler("crawl_shop", first)
    register_handler("crawl_shop", second)

    assert get_handler("crawl_shop") is second


# --- InlineQueueBackend ---------------------------------------------------------


def test_inline_enqueue_runs_registered_handler_before_returning(handlers):
    calls = []
    handlers["crawl_shop"] = lambda payload: calls.append(payload)
    backend = InlineQueueBackend()

    job_id = backend.enqueue("crawl_shop", {"shop_id": 1})

    assert calls == [{"shop_id": 1}]
    assert len(backend.executed) == 1
    assert backend.executed[0]["id"] == job_id
    assert backend.executed[0]["worker_id"] == "inline"


def test_inline_enqueue_records_entry_without_a_handler(handlers):
    backend = InlineQueueBackend()

    job_id = backend.enqueue("crawl_shop", {"shop_id": 1})

    assert len(job_id) == 32
    assert all(c in "0123456789abcdef" for c in job_id)
    assert backend.executed[0]["type"] == "crawl_shop"
    assert backend.executed[0]["payload"] == {"shop_id": 1}


def test_inline_claim_returns_none_and_never_delivers():
    backend = InlineQueueBackend()

    assert backend.claim("worker-1", timeout_seconds=0) is None


def test_inline_depth_reports_zero_counters():
    backend = InlineQueueBackend()

    assert backend.depth() == {"queued": 0, "delayed": 0, "dead_letter": 0, "processing": 0}


def test_inline_ping_is_true():
    assert InlineQueueBackend().ping() is True


def test_inline_lifecycle_calls_are_no_ops(handlers):
    backend = InlineQueueBackend()
    job_id = backend.enqueue("crawl_shop", {"shop_id": 1})
    entry = dict(backend.executed[0])

    backend.complete(entry)
    backend.requeue(entry, delay_seconds=5)
    backend.dead_letter(entry, "boom")
    assert backend.reclaim_stale(300) == 0

    assert [e["id"] for e in backend.executed] == [job_id]
    assert backend.depth() == {"queued": 0, "delayed": 0, "dead_letter": 0, "processing": 0}



# --- RedisQueueBackend: enqueue ---------------------------------------------------


def test_redis_enqueue_pushes_entry_to_queue_and_returns_generated_id(redis_backend):
    fake = redis_backend.client

    job_id = redis_backend.enqueue("crawl_shop", {"shop_id": 7})

    assert len(job_id) == 32
    raw = fake.lists[QUEUE_KEY][0]
    data = json.loads(raw)
    assert data["id"] == job_id
    assert data["type"] == "crawl_shop"
    assert data["payload"] == {"shop_id": 7}
    assert data["enqueued_at"] == FAKE_EPOCH


def test_redis_enqueue_honours_an_explicit_job_id(redis_backend):
    fake = redis_backend.client

    job_id = redis_backend.enqueue("crawl_shop", {}, job_id="fixed-id")

    assert job_id == "fixed-id"
    assert json.loads(fake.lists[QUEUE_KEY][0])["id"] == "fixed-id"


def test_redis_enqueue_with_delay_parks_entry_in_delayed_set(redis_backend):
    fake = redis_backend.client

    job_id = redis_backend.enqueue("crawl_shop", {}, delay_seconds=30)

    assert QUEUE_KEY not in fake.lists
    members = list(fake.zsets[DELAYED_KEY])
    assert len(members) == 1
    data = json.loads(members[0])
    assert data["id"] == job_id
    assert fake.zsets[DELAYED_KEY][members[0]] == FAKE_EPOCH + 30
    assert redis_backend.depth() == {"queued": 0, "delayed": 1, "dead_letter": 0, "processing": 0}


# --- RedisQueueBackend: claim -------------------------------------------------------


def test_redis_claim_moves_due_entry_to_processing_list(redis_backend):
    fake = redis_backend.client
    job_id = redis_backend.enqueue("crawl_shop", {"shop_id": 7})

    entry = redis_backend.claim("worker-1", timeout_seconds=0)

    assert entry["id"] == job_id
    assert entry["type"] == "crawl_shop"
    assert entry["payload"] == {"shop_id": 7}
    assert fake.lists[QUEUE_KEY] == []
    assert len(fake.lists[f"{PROCESSING_KEY}:worker-1"]) == 1


def test_redis_claim_returns_none_when_queue_is_empty(redis_backend):
    fake = redis_backend.client

    entry = redis_backend.claim("worker-1", timeout_seconds=0)

    assert entry is None
    assert f"{PROCESSING_KEY}:worker-1" not in fake.lists


def test_redis_claim_uses_blocking_move_for_positive_timeout(redis_backend):
    fake = redis_backend.client
    job_id = redis_backend.enqueue("crawl_shop", {})

    entry = redis_backend.claim("worker-2")  # default timeout_seconds=1.0 -> brpoplpush

    assert entry["id"] == job_id
    assert len(fake.lists[f"{PROCESSING_KEY}:worker-2"]) == 1


def test_redis_claim_discards_malformed_entry(redis_backend):
    fake = redis_backend.client
    fake.lpush(QUEUE_KEY, "not-json")

    entry = redis_backend.claim("worker-3", timeout_seconds=0)

    assert entry is None
    assert fake.lists.get(f"{PROCESSING_KEY}:worker-3", []) == []


def test_redis_delayed_entries_are_promoted_once_due(redis_backend, clock):
    fake = redis_backend.client
    job_id = redis_backend.enqueue("crawl_shop", {}, delay_seconds=30)

    early = redis_backend.claim("worker-1", timeout_seconds=0)
    assert early is None
    assert len(fake.zsets[DELAYED_KEY]) == 1

    clock.advance(31)
    entry = redis_backend.claim("worker-1", timeout_seconds=0)

    assert entry["id"] == job_id
    assert fake.zsets.get(DELAYED_KEY, {}) == {}



# --- RedisQueueBackend: complete / requeue / dead_letter ------------------------------


def test_redis_complete_removes_entry_from_processing(redis_backend):
    """complete() must remove the entry from sparton:processing:{worker_id}."""
    fake = redis_backend.client
    redis_backend.enqueue("crawl_shop", {})
    entry = redis_backend.claim("worker-1", timeout_seconds=0)
    entry["worker_id"] = "worker-1"  # the worker tags claimed entries this way

    redis_backend.complete(entry)

    assert fake.lists.get(f"{PROCESSING_KEY}:worker-1", []) == []


def test_redis_requeue_enqueues_new_copy_with_same_id_and_delay(redis_backend):
    fake = redis_backend.client
    job_id = redis_backend.enqueue("crawl_shop", {"shop_id": 7})
    entry = redis_backend.claim("worker-1", timeout_seconds=0)
    entry["worker_id"] = "worker-1"

    redis_backend.requeue(entry, delay_seconds=15)

    members = list(fake.zsets[DELAYED_KEY])
    assert len(members) == 1
    data = json.loads(members[0])
    assert data["id"] == job_id
    assert data["payload"] == {"shop_id": 7}
    assert fake.zsets[DELAYED_KEY][members[0]] == FAKE_EPOCH + 15


def test_redis_requeue_without_delay_puts_new_copy_back_in_queue(redis_backend):
    fake = redis_backend.client
    job_id = redis_backend.enqueue("crawl_shop", {"shop_id": 7})
    entry = redis_backend.claim("worker-1", timeout_seconds=0)
    entry["worker_id"] = "worker-1"

    redis_backend.requeue(entry)

    assert DELAYED_KEY not in fake.zsets
    data = json.loads(fake.lists[QUEUE_KEY][0])
    assert data["id"] == job_id


def test_redis_requeue_preserves_retry_count(redis_backend):
    """The worker's failure ladder needs the attempt count to survive requeue."""
    fake = redis_backend.client
    job_id = redis_backend.enqueue("crawl_shop", {})
    entry = redis_backend.claim("worker-1", timeout_seconds=0)
    entry["worker_id"] = "worker-1"
    entry["retry_count"] = 2

    redis_backend.requeue(entry, delay_seconds=15)

    data = json.loads(list(fake.zsets[DELAYED_KEY])[0])
    assert data["id"] == job_id
    assert data["retry_count"] == 2


def test_redis_requeue_removes_original_from_processing(redis_backend):
    """requeue() must remove the original entry from processing before re-enqueuing."""
    fake = redis_backend.client
    redis_backend.enqueue("crawl_shop", {})
    entry = redis_backend.claim("worker-1", timeout_seconds=0)
    entry["worker_id"] = "worker-1"

    redis_backend.requeue(entry, delay_seconds=15)

    assert fake.lists.get(f"{PROCESSING_KEY}:worker-1", []) == []


def test_redis_dead_letter_parks_entry_with_truncated_error(redis_backend):
    fake = redis_backend.client
    job_id = redis_backend.enqueue("crawl_shop", {})
    entry = redis_backend.claim("worker-1", timeout_seconds=0)
    entry["worker_id"] = "worker-1"

    redis_backend.dead_letter(entry, "x" * 2500)

    raw = fake.lists[DEAD_LETTER_KEY][0]
    data = json.loads(raw)
    assert data["id"] == job_id
    assert len(data["error"]) == 2000
    assert data["failed_at"] == FAKE_EPOCH


def test_redis_dead_letter_removes_original_from_processing(redis_backend):
    """dead_letter() must remove the original entry from processing."""
    fake = redis_backend.client
    redis_backend.enqueue("crawl_shop", {})
    entry = redis_backend.claim("worker-1", timeout_seconds=0)
    entry["worker_id"] = "worker-1"

    redis_backend.dead_letter(entry, "boom")

    assert fake.lists.get(f"{PROCESSING_KEY}:worker-1", []) == []


# --- RedisQueueBackend: reclaim_stale ---------------------------------------------------


def test_redis_reclaim_stale_returns_abandoned_entries_to_queue(redis_backend, clock):
    fake = redis_backend.client
    job_id = redis_backend.enqueue("crawl_shop", {})
    redis_backend.claim("worker-1", timeout_seconds=0)

    clock.advance(400)
    reclaimed = redis_backend.reclaim_stale(300)

    assert reclaimed == 1
    data = json.loads(fake.lists[QUEUE_KEY][0])
    assert data["id"] == job_id
    assert fake.lists.get(f"{PROCESSING_KEY}:worker-1", []) == []


def test_redis_reclaim_stale_ignores_fresh_entries(redis_backend, clock):
    fake = redis_backend.client
    redis_backend.enqueue("crawl_shop", {})
    redis_backend.claim("worker-1", timeout_seconds=0)

    clock.advance(60)
    reclaimed = redis_backend.reclaim_stale(300)

    assert reclaimed == 0
    assert len(fake.lists[f"{PROCESSING_KEY}:worker-1"]) == 1


def test_redis_reclaim_stale_skips_unparseable_entries(redis_backend):
    fake = redis_backend.client
    fake.lpush(f"{PROCESSING_KEY}:worker-9", "garbage")

    reclaimed = redis_backend.reclaim_stale(1)

    assert reclaimed == 0
    assert fake.lists[f"{PROCESSING_KEY}:worker-9"] == ["garbage"]



# --- RedisQueueBackend: depth and ping ----------------------------------------------------


def test_redis_depth_counts_all_structures(redis_backend):
    fake = redis_backend.client
    redis_backend.enqueue("crawl_shop", {})
    redis_backend.enqueue("crawl_shop", {"shop_id": 2})
    redis_backend.enqueue("crawl_shop", {}, delay_seconds=30)
    fake.lpush(DEAD_LETTER_KEY, json.dumps({"id": "dead-1"}))
    fake.lpush(f"{PROCESSING_KEY}:worker-1", json.dumps({"id": "p-1"}))
    fake.lpush(f"{PROCESSING_KEY}:worker-2", json.dumps({"id": "p-2"}))

    assert redis_backend.depth() == {
        "queued": 2,
        "delayed": 1,
        "dead_letter": 1,
        "processing": 2,
    }


def test_redis_ping_is_true_when_client_reachable(redis_backend):
    assert redis_backend.ping() is True


def test_redis_ping_is_false_when_client_raises():
    backend = RedisQueueBackend(UnreachableFakeRedis())

    assert backend.ping() is False


# --- get_backend / set_backend -------------------------------------------------------------


def test_get_backend_returns_inline_backend_without_redis_url(process_backend, monkeypatch):
    monkeypatch.setattr(settings, "redis_url", None)

    first = get_backend()
    second = get_backend()

    assert isinstance(first, InlineQueueBackend)
    assert second is first  # process-wide: the backend is cached


def test_get_backend_builds_redis_backend_when_url_configured(process_backend, monkeypatch):
    fake = FakeRedis()
    captured = {}

    def from_url(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return fake

    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
    monkeypatch.setattr(redis_module.Redis, "from_url", staticmethod(from_url))

    backend = get_backend()

    assert isinstance(backend, RedisQueueBackend)
    assert backend.client is fake
    assert captured["url"] == "redis://localhost:6379/0"
    assert captured["decode_responses"] is True


def test_set_backend_overrides_process_backend(process_backend):
    custom = InlineQueueBackend()

    set_backend(custom)

    assert get_backend() is custom

