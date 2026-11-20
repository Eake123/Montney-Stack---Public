-- 0017_region_rockies_midwest: two US gas/power regions (L-094 / L-095), same pattern as pnw (0014), cal (0015),
-- ont/qc (0016). A source joins with `region: rockies` / `region: midwest` in its spec (runner.seed_catalog).
--   rockies: Kern River, NWP east of Stanfield, Opal / the Rockies hubs.
--   midwest: GLGT, Viking, ANR, Northern Border, Chicago, Wisconsin, Michigan, the Bakken, MISO.
-- Storage budgets: 2 GB each, carved out of `rest` (87 -> 83 GB), so the L-003 total stays 100 GB.
-- Requests: sources/BUDGET.yaml `rockies` / `midwest` (deliberately low: Erik, "really limit the requests").
UPDATE montney.region SET budget_bytes = 83000000000 WHERE code = 'rest';
INSERT INTO montney.region (id, code, name, budget_bytes) VALUES
    (7, 'rockies', 'Rockies (Kern River, NWP east of Stanfield, Opal)', 2000000000),
    (8, 'midwest', 'Midwest (GLGT, Viking, ANR, Northern Border, Chicago, WI, MI, Bakken, MISO)', 2000000000);
