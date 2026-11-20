"""Alberta supply cushion, estimated (L-106 8c wcan zoom-in; orchestrator: "AB supply cushion row: yes, build it").

ZERO requests: it reads AESO series already in the store (gas_common.read_stored, read-only), like region_balance.

    cushion_est = sum over dispatchable fuels of (CSD max capability - CSD net generation)
                  - today's realized outages (gas excl. mothballed + mothballed + hydro + storage + other)

* Dispatchable fuels: cogeneration, combined cycle, simple cycle, gas-fired steam, hydro, energy storage, other.
  **Wind and solar are left out**: their CSD capability is nameplate (wind 5,684 MW = the installed fleet), not what the
  weather allows.
* The CSD's capability isn't reduced for outages (gas-fired steam showed 3,078 MW of capability against 239 MW of
  output while 643 MW of it was out on 2026-11-07), so the day's realized outage MW (`aeso.outage.*.realized`, the
  last value AESO published for the Alberta day) are taken off. Without that the "cushion" would be ~4-5 GW too big.
* It is an estimate of headroom, **not AESO's official supply cushion** (AESO doesn't publish one in the CSD): it
  ignores start-up times (slow units count), contingency reserve held back, and intra-day outage changes.

Series: power.ab.supply_cushion_est (MW, freq 5min, at the CSD stamp), written each run for the newest CSD point.
A missing input (any capability, net, or the day's outage figures) means no point: never a guess.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import read_stored
from montney.store import Obs, SeriesMeta

DISPATCHABLE = ("cogeneration", "combined_cycle", "simple_cycle", "gas_fired_steam", "hydro", "energy_storage", "other")
OUTAGES = ("gas_total", "mbo", "hydro", "energy_storage", "biomass_and_other")
SID = "power.ab.supply_cushion_est"
AB_DAY = timedelta(hours=6)            # the Alberta day (UTC-6, as the AESO realized outages are keyed)
LOOKBACK = timedelta(days=3)


def latest(points: dict[datetime, float], at: datetime) -> tuple[datetime, float] | None:
    """The newest point at or before ``at`` (the store is change-only, so an unchanged value is still current)."""
    past = [t for t in points if t <= at]
    return (max(past), points[max(past)]) if past else None


def cushion(stored: dict[str, dict[datetime, float]], now: datetime) -> tuple[datetime, float] | None:
    csd_ids = [f"aeso.gen.{f}.{k}" for f in DISPATCHABLE for k in ("cap", "net")]
    got = {sid: latest(stored.get(sid, {}), now) for sid in csd_ids}
    if any(v is None for v in got.values()):
        return None
    at = max(v[0] for v in got.values())                          # the newest CSD stamp among the parts
    headroom = sum(got[f"aeso.gen.{f}.cap"][1] - got[f"aeso.gen.{f}.net"][1] for f in DISPATCHABLE)
    day = (at - AB_DAY).replace(hour=0, minute=0, second=0, microsecond=0)
    out = [stored.get(f"aeso.outage.{o}.realized", {}).get(day) for o in OUTAGES]
    if any(v is None for v in out):
        return None
    return at, round(headroom - sum(out), 1)


class AbCushion(Connector):
    stored = staticmethod(read_stored)                            # tests inject a fake

    def declared_series(self) -> list[SeriesMeta]:
        return [SeriesMeta(SID, self.id, "Alberta supply cushion, est. (dispatchable capability - net generation - "
                           "today's outages; wind/solar excluded; not AESO's official figure)", "MW", "5min",
                           "AESO / Supply & Demand", extra={"tier": "fast", "estimate": True})]

    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        ids = [f"aeso.gen.{f}.{k}" for f in DISPATCHABLE for k in ("cap", "net")]
        ids += [f"aeso.outage.{o}.realized" for o in OUTAGES]
        try:
            data = self.stored(ids, ctx.now - LOOKBACK, ctx.now + timedelta(days=1))
        except Exception as e:                                    # noqa: BLE001 - a store problem: NO-RESULT, no guess
            res.warnings.append(f"supply cushion: store not readable ({e})")
            return res
        c = cushion(data, ctx.now)
        if c is None:
            res.warnings.append("supply cushion: an input (CSD capability/net or today's realized outages) is missing")
            return res
        res.obs.append(Obs(SID, c[0], c[1], c[0]))
        return res
