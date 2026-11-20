"""L-106: every region (docs/REGIONS.md) has exactly one population-weighted weather composite, wx.region.<r>.*, whose
weights name locations of its own spec (a typo would silently drop a city from the weighting)."""

from montney.connectors import build, load_specs
from montney.connectors.base import slug

REGIONS = {"wcan", "mbnwo", "sont", "qc", "upne", "nyc", "umw", "pnw", "bakken", "mtnw", "cal", "sunbelt", "tx"}


def test_every_region_has_one_weather_composite():
    seen = {}
    for sid, spec in load_specs(include_disabled=True).items():
        p = spec.get("params") or {}
        if not sid.startswith("weather") or "locations" not in p:
            continue
        locs = {slug(l["name"]) for l in p["locations"]}
        declared = {m.series_id for m in build(spec).declared_series()}
        for c in p.get("composites", []):
            if not c["id"].startswith("region."):
                continue
            r = c["id"].split(".", 1)[1]
            assert r not in seen, f"{r} in both {seen[r]} and {sid}"
            seen[r] = sid
            assert set(c["weights"]) <= locs, (sid, r, set(c["weights"]) - locs)
            for k in ("hdd", "cdd", "hdd_normal", "cdd_normal", "hdd.realized", "cdd.realized", "hdd_1_7", "hdd_8_15",
                      "cdd_1_7", "cdd_8_15", "cdd_1_7_normal", "cdd_8_15_normal"):
                assert f"wx.region.{r}.{k}" in declared, (sid, r, k)
    assert set(seen) == REGIONS, set(seen) ^ REGIONS


def test_region_files_weather_series_are_declared_by_their_source():
    """regions/<r>.yaml `weather:` (Weather's section) names only series its `source` spec declares."""
    from montney import regions

    specs = load_specs(include_disabled=True)
    rs = regions.load()
    assert set(rs) == REGIONS
    for r, reg in rs.items():
        w = reg.weather
        assert w, f"{r}: no weather section"
        declared = {m.series_id for m in build(specs[w["source"]]).declared_series()}
        ids = list(w["cities"]) + [w["hdd"], w["cdd"]] + list(w["normals"]) + list(w["realized"]) + list(w["outlook"])
        assert w["hdd"] == f"wx.region.{r}.hdd" and w["cdd"] == f"wx.region.{r}.cdd"
        missing = [i for i in ids if i not in declared]
        assert not missing, (r, w["source"], missing)
