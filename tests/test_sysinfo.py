"""L-097: /api/system's local process and machine readings (no DB, no network)."""
import os

from montney import sysinfo


def test_processes_reads_this_process_and_skips_a_missing_runner():
    p = sysinfo.processes(None)
    assert p["runner"] is None
    assert p["map"]["n"] >= 1 and p["map"]["rss_mb"] > 0 and p["map"]["cpu_pct"] >= 0
    assert p["postgres"]["n"] >= 0


def test_a_dead_pid_reads_as_empty_not_an_error():
    g = sysinfo._group([2**22 + 12345])
    assert g == {"n": 0, "rss_mb": 0.0, "cpu_pct": 0.0}


def test_machine_shape():
    m = sysinfo.machine()
    assert m["cores"] >= 1 and 0 < m["ram_used_gb"] <= m["ram_total_gb"] and m["uptime_s"] > 0


def test_children_are_counted_once():
    g = sysinfo._group([os.getpid(), os.getpid()])
    assert g["n"] == sysinfo._group([os.getpid()])["n"]


def test_dead_entries_are_pruned():
    sysinfo._procs[2**22 + 54321] = sysinfo.psutil.Process(os.getpid())   # stand-in, then make it look dead
    sysinfo._procs[2**22 + 54321].is_running = lambda: False
    sysinfo.processes(None)
    assert 2**22 + 54321 not in sysinfo._procs


def test_concurrent_calls_do_not_race():
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(lambda _: sysinfo.processes(None), range(32)))
    assert all(r["map"]["n"] >= 1 for r in res)
