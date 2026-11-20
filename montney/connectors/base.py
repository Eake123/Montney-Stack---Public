"""Connector contract.

A connector turns a source spec (YAML in sources/) into observations. Two kinds:

* type: http    - fully declarative, see montney/connectors/http.py
* type: python  - escape hatch: a class with `fetch(ctx) -> FetchResult`

Both can declare `derive:` rules in the spec (sums/differences of series at the same
timestamp), applied after fetch.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from montney import secrets
from montney.store import Event, Obs, SeriesMeta

DEFAULT_TZ = "America/Edmonton"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


@dataclass
class FetchResult:
    obs: list[Obs] = field(default_factory=list)
    meta: list[SeriesMeta] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    # (source, kind) pairs for which `events` is the COMPLETE current set; missing ones get withdrawn
    event_scopes: list[tuple[str, str]] = field(default_factory=list)
    grids: dict[str, dict] = field(default_factory=dict)  # var -> payload, see montney/grids.py


# Politeness (L-081, docs/ARCHITECTURE-REQUESTS.md §3): at most PER_HOST_LIMIT requests in flight to one host across
# every source the process runs at once (a restart used to fire every stale source together).
PER_HOST_LIMIT = 2
_host_slots: dict[str, threading.BoundedSemaphore] = {}
_host_slots_lock = threading.Lock()
# L-107: the request limits went up (kraken's runner is dedicated and goes out through a VPN), so the floor moved into
# the client: request STARTS to one host are at least HOST_GAP_S apart across every source in the process, retries
# included. 0.5 s (compose.yaml, the runner) = at most 2 requests a second to any one host, whatever a spec asks for.
# 0 (the default: tests, `montney try`) = no pacing.
HOST_GAP_S = float(os.environ.get("MONTNEY_HOST_GAP_S") or 0)
_host_next: dict[str, float] = {}


def _pace(url: str, gap: float | None = None, clock=time.monotonic, sleep=time.sleep) -> float:
    """Wait until this host's next allowed start; returns the seconds waited. The slot is reserved under the lock, so
    concurrent sources queue up `gap` apart instead of all firing when the gap ends."""
    gap = HOST_GAP_S if gap is None else gap
    if gap <= 0:
        return 0.0
    host = httpx.URL(url).host or ""
    with _host_slots_lock:
        now = clock()
        start = max(now, _host_next.get(host, 0.0))
        _host_next[host] = start + gap
    wait = start - now
    if wait > 0:
        sleep(wait)
    return wait


def _host_slot(url: str) -> threading.BoundedSemaphore:
    host = httpx.URL(url).host or ""
    with _host_slots_lock:
        if host not in _host_slots:
            _host_slots[host] = threading.BoundedSemaphore(PER_HOST_LIMIT)
        return _host_slots[host]


# Conditional GET (L-107, docs/REGIONS.md §6 "never download the same thing twice"): the ETag / Last-Modified a URL last
# answered with, kept in memory for the life of the process. A validator is committed only after the run that saw it
# has been written (Context.commit_validators, called by the runner), so a parse or write failure downloads the file
# again next time instead of answering "unchanged" for data that was never stored. After a restart the first pull of
# each URL is a full download. Keyed by (source, URL): "unchanged" is only true for the source that stored the file, so
# two sources reading one URL never answer for each other (Reviewer on #461). A Context without a source (`montney try`,
# importers, tests) never sends a validator and never keeps one.
_validators: dict[tuple[str, str], tuple[str | None, str | None]] = {}
_validators_lock = threading.Lock()


@dataclass
class Context:
    client: httpx.Client
    now: datetime  # naive UTC, start of this fetch
    # Measured per run (L-081): every HTTP attempt, retries included, and the bytes downloaded (on the wire). The runner
    # logs them in fetch_run (n_requests, bytes_in). Connectors must use ctx.get/post/request, not ctx.client.
    n_requests: int = 0
    bytes_in: int = 0
    source: str = ""          # the source id this run fetches for (the runner sets it); scopes get_if_changed
    pending_validators: dict[tuple[str, str], tuple[str | None, str | None]] = field(default_factory=dict)

    def get_if_changed(self, url: str, *, params: dict | None = None, headers: dict | None = None,
                       timeout: float = 30.0, retries: int = 3, allow: tuple[int, ...] = (),
                       key: str | None = None) -> httpx.Response | None:
        """GET, or None when the server says the file hasn't changed since the last run that was written (HTTP 304).
        Use it for files polled more often than they change (a daily report checked hourly). None means "nothing new":
        what the file says is already stored. `key` names the resource when the URL carries a changing token.
        Only for a connector whose output depends on the file alone: one that also depends on `ctx.now` (or on another
        file) must keep its own parsed copy, because None gives it nothing to recompute from."""
        k = (self.source, key or str(httpx.URL(url, params=params)))
        with _validators_lock:
            etag, modified = _validators.get(k, (None, None)) if self.source else (None, None)
        h = dict(headers or {})
        if etag:
            h["If-None-Match"] = etag
        if modified:
            h["If-Modified-Since"] = modified
        r = self.request("GET", url, params=params, headers=h, timeout=timeout, retries=retries, allow=(*allow, 304))
        if r.status_code == 304:
            return None
        seen = (r.headers.get("etag"), r.headers.get("last-modified"))
        if self.source and r.status_code == 200 and any(seen):
            self.pending_validators[k] = seen
        return r

    def commit_validators(self) -> int:
        """Keep this run's validators: call only after the run's observations are written. Returns how many."""
        with _validators_lock:
            _validators.update(self.pending_validators)
        n = len(self.pending_validators)
        self.pending_validators.clear()
        return n

    def get(self, url: str, *, params: dict | None = None, headers: dict | None = None,
            timeout: float = 30.0, retries: int = 3, allow: tuple[int, ...] = ()) -> httpx.Response:
        return self.request("GET", url, params=params, headers=headers, timeout=timeout, retries=retries,
                            allow=allow)

    def post(self, url: str, *, json: Any = None, data: Any = None, params: dict | None = None,
             headers: dict | None = None, timeout: float = 30.0, retries: int = 3) -> httpx.Response:
        return self.request("POST", url, json=json, data=data, params=params, headers=headers,
                            timeout=timeout, retries=retries)

    def request(self, method: str, url: str, *, params: dict | None = None,
                headers: dict | None = None, json: Any = None, data: Any = None,
                timeout: float = 30.0, retries: int = 3, allow: tuple[int, ...] = ()) -> httpx.Response:
        """HTTP with backoff on 429/5xx and network errors; daily-quota 429s fail fast. A status in `allow` (e.g. 404,
        "not published yet") is returned rather than raised."""
        delay = 2.0
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                with _host_slot(url):
                    _pace(url)
                    self.n_requests += 1
                    r = self.client.request(method, url, params=params, headers=headers, json=json,
                                            data=data, timeout=timeout)
                    self.bytes_in += r.num_bytes_downloaded or len(r.content)    # wire bytes; a pre-read body: its length
                if r.status_code == 429 and any(w in r.text.lower() for w in ("daily", "tomorrow", "quota")):
                    r.raise_for_status()  # a daily quota won't clear with backoff
                if r.status_code == 429 or r.status_code >= 500:
                    ra = r.headers.get("retry-after")
                    wait = float(ra) if ra and ra.isdigit() else delay
                    last = httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
                    if attempt < retries:
                        time.sleep(min(wait, 30))
                        delay *= 2
                        continue
                if r.status_code not in allow:
                    r.raise_for_status()
                return r
            except (httpx.TransportError,) as e:
                last = e
                if attempt < retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
        assert last is not None
        raise last


class Connector:
    def __init__(self, spec: dict):
        self.spec = spec
        self.id: str = spec["id"]
        self.live: bool = spec.get("mode", "cached") == "live"

    def fetch(self, ctx: Context) -> FetchResult:  # pragma: no cover - interface
        raise NotImplementedError

    def declared_series(self) -> list[SeriesMeta]:
        """Series known without fetching (for the catalog / importers)."""
        return []

    # ------------------------------------------------------------- derive
    def run(self, ctx: Context) -> FetchResult:
        res = self.fetch(ctx)
        derive = self.spec.get("derive") or []
        if derive:
            res.obs.extend(_derive(derive, res.obs))
            for d in derive:
                res.meta.append(SeriesMeta(d["id"], self.id, d.get("label", d["id"]),
                                           d.get("unit", ""), d.get("freq", ""), d.get("group", ""),
                                           day=d.get("day", "")))
        return res


def _derive(rules: list[dict], obs: list[Obs]) -> list[Obs]:
    """`sum: [a, b]`, `minus: [c]` and/or `weights: {d: 0.15}` across series sharing (ts, as_of).

    A derived point is only produced when every input is present, so a partial
    fetch never yields a misleading total.
    """
    by_key: dict[tuple, dict[str, float]] = {}
    for o in obs:
        by_key.setdefault((o.ts, o.as_of), {})[o.series_id] = o.value
    out: list[Obs] = []
    for rule in rules:
        plus = rule.get("sum", [])
        minus = rule.get("minus", [])
        weights = rule.get("weights", {})
        need = plus + minus + list(weights)
        for (ts, as_of), vals in by_key.items():
            if need and all(n in vals for n in need):
                v = (sum(vals[n] for n in plus) - sum(vals[n] for n in minus)
                     + sum(vals[n] * float(w) for n, w in weights.items()))
                out.append(Obs(rule["id"], ts, v * rule.get("scale", 1.0), as_of))
    return out


# --------------------------------------------------------------- templating
_TPL = re.compile(r"\{([^{}]+)\}")
_DATE_EXPR = re.compile(r"^today(?:([+-])(\d+)d)?$")


def local_today(tz: str = DEFAULT_TZ) -> date:
    return datetime.now(ZoneInfo(tz)).date()


def render(template: Any, vars: dict[str, Any] | None = None, tz: str = DEFAULT_TZ) -> Any:
    """Expand {secret:NAME}, {today}, {today-3d}, {today+1d}, {var}, {var|slug}, {var|title}."""
    if isinstance(template, dict):
        return {k: render(v, vars, tz) for k, v in template.items()}
    if isinstance(template, list):
        return [render(v, vars, tz) for v in template]
    if not isinstance(template, str):
        return template
    vars = vars or {}

    def sub(m: re.Match) -> str:
        expr = m.group(1).strip()
        if expr.startswith("secret:"):
            return secrets.get(expr[7:])
        name, _, filt = expr.partition("|")
        dm = _DATE_EXPR.match(name)
        if dm:
            d = local_today(tz)
            if dm.group(1):
                n = int(dm.group(2))
                d = d + timedelta(days=n if dm.group(1) == "+" else -n)
            val: Any = d.isoformat()
        elif name in vars:
            val = vars[name]
        else:
            return m.group(0)
        return _filter(str(val), filt)

    return _TPL.sub(sub, template)


def _filter(s: str, filt: str) -> str:
    if filt == "slug":
        return slug(s)
    if filt == "title":
        return s.replace("_", " ").title()
    if filt == "upper":
        return s.upper()
    return s


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def secret_names(obj: Any) -> set[str]:
    """All {secret:NAME} references inside a spec."""
    found: set[str] = set()
    if isinstance(obj, dict):
        for v in obj.values():
            found |= secret_names(v)
    elif isinstance(obj, list):
        for v in obj:
            found |= secret_names(v)
    elif isinstance(obj, str):
        found |= set(re.findall(r"\{secret:([A-Za-z0-9_]+)\}", obj))
    return found


# --------------------------------------------------------------- timestamps
def parse_ts(raw: Any, cfg: dict) -> datetime | None:
    """Parse a timestamp per a `ts:` config and return naive UTC (or a naive date for date_only)."""
    if raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        unit = cfg.get("epoch", "s")
        dt = datetime.fromtimestamp(raw / (1000 if unit == "ms" else 1), tz=timezone.utc)
    else:
        s = str(raw).strip()
        fmt = cfg.get("format")
        if fmt:
            dt = datetime.strptime(s, fmt)
        else:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if cfg.get("date_only"):
        d = dt.date() + timedelta(days=int(cfg.get("offset_days", 0)))
        return datetime(d.year, d.month, d.day)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz(cfg.get("tz", "UTC")))
    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    if cfg.get("offset_days"):
        dt += timedelta(days=int(cfg["offset_days"]))
    return dt


def _tz(name: str):
    if name.upper() == "UTC":
        return timezone.utc
    m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", name)
    if m:
        delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
        return timezone(delta if m.group(1) == "+" else -delta)
    return ZoneInfo(name)


def to_float(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "N/A", "null", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None
