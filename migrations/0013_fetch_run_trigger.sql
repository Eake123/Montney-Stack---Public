-- 0013_fetch_run_trigger: what started each run (L-081). The request budget (montney/db/reqbudget.py) counts
-- schedule + refresh against sources/BUDGET.yaml and reports manual fetches and importers separately, so hand-runs
-- and history back-fills don't distort the caps. NULL for runs logged before this migration.
ALTER TABLE montney.fetch_run
    ADD COLUMN trigger text CHECK (trigger IN ('schedule', 'refresh', 'manual', 'import'));
