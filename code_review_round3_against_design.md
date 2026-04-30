# DeepTrade 第三轮代码评审报告

评审对象：`E:\personal\DeepTrade`  
设计基准：`DESIGN.md` v0.3.1  
评审日期：2026-04-28  
说明：本目录不是 Git 仓库，无法基于 diff 精确识别本轮改动；本报告采用“当前源码 + DESIGN.md + 上一轮报告”的方式逐项复核。

## 1. 总体结论

第三轮代码已经明显推进：上一轮指出的多个 Critical / High 项已经部分或全部闭环，包括：

- `strategy run` 已支持不传 `plugin_id` 时交互选择，并在创建 run 之前调用 `validate()` / `configure()`。
- 插件加载已注入 `metadata`。
- `plugin install` 的 entrypoint / `validate_static()` 失败已经改为阻断安装并回滚。
- 打板策略已开始采集 `daily`、`daily_basic`、`moneyflow`，`stock_st` 未授权也不再软跳过。
- Tushare 5xx fallback 已接入 `_fetch_and_store()`。
- LLM transport 异常已被 R1/R2/final_ranking 捕获为 `validation.failed`，runner 可落到 `partial_failed`。
- `lub_stage_results` 已开始写入 R1/R2/final_ranking 结构化结果。
- CLI 已增加 `data sync` 和裸命令交互主菜单。
- `SecretStore` 在 keyring 不可用时不再 assert 崩溃。

自动化验证结果良好：

```text
uv run pytest         -> 194 passed
uv run ruff check .   -> All checks passed
uv run mypy deeptrade -> Success: no issues found in 40 source files
```

但从“是否可以按 DESIGN.md 进入真实 Tushare + DeepSeek 长周期运行”的角度看，仍有几处高风险问题。最需要优先处理的是：Tushare 参数化缓存可能返回错误历史窗口、`--force-sync` 暴露但未真正作用到策略数据层、内置策略 `validate()`/`configure()` 仍是空实现、LLM 候选集合不一致没有按设计重试且 final_ranking 缺少集合校验。

## 2. 关键发现

### Critical 1：参数化 Tushare 调用共用 `*` cache key，可能返回错误历史窗口

位置：

- [deeptrade/core/tushare_client.py](E:/personal/DeepTrade/deeptrade/core/tushare_client.py:390)
- [deeptrade/core/tushare_client.py](E:/personal/DeepTrade/deeptrade/core/tushare_client.py:420)
- [deeptrade/core/tushare_client.py](E:/personal/DeepTrade/deeptrade/core/tushare_client.py:446)
- [deeptrade/core/tushare_client.py](E:/personal/DeepTrade/deeptrade/core/tushare_client.py:677)
- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/data.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/data.py:486)

设计要求：

- `daily`、`daily_basic`、`moneyflow` 属于按交易日或时间窗口同步的数据，必须按目标日期/窗口正确命中缓存。
- 本地缓存/fallback 不能把其他日期或其他参数范围的数据当作当前输入。

实际实现：

`TushareClient.call()` 只有传入 `trade_date` 时才使用该日期作为 `cache_key_date`，否则统一使用 `"*"`：

```python
cache_key_date = trade_date if trade_date is not None else "*"
```

而 `_fetch_history_window()` 调用历史数据时只传：

```python
tushare.call(api_name, params={"start_date": start_date, "end_date": end_date})
```

这意味着 `daily` / `daily_basic` / `moneyflow` 的不同 `start_date/end_date` 窗口都会落到同一个 `(api_name, "*")` 状态。更严重的是，`_payload_exists()` 和 `_read_cached()` 在精确 `params_hash` 未命中时会退化为“同 api + 同 date 任意 payload”：

```python
# Fallback: any row with same (api, date) is acceptable
```

因此一次历史窗口缓存成功后，下一次同 API 但不同窗口的调用，可能被判断为 cache hit，并读出旧窗口的数据。

影响：

- R1/R2 的 `prev_daily`、`prev_moneyflow` 可能来自上一次运行的日期窗口。
- 这属于静默数据污染，测试很难发现，但会直接影响 LLM 分析结论。
- `force_sync=False` 的正常路径尤其容易触发。

建议：

1. 对 `params` 中带 `trade_date`、`start_date/end_date`、`ts_code` 等业务维度的调用，不允许使用参数无关 fallback。
2. 将 cache key 扩展为 `(api_name, trade_date_or_range_key, params_hash)`，例如历史窗口使用 `start_date:end_date` 或直接只以 `params_hash` 判断 freshness。
3. `_payload_exists()` 的“任意 payload fallback”仅白名单给真正参数无关的 API，如无参数 `stock_basic`。
4. 增加测试：先缓存 `daily(start=20260401,end=20260410)`，再请求 `daily(start=20260420,end=20260427)`，断言必须触发 transport，而不是返回旧窗口。

### Critical 2：`--force-sync` 参数没有穿透到打板策略的数据调用

位置：

- [deeptrade/cli_strategy.py](E:/personal/DeepTrade/deeptrade/cli_strategy.py:53)
- [deeptrade/cli_strategy.py](E:/personal/DeepTrade/deeptrade/cli_strategy.py:116)
- [deeptrade/cli_data.py](E:/personal/DeepTrade/deeptrade/cli_data.py:44)
- [deeptrade/cli_data.py](E:/personal/DeepTrade/deeptrade/cli_data.py:77)
- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/strategy.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/strategy.py:107)
- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/strategy.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/strategy.py:158)
- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/data.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/data.py:314)
- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/data.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/data.py:486)

设计要求：

- `StrategyParams.force_sync` 表示强制重新同步，忽略本地缓存。
- `data sync` 和 `strategy run --force-sync` 都应触发数据层强刷。

实际实现：

- CLI 正确接收 `--force-sync` 并写入 `StrategyParams`。
- `TushareClient.call()` 也支持 `force_sync`。
- 但 `LimitUpBoardStrategy.run()` / `sync_data()` 调用 `collect_round1()` 时没有传入 `params.force_sync`。
- `collect_round1()` 没有 `force_sync` 参数，内部所有 `tushare.call()` 都没有传 `force_sync=True`。

影响：

- 用户执行 `deeptrade strategy run --force-sync` 或 `deeptrade data sync --force-sync` 时，实际仍会命中缓存。
- 结合 Critical 1，可能导致用户以为已经强刷，但 LLM 仍基于旧窗口或旧交易日数据分析。

建议：

1. 为 `collect_round1(..., force_sync: bool = False)` 增参。
2. 所有 required 接口和 optional 接口调用都显式传递 `force_sync=force_sync`。
3. `_fetch_history_window()` 也应接受并传递 `force_sync`。
4. 增加端到端测试：先写缓存，再以 `StrategyParams(force_sync=True)` 执行 `sync_data()`，断言 transport 被重新调用。

### Critical 3：内置打板策略的 `validate()` / `configure()` 仍为空，设计中的运行前自检和策略参数问卷没有真正落地

位置：

- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/strategy.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/strategy.py:73)
- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/strategy.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/strategy.py:77)
- [DESIGN.md](E:/personal/DeepTrade/DESIGN.md:580)
- [DESIGN.md](E:/personal/DeepTrade/DESIGN.md:1050)

设计要求：

- `validate(ctx)` 是 run 前联网自检，检查 Tushare / DeepSeek 配置和必需接口可用性。
- `configure(ctx)` 收集策略参数，如 `allow_intraday`、`force_sync`、`daily_lookback`、`moneyflow_lookback`、R1/R2 token budget。
- `strategy run` 应在创建 run 记录之前完成这些前置检查和收参。

实际实现：

框架层已经调用 hook，但内置策略实现是：

```python
def validate(self, ctx) -> None:
    pass

def configure(self, ctx) -> dict:
    return {}
```

影响：

- Tushare token / DeepSeek key 缺失仍会在 runner 创建 `strategy_runs` 后、进入 `plugin.run()` 时失败。
- 必需接口权限没有在前置阶段集中暴露。
- `daily_lookback` / `moneyflow_lookback` / R1/R2 budget 等设计参数无法交互调整。
- 上一轮的“`strategy run` 调用了 hook”已经修复，但“hook 真正完成设计职责”还没有闭环。

建议：

1. `validate()` 至少检查：
   - `tushare.token`、`deepseek.api_key` 存在；
   - Tushare `stock_basic` 或轻量 API 可访问；
   - DeepSeek JSON echo 可用；
   - metadata.required 中的 API 权限可被验证，或在不可低成本验证时给出明确降级说明。
2. `configure()` 在 TTY 下用 questionary 收集策略参数；非 TTY 下只返回默认值。
3. 将 `daily_lookback`、`moneyflow_lookback`、`r1_batch_token_budget`、`r2_batch_token_budget` 合并到 `StrategyParams.extra` 并传给数据层/管线。

### High 1：LLM candidate_id 集合不一致没有按设计重试，final_ranking 也没有集合一致性校验

位置：

- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/pipeline.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/pipeline.py:216)
- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/pipeline.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/pipeline.py:351)
- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/pipeline.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/pipeline.py:448)
- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/pipeline.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/pipeline.py:488)
- [DESIGN.md](E:/personal/DeepTrade/DESIGN.md:1132)
- [DESIGN.md](E:/personal/DeepTrade/DESIGN.md:1268)
- [DESIGN.md](E:/personal/DeepTrade/DESIGN.md:1401)
- [DESIGN.md](E:/personal/DeepTrade/DESIGN.md:1496)

设计要求：

- R1/R2 输出 candidate_id 集合必须与输入集合严格一致。
- 不一致时该批温度 0 重试一次；仍失败才标记 batch failed。
- final_ranking 需要对所有 finalists 输出连续全量排名。

实际实现：

- R1/R2 在集合不一致时直接 `VALIDATION_FAILED`，没有重试。
- final_ranking 只校验 `final_rank` 是否连续，不校验输出 `candidate_id` 集合是否等于输入 finalists。
- LLM 可以遗漏某个 finalist 或新增一个 candidate_id，只要 ranks 连续，Pydantic 就会通过。

影响：

- 长上下文遗漏候选时无法通过重试自恢复。
- final_ranking 可能静默丢失强候选，最终报告仍显示成功。
- 这会削弱“严禁丢弃候选、严禁限制候选数”的核心约束。

建议：

1. 对集合不一致增加一次 repair retry；至少在 user prompt 中追加错误详情和原始 candidate_id 列表。
2. final_ranking 增加 `candidate_id_set_equal(finalists, obj.finalists)` 校验。
3. final_ranking 集合不一致也应产生 `VALIDATION_FAILED`，报告进入 `partial_failed`。
4. 增加测试覆盖 R1、R2、final_ranking 三类集合不一致。

### High 2：`limit_list_d` 当日空结果总是被当作“合法无候选”，没有实现设计中的“数据尚未入库”失败分支

位置：

- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/data.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/data.py:324)
- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/strategy.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/strategy.py:177)
- [DESIGN.md](E:/personal/DeepTrade/DESIGN.md:1047)

设计要求：

- 极端行情下 0 候选是合法结果。
- 但如果 `today.time() >= close_after` 且 `limit_list_d(today)` 返回空且非 unauthorized，应视为 Tushare 数据可能尚未入库，run.status = `failed`，提示稍后重试或显式指定前一交易日。

实际实现：

`collect_round1()` 只要 `limit_list_d.empty` 就返回空 bundle：

```python
if limit_list_d.empty:
    bundle.candidates = []
    return bundle
```

`strategy.run()` 随后写空报告并返回 success。

影响：

- 当日数据未发布、接口暂时返回空、参数错误导致空表时，用户会得到一个“成功但无候选”的报告。
- 真实无涨停与数据未就绪被混淆。

建议：

1. Step 0 记录 `T` 是否为“今天且已过 close_after”。
2. Step 1 在当日收盘后空 `limit_list_d` 时抛出明确的数据未就绪错误。
3. 对用户显式 `--trade-date` 的历史日期，可继续允许 0 候选作为合法空结果。

### High 3：可选 Tushare 接口只处理 unauthorized，5xx/超时且无可用 fallback 时会拖垮整个 run

位置：

- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/data.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/data.py:347)
- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/data.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/data.py:361)
- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/data.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/data.py:368)
- [deeptrade/core/tushare_client.py](E:/personal/DeepTrade/deeptrade/core/tushare_client.py:490)
- [DESIGN.md](E:/personal/DeepTrade/DESIGN.md:48)
- [DESIGN.md](E:/personal/DeepTrade/DESIGN.md:1526)

设计要求：

- required 接口失败应终止 run。
- optional 接口缺失应进入 `data_unavailable` 并降级。
- Tushare 5xx 重试耗尽后可 fallback；不可 fallback 时由调用层按 required/optional 决定终止或降级。

实际实现：

optional 接口只捕获 `TushareUnauthorizedError`。如果 `suspend_d`、`limit_list_ths`、`limit_cpt_list` 发生 `TushareServerError` 且无 fallback，异常会直接抛出并使 run failed。

影响：

- 可选增强接口的短暂 5xx 会使整个打板策略失败。
- 这与 optional/fallback 的设计定位不一致。

建议：

1. optional 调用统一封装，例如 `try_optional(api_name, ...)`。
2. 捕获 `TushareUnauthorizedError`、`TushareServerError`、可判定的 timeout/rate-limit exhaustion。
3. 对 optional 失败写 `data_unavailable`，并把失败原因写入事件或 bundle。

### Medium 1：Pydantic schema 未强制 EvidenceItem.unit，R2 rank 也未强制 1..N 连续

位置：

- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/schemas.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/schemas.py:32)
- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/schemas.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/schemas.py:77)
- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/schemas.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/schemas.py:99)
- [DESIGN.md](E:/personal/DeepTrade/DESIGN.md:831)
- [DESIGN.md](E:/personal/DeepTrade/DESIGN.md:936)

设计要求：

- `EvidenceItem` 必须带 `unit`，证据要自包含数值含义。
- R2 `rank` 在本批内唯一且 `1..N` 连续。

实际实现：

- `EvidenceItem.unit: str | None = None`，缺失 unit 可以通过校验。
- `ContinuationResponse.ranks_must_be_unique()` 只检查唯一性，不检查是否是 `1..N`。

影响：

- LLM 返回 `unit=null` 或遗漏单位时不会被拒绝。
- R2 返回 rank `[10, 20, 30]` 这类非连续排名也会通过。

建议：

1. 将 `unit` 改为 `str = Field(..., min_length=1)`。
2. R2 rank validator 改为：
   `sorted(ranks) == list(range(1, len(ranks) + 1))`。
3. 增加缺失 unit、rank 非连续的负向测试。

### Medium 2：`deepseek.audit_full_payload` 配置没有传入 DeepSeekClient，且 `llm_calls.jsonl` 并不包含完整 prompt/response

位置：

- [deeptrade/core/config.py](E:/personal/DeepTrade/deeptrade/core/config.py:120)
- [deeptrade/core/deepseek_client.py](E:/personal/DeepTrade/deeptrade/core/deepseek_client.py:241)
- [deeptrade/core/deepseek_client.py](E:/personal/DeepTrade/deeptrade/core/deepseek_client.py:387)
- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/strategy.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/strategy.py:297)
- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/render.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/render.py:219)

设计要求：

- `llm_calls` 用于可重放审计，至少应能追踪 prompt_hash 与原始响应。
- 配置中已有 `deepseek.audit_full_payload`，注释说明 full prompt/response 会进入报告目录。

实际实现：

- `AppConfig.deepseek_audit_full_payload` 存在，但 `_build_llm_client()` 构造 `DeepSeekClient` 时没有传 `audit_full_payload=cfg.deepseek_audit_full_payload`。
- `audit_full_payload=False` 时 DB 只存 system/user 长度和 response 前 200 字。
- `export_llm_calls()` 只从 DB 导出 lean 字段，并不导出完整 prompt/response。

影响：

- 用户打开 `reports/<run_id>/llm_calls.jsonl` 无法看到完整 prompt/response。
- 出现 LLM 争议结果时难以重放和审计。
- 配置项设置为 true 也不会生效。

建议：

1. `_build_llm_client()` 传入 `audit_full_payload=cfg.deepseek_audit_full_payload`。
2. 如果默认 lean 是出于 DB 体积考虑，应在文件报告中另行写完整 payload，而不是只导出 DB lean 行。
3. 文档和注释统一：要么明确 lean 模式不可完整重放，要么实现完整报告审计。

### Medium 3：final_ranking 入围集合选择存在顺序偏置，且硬编码 `batch_size_hint=20`

位置：

- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/pipeline.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/pipeline.py:398)
- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/pipeline.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/pipeline.py:413)
- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/strategy.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/strategy.py:211)
- [DESIGN.md](E:/personal/DeepTrade/DESIGN.md:1280)

设计要求：

- R2 多批时 final_ranking 只对 finalists 做全局校准，避免再次超长。
- finalists 应按每批 top/watchlist 和边界 avoid 合理抽样。

实际实现：

- `select_finalists()` 先收集所有 `top_candidate/watchlist`，然后在原始顺序上截断：
  `finalists = finalists[:cap]`。
- 原始顺序来自 batch 顺序，不一定按 `continuation_score` 或 rank。
- 策略层硬编码 `batch_size_hint=20`，没有使用实际 R2 batch size。

影响：

- 后面批次的高分候选可能被前面批次的低分 watchlist 挤掉。
- final_ranking 结果会带有批次顺序偏置。

建议：

1. `RoundResult` 记录 R2 实际 `batch_size` 或每个候选的 `batch_no`。
2. finalists 按每批内部 rank/score 采样，而不是全局原始顺序截断。
3. 边界 avoid 也应按每批抽样，而不是只从全局 avoid 取前 20%。

### Medium 4：moneyflow 没有 queryable 业务表，materialize 失败也被静默降级为 warning

位置：

- [deeptrade/core/migrations/core/20260427_001_init.sql](E:/personal/DeepTrade/deeptrade/core/migrations/core/20260427_001_init.sql:160)
- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/data.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/data.py:435)
- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/data.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/data.py:457)
- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/data.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/data.py:462)

设计要求：

- `moneyflow` 是打板策略 required 数据。
- DuckDB 是持久化数据底座，策略所需数据应可审计、可查询。

实际实现：

- 核心共享表只有 `stock_basic`、`trade_cal`、`daily`、`daily_basic`。
- `moneyflow` 只进入 `tushare_cache_blob`，没有 queryable shared table。
- `_materialize_business_tables()` 对写表异常全部 `logger.warning()` 后继续，不会让用户在报告中看到持久化失败。

影响：

- 资金流数据虽然参与 prompt，但无法像 `daily` / `daily_basic` 一样用 SQL 复查。
- 如果 schema 与 Tushare 返回字段不匹配，策略仍可能 success，只是业务表缺数据。

建议：

1. 增加 `moneyflow` shared table，或者在插件表中声明 `lub_moneyflow`。
2. `materialize` 失败至少写入 `strategy_events` 或 `data_unavailable` / report，而不是只写日志。
3. 对 required 数据的业务表写入失败，建议标记 run 为 `partial_failed`。

### Medium 5：`plugin upgrade` 没有与 install 相同的 static validation / 声明表校验 / 回滚策略

位置：

- [deeptrade/core/plugin_manager.py](E:/personal/DeepTrade/deeptrade/core/plugin_manager.py:321)
- [deeptrade/core/plugin_manager.py](E:/personal/DeepTrade/deeptrade/core/plugin_manager.py:211)
- [deeptrade/core/plugin_manager.py](E:/personal/DeepTrade/deeptrade/core/plugin_manager.py:226)

设计要求：

- plugin install / upgrade 都应保证 entrypoint 可导入、migration 正确、metadata 声明表已创建。
- 升级只追加新 migrations，但新版本也应可运行。

实际实现：

- `install()` 已检查 missing declared tables，并执行 `_load_entrypoint()` + `validate_static()`。
- `upgrade()` 只复制新版本、跑新 migration、更新 metadata，没有执行 `_missing_declared_tables()`、entrypoint import、`validate_static()`。

影响：

- 一个无法导入的新版本可以被 upgrade 成功。
- 新 metadata 声明了新表但 migration 未创建，也可能直到运行或 uninstall 时才暴露。

建议：

1. 抽取 install/upgrade 共用的 post-copy validation。
2. upgrade 更新 DB row 前验证新 entrypoint + `validate_static()`。
3. 失败时删除新版本目录，并保留旧版本 registry / install_path。

### Low 1：`plugin uninstall` 默认删除 install_path 但保留 disabled registry，`plugin enable` 可重新启用一个缺文件插件

位置：

- [deeptrade/core/plugin_manager.py](E:/personal/DeepTrade/deeptrade/core/plugin_manager.py:284)
- [deeptrade/core/plugin_manager.py](E:/personal/DeepTrade/deeptrade/core/plugin_manager.py:289)
- [deeptrade/cli_plugin.py](E:/personal/DeepTrade/deeptrade/cli_plugin.py:122)

设计中默认 uninstall 是“disable + 删除 install_path 副本（保留表与历史 run）”。这个设计可以接受，但当前 `enable()` 不检查 `install_path` 是否存在。用户执行：

```text
deeptrade plugin uninstall some-plugin
deeptrade plugin enable some-plugin
deeptrade strategy run some-plugin
```

会得到一个 enabled 但无法加载的插件。

建议：

- `enable()` 时检查 `install_path` 和 entrypoint 可导入。
- 或把默认 uninstall 的状态标记为 `uninstalled`，与 `disabled` 区分。

### Low 2：Tushare fallback 成功没有进入事件流，dashboard/report 不可见

位置：

- [deeptrade/core/tushare_client.py](E:/personal/DeepTrade/deeptrade/core/tushare_client.py:490)
- [DESIGN.md](E:/personal/DeepTrade/DESIGN.md:607)

设计中定义了 `TUSHARE_FALLBACK = "tushare.fallback"`，但当前 fallback 只写 logger warning，不会进入 `strategy_events`，Rich dashboard 和最终报告也看不到。

建议：

- `TushareClient` 返回 fallback metadata，或通过 callback/event sink 注入事件。
- 至少在 bundle/report 中记录 `fallback_used`。

### Low 3：partial_failed 报告横幅没有列出失败 batches

位置：

- [deeptrade/strategies_builtin/limit_up_board/limit_up_board/render.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/limit_up_board/render.py:28)
- [DESIGN.md](E:/personal/DeepTrade/DESIGN.md:1475)

设计要求 report 顶部红色横幅提示“结果不完整，缺失 batches=[...]”。当前 banner 只说明 partial，不列失败批次。  
建议在 `RoundResult` 中保留 `failed_batch_ids`，并写入 `data_snapshot.json` 与 `summary.md`。

## 3. 上一轮问题复查表

| 上一轮问题 | 第三轮状态 | 说明 |
|---|---:|---|
| `strategy run` 不调用 `validate()` / `configure()` | 部分解决 | 框架已调用，但内置策略 hook 仍为空 |
| 不支持不传 plugin_id 的策略选择 | 已解决 | `cmd_run()` 已接入交互选择 |
| 打板策略未采集 `daily` / `daily_basic` / `moneyflow` | 部分解决 | 已采集，但参数化缓存和 force_sync 存在新风险 |
| `stock_st` required 被软跳过 | 已解决 | 未授权会抛出 |
| Tushare 5xx fallback 未接入 | 已解决 | `_fetch_and_store()` 已尝试 fallback |
| LLM transport 失败导致整 run failed | 已解决 | R1/R2/final_ranking 已捕获为 `VALIDATION_FAILED` |
| plugin install static validation 失败仍成功 | 已解决 | install 已阻断并回滚 |
| 策略结果未落 `lub_stage_results` | 已解决 | `_write_stage_results()` 已落库 |
| final_ranking 失败报告仍 success | 已解决 | report status 已考虑 final_ranking 失败 |
| 缺 `data sync` / 裸命令主菜单 | 已解决 | 已实现 |
| keyring 不可用时读取 keyring 记录 assert | 已解决 | 已改为返回 None + log |
| metadata 未注入插件实例 | 已解决 | `_load_entrypoint(..., metadata)` 已注入 |
| LLM 审计 DB 过大 | 部分解决 | DB lean 化已做，但 full report 审计未真正实现 |

## 4. 测试覆盖建议

建议下一轮补以下测试，优先覆盖静默错误：

1. `test_parameterized_cache_does_not_reuse_other_date_range_payload`
2. `test_strategy_force_sync_passes_to_all_tushare_calls`
3. `test_limit_up_today_empty_after_close_is_failed_not_empty_success`
4. `test_r1_candidate_set_mismatch_retries_once`
5. `test_r2_candidate_set_mismatch_retries_once`
6. `test_final_ranking_candidate_set_mismatch_marks_partial_failed`
7. `test_evidence_unit_is_required`
8. `test_r2_rank_must_be_dense_1_to_n`
9. `test_deepseek_audit_full_payload_config_is_honored`
10. `test_upgrade_rejects_broken_entrypoint_and_keeps_old_version`

## 5. 建议修复优先级

P0：

- 修复参数化 Tushare 缓存 key / 参数无关 fallback。
- 让 `--force-sync` 真正穿透到 `collect_round1()` 和所有 `tushare.call()`。
- 实现内置策略 `validate()` 的最小可用自检。

P1：

- 实现 LLM candidate_id 集合不一致的 retry。
- final_ranking 增加 candidate_id 集合校验。
- 区分“真实 0 候选”和“当日数据尚未入库”。
- optional API 5xx 无 fallback 时降级为 `data_unavailable`。

P2：

- schema 强化：EvidenceItem.unit 必填、R2 rank 连续。
- 修复 full audit 配置和 `llm_calls.jsonl` 内容。
- 改进 final_ranking finalists 采样。
- 增加 moneyflow queryable 表或明确只使用 cache_blob 的设计边界。
- 补齐 upgrade 与 install 同等级校验。

## 6. 结论

第三轮代码相比上一轮成熟很多，基础工程质量也稳定，194 个测试、ruff、mypy 都通过。当前主要问题不是代码风格或类型安全，而是几处真实运行语义还没有完全符合设计：缓存维度、强制同步、前置自检、LLM 集合一致性和审计可重放性。

建议先处理 P0/P1，再接入真实 Tushare/DeepSeek 做长跑验证。否则即使命令成功结束，也可能存在“输入数据不是用户以为的那批数据”或“LLM 静默遗漏候选”的风险。
