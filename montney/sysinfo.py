"""/api/system (L-097): what Montney costs this machine, for the ☰ System tab. Local only: no upstream requests.

Processes: the map server (this process), the runner (its heartbeat pid) and Postgres (by process name), each with
its children. CPU % is since the previous call (psutil keeps the last reading on each Process object, so the first
call after a restart reads 0). Storage is the runner's last `du` snapshot; requests are `reqbudget.measure`.
"""
from __future__ import annotations

import os
import threading
import time

import psutil

_procs: dict[int, psutil.Process] = {}     # kept across calls so cpu_percent(None) measures since the last call
_lock = threading.Lock()                   # FastAPI runs sync endpoints on a thread pool: two open tabs share _procs


def _proc(pid: int) -> psutil.Process | None:
    p = _procs.get(pid)
    try:
        if p is None or not p.is_running():
            p = _procs[pid] = psutil.Process(pid)
            p.cpu_percent(None)                 # prime: the first reading is always 0
        return p
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        _procs.pop(pid, None)
        return None


def _group(pids: list[int]) -> dict:
    """Sum RSS and CPU % over these processes and their children (read-only: never signals anything)."""
    seen: set[int] = set()
    rss = cpu = 0.0
    for pid in pids:
        root = _proc(pid)
        if root is None:
            continue
        try:
            kids = root.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            kids = []
        for p in [root, *(_proc(k.pid) for k in kids)]:
            if p is None or p.pid in seen:
                continue
            seen.add(p.pid)
            try:
                rss += p.memory_info().rss
                cpu += p.cpu_percent(None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    return {"n": len(seen), "rss_mb": round(rss / 2**20, 1), "cpu_pct": round(cpu / (psutil.cpu_count() or 1), 1)}


def _postgres_pids() -> list[int]:
    out = []
    for p in psutil.process_iter(["name"]):
        if (p.info.get("name") or "").lower().removesuffix(".exe") == "postgres":
            out.append(p.pid)
    return out


def processes(runner_pid: int | None) -> dict:
    """CPU % is of the whole machine (all cores = 100 %)."""
    with _lock:
        out = {"map": _group([os.getpid()]),
               "runner": _group([runner_pid]) if runner_pid else None,
               "postgres": _group(_postgres_pids())}
        for pid in [k for k, p in list(_procs.items()) if not p.is_running()]:   # backends come and go (Reviewer #384)
            del _procs[pid]
    return out


def machine() -> dict:
    vm = psutil.virtual_memory()
    return {"cpu_pct": psutil.cpu_percent(None), "cores": psutil.cpu_count() or 1,
            "ram_used_gb": round((vm.total - vm.available) / 1e9, 1), "ram_total_gb": round(vm.total / 1e9, 1),
            "uptime_s": int(time.time() - psutil.boot_time())}


def storage(conn) -> dict:
    """Bytes per region from the runner's last du snapshot, against each region's budget."""
    taken = conn.execute("SELECT max(taken_at) FROM montney.usage_snapshot").fetchone()[0]
    rows = conn.execute("""
        SELECT r.code, r.budget_bytes, coalesce(u.bytes, 0)
        FROM montney.region r
        LEFT JOIN (SELECT region_id, sum(bytes) bytes FROM montney.usage_snapshot WHERE taken_at = %s
                   GROUP BY region_id) u ON u.region_id = r.id
        ORDER BY r.id""", (taken,)).fetchall()
    system = conn.execute("SELECT coalesce(sum(bytes), 0) FROM montney.usage_snapshot "
                          "WHERE taken_at = %s AND region_id IS NULL", (taken,)).fetchone()[0]
    return {"taken_at": taken.isoformat() if taken else None, "system_bytes": int(system),
            "regions": [{"code": c, "bytes": int(b), "budget_bytes": int(cap)} for c, cap, b in rows]}


def requests(conn) -> dict:
    from montney.db import reqbudget

    u = reqbudget.measure(conn)
    return {"hours": round(u.hours, 1), "judged": u.judged,
            "regions": [{"code": r.code, "req_per_h": round(r.req_per_h, 2), "cap": r.cap,
                         "mb_per_h": round(r.mb_per_h, 3), "over": r.over and u.judged,
                         "top": [[s, round(q, 2)] for s, q, _ in r.top()]}
                        for r in sorted(u.regions.values(), key=lambda r: r.code)]}
