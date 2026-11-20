"""Gas network files: schema, citations, and that every id/series they and the gas connectors
reference actually exists."""

import json

from montney import network
from montney.connectors import build, load_specs
from montney.connectors import gas_petrinex, gas_westcoast, ngtl_bulletins, ngtl_dop, ngtl_station_util

GAS = network.NETWORK_DIR / "gas"


def gas_features():
    feats = []
    for f in sorted(GAS.glob("*.geojson")):
        feats += json.loads(f.read_text(encoding="utf-8"))["features"]
    return feats


def test_gas_network_validates():
    fc = network.load()
    assert fc["errors"] == []
    ids = [f["properties"]["id"] for f in gas_features()]
    assert len(ids) == len(set(ids)) and all(i.startswith("gas.") for i in ids)


def test_every_feature_cites_a_source_and_links_are_schematic():
    for f in gas_features():
        p = f["properties"]
        assert len(p["source"]) > 20, p["id"]
        if p["kind"] == "link":
            assert p["schematic"] is True and p["source"].startswith("schematic:")
        if "callout" in p:
            assert p["callout"]["priority"] in (1, 2, 3) and p["callout"]["label"]


def test_series_refs_are_declared_by_a_source():
    declared = set()
    for spec in load_specs().values():
        declared |= {m.series_id for m in build(spec).declared_series()}
    for f in gas_features():
        p = f["properties"]
        refs = [p[k] for k in ("value", "capacity", "target") if k in p] + (p.get("panel") or {}).get("series", [])
        # station series exist only once fetched; their ids follow the connector's declared pattern
        missing = [s for s in refs if s not in declared and not ngtl_station_util.SERIES_PATTERN.match(s)
                   and not gas_petrinex.SERIES_PATTERN.match(s)]
        assert not missing, (p["id"], missing)


def test_event_refs_used_by_gas_connectors_exist():
    ids = {f["properties"]["id"] for f in gas_features()}
    used = set(ngtl_dop.AREA_REFS.values())
    used |= {r for refs in ngtl_bulletins.PROVIDERS.values() for r in refs}
    used |= set(ngtl_bulletins.AREA_PATTERNS)
    used |= {"gas.station2", "gas.huntingdon", "gas.westcoast.t_north", "gas.westcoast.t_south"}
    assert used <= ids, sorted(used - ids)
    # and the Westcoast refs really are what the connector emits
    assert set(gas_westcoast.notice_refs("Station 2 South linepack Aitken Creek", "")) <= ids


def test_required_v1_nodes_present():
    ids = {f["properties"]["id"] for f in gas_features()}
    assert {"gas.empress", "gas.mcneill", "gas.alberta_bc", "gas.willow_valley", "gas.gordondale",
            "gas.groundbirch_east", "gas.station2", "gas.huntingdon", "gas.kitimat", "gas.ngtl.storage",
            "gas.montney_receipts", "gas.ngtl.linepack", "gas.ngtl.usjr", "gas.ngtl.wgat", "gas.ngtl.egat", "gas.ngtl.osda",
            "gas.westcoast.t_north", "gas.westcoast.t_south", "gas.alliance", "gas.cgl",
            "gas.foothills.bc"} <= ids
