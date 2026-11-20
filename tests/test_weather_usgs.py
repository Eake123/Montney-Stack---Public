"""Columbia hydro (L-085): the parts that could put a wrong number on the map: cfs -> m3/s and the -999999 sentinel,
the storage aggregate (% full, % of normal, blank Hydromet cells) and the NWRFC column order (50 % vs 10/90 %)."""

from datetime import date, datetime, timedelta

from montney.connectors import weather_columbia_hydro as ch, weather_usgs as us


def series(site, values, nodata=-999999.0):
    return {"sourceInfo": {"siteCode": [{"value": site}]}, "variable": {"noDataValue": nodata},
            "values": [{"value": [{"dateTime": t, "value": str(v), "qualifiers": ["P"]} for t, v in values]}]}


def test_parse_converts_cfs_drops_sentinel_and_uses_the_offset():
    j = {"value": {"timeSeries": [series("14105700", [("2026-11-03T10:15:00.000-08:00", 113000),
                                                      ("2026-11-03T10:30:00.000-08:00", -999999)])]}}
    assert us.parse(j) == {"14105700": {datetime(2026, 11, 3, 18, 15): 3199.798}}      # 113,000 cfs, UTC


def test_storage_aggregate_pct_full_and_normal():
    """Three projects, 12 years of the same seasonal value; today they hold half of normal. Grand Coulee's blank cell
    on the last day moves the aggregate to the latest COMMON date."""
    rows = ["DateTime,gcl_af,hgh_af,dwr_af"]
    d = date(2014, 1, 1)
    while d <= date(2026, 11, 3):
        g = "" if d == date(2026, 11, 3) else ("2000000" if d >= date(2026, 10, 1) else "4000000")
        h, w = ("1000000", "500000") if d >= date(2026, 10, 1) else ("2000000", "1000000")
        rows.append(f"{d:%Y-%m-%d},{g},{h},{w}")
        d += timedelta(days=1)
    s = ch.storage_summary(ch.parse_hydromet("\n".join(rows)), date(2026, 11, 3))
    assert s["date"] == date(2026, 11, 2)
    assert s["kaf"] == 3500.0 and s["pct_normal"] == 50.0 and s["pct_full"] == 50.0   # 3.5 of 7.0 MAF


def test_nwrfc_reads_the_newest_jan_jul_row_in_the_right_columns():
    text = ("ID,Ensemble Date,Issuance Date,Start Month,End Month,Min FCST,90% FCST,75% FCST,50% FCST,25% FCST,10% FCST,"
            "Max FCST\nTDAO3,2026-11-03,2026-11-03,JAN,JUL,53727.1,66057,73980.6,83121.9,92126.8,111287,136537\n"
            "TDAO3,2026-11-02,2026-11-02,JAN,JUL,1,2,3,4,5,6,7\n")
    w = ch.parse_nwrfc(text)
    assert w == {"issued": date(2026, 11, 3), "p90": 66057.0, "p50": 83121.9, "p10": 111287.0}
    assert round(100 * w["p50"] / ch.JAN_JUL_AVG_KAF, 1) == 80.1
