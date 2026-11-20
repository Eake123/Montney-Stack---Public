-- 0009_budget_guard: the 100 GB TOTAL guard keeps its OWN state (reviewer, #85). Before, its hysteresis took
-- "any source blocked" as its previous state, so one region-blocked source plus a total at 95-100 % blocked
-- every region. `du.apply_states` writes the total's state here; `du.compute` reads it back as the previous
-- state for the 95 % unblock rule.
CREATE TABLE montney.budget_guard (
    scope   text      PRIMARY KEY CHECK (scope IN ('total')),
    state   text      NOT NULL CHECK (state IN ('ok', 'warn', 'blocked')),
    pct     double precision,
    updated timestamp NOT NULL
);
