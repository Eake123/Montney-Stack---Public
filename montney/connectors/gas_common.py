"""Small helpers shared by the gas connectors (TC Customer Express, Enbridge infopost)."""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from montney.connectors.ngtl_gasday import ALBERTA, CCT, gas_day_start_utc  # noqa: F401 (re-export)

AB = ALBERTA
E3M3_TO_MMCF = 0.0353146667   # 1 10^3 m3 = 35.3146667 Mcf (matches montney.units)

TC_API = "https://f51561ras5.execute-api.us-west-2.amazonaws.com/production"
TC_SITE = "https://my.tccustomerexpress.com"


def local_to_utc(dt: datetime, tz: ZoneInfo = AB) -> datetime:
    """Naive wall time in `tz` (default Alberta) -> naive UTC."""
    return dt.replace(tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)


def html_text(s: str | None, limit: int = 4000) -> str:
    """Strip tags to readable plain text (keeps line breaks from <br>/<p>/<li>)."""
    if not s:
        return ""
    s = re.sub(r"(?s)<!--.*?-->", "", s)  # incl. Word's <!--[if gte mso 9]><xml>... metadata
    s = re.sub(r"(?is)<(script|style|xml|head|title)\b.*?</\1>", "", s)
    s = re.sub(r"(?is)<o:[a-z]+\b.*?</o:[a-z]+>", "", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d)>", "\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r"\s*\n\s*", "\n", s).strip()
    return s[:limit]


def parse_dt(s: str | None, fmts: tuple[str, ...] = ("%Y-%m-%d %H:%M:%S",)) -> datetime | None:
    if not s or str(s).strip() in ("", "None", "null"):
        return None
    s = str(s).strip()
    for f in fmts:
        for cand in (s, s.split(".")[0]):  # tolerate fractional seconds
            try:
                return datetime.strptime(cand, f)
            except ValueError:
                continue
    raise ValueError(f"unexpected date format {s!r}")


class FormatDrift(RuntimeError):
    """Raised when a provider payload no longer looks like what the connector was written for."""


def cache_load(name: str) -> dict:
    """<home>/cache/<name>.json, or {} (L-081: request cuts). Never the repo."""
    import json

    from montney import paths
    try:
        return json.loads((paths.home() / "cache" / f"{name}.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def cache_save(name: str, data: dict) -> None:
    """Atomic write of <home>/cache/<name>.json (temp file + replace). Callers save only after a run succeeded."""
    import json
    import os

    from montney import paths
    d = paths.home() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, d / f"{name}.json")


_ONCE: "weakref.WeakKeyDictionary" = None     # client -> {(url, params): (fetched at, response, run)}; built on use


def get_at_most_every(ctx, url: str, max_age: timedelta, *, params: dict | None = None, before=None, **kw):
    """`ctx.get(url)`, but at most once per `max_age`: a 200 is kept and handed back to later runs (PLAN 9 / L-107,
    Erik: "never re-download an unchanged file").

    For sources that run hourly to catch a daily input as soon as it lands (ngtl_split waits on the GDSR, gas_lng_bcer
    on Willow Valley), while their big upstream files change at most daily. The cache is in memory and keyed on the
    run's HTTP client: the runner shares one client across runs, so its runs share the cache; a test's fresh client
    starts empty, and the entries go with the client (weak keys). A restart re-downloads once. A cached answer isn't
    a request, so ctx.n_requests counts only real fetches. `before()` runs only ahead of a real fetch (a session page).
    A run that raises drops what it downloaded (`forget_on_error`), so a bad 200 isn't reused for `max_age`."""
    import weakref

    global _ONCE
    if _ONCE is None:
        _ONCE = weakref.WeakKeyDictionary()
    cache = _ONCE.setdefault(ctx.client, {})
    key = (url, tuple(sorted((params or {}).items())))
    hit = cache.get(key)
    if hit and timedelta(0) <= ctx.now - hit[0] < max_age:
        return hit[1]
    if before is not None:
        before()
    r = ctx.get(url, params=params, **kw)
    if r.status_code == 200:
        cache[key] = (ctx.now, r, id(ctx))
    return r


def forget_on_error(fetch):
    """Decorator for a connector's `fetch(self, ctx)`: if the run raises, what it downloaded through
    get_at_most_every is dropped, so the next run downloads again (Reviewer on #462: a 200 that fails to parse, such
    as an APEX error page or a half-replaced workbook, would otherwise fail every run for `max_age`)."""
    import functools

    @functools.wraps(fetch)
    def run(self, ctx):
        try:
            return fetch(self, ctx)
        except BaseException:
            cache = _ONCE.get(ctx.client) if _ONCE is not None else None
            for key in [k for k, v in (cache or {}).items() if v[2] == id(ctx)]:
                del cache[key]
            raise
    return run


def drop_cookies(ctx, host: str) -> int:
    """Forget every cookie for `host` (and its subdomains) in the run's shared client; returns how many went.

    BCER's APEX report keeps an interactive-report filter in the server session, and the runner shares one client (and
    its cookies) across sources, so gas_lng_bcer's facility filter leaked into gas_bcer_production (11-04 17:51: a
    69 KB CSV, "no month with gas plants"; Reviewer on #394). Each BCER read drops the cookies first: a fresh session,
    no filter but its own. Clearing cookies isn't a request, so it doesn't bypass ctx.get's accounting."""
    jar = ctx.client.cookies.jar
    gone = [c for c in jar if c.domain.lstrip(".") == host or c.domain.lstrip(".").endswith("." + host)]
    for c in gone:
        jar.clear(c.domain, c.path, c.name)
    return len(gone)


def read_stored(ids: list[str], start: datetime, end: datetime) -> dict[str, dict[datetime, float]]:
    """Latest-vintage stored points of `ids` with start <= ts <= end, as {series: {ts: value}}. **Read-only in both modes.**

    For the connectors that build an estimate from their own (or another source's) stored history, e.g. the 30-day LNG
    feed. `Context` has no store, so the orchestrator ruled (#162) that they read it here:
    - Postgres (MONTNEY_DB=postgres): `open_store("reader")`, the read-only `montney_reader` role.
    - DuckDB: the data dir's file, opened with `read_only=True`. Never `open_store()`, whose DuckDB Store is a
      read-write connection that runs DDL. No file gives {}.

    A failure (no server, or DuckDB's file held with a different configuration in this process) raises. Callers turn
    that into a NO-RESULT, never a guess.
    """
    import os

    epoch = datetime(1970, 1, 1)
    if os.environ.get("MONTNEY_DB", "duckdb").lower() == "postgres":
        from montney.store import open_store

        store = open_store("reader")
        try:
            data = store.series(list(ids), start=start, end=end)
        finally:
            store.close()
        return {sid: {epoch + timedelta(seconds=t): v for t, v in zip(s.get("ts", []), s.get("v", [])) if v is not None}
                for sid, s in data.items()}
    import duckdb

    from montney import paths

    path = paths.db_path()
    if not path.exists():
        return {}
    con = duckdb.connect(str(path), read_only=True)
    try:
        rows = con.execute(
            "SELECT series_id, ts, arg_max(value, as_of) FROM observations "
            "WHERE series_id IN (SELECT unnest(?)) AND ts >= ? AND ts <= ? GROUP BY series_id, ts",
            [list(ids), start, end]).fetchall()
    finally:
        con.close()
    out: dict[str, dict[datetime, float]] = {sid: {} for sid in ids}
    for sid, ts, v in rows:
        if v is not None:
            out[sid][ts] = v
    return out
