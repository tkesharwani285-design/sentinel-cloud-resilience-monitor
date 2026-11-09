import os, sys, time
os.environ.setdefault("NAME", "a")
os.environ.setdefault("PORT", "5001")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services"))
import fakeredis, pytest, redis
import provider as p


class DeadRedis:
    """Stands in for an unreachable Redis: every call raises a connection error."""
    def get(self, *_):
        raise redis.ConnectionError("redis is down")


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(p, "last", (False, 0))


def live_redis(monkeypatch, **flags):
    fake = fakeredis.FakeRedis(decode_responses=True)
    for k, v in flags.items():
        fake.set(f"a:{k}", v)
    monkeypatch.setattr(p, "r", fake)


def test_refresh_reads_flags_from_redis(monkeypatch):
    live_redis(monkeypatch, killed=1, spike=250)
    p.refresh()
    assert p.last == (True, 250)


def test_refresh_keeps_last_known_flags_when_redis_is_down(monkeypatch):
    live_redis(monkeypatch, killed=1)
    p.refresh()
    monkeypatch.setattr(p, "r", DeadRedis())
    p.refresh()
    assert p.last == (True, 0)


def test_requests_still_served_quickly_when_redis_is_down(monkeypatch):
    monkeypatch.setattr(p, "r", DeadRedis())
    p.refresh()
    start = time.time()
    resp = p.app.test_client().get("/work")
    assert resp.status_code == 200 and resp.get_json()["ok"] is True
    assert time.time() - start < 0.5


def test_killed_provider_stays_killed_during_redis_outage(monkeypatch):
    live_redis(monkeypatch, killed=1)
    p.refresh()
    monkeypatch.setattr(p, "r", DeadRedis())
    p.refresh()
    client = p.app.test_client()
    assert client.get("/work").status_code == 503
    assert client.get("/health").status_code == 503


def test_provider_picks_up_new_flags_after_redis_returns(monkeypatch):
    monkeypatch.setattr(p, "r", DeadRedis())
    p.refresh()
    live_redis(monkeypatch, killed=1)
    p.refresh()
    assert p.app.test_client().get("/work").status_code == 503