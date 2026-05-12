-- v0.4.1 — drop legacy tushare_cache_blob entries.
--
-- Pre-v0.4.1 payloads were stored as a bare JSON array, then read back via
-- pd.read_json which triggered a pandas FutureWarning on string columns whose
-- names matched its date heuristic (trade_date, cal_date, ...). The cache
-- read/write path now wraps payloads as {version:1, schema:{...}, data:[...]}
-- and restores dtypes explicitly. Old rows are incompatible with that reader,
-- so wipe the table; TushareClient._ensure_cache_table re-creates it lazily on
-- the next write.
DROP TABLE IF EXISTS tushare_cache_blob;
