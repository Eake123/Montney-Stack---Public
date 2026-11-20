"""L-102: sub-daily series read a 30 h window, re-reading 15 days only when a needed point isn't in it. The output must
equal a full 15-day read in every edge case (fresh, stale, sparse, `prev` compare, capacity read at a stale value)."""
from datetime import datetime, timedelta

from montney import snapshot

NOW = datetime(2026, 11, 4, 12, 0)
EP = lambda d: (d - datetime(1970, 1, 1)).total_seconds()


class FakeStore:
    def __init__(self, series, meta):
        self.s, self.m, self.calls = series, meta, []

    def meta(self, ids):
        return {i: self.m[i] for i in ids if i in self.m}

    def series(self, ids, start=None, end=None, **_):
        self.calls.append((sorted(ids), start))
        out = {}
        for i in ids:
            pts = [(t, v) for t, v in self.s.get(i, []) if (start is None or t >= EP(start)) and (end is None or t <= EP(end))]
            out[i] = {"ts": [t for t, _ in pts], "v": [v for _, v in pts], "as_of": None}
        return out


def five_min(last: datetime, days: float, v0=100.0):
    n = int(days * 288)
    return [(EP(last - timedelta(minutes=5 * k)), v0 + k) for k in range(n)][::-1]


def build(store, monkeypatch, short_h):
    monkeypatch.setattr(snapshot, "utcnow", lambda: NOW)
    monkeypatch.setattr(snapshot, "SHORT_WINDOW_H", short_h)
    monkeypatch.setattr(snapshot.weather_percentile, "snapshot_section", lambda *a, **k: {})
    refs = {"f.fresh": {"value": "fresh"}, "f.stale": {"value": "stale", "capacity": "cap_stale"},
            "f.prev": {"value": "sparse"}, "f.cap": {"value": "fresh2", "capacity": "cap"},
            "f.day": {"value": "daily"}}
    return snapshot.build(store, refs, {"f.prev": "prev"}, ["fresh@1h", "fresh@24h"], {}, {"f.fresh": "24h"})


def test_short_window_matches_a_full_read(monkeypatch):
    m5 = {"freq": "5min", "unit": "MW"}
    series = {"fresh": five_min(NOW - timedelta(minutes=5), 15), "fresh2": five_min(NOW, 15, 7),
              "cap": five_min(NOW, 15, 900),
              "stale": five_min(NOW - timedelta(days=4), 10),                   # last point 4 days ago
              "cap_stale": five_min(NOW, 15, 500),                             # read at the stale value's ts
              "sparse": [(EP(NOW - timedelta(days=5)), 1.0), (EP(NOW - timedelta(minutes=10)), 2.0)],
              "daily": [(EP(datetime(2026, 11, d)), float(d)) for d in range(1, 5)]}
    meta = {k: m5 for k in series} | {"daily": {"freq": "D", "unit": "MMcf/d"}}
    full = build(FakeStore(series, meta), monkeypatch, 15 * 24)
    st = FakeStore(series, meta)
    short = build(st, monkeypatch, 30)
    assert short == full
    redone = [ids for ids, start in st.calls if start == NOW - timedelta(days=15) and "daily" not in ids]
    assert redone and set(redone[0]) == {"stale", "cap_stale", "sparse"}      # fresh ones never re-read
    assert short["values"]["f.stale"]["v"] is not None and short["values"]["f.prev"]["prev"] == 1.0
