"""Sentinel: health-checks both providers, tracks failovers, simulated state sync, control API."""
import os, time, json, csv, io, socket, ssl, threading, requests, redis
from urllib.parse import urlparse
from flask import Flask, Response, jsonify, request

R = redis.Redis(host=os.environ.get("REDIS_HOST", "redis"), decode_responses=True)
P = {"a": {"url": "http://provider_a:5001", "label": "Nimbus Cloud", "region": "Mumbai"},
     "b": {"url": "http://provider_b:5002", "label": "Stratus Cloud", "region": "Frankfurt"}}
SYNC_STEP, SPIKE_MS = 10, 250
# Live mode: real public cloud endpoints (override with LIVE_URL_A / LIVE_URL_B env vars)
LIVE = {"a": {"url": os.environ.get("LIVE_URL_A", "https://dynamodb.ap-south-1.amazonaws.com/ping"),
              "label": "Amazon Web Services", "region": "Mumbai"},
        "b": {"url": os.environ.get("LIVE_URL_B", "https://storage.googleapis.com/"),
              "label": "Google Cloud", "region": "Global edge"}}

def mode():
    return R.get("mode") or "sim"

def info(k):
    return (LIVE if mode() == "live" else P)[k]

def probe(url):
    """Real HTTPS probe with a per-phase timing breakdown (ms). Any HTTP status below 500 counts as reachable."""
    u = urlparse(url); host, port = u.hostname, u.port or 443
    d = {"ok": False, "status": 0, "dns": 0, "tcp": 0, "tls": 0, "ttfb": 0, "total": 0}
    try:
        t0 = time.perf_counter()
        ip = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][4][0]; t1 = time.perf_counter()
        s = socket.create_connection((ip, port), timeout=3); t2 = time.perf_counter()
        s = ssl.create_default_context().wrap_socket(s, server_hostname=host); t3 = time.perf_counter()
        s.sendall(f"GET {u.path or '/'} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\nUser-Agent: Sentinel/1.0\r\n\r\n".encode())
        first = s.recv(64); t4 = time.perf_counter(); s.close()
        status = int(first.split(b" ")[1])
        d.update(ok=status < 500, status=status, dns=(t1-t0)*1000, tcp=(t2-t1)*1000, tls=(t3-t2)*1000,
                 ttfb=(t4-t3)*1000, total=(t4-t0)*1000)
    except Exception:
        pass
    return {k: round(v) if isinstance(v, float) else v for k, v in d.items()}
app = Flask(__name__)

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
ICON = {"bad": "\U0001F534", "warn": "\U0001F7E0", "ok": "\U0001F7E2"}

def alert(kind, msg):
    try:
        requests.post(WEBHOOK, json={"content": f"{ICON[kind]} **Sentinel:** {msg}"}, timeout=3)
    except requests.RequestException:
        pass

def event(kind, msg):
    R.lpush("events", json.dumps({"t": time.time(), "kind": kind, "msg": msg}))
    R.ltrim("events", 0, 199)
    if WEBHOOK and kind in ICON:
        threading.Thread(target=alert, args=(kind, msg), daemon=True).start()

def check(k):
    p, t = info(k), time.time()
    if mode() == "live":
        d = probe(p["url"])
        ok = d["ok"] and R.get(f"{k}:killed") != "1"   # kill switch = simulated blocked route
        ms = d["total"] if ok else -1
        R.set(f"probe:{k}", json.dumps(d))
    else:
        try:
            ok = requests.get(P[k]["url"] + "/health", timeout=1.5).status_code == 200
        except requests.RequestException:
            ok = False
        ms = round((time.time() - t) * 1000) if ok else -1
    was = R.hget(f"s:{k}", "up")
    R.hset(f"s:{k}", mapping={"up": int(ok), "lat": ms})
    R.incr(f"checks:{k}")
    if ok: R.incr(f"ok:{k}")
    R.lpush(f"hist:{k}", ms); R.ltrim(f"hist:{k}", 0, 59)
    if was is not None and was != str(int(ok)):
        if ok:
            dur = time.time() - float(R.get(f"down:{k}") or time.time())
            R.delete(f"down:{k}"); R.delete(f"killed_at:{k}")
            ra = R.get(f"revive_at:{k}")   # recovery time = Restart press to healthy again
            if ra:
                R.lpush("mttr", round((time.time() - float(ra)) * 1000)); R.ltrim("mttr", 0, 49)
                R.delete(f"revive_at:{k}")
            event("ok", f"{p['label']} recovered after {dur:.1f}s")
        else:
            R.set(f"down:{k}", time.time())
            ka = R.get(f"killed_at:{k}")
            if ka:
                R.lpush("mttd", round((time.time() - float(ka)) * 1000)); R.ltrim("mttd", 0, 49)
            event("bad", f"{p['label']} failed its health check")
    record(k, ok, ms)
    return ok

RANGES = {"1h": (60, 1), "24h": (1440, 15), "7d": (10080, 120)}  # (minutes covered, bucket size in minutes)

def record(k, ok, ms):
    """Per-minute counters kept for 7 days (persisted by Redis AOF)."""
    m = int(time.time() // 60)
    R.hincrby(f"h:{k}:n", m, 1)
    if ok:
        R.hincrby(f"h:{k}:ok", m, 1); R.hincrby(f"h:{k}:lat", m, max(ms, 0))
    if m % 60 == 0 and R.set(f"pruned:{k}:{m}", 1, nx=True, ex=7200):
        for suf in ("n", "ok", "lat"):
            for f in R.hkeys(f"h:{k}:{suf}"):
                if int(f) < m - 10080: R.hdel(f"h:{k}:{suf}", f)

def history(rng):
    span, step = RANGES.get(rng, RANGES["1h"])
    now = int(time.time() // 60); start = (now - span) // step * step
    out = {}
    for k in P:
        n, ok, lat = (R.hgetall(f"h:{k}:{x}") for x in ("n", "ok", "lat"))
        b = {}
        for f, v in n.items():
            m = int(f)
            if m < start: continue
            c = b.setdefault(m // step * step, [0, 0, 0])
            c[0] += int(v); c[1] += int(ok.get(f, 0)); c[2] += int(lat.get(f, 0))
        out[k] = [{"t": t * 60, "uptime": round(100 * c[1] / c[0], 2), "latency": round(c[2] / c[1]) if c[1] else None}
                  for t, c in sorted(b.items())]
    return out

def stat(key):
    v = [int(x) for x in R.lrange(key, 0, 49)]
    return {"last": v[0], "avg": round(sum(v) / len(v)), "n": len(v)} if v else None

def csv_response(name, header, rows):
    buf = io.StringIO(); w = csv.writer(buf); w.writerow(header); w.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={name}"})

def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))

def route(up):
    active = "a" if up["a"] else "b" if up["b"] else "none"
    prev = R.get("active")
    if prev != active:
        R.set("active", active)
        if prev:
            R.incr("failovers")
            ka = R.get(f"killed_at:{prev}")
            if ka and active != "none":   # failover time = kill pressed -> traffic rerouted
                R.lpush("failover_ms", round((time.time() - float(ka)) * 1000)); R.ltrim("failover_ms", 0, 49)
            if active == "none": event("bad", "Total outage: both providers are down")
            elif active == "b": event("warn", f"Failover: traffic moved to {info('b')['label']}")
            else: event("ok", f"Failback: traffic returned to {info('a')['label']}")

def traffic():
    for _ in range(3):
        R.incr("req")
        try:
            x = requests.get("http://gateway/work", timeout=3)
            if x.status_code == 200:
                R.incr(f"served:{x.headers.get('X-Provider')}"); R.incr("seq"); continue
        except requests.RequestException:
            pass
        R.incr("fail")

def live_traffic(up):
    act = "a" if up["a"] else "b" if up["b"] else None
    R.incr("req")
    if act: R.incr(f"served:{act}"); R.incr("seq")
    else: R.incr("fail")

def replicate(up):
    seq = int(R.get("seq") or 0)
    for k in P:
        rep = int(R.get(f"rep:{k}") or 0)
        if up[k]: R.set(f"rep:{k}", min(seq, rep + SYNC_STEP))

def loop():
    while True:
        try:
            up = {k: check(k) for k in P}
            route(up); live_traffic(up) if mode() == "live" else traffic(); replicate(up)
        except redis.RedisError:
            pass
        time.sleep(1)

@app.route("/api/status")
def status():
    seq, out = int(R.get("seq") or 0), {}
    for k, p in P.items():
        s, checks = R.hgetall(f"s:{k}"), int(R.get(f"checks:{k}") or 0)
        up, rep = s.get("up") == "1", int(R.get(f"rep:{k}") or 0)
        lag = seq - rep
        out[k] = {"label": info(k)["label"], "region": info(k)["region"], "probe": json.loads(R.get(f"probe:{k}") or "null"), "up": up,
                  "killed": R.get(f"{k}:killed") == "1", "spike": int(R.get(f"{k}:spike") or 0) > 0,
                  "latency": int(s.get("lat", -1)),
                  "uptime": round(100 * int(R.get(f"ok:{k}") or 0) / checks, 2) if checks else 100,
                  "served": int(R.get(f"served:{k}") or 0), "lag": lag,
                  "sync": "stale" if not up else "syncing" if lag > 0 else "in sync",
                  "hist": [int(v) for v in reversed(R.lrange(f"hist:{k}", 0, 59))]}
    req, fail = int(R.get("req") or 0), int(R.get("fail") or 0)
    return jsonify({"mode": mode(), "active": R.get("active") or "a", "providers": out, "seq": seq,
                    "metrics": {x: stat(x) for x in ("failover_ms", "mttd", "mttr")},
                    "req": req, "fail": fail, "failovers": int(R.get("failovers") or 0),
                    "success": round(100 * (req - fail) / req, 2) if req else 100,
                    "events": [json.loads(e) for e in R.lrange("events", 0, 14)]})

@app.route("/api/<action>/<k>", methods=["POST"])
def control(action, k):
    if k not in P: return jsonify(error="unknown provider"), 404
    name = info(k)["label"]
    if action == "kill":
        R.set(f"{k}:killed", 1); R.set(f"killed_at:{k}", time.time()); R.delete(f"revive_at:{k}"); event("bad", f"Kill switch pressed on {name}")
    elif action == "revive":
        R.delete(f"{k}:killed")
        if R.hget(f"s:{k}", "up") == "0": R.set(f"revive_at:{k}", time.time())   # start of the recovery timer
        event("info", f"{name} restart requested")
    elif action == "spike":
        if R.get(f"{k}:spike"): R.delete(f"{k}:spike"); event("info", f"Latency spike cleared on {name}")
        else: R.set(f"{k}:spike", SPIKE_MS); event("warn", f"Latency spike injected on {name} (+{SPIKE_MS} ms)")
    else: return jsonify(error="unknown action"), 404
    return jsonify(ok=True)

@app.route("/api/history")
def history_api():
    return jsonify(history(request.args.get("range", "1h")))

@app.route("/api/export/events.csv")
def export_events():
    rows = [(iso(e["t"]), e["kind"], e["msg"]) for e in map(json.loads, R.lrange("events", 0, -1))]
    return csv_response("sentinel_events.csv", ["time_utc", "level", "message"], rows)

@app.route("/api/export/history.csv")
def export_history():
    rows = [(iso(r["t"]), info(k)["label"], r["uptime"], r["latency"]) for k, pts in history("24h").items() for r in pts]
    return csv_response("sentinel_history.csv", ["time_utc", "provider", "uptime_pct", "avg_latency_ms"], rows)

@app.route("/api/mode/<m>", methods=["POST"])
def set_mode(m):
    if m not in ("sim", "live"): return jsonify(error="unknown mode"), 404
    R.flushdb(); R.set("mode", m)
    event("info", "Switched to live mode: probing real cloud endpoints" if m == "live" else "Switched to simulation mode")
    return jsonify(ok=True)

@app.route("/api/reset", methods=["POST"])
def reset():
    m = mode(); R.flushdb(); R.set("mode", m); event("info", "Metrics reset"); return jsonify(ok=True)

if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8000)