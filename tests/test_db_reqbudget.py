"""L-081: the measured upstream request budget warns per region, names the top sources, and doesn't judge thin data.
Named test_db_* (it reads fetch_run on local CI's throwaway cluster)."""

from __future__ import annotations

from montney.db import reqbudget
from montney.db.testing import scratch_db


def _runs(conn, source: str, hours_ago: list[float], n_requests: int, bytes_in: int = 1000,
          trigger: str = "schedule") -> None:
    conn.execute("INSERT INTO montney.source (code) VALUES (%s) ON CONFLICT (code) DO NOTHING", (source,))
    sid = conn.execute("SELECT id FROM montney.source WHERE code = %s", (source,)).fetchone()[0]
    for h in hours_ago:
        conn.execute("INSERT INTO montney.fetch_run (source_id, started, finished, ok, n_written, n_requests, bytes_in, "
                     "trigger) VALUES (%s, (now() AT TIME ZONE 'UTC') - make_interval(secs => %s), "
                     "now() AT TIME ZONE 'UTC', true, 1, %s, %s, %s)", (sid, h * 3600, n_requests, bytes_in, trigger))


def test_over_cap_warns_with_the_top_sources():
    with scratch_db() as conn:
        _runs(conn, "test_hot", [i for i in range(10)], 30)          # 10 runs x 30 over ~9 h
        _runs(conn, "test_cold", [1, 5], 2)
        u = reqbudget.measure(conn, cap_by_region={"ab_nebc": 20})
        r = u.regions["ab_nebc"]
        print(reqbudget.format_usage(u))
        assert u.judged and 9 <= u.hours <= 9.1
        assert abs(r.req_per_h - 304 / u.hours) < 0.5                 # (300 + 4) requests over the span
        assert r.top()[0][0] == "test_hot"
        (w,) = u.warnings()
        assert "ab_nebc" in w and "over its cap 20" in w and "test_hot" in w
        assert reqbudget.measure(conn, cap_by_region={"ab_nebc": 1000}).warnings() == []     # under the cap


def test_thin_data_is_reported_but_not_judged_and_unmeasured_runs_dont_count():
    with scratch_db() as conn:
        _runs(conn, "test_hot", [0.5, 0.1], 500)                     # a restart burst: 30 min of data
        conn.execute("INSERT INTO montney.fetch_run (source_id, started, finished, ok, n_written) VALUES "
                     "((SELECT id FROM montney.source WHERE code = 'test_hot'), (now() AT TIME ZONE 'UTC') - "
                     "interval '20 hours', now() AT TIME ZONE 'UTC', true, 1)")   # pre-0012 run: n_requests NULL
        u = reqbudget.measure(conn, cap_by_region={"ab_nebc": 20})
        assert not u.judged and u.hours < 1 and u.warnings() == []
        assert "not judged" in reqbudget.format_usage(u)


def test_budget_file_parses():
    caps = reqbudget.caps()
    assert caps and all(v > 0 for v in caps.values()) and "ab_nebc" in caps


def test_manual_and_importer_runs_are_reported_not_counted():
    """The orchestrator's hand-runs and history back-fills must not distort the caps (L-081, 0013)."""
    with scratch_db() as conn:
        _runs(conn, "test_hot", [i for i in range(10)], 1)                       # schedule: 10 over ~9 h
        _runs(conn, "test_hot", [2], 500, trigger="import")                       # a back-fill
        _runs(conn, "test_hot", [3], 100, trigger="manual")                       # a hand-run
        _runs(conn, "test_hot", [4], 9, trigger="refresh")                        # the refresh button: counted
        u = reqbudget.measure(conn, cap_by_region={"ab_nebc": 5})
        r = u.regions["ab_nebc"]
        assert abs(r.req_per_h - 19 / u.hours) < 0.1 and u.warnings() == []
        assert set(r.other) == {"import", "manual"} and r.other["import"] > r.other["manual"] > 0
        assert "not counted: import" in reqbudget.format_usage(u)
