#!/usr/bin/env "achieved_rps": round(len(results) / max(duration, max((r[0] + r[3] / 1000 for r in results), default=duration)), 1),ython3
"""Load test for Sentinel: steady traffic through Nginx while the primary provider is killed and restarted.

Run from the project root with the stack up and the dashboard in Simulation mode:

    python loadtest/run_loadtest.py --rates 50,100,200,400 --duration 60

Standard library only. Latency is measured from each request's scheduled send time, so a slow server
cannot hide its delay by slowing the generator down (no coordinated omission).
"""
import argparse, csv, http.client, json, math, os, platform, sys, threading, time
from urllib.parse import urlparse

SETTLE = 2.0   # seconds after a restart that still count as part of the outage window


# ---------- pure helpers (unit-tested) ----------
def percentile(sorted_vals, p):
    """Nearest-rank percentile (0-100) of an already sorted list."""
    if not sorted_vals:
        return None
    return sorted_vals[max(1, math.ceil(len(sorted_vals) * p / 100)) - 1]


def outage_windows(events):
    """[('kill', t), ('revive', t), ...] -> [(kill_time, revive_time + SETTLE), ...]"""
    windows, start = [], None
    for kind, t in events:
        if kind == "kill":
            start = t
        elif kind == "revive" and start is not None:
            windows.append((start, t + SETTLE))
            start = None
    return windows


def in_window(t, windows):
    return any(a <= t <= b for a, b in windows)


def summarise(rate, duration, results, events, failovers_before, failovers_after):
    """results: [(seconds_since_start, http_status, served_by, latency_ms)]"""
    windows = outage_windows(events)
    ok = [r for r in results if r[1] == 200]
    lat = sorted(r[3] for r in ok)
    kills = sum(1 for kind, _ in events if kind == "kill")
    rnd = lambda v: None if v is None else round(v, 1)
    return {
        "target_rps": rate,
        "achieved_rps": round(len(results) / max(duration, max((r[0] + r[3] / 1000 for r in results), default=duration)), 1),
        "requests": len(results),
        "success_pct": round(100 * len(ok) / len(results), 3) if results else 0.0,
        "failed": len(results) - len(ok),
        "failed_in_outage": sum(1 for r in results if r[1] != 200 and in_window(r[0], windows)),
        "failed_steady": sum(1 for r in results if r[1] != 200 and not in_window(r[0], windows)),
        "p50_ms": rnd(percentile(lat, 50)), "p95_ms": rnd(percentile(lat, 95)),
        "p99_ms": rnd(percentile(lat, 99)), "max_ms": rnd(lat[-1] if lat else None),
        "served_by_backup_pct": round(100 * sum(1 for r in ok if r[2] == "b") / len(ok), 1) if ok else 0.0,
        "kills": kills,
        "sentinel_failovers": failovers_after - failovers_before,
        "expected_failovers": 2 * kills,
    }


def table(rows):
    lines = ["| Target req/s | Achieved req/s | Requests | Success | p50 | p95 | p99 | Max | Failed (outage / steady) | Served by backup | Sentinel failovers (expected) |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in rows:
        lines.append(f"| {s['target_rps']} | {s['achieved_rps']} | {s['requests']} | {s['success_pct']}% | {s['p50_ms']} ms | "
                     f"{s['p95_ms']} ms | {s['p99_ms']} ms | {s['max_ms']} ms | {s['failed_in_outage']} / {s['failed_steady']} | "
                     f"{s['served_by_backup_pct']}% | {s['sentinel_failovers']} ({s['expected_failovers']}) |")
    return "\n".join(lines)


# ---------- network ----------
class Target:
    def __init__(self, url):
        u = urlparse(url)
        self.base, self.host, self.port = url, u.hostname, u.port or 80

    def conn(self):
        return http.client.HTTPConnection(self.host, self.port, timeout=5)

    def post(self, path):
        c = self.conn()
        try:
            c.request("POST", path)
            r = c.getresponse(); r.read()
            return r.status
        finally:
            c.close()

    def get_json(self, path):
        c = self.conn()
        try:
            c.request("GET", path)
            return json.loads(c.getresponse().read())
        finally:
            c.close()


def sleep_until(t, stop):
    while not stop.is_set():
        d = t - time.perf_counter()
        if d <= 0:
            return True
        time.sleep(min(d, 0.2))
    return False


def worker(idx, workers, target, interval, start, end, results, stop):
    """Sends requests on a fixed schedule; latency is measured from the scheduled time."""
    conn, n, offset = target.conn(), 0, idx * interval / workers
    while not stop.is_set():
        sched = start + offset + n * interval
        if sched >= end:
            break
        sleep_until(sched, stop)
        status, served = 0, ""
        try:
            conn.request("GET", "/work")
            r = conn.getresponse(); r.read()
            status, served = r.status, r.getheader("X-Provider") or ""
        except Exception:
            try: conn.close()
            except Exception: pass
            conn = target.conn()
        results.append((sched - start, status, served, (time.perf_counter() - sched) * 1000))
        n += 1


def chaos(target, start, end, every, down_for, first_kill, events, stop):
    """Kill provider A, wait, restart it, repeat."""
    t = start + first_kill
    try:
        while t + down_for < end:
            if not sleep_until(t, stop): return
            target.post("/api/kill/a"); events.append(("kill", time.perf_counter() - start))
            if not sleep_until(t + down_for, stop): return      # the caller restarts A in its finally block
            target.post("/api/revive/a"); events.append(("revive", time.perf_counter() - start))
            t += every
    except Exception as e:
        print(f"chaos thread stopped: {e}", file=sys.stderr)


def run_once(target, rate, args):
    before = target.get_json("/api/status")["failovers"]
    workers = max(1, min(args.workers, rate))
    interval = workers / rate
    results, events, stop = [], [], threading.Event()
    start = time.perf_counter() + 0.5
    end = start + args.duration
    threads = [threading.Thread(target=worker, args=(i, workers, target, interval, start, end, results, stop), daemon=True)
               for i in range(workers)]
    chaos_thread = threading.Thread(target=chaos, args=(target, start, end, args.kill_every, args.down_for, args.first_kill, events, stop),
                                    daemon=True) if args.kill_every else None
    try:
        for t in threads + ([chaos_thread] if chaos_thread else []):
            t.start()
        for t in threads:
            t.join(args.duration + 30)
        if chaos_thread:
            chaos_thread.join(10)
    finally:
        stop.set()
        try: target.post("/api/revive/a")
        except Exception: pass
    time.sleep(4)   # let Sentinel finish its last failback before reading the counter
    after = target.get_json("/api/status")["failovers"]
    return summarise(rate, args.duration, results, events, before, after), results


def preflight(target):
    try:
        st = target.get_json("/api/status")
    except Exception as e:
        sys.exit(f"Cannot reach {target.base}: {e}\nStart the stack first: docker compose up -d")
    if st.get("mode") != "sim":
        sys.exit("Switch the dashboard to Simulation mode first (this test kills provider A).")
    if not (st["providers"]["a"]["up"] and st["providers"]["b"]["up"]):
        sys.exit("Both providers must be healthy. Click Restart on any offline provider, wait 10 seconds, and try again.")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://localhost:8080")
    p.add_argument("--rates", default="50,100,200,400", help="comma-separated target requests per second, one run each")
    p.add_argument("--duration", type=int, default=60, help="seconds per run")
    p.add_argument("--kill-every", type=int, default=20, help="seconds between kills (0 = no kills)")
    p.add_argument("--down-for", type=int, default=8, help="seconds the primary stays killed")
    p.add_argument("--first-kill", type=int, default=10, help="seconds before the first kill")
    p.add_argument("--workers", type=int, default=64, help="maximum concurrent connections")
    p.add_argument("--pause", type=int, default=10, help="seconds between runs")
    p.add_argument("--out-dir", default=os.path.join("loadtest", "results"))
    return p.parse_args(argv)


def main():
    args = parse_args()
    target = Target(args.url)
    preflight(target)
    os.makedirs(args.out_dir, exist_ok=True)
    rates, rows = [int(x) for x in args.rates.split(",")], []
    for i, rate in enumerate(rates):
        print(f"\n=== {rate} req/s for {args.duration} s, kill every {args.kill_every} s for {args.down_for} s ===", flush=True)
        summary, results = run_once(target, rate, args)
        rows.append(summary)
        with open(os.path.join(args.out_dir, f"loadtest_{rate}rps.csv"), "w", newline="") as f:
            w = csv.writer(f); w.writerow(["t_seconds", "http_status", "served_by", "latency_ms"])
            w.writerows((round(t, 3), s, b, round(ms, 1)) for t, s, b, ms in sorted(results))
        print(f"done: {summary['requests']} requests, {summary['success_pct']}% success, p99 {summary['p99_ms']} ms", flush=True)
        if i < len(rates) - 1:
            time.sleep(args.pause)

    notes = [f"- Run on {platform.platform()}, Python {platform.python_version()}. The load generator, Docker and the stack share one machine, so numbers are machine-specific.",
             f"- {args.duration} s per run; provider A killed {args.down_for} s every {args.kill_every} s (first kill at {args.first_kill} s). 'outage' = from kill to 2 s after restart.",
             "- Latency is measured from each request's scheduled send time."]
    slow = [s["target_rps"] for s in rows if s["achieved_rps"] < 0.9 * s["target_rps"]]
    if slow:
        notes.append(f"- Achieved rate was under 90% of target at {slow} req/s: the generator or the stack was saturated.")
    report = f"## Load test results\n\n{table(rows)}\n\n" + "\n".join(notes) + "\n"
    print("\n" + report)
    with open(os.path.join(args.out_dir, "summary.md"), "w") as f:
        f.write(report)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted. Restarting provider A...")
        try: Target("http://localhost:8080").post("/api/revive/a")
        except Exception: pass