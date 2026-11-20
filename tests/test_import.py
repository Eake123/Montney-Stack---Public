"""Importer tests use synthetic numbers only - real TC exports never go in the repo."""

from datetime import datetime

import pytest

from montney.importers import ngtl_csv

BLOCKS = """Item,Prorated,Extrapolated,NextDayNominated
EMPRESS BORDER,-4000,-4100,-4050
ALBERTA-B.C. BDR,-2000,-2100,-2200
**Total Net Storage,150,200,250
Total NGTL Receipts,25000,15000,25500
***NGTL Field Receipts,,14000,
End of Day Linepack,,19700,
Linepack Change (Last 24 hours),,-180,
Tolerance,,0/2 (%),
Total SD Account (TJ),,330.7,
Gas Day,2026-Oct-29,2026-Oct-29,2026-Oct-30
Item,Prorated,Extrapolated,NextDayNominated
EMPRESS BORDER,-4001,-4101,-4051
Total NGTL Receipts,25001,15001,25501
Gas Day,2026-Oct-30,2026-Oct-30,2026-Oct-31
"""


def obs_map(path, units=None):
    p = ngtl_csv.parse(path)
    u = units or ngtl_csv.guess_units(p, path)
    return p, u, {(o.series_id, o.ts): o.value for o in ngtl_csv.to_observations(p, u)}


def test_blocks_layout(tmp_path):
    f = tmp_path / "MultiDayGasDaySummaryReport.csv"
    f.write_text(BLOCKS)
    p, u, m = obs_map(f)
    assert p.layout == "blocks" and u == "MMcf"
    assert m[("ngtl.gdsr.empress.flow", datetime(2026, 10, 29))] == 4100
    assert m[("ngtl.gdsr.empress.prorated", datetime(2026, 10, 30))] == 4001
    assert m[("ngtl.gdsr.empress.nom", datetime(2026, 10, 31))] == 4051
    assert m[("ngtl.gdsr.alberta_bc.nom", datetime(2026, 10, 30))] == 2200
    assert m[("ngtl.gdsr.net_storage.flow", datetime(2026, 10, 29))] == 200  # sign kept
    assert m[("ngtl.gdsr.linepack_change", datetime(2026, 10, 29))] == -180
    assert m[("ngtl.gdsr.sd_account.flow", datetime(2026, 10, 29))] == pytest.approx(330.7)
    assert m[("ngtl.gdsr.eod_linepack.flow", datetime(2026, 10, 29))] == 19700


def test_long_layout_metric(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("Gas Day,Item,Prorated,Extrapolated,NextDayNominated\n"
                 "2026-10-29,Empress Border,-128000,-130000,-129000\n"
                 "2026-10-29,Total NGTL Receipts,700000,430000,720000\n")
    p, u, m = obs_map(f)
    assert p.layout == "long" and u == "e3m3"
    assert m[("ngtl.gdsr.empress.flow", datetime(2026, 10, 29))] == pytest.approx(4590.9, abs=0.1)
    assert ("ngtl.gdsr.empress.nom", datetime(2026, 10, 30)) in m


def test_wide_layout(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("Gas Day,Empress Border,McNeill Border,Total NGTL Receipts,Mystery Column\n"
                 "10/29/2026,\"-4,100\",-450,\"15,000\",7\n")
    p, u, m = obs_map(f)
    assert p.layout == "wide"
    assert m[("ngtl.gdsr.empress.flow", datetime(2026, 10, 29))] == 4100
    assert m[("ngtl.gdsr.mcneill.flow", datetime(2026, 10, 29))] == 450
    assert "Mystery Column" in p.unknown_items


def test_matrix_layout(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("Item,2026-10-29,2026-10-30\nEMPRESS BORDER,-4100,-4200\nTotal NGTL Receipts,15000,15100\n")
    p, u, m = obs_map(f)
    assert p.layout == "matrix"
    assert m[("ngtl.gdsr.empress.flow", datetime(2026, 10, 30))] == 4200


def test_item_mapping():
    k = ngtl_csv.item_key
    assert k("Total Storage + Intraprovincial") == "storage_plus_intra"
    assert k("Intraprovincial") == "intraprovincial"
    assert k("Total Storage Deliveries") == "storage_deliveries"
    assert k("*OTHER BORDERS") == "other_borders"
    assert k("WILLOW VALLEY INTERCONNECT") == "willow_valley"
    assert k("Tolerance") is None
