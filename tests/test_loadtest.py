import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "loadtest"))
import run_loadtest as lt


def test_percentile_uses_nearest_rank():
    values = list(range(1, 101))
    assert lt.percentile(values, 50) == 50
    assert lt.percentile(values, 95) == 95
    assert lt.percentile(values, 100) == 100
    assert lt.percentile([], 50) is None


def test_outage_windows_include_a_settle_period():
    windows = lt.outage_windows([("kill", 10.0), ("revive", 18.0), ("kill", 30.0), ("revive", 38.0)])
    assert windows == [(10.0, 18.0 + lt.SETTLE), (30.0, 38.0 + lt.SETTLE)]
    assert lt.in_window(19.0, windows) and not lt.in_window(25.0, windows)


def test_summary_splits_failures_by_phase():
    results = [(1.0, 200, "a", 40.0), (12.0, 200, "b", 80.0), (13.0, 502, "", 5000.0), (25.0, 500, "", 10.0)]
    events = [("kill", 10.0), ("revive", 18.0)]
    summary = lt.summarise(100, 30, results, events, 0, 2)
    assert summary["requests"] == 4 and summary["failed"] == 2
    assert summary["failed_in_outage"] == 1 and summary["failed_steady"] == 1
    assert summary["served_by_backup_pct"] == 50.0
    assert summary["kills"] == 1 and summary["sentinel_failovers"] == 2 and summary["expected_failovers"] == 2


def test_achieved_rate_counts_late_completions():
    # 100 requests scheduled over 10 s, but the last one only finished 40 s after the run began
    results = [(i / 10, 200, "a", 50.0) for i in range(99)] + [(9.9, 200, "a", 30100.0)]
    summary = lt.summarise(10, 10, results, [], 0, 0)
    assert summary["achieved_rps"] == 2.5