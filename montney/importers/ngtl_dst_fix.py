"""One-off repair: NGTL rows stored while the specs read TC times as fixed MST.

Before this fix, `ngtl_csr` read `lastUpdated` and `ngtl_gdsr` read `asofDay` as fixed UTC-7.
The feeds are Alberta wall-clock time, so every reading taken while Alberta was on UTC-6 (DST,
and permanently from 2026-11-01) was stored 1 h late in UTC. UTC-7 readings were already correct.

What this does, for rows written before `--before` (UTC):
  * ngtl.csr.*  : ts and as_of move 1 h earlier when the reading was in daylight time.
  * ngtl.gdsr.* : as_of moves 1 h earlier when it was in daylight time. Rows from the CSV
                  importer (as_of = gas day + 1 at 00:00) are left alone.
A corrected row never overwrites a row that already exists at the target key (the fixed
connector has re-fetched the last 7 days correctly); the stale row is just dropped.

Pick --before = the time you upgraded. Rows written by the fixed code have a later fetched_at
and are not touched. Run with --dry-run first.

    uv run python -m montney.importers.ngtl_dst_fix --before "2026-11-01 00:00" --dry-run
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta

from zoneinfo import ZoneInfo

from montney.store import Store, _utcnow

MST = timedelta(hours=7)


def daylight_shift(local_naive: datetime, tz: str = "America/Edmonton") -> timedelta:
    """Correction for a reading stored as fixed MST: -1 h whenever Alberta was on UTC-6
    (DST, and permanently from 2026-11-01), else 0."""
    off = local_naive.replace(tzinfo=ZoneInfo(tz)).utcoffset()
    return timedelta(hours=-7) - off if off is not None else timedelta(0)


def plan(store: Store, before: datetime) -> list[tuple]:
    """Rows to move: (series_id, old_ts, old_as_of, new_ts, new_as_of, value)."""
    rows = store.con.execute(
        """SELECT series_id, ts, as_of, value FROM observations
           WHERE fetched_at < ? AND (series_id LIKE 'ngtl.csr.%' OR series_id LIKE 'ngtl.gdsr.%')""",
        [before],
    ).fetchall()
    out = []
    for sid, ts, as_of, value in rows:
        if sid.startswith("ngtl.csr."):
            shift = daylight_shift(ts - MST)
            if shift:
                out.append((sid, ts, as_of, ts + shift, as_of + shift if as_of == ts else as_of, value))
        else:
            if as_of == ts + timedelta(days=1):  # CSV importer convention, not a TC timestamp
                continue
            shift = daylight_shift(as_of - MST)
            if shift:
                out.append((sid, ts, as_of, ts, as_of + shift, value))
    return out


def apply(store: Store, moves: list[tuple]) -> tuple[int, int]:
    """Returns (moved, dropped_because_target_exists).

    Every planned row is deleted first and only then re-inserted at its corrected key. CSR
    readings exactly 1 h apart often share the same seconds (13:29:44 / 14:29:44), so one stale
    row's target is frequently another stale row's current key; moving rows one at a time would
    mistake that for "already re-fetched" and lose data. After the bulk delete, a row still at
    the target can only be one the fixed connector wrote (fetched_at >= --before), so it wins.
    """
    moved = dropped = 0
    now = _utcnow().replace(microsecond=0)
    with store.lock:
        con = store.con
        con.execute("BEGIN")
        try:
            con.executemany("DELETE FROM observations WHERE series_id=? AND ts=? AND as_of=?",
                            [[sid, ts, as_of] for sid, ts, as_of, *_ in moves])
            for sid, _ts, _as_of, nts, nas, value in moves:
                exists = con.execute(
                    "SELECT 1 FROM observations WHERE series_id=? AND ts=? AND as_of=?",
                    [sid, nts, nas]).fetchone()
                if exists:
                    dropped += 1
                    continue
                # fetched_at = now, so a second run never shifts this row again.
                con.execute("INSERT INTO observations VALUES (?, ?, ?, ?, ?)",
                            [sid, nts, value, nas, now])
                moved += 1
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    return moved, dropped


def main(argv: list[str] | None = None) -> int:
    from montney import paths

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--before", required=True,
                    help="UTC time you upgraded, e.g. '2026-11-01 00:00'. Rows fetched later are kept.")
    ap.add_argument("--db", help="DuckDB path (default: your Montney home)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    before = datetime.fromisoformat(a.before)
    store = Store(a.db or paths.db_path())
    moves = plan(store, before)
    csr = sum(1 for m in moves if m[0].startswith("ngtl.csr."))
    print(f"{len(moves)} rows to correct ({csr} CSR, {len(moves) - csr} GDSR as_of)")
    if a.dry_run or not moves:
        return 0
    moved, dropped = apply(store, moves)
    print(f"moved {moved}, dropped {dropped} already re-fetched correctly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
