import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services"))
import fakeredis, pytest
import sentinel as s

@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(s, "R", fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(s, "WEBHOOK", "")

def test_failover_and_failback():
    s.route({"a": True, "b": True})
    s.route({"a": False, "b": True})
    assert s.R.get("active") == "b" and s.R.get("failovers") == "1"
    s.route({"a": True, "b": True})
    assert s.R.get("active") == "a" and s.R.get("failovers") == "2"

def test_total_outage():
    s.route({"a": True, "b": True})
    s.route({"a": False, "b": False})
    assert s.R.get("active") == "none"

def test_failover_time_measured_from_kill():
    s.route({"a": True, "b": True})
    s.R.set("killed_at:a", time.time() - 2)
    s.route({"a": False, "b": True})
    assert 2000 <= int(s.R.lrange("failover_ms", 0, 0)[0]) < 3000

def test_state_sync_catches_up():
    s.R.set("seq", 25)
    for _ in range(3):
        s.replicate({"a": True, "b": False})
    assert s.R.get("rep:a") == "25" and s.R.get("rep:b") is None

def test_history_uptime(monkeypatch):
    monkeypatch.setattr(s.time, "time", lambda: 1_700_000_000.0)
    for ok in (True, True, False, True):
        s.record("a", ok, 40)
    pt = s.history("1h")["a"][-1]
    assert pt["uptime"] == 75.0 and pt["latency"] == 40

def test_status_endpoint_shape():
    d = s.app.test_client().get("/api/status").get_json()
    assert d["active"] == "a" and set(d["providers"]) == {"a", "b"} and "metrics" in d

def test_events_csv_export():
    s.event("warn", "Failover: test")
    r = s.app.test_client().get("/api/export/events.csv")
    assert r.status_code == 200 and "Failover: test" in r.get_data(as_text=True)


def test_recovery_time_is_measured_from_the_restart_press(monkeypatch):
    class Resp:
        status_code = 200
    monkeypatch.setattr(s.requests, "get", lambda *a, **k: Resp())
    s.R.hset("s:a", mapping={"up": 0, "lat": -1})
    s.R.set("down:a", time.time() - 100)       # a long outage ...
    s.R.set("revive_at:a", time.time() - 2)    # ... but Restart was pressed 2 s ago
    assert s.check("a") is True
    assert 2000 <= int(s.R.lrange("mttr", 0, 0)[0]) < 3500


def test_restart_only_starts_the_recovery_timer_when_the_provider_is_down():
    client = s.app.test_client()
    s.R.hset("s:a", mapping={"up": 1, "lat": 20})
    client.post("/api/revive/a")
    assert s.R.get("revive_at:a") is None
    s.R.hset("s:a", mapping={"up": 0, "lat": -1})
    client.post("/api/revive/a")
    assert s.R.get("revive_at:a") is not None


def test_reset_clears_metrics_but_keeps_the_mode():
    s.R.set("failovers", 5)
    s.R.set("mode", "live")
    assert s.app.test_client().post("/api/reset").status_code == 200
    assert s.R.get("failovers") is None and s.R.get("mode") == "live"