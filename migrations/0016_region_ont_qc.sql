-- 0016_region_on_qc: Ontario and Quebec (L-091). The Ontario code is `ont`, not `on`: YAML 1.1 (PyYAML) reads a bare `on` as
-- boolean true, so `region: on` in a spec would silently become True.
-- A source joins with `region: ont` / `region: qc` (runner.seed_catalog, as for pnw 0014 and cal 0015).
-- Storage budgets: 2 GB each, carved out of `rest` (91 -> 87 GB), so the L-003 total stays 100 GB.
-- Requests: sources/BUDGET.yaml `ont` / `qc`.
-- Manitoba / Saskatchewan get no region of their own: their sources (tc_mainline's SK/MB demand, power_statcan_gen,
-- weather_gfs_east) have always counted in ab_nebc, so a new Manitoba source (e.g. Manitoba Hydro reservoirs) does too.
UPDATE montney.region SET budget_bytes = 87000000000 WHERE code = 'rest';
INSERT INTO montney.region (id, code, name, budget_bytes) VALUES
    (5, 'ont', 'Ontario', 2000000000),
    (6, 'qc', 'Quebec', 2000000000);
