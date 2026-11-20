-- 0015_region_cal: California (L-090: Weather's weather_gfs_south / weather_cal; Gas's PG&E / SoCalGas and Power's
-- CAISO sources to follow). A source joins it with `region: cal` in its spec (runner.seed_catalog, as for pnw, 0014).
-- Storage budget: 2 GB, carved out of `rest` (93 -> 91 GB), so the L-003 total stays 100 GB. Requests:
-- sources/BUDGET.yaml `cal`. (Weather grid FILES in the data dir still count toward ab_nebc, as du has always done.)
UPDATE montney.region SET budget_bytes = 91000000000 WHERE code = 'rest';
INSERT INTO montney.region (id, code, name, budget_bytes) VALUES
    (4, 'cal', 'California', 2000000000);
