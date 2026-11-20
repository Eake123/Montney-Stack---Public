"""A/B parity: v1's DuckDB `Store` vs `PostgresStore`, same calls, same process (docs/db/PORT.md, L-035;
Wellsite §4.6).

One scenario of metas, fetches (re-fetches, revisions, identical and changed forecast runs, live
replacement), events (TTL kinds, refs, withdrawal and return, content changes) and fetch logs is applied to
both stores under one frozen clock. Then every read method is compared over a grid of arguments, and so is
the shipped consumer, `snapshot.build()`. The deliberate differences are asserted exactly as documented, not
skipped:
* `count()`: Postgres <= DuckDB (change-only skips repeated vintages);
* `series()["as_of"]`: Postgres <= DuckDB, equal when nothing was skipped; `ts` and `v` always equal.
Seeded red: `_VINTAGE_ORDER = "ASC"` (the earliest vintage per ts, a classic port bug) must break parity.
Fails closed: a comparison grid that compared nothing is a failure.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from montney import snapshot
from montney.db import pgstore as pgmod
from montney.db import store as dbstore
from montney.db.pgstore import PostgresStore
from montney.db.testing import scratch_db
from montney import store as v1mod
from montney.store import Event, Obs, SeriesMeta, Store

# Relative to now, so the scenario stays inside every tier's raw retention (60 d for fast) whenever CI runs:
# retention must never masquerade as a parity failure (PORT.md).
T0 = datetime.now(timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0) - timedelta(days=1)
D0 = T0.replace(hour=0)
# Allowed differences (PORT.md, L-035 c). One entry per difference, each approved by the orchestrator and
# asserted in its exact form below; anything not listed must be EQUAL.
ALLOWED = {
    "count": "postgres <= duckdb: change-only stores fewer rows than DuckDB, which stored every repeated vintage",
    "series.as_of@as_of_max": "with as_of_max set, postgres <= duckdb (ts and v equal): a skipped identical "
                              "vintage between two stored ones isn't known to series_state",
    "events.refs.duplicates": "a ref repeated within one event appears once in postgres (event_scope's key): "
                              "refs equal DuckDB's with repeats removed, first occurrence kept, order kept",
}

IDS = ["test.fast.mw", "test.hourly.price", "test.gdsr.flow", "test.fc.wind", "test.monthly.cap", "test.live.flow"]


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


def _scenario(stores, clock):
    metas = [
        SeriesMeta("test.fast.mw", "src_fast", label="MW", unit="MW", freq="5min", group="power"),
        SeriesMeta("test.hourly.price", "src_price", unit="$/MWh", freq="1h"),
        SeriesMeta("test.gdsr.flow", "src_gdsr", unit="MMcf/d", freq="D", day="gas"),
        SeriesMeta("test.fc.wind", "src_fc", unit="MW", freq="1h", extra={"tier": "forecast"}),
        SeriesMeta("test.monthly.cap", "src_m", unit="MMcf/d", freq="M"),
        SeriesMeta("test.live.flow", "src_live", unit="MMcf/d", freq="5min"),
    ]
    for s in stores:
        s.upsert_meta(metas)
    times = []
    for k in range(4):
        clock.t = T0 + timedelta(minutes=15 * k)
        now = clock.t
        times.append(now)
        # i == 2 is revised in place from fetch 2 on (same as_of). One row per point per batch: v1's DISTINCT ON
        # picks an arbitrary duplicate, so a batch must not carry one.
        fast = [Obs("test.fast.mw", T0 - timedelta(minutes=5 * i), 99.0 if (k >= 2 and i == 2) else float(i % 4),
                    T0 - timedelta(minutes=5 * i)) for i in range(12)]
        fast += [Obs("test.fast.mw", T0 + timedelta(minutes=5 * j), float(j), T0 + timedelta(minutes=5 * j))
                 for j in range(1, 3 * k + 1)]
        hourly = [Obs("test.hourly.price", T0 - timedelta(hours=h), 40.0 + h, T0 - timedelta(hours=h)) for h in range(6)]
        report = T0 + timedelta(days=k // 2)                # GDSR: same values re-published on a later day
        gdsr = [Obs("test.gdsr.flow", D0 - timedelta(days=5 - d), 100.0 + d + (5 if (k == 3 and d == 2) else 0),
                    report) for d in range(5)]
        base = 1.0 if k < 2 else 2.0                        # forecast: runs 0,1 identical; 2,3 identical
        fc = [Obs("test.fc.wind", T0 + timedelta(hours=h), base * h + (0.5 if (k >= 2 and h == 3) else 0), now)
              for h in range(1, 13)]
        monthly = [Obs("test.monthly.cap", D0.replace(day=1), 500.0, D0.replace(day=1) - timedelta(days=5))]
        live = [Obs("test.live.flow", now - timedelta(minutes=5 * i), 10.0 + k + i, now) for i in range(3)]
        for s in stores:
            for batch in (fast, hourly, gdsr, fc, monthly):
                s.write(batch)
            s.write(live, live=True)
            for src in ("src_fast", "src_price", "src_gdsr", "src_fc", "src_m", "src_live"):
                ok = not (src == "src_fc" and k == 1)
                s.log_fetch(src, now - timedelta(seconds=30), ok, 12, None if ok else "HTTPError: 503")
        ev = [
            Event("src_ev:o1", "src_ev", "outage", "EGAT outage", refs=["gas.ngtl.egat"],
                  starts=T0 + timedelta(hours=1), ends=T0 + timedelta(days=2), severity="warn",
                  published=T0 - timedelta(hours=1, minutes=k if k == 9 else 0)),
            Event("src_ev:b1", "src_ev", "bulletin", "Bulletin" + (" (updated)" if k >= 2 else ""),
                  refs=["gas.ngtl.wgat", "gas.ngtl.egat"], published=T0 - timedelta(hours=2)),   # unsorted on purpose
            Event("src_ev:a1", "src_ev", "alert", "Alert", published=T0 - timedelta(hours=3), extra={"x": 1},
                  refs=["power.ab", "gas.ngtl.egat", "power.ab"]),   # a repeated ref: ALLOWED["events.refs.duplicates"]
        ]
        if k == 2:
            ev = ev[1:]          # the outage drops out of a complete set: withdrawn; it returns at k == 3
        for s in stores:
            s.upsert_events(ev, complete_scopes=[("src_ev", "outage")])
    return times


def _parse(a):
    return datetime.fromisoformat(a) if a else None


ALLOWED_HITS: dict[str, int] = {}


def _events_allowed(a: list, b: list) -> bool:
    """ALLOWED["events.refs.duplicates"], in its exact form: identical events except that Postgres's refs are
    DuckDB's with repeats removed (first occurrence kept, order preserved)."""
    if len(a) != len(b):
        return False
    hit = False
    for x, y in zip(a, b):
        if {k: v for k, v in x.items() if k != "refs"} != {k: v for k, v in y.items() if k != "refs"}:
            return False
        if y["refs"] != list(dict.fromkeys(x["refs"])):
            return False
        hit = hit or x["refs"] != y["refs"]
    if hit:
        ALLOWED_HITS["events.refs.duplicates"] = ALLOWED_HITS.get("events.refs.duplicates", 0) + 1
    return True


def compare(v1, pg, times) -> tuple[int, list[str]]:
    """Run the comparison grid. Returns (number of comparisons, problems)."""
    n, problems = 0, []

    def same(name, a, b):
        nonlocal n
        n += 1
        if a != b and not (name.startswith("events") and _events_allowed(a, b)):
            problems.append(f"{name}:\n  duckdb   {a!r}\n  postgres {b!r}")

    grid = [(None, None, None), (T0 - timedelta(hours=2), T0 + timedelta(hours=6), None),
            (D0 - timedelta(days=4), D0 - timedelta(days=2), None)] + [(None, None, t) for t in times]
    for start, end, amax in grid:
        a, b = v1.series(IDS, start, end, amax), pg.series(IDS, start, end, amax)
        for sid in IDS:
            same(f"series[{sid}] ts {start, end, amax}", a[sid]["ts"], b[sid]["ts"])
            same(f"series[{sid}] v {start, end, amax}", a[sid]["v"], b[sid]["v"])
            pa, pb = _parse(a[sid]["as_of"]), _parse(b[sid]["as_of"])
            if amax is None:
                same(f"series[{sid}] as_of {start, end}", pa, pb)          # sourced from series_state: equal
            else:                                                          # ALLOWED["series.as_of@as_of_max"]
                n += 1
                if (pa is None) != (pb is None) or (pa and pb > pa):
                    problems.append(f"series[{sid}] as_of {start, end, amax}: duckdb {pa}, postgres {pb} (must be <=)")
    same("meta", v1.meta(IDS), pg.meta(IDS))
    same("catalog", v1.catalog(), pg.catalog())
    same("last_fetch", v1.last_fetch(), pg.last_fetch())
    n += 1                                                                 # ALLOWED["count"]
    if not pg.count() <= v1.count():
        problems.append(f"count: postgres {pg.count()} > duckdb {v1.count()}")
    for kinds in (None, ["outage"], ["bulletin", "alert"]):
        for ref in (None, "gas.ngtl.egat", "gas.ngtl.wgat"):
            for state in (None, "ongoing", "upcoming", "current", "past"):
                # T0 + 9.5 h sits between the alert's 12 h TTL end (published T0 - 3 h) and a 13 h one
                for at in (T0, T0 + timedelta(hours=9, minutes=30), T0 + timedelta(hours=13), T0 + timedelta(days=3)):
                    for wd in (False, True):
                        same(f"events {kinds, ref, state, at, wd}",
                             v1.events(kinds=kinds, ref=ref, state=state, at=at, include_withdrawn=wd),
                             pg.events(kinds=kinds, ref=ref, state=state, at=at, include_withdrawn=wd))
    for since in times:
        same(f"events since {since}", v1.events(since=since, at=T0), pg.events(since=since, at=T0))
    same("events limit 2", v1.events(limit=2, at=T0), pg.events(limit=2, at=T0))
    return n, problems


def _snapshots(v1, pg, monkeypatch):
    monkeypatch.setattr(snapshot, "utcnow", lambda: T0 + timedelta(hours=1))
    refs = {"f.fast": {"value": "test.fast.mw"}, "f.price": {"value": "test.hourly.price"},
            "f.gas": {"value": "test.gdsr.flow", "capacity": "test.monthly.cap"},
            "f.wind": {"value": "test.fc.wind"}, "f.live": {"value": "test.live.flow"}}
    compares = {"f.fast": "1h", "f.gas": "prev"}
    return (snapshot.build(v1, refs, compares, extra=["test.fc.wind"]),
            snapshot.build(pg, refs, compares, extra=["test.fc.wind"]))


@pytest.fixture
def both(tmp_path, monkeypatch):
    clock = Clock()
    for mod in (v1mod, dbstore, pgmod):
        monkeypatch.setattr(mod, "_utcnow", clock)
    v1 = Store(tmp_path / "v1.duckdb")
    with scratch_db() as conn:
        pg = PostgresStore(conn)
        times = _scenario([v1, pg], clock)
        yield v1, pg, times
    v1.close()


def test_every_read_method_matches_duckdb(both, monkeypatch):
    v1, pg, times = both
    n, problems = compare(v1, pg, times)
    print(f"{n} comparisons, {len(problems)} problems; rows stored: duckdb {v1.count()}, postgres {pg.count()}")
    for p in problems[:10]:
        print(p)
    assert n > 300, f"only {n} comparisons: the grid compared too little (NO-RESULT)"
    assert not problems
    # every ALLOWED entry this scenario can exercise really was exercised (an unused entry would be untested)
    print(f"allowed differences hit: {ALLOWED_HITS}")
    assert ALLOWED_HITS.get("events.refs.duplicates", 0) > 0
    assert pg.count() < v1.count()          # change-only really skipped something in this scenario


def test_snapshot_build_is_identical(both, monkeypatch):
    v1, pg, _ = both
    a, b = _snapshots(v1, pg, monkeypatch)
    print({k: (v.get("v"), v.get("ts"), v.get("prev")) for k, v in b["values"].items()})
    assert a["values"] and all(v.get("v") is not None for v in a["values"].values())   # not an empty comparison
    assert a == b


RED_SWITCHES = [
    ("_VINTAGE_ORDER", "ASC", "earliest vintage instead of latest"),
    ("_ASOF_OP", "<", "as_of_max boundary off by one"),
    ("_INCLUDE_LIVE", False, "live union left out"),
    ("EVENT_TTL_HOURS", {"alert": 13, "bulletin": 48, "notice": 72}, "a TTL changed in the state CASE"),
    ("_SINCE_OP", ">=", "since off by one"),
]


@pytest.mark.parametrize("attr, value, what", RED_SWITCHES, ids=[r[2] for r in RED_SWITCHES])
def test_RED_each_port_bug_breaks_parity(both, monkeypatch, attr, value, what):
    """Seeded reds (PORT.md): each classic port bug must make the comparison grid fail."""
    v1, pg, times = both
    monkeypatch.setattr(PostgresStore, attr, value)
    n, problems = compare(v1, pg, times)
    print(f"RED {what}: {len(problems)} problems, e.g. {problems[:1]}")
    assert problems, f"parity did not notice: {what}"


def test_allowlist_is_exactly_the_approved_entries():
    """A new difference needs a new ALLOWED entry, i.e. the orchestrator's approval (L-035 c)."""
    assert set(ALLOWED) == {"count", "series.as_of@as_of_max", "events.refs.duplicates"}


# ------------------------------------------------------------------ events.tie_order (approved 2026-11-01)
def _ev(i, key, **kw):
    return {"id": i, "kind": "outage", "title": f"t{i}", "published": key, "first_seen": key, "refs": ["r"], **kw}


def test_tie_order_allows_only_a_swap_among_equal_sort_keys():
    """v1 has no tie-breaker after coalesce(published, first_seen); Postgres breaks ties by id (PORT.md "Event
    order"). Allowed: the same events, equal by id, in the same key sequence, swapped only among equal keys."""
    from montney.db.parity import events_equal

    duck = [_ev("b", "2026-11-01T10:00"), _ev("a", "2026-11-01T10:00"), _ev("c", "2026-11-01T09:00")]
    pg = [_ev("a", "2026-11-01T10:00"), _ev("b", "2026-11-01T10:00"), _ev("c", "2026-11-01T09:00")]
    hits = {}
    assert events_equal(duck, pg, hits) and hits == {"events.tie_order": 1}
    hits = {}
    assert events_equal(duck, list(duck), hits) and hits == {}                    # identical: nothing hit


@pytest.mark.parametrize("break_it", ["swap-different-keys", "content-changed", "event-missing", "event-replaced"])
def test_RED_tie_order_never_hides_a_real_difference(break_it):
    """Seeded red (orchestrator): two events with DIFFERENT sort keys swapped is a real order difference and
    must fail; so must changed content, a missing event or a different event under an equal key."""
    from montney.db.parity import events_equal

    duck = [_ev("a", "2026-11-01T10:00"), _ev("b", "2026-11-01T09:00"), _ev("c", "2026-11-01T08:00")]
    if break_it == "swap-different-keys":
        pg = [duck[1], duck[0], duck[2]]
    elif break_it == "content-changed":
        pg = [duck[0], {**duck[1], "title": "changed"}, duck[2]]
    elif break_it == "event-missing":
        pg = duck[:2]
    else:
        pg = [duck[0], _ev("z", "2026-11-01T09:00"), duck[2]]
    assert not events_equal(duck, pg, {})


def test_refs_duplicates_still_allowed_alongside_tie_order():
    from montney.db.parity import events_equal

    duck = [_ev("b", "K", refs=["x", "x", "y"]), _ev("a", "K")]
    pg = [_ev("a", "K"), _ev("b", "K", refs=["x", "y"])]
    hits = {}
    assert events_equal(duck, pg, hits) and hits == {"events.tie_order": 1, "events.refs.duplicates": 1}


def test_real_data_allowlist_is_exactly_the_approved_entries():
    """montney/db/parity.py (the real-data run): the three PORT.md entries, catalog.realized (L-036) and
    events.tie_order (approved 2026-11-01). Adding one needs the orchestrator."""
    from montney.db import parity

    assert set(parity.ALLOWED) == {"count", "series.as_of@as_of_max", "events.refs.duplicates",
                                   "catalog.realized", "events.tie_order"}
