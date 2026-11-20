"""CER key-point throughput (step 5 PR B): the 10³m³/d → MMcf/d parse, summed pairs, and the missing-key-point guard."""

from __future__ import annotations

from datetime import datetime

import pytest

from montney.connectors import gas_cer_keypoints as ck
from montney.connectors.gas_common import FormatDrift

HDR = ("Date,Day,Month,Year,Company,Pipeline,Key Point,Latitude,Longitude,Direction Of Flow,Trade Type,"
       "Capacity (1000 m3/d),Throughput (1000 m3/d),Throughput (GJ/d)")


def row(date, kp, direction, cap, thr):
    return f"{date},1,6,2026,TransCanada,Mainline,{kp},49.0,-97.0,{direction},export,{cap},{thr},"


def test_mmcfd_and_summed_emerson():
    text = "\n".join([HDR, row("2026-06-30", "Emerson I", "south", 20100.0, 12297.0),
                      row("2026-06-30", "Emerson II", "south", 89684.0, 39010.0),
                      row("2026-06-30", "Emerson II", "north", 20180.0, 0.0),        # an import direction: not ours
                      row("2023-12-31", "Emerson I", "south", 1.0, 1.0)])             # before KEEP_FROM: dropped
    pts = {"mainline.emerson": ("Emerson", [("Emerson I", "south"), ("Emerson II", "south")])}
    out = ck.parse(text, pts)
    thr, cap = out["mainline.emerson"][datetime(2026, 6, 30)]
    assert thr == pytest.approx((12297.0 + 39010.0) * 0.0353147) and cap == pytest.approx((20100 + 89684) * 0.0353147)
    assert list(out["mainline.emerson"]) == [datetime(2026, 6, 30)]
    with pytest.raises(FormatDrift, match="missing"):
        ck.parse(text, {"x": ("x", [("Niagara", "east")])})


def test_quarter_avg_only_for_complete_quarters():
    from datetime import timedelta
    q2 = {datetime(2026, 4, 1) + timedelta(days=i): [100.0 + i % 2, 0.0] for i in range(91)}      # Apr-Jun, full
    q3 = {datetime(2026, 7, 1) + timedelta(days=i): [50.0, 0.0] for i in range(10)}               # 10 days: not shown
    out = ck.quarter_means({**q2, **q3})
    assert list(out) == [datetime(2026, 4, 1)] and out[datetime(2026, 4, 1)] == pytest.approx(100.0 + 45 / 91)
