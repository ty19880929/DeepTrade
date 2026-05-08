-- volume-anomaly v0.6.0 — explicit per-dimension scoring (PR-6).
--
-- LLM now emits a `dimension_scores` sub-object on every VATrendCandidate
-- (washout / pattern / capital / sector / historical / risk, each 0–100).
-- G6 decision: persist as 6 dedicated DOUBLE columns rather than a JSON blob,
-- so `stats --by dimension_scores` can aggregate via plain SQL.

ALTER TABLE va_stage_results ADD COLUMN dim_washout    DOUBLE;
ALTER TABLE va_stage_results ADD COLUMN dim_pattern    DOUBLE;
ALTER TABLE va_stage_results ADD COLUMN dim_capital    DOUBLE;
ALTER TABLE va_stage_results ADD COLUMN dim_sector     DOUBLE;
ALTER TABLE va_stage_results ADD COLUMN dim_historical DOUBLE;
ALTER TABLE va_stage_results ADD COLUMN dim_risk       DOUBLE;
