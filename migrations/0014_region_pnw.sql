-- 0014_region_pnw: the Pacific Northwest region (L-085: Gas's gas_nwp / gas_gtn, Weather's Columbia sources).
-- A source joins it with `region: pnw` in its spec (runner.seed_catalog -> PostgresStore.set_source_region).
-- Storage budget: 2 GB, carved out of `rest` (95 -> 93 GB), so the L-003 total stays 100 GB. Expected use is far below
-- that: ~5 MB/day downloaded (Gas), stored change-only. Requests: sources/BUDGET.yaml `pnw`.
UPDATE montney.region SET budget_bytes = 93000000000 WHERE code = 'rest';
INSERT INTO montney.region (id, code, name, budget_bytes) VALUES
    (3, 'pnw', 'Pacific Northwest (WA, OR, ID; the Columbia)', 2000000000);
