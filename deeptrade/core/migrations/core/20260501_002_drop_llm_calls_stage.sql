-- v0.7 — drop llm_calls.stage column.
--
-- Stage 概念已彻底归插件维护：``LLMClient.complete_json`` 不再接收 stage 入
-- 参，框架因此也不再写入这一列。历史 run 的 stage 信息仍可在
-- ``~/.deeptrade/reports/<run_id>/llm_calls.jsonl`` 中按需查阅（旧文件不动；
-- v0.7 起新写入的 jsonl 行也不再含 ``stage`` 键）。
--
-- DuckDB 1.0+ 支持 ALTER TABLE ... DROP COLUMN，且为 IF EXISTS 安全。

ALTER TABLE llm_calls DROP COLUMN IF EXISTS stage;
