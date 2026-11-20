-- 0018_regions_upne_nyc_bakken_sunbelt_tx: the five new budget regions of PLAN step 8 (L-106, docs/REGIONS.md §1/§6),
-- same pattern as pnw (0014), cal (0015), ont/qc (0016), rockies/midwest (0017). A source joins with `region: <code>`
-- in its spec (runner.seed_catalog -> PostgresStore.set_source_region).
--   upne:    Upper NE / New England (ISO-NE: ME, NH, VT, MA, RI, CT)
--   nyc:     New York (NYISO)
--   bakken:  MT + ND
--   sunbelt: NV + AZ + NM
--   tx:      Texas
-- Storage budgets: 2 GB each, like pnw/cal, carved out of `rest` (83 -> 73 GB), so the L-003 total stays 100 GB.
-- Requests: sources/BUDGET.yaml (provisional floors until a measured day).
UPDATE montney.region SET budget_bytes = 73000000000 WHERE code = 'rest';
INSERT INTO montney.region (id, code, name, budget_bytes) VALUES
    (9,  'upne',    'Upper NE / New England (ISO-NE)', 2000000000),
    (10, 'nyc',     'New York (NYISO)', 2000000000),
    (11, 'bakken',  'Bakken (MT, ND)', 2000000000),
    (12, 'sunbelt', 'Upper Sunbelt (NV, AZ, NM)', 2000000000),
    (13, 'tx',      'Texas', 2000000000);
