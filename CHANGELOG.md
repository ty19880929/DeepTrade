# Changelog

All notable changes to DeepTrade. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and SemVer.

## [volume-anomaly v0.6.0] — 2026-05-08 — 显式分维度评分（PR-6）

> 仅升级 volume-anomaly 插件版本（0.5.0 → 0.6.0）；框架版本不变。
> 升级方式：`deeptrade plugin upgrade <plugin-source>`，会自动应用新增的
> migration（`20260601_002_dimension_scores.sql`）。

> ⚠ **Schema 变更**：`VATrendCandidate.dimension_scores` 现为**必填字段**；
> 旧 LLM 响应不再可解析（但持久化到 `va_stage_results.raw_response_json`
> 不受影响——升级前的历史数据完整保留可读，仅新写入要求新 schema）。

参考 `docs/volume_anomaly_wave2_design.md` § 2。

### Added (volume-anomaly plugin)

- **新子模型 `VADimensionScores`**：6 个维度（`washout` / `pattern` /
  `capital` / `sector` / `historical` / `risk`）每个 0–100 整数评分，
  其中 `risk` 为反向极性（高分 = 高风险），其余为正向极性。
- **`VATrendCandidate.dimension_scores` 必填**（F8）：每只候选都必须输出
  6 维评分，由 LLM 自洽与 `launch_score` 大致一致（F3 / F14 软约束，
  不强制公式）。
- **Prompt 评分尺度锚定**：`VA_TREND_SYSTEM` 加入【dimension_scores 评分尺度】
  段（0-30 / 30-60 / 60-80 / 80-100 四档语义），每个判断维度（A–F）末尾
  显式标注对应 `dimension_scores.<name>`。
- **Few-Shot 示例同步**：`prompts_examples.py` A/B 示例补 `dimension_scores`
  对象，与示例评分尺度一致。
- **拆 6 列持久化**（G6）：`va_stage_results` 新增 `dim_washout` /
  `dim_pattern` / `dim_capital` / `dim_sector` / `dim_historical` /
  `dim_risk` 6 个 `DOUBLE` 列。`runner._write_stage_results` 写入
  对应列；旧行 `dim_*` 全部为 `NULL`，stats SQL 自动过滤。
  原始 JSON 仍写入 `raw_response_json` 作为审计备份。
- **stats `--by dimension_scores`**：新支持子选项，输出 6 个维度评分与
  `ret_t3` 的 Pearson 相关系数（DuckDB `CORR(...)` 内置聚合）。
- **报告紧凑表达**：`write_analyze_report` 在 imminent_launch / watching
  表中新增"W/P/C/S/H/R"列，紧凑展示 6 维分数（如 `80/75/70/75/60/25`）。

### Implementation notes

- 拆列而非单 JSON 列的理由（G6）：`stats --by dimension_scores` 要求
  6 维与 `ret_t3` 做 Pearson，`AVG`/`CORR` 在拆列模式下纯 SQL 表达；
  `JSON_EXTRACT` 在 SQL 层昂贵且 DuckDB 跨版本兼容性差。
- 输出 token 预算上调：`DEFAULT_AVG_OUTPUT_TOKENS_PER_CANDIDATE`
  900 → 1100（吸纳 6 维评分 + alpha 字段），`plan_batches` 按新预算切批。
- F13 决策：6 维（W/P/C/S/H/R），不加 liquidity。

### Breaking changes

- 旧 LLM 响应（无 `dimension_scores`）会在 Pydantic 解析阶段抛
  `ValidationError`。已落地的批次再 retry 时需更新 prompt（已自动包含）。
- 回滚路径：revert PR-6 不需要 drop 列（`NULL` 列不影响旧逻辑），但
  已经按新 schema 持久化的行将无法被旧版本读取——因此降级时**保留
  `raw_response_json` 而忽略 `dimension_scores_json`**。

## [volume-anomaly v0.5.0] — 2026-05-08 — RPS / 大盘相对 alpha（PR-5）

> 仅升级 volume-anomaly 插件版本（0.4.0 → 0.5.0）；框架版本不变。
> 升级方式：`deeptrade plugin upgrade <plugin-source>`。本版本不新增 migration。

参考 `docs/volume_anomaly_wave2_design.md` § 1。

### Added (volume-anomaly plugin)

- **沪深 300 alpha 字段**（P1-1）：候选行新增 `alpha_5d_pct` / `alpha_20d_pct` /
  `alpha_60d_pct`（个股相对沪深 300 同期累计收益差，单位 %）+ `baseline_index_code`
  + `rel_strength_label ∈ {leading, in_line, lagging}`（基于 alpha_20d_pct
  ±5% 分档）。让 LLM 在判断"放量异动"时区分跟随性反弹与抗跌强势。
- **新 tushare 权限 `index_daily`（optional）**：250d 拉一次沪深 300 基准
  收盘价；G1—与个股 daily 长度对称、cache 友好。
- **维度 D 改名 / 扩写**：`VA_TREND_SYSTEM` 中维度 D 由"板块强度"改为
  "**板块与市场相对强度**"，提示 LLM 同时考虑板块层 (`sector_strength_*`)
  与市场层 (`alpha_*_pct` / `rel_strength_label`)。
- **Few-Shot 示例补充 alpha 引用**：`prompts_examples.py` 的 A/B 示例
  各加一条 `alpha_20d_pct` 引用，确保 anchoring 与新维度一致。
- **G8 显式降级提示**：`index_daily` 权限缺失或 fetch 失败时，runner emit
  一条 `EventLevel.WARN` LOG 事件，明确提示用户"alpha 字段降级为 None；
  如需启用 alpha，请确认 Tushare 账户已开通 index_daily 权限"。

### Behavior change

- analyze 阶段每次 run 多 1 次 `index_daily` 调用（cache 友好；首次冷拉
  ~5s），无新数据消费 SLA 影响。
- LLM 输入每候选 +30–40 tokens（5d/20d/60d alpha + label）；
  在 200K 输入预算下完全可接受。

### Implementation notes

- alpha 计算口径：`alpha_n = stock_pct_chg_n − baseline_pct_chg_n`（简单收益）。
- `rel_strength_label` 仅基于 `alpha_20d_pct`；alpha_20d 缺失时
  `rel_strength_label = None`，但 `baseline_index_code` 永远输出（元数据）。
- F2 决策：行业相对 alpha 暂不做，留给波次 3 视效果再加。

## [volume-anomaly v0.4.0] — 2026-05-08 — T+N 自动回测闭环（PR-4）

> 仅升级 volume-anomaly 插件版本（0.3.0 → 0.4.0）；框架版本不变。
> 升级方式：`deeptrade plugin upgrade <plugin-source>`，会自动应用新增的
> migration（`20260601_001_realized_returns.sql`）。

参考 `docs/volume_anomaly_wave2_design.md` § 3。

### Added (volume-anomaly plugin)

- **新表 `va_realized_returns`**：每条 hit 一行（PK=`anomaly_date+ts_code`），
  存 T+1/T+3/T+5/T+10 收盘价、对应收益、5d/10d 窗口最大涨幅与回撤。
  `data_status ∈ {pending, partial, complete}`（G5 严格三态：T+1 未到→pending；
  max_horizon 未到 OR 任一 horizon 数据缺失→partial；max_horizon 已到且全填→complete）。
- **新子命令 `evaluate`**：
  ```
  deeptrade volume-anomaly evaluate [--lookback-days N] [--trade-date YYYYMMDD]
                                    [--backfill-all] [--force-recompute]
  ```
  从 `va_anomaly_history` 全集（G3）里取 anomaly_date 在 lookback 内的 hits，
  按 trade-day horizon 解析 T+1/T+3/T+5/T+10 实际 trade_date，复用
  `_fetch_daily_history_by_date` 的 cache 拉取收盘价，UPSERT 进
  `va_realized_returns`。`complete` 行默认跳过；`--force-recompute` 强制重算。
- **新子命令 `stats`**：
  ```
  deeptrade volume-anomaly stats [--from] [--to] [--by prediction|pattern|launch_score_bin]
  ```
  纯只读 SQL 聚合 `va_stage_results JOIN va_realized_returns`，输出每桶的
  样本数 / T+3 平均收益 / T+3 胜率 / T+5 最大涨幅均。`launch_score_bin`
  默认分箱 `0-40 / 40-60 / 60-80 / 80-100`（G4）。
- **`evaluate` 写 va_runs / va_events**（G10）：`mode='evaluate'` 与
  screen / analyze / prune 同级，可在 `history` 子命令中查看。

### Implementation notes

- 所有 horizon 时间换算用 `TradeCalendar.next_open` 跳过周末/节假日。
- `va_realized_returns.t_close` 冗余存（G9）——让 stats 在 va_anomaly_history
  被异常清空时仍可读取。
- 不新增 tushare API 权限（仍只用 `daily`）。
- 不引入新插件级 config 表（F6）；horizon 列表写死在 module-level
  `EVALUATE_HORIZONS = (1, 3, 5, 10)`。

## [volume-anomaly v0.3.0] — 2026-05-08 — 波次 1（PR-1 + PR-2 + PR-3）

> 仅升级 volume-anomaly 插件版本（0.2.0 → 0.3.0）；框架版本不变。
> 升级方式：`deeptrade plugin upgrade <plugin-source>`。本版本不新增 migration。

参考 `docs/volume_anomaly_wave1_design.md`（§ 1–5 共五项 P0 优化）。

### Added (volume-anomaly plugin)

**PR-1 — Screen 规则**

- **上影线过滤**（P0-1）：T-day 规则后、换手率前新增一道过滤，
  `upper_shadow_ratio = (high − max(open, close)) / range > upper_shadow_ratio_max`
  的候选直接淘汰，避免"避雷针"型的纯上影 K 线进入候选。诊断字段
  `n_after_upper_shadow` 暴露在漏斗中；hit 行新增 `upper_shadow_ratio` 字段。
- **按流通市值分桶的换手率阈值**（P0-2）：用 `daily_basic.circ_mv` 把候选按
  流通市值（亿元）切 4 档，每档使用独立的 `[turnover_min, turnover_max]`：
  `≤50亿: [5, 15]` / `50–200亿: [3.5, 12]` / `200–1000亿: [2.5, 9]` /
  `>1000亿: [1.5, 6]`。边界值归"较小桶"（E4）。`circ_mv` 缺失时退化到
  全局 `turnover_min/max`，并写入 `data_unavailable`。诊断字段
  `turnover_bucket_hits` / `n_missing_circ_mv` / `circ_mv_missing_codes`
  暴露在 screen 报告中；hit 行新增 `circ_mv_yi` / `turnover_bucket` 字段。

**PR-2 — Analyze 候选行特征**

- **VCP 三维收敛指标**（P0-3）：候选行新增
  `atr_10d_pct` / `atr_10d_quantile_in_60d`（10 日 ATR 在近 60 日 ATR 序列中的分位数）/
  `bbw_20d`（20 日 Bollinger 带宽）/ `bbw_compression_ratio`
  （当前 BBW / 近 60 日 BBW 均值，<1 表示在收敛）。零新数据，复用现有
  60d 历史窗口（实际从 250d 切片）。
- **120/250 日阻力位距离**（P0-4）：`collect_analyze_bundle` 默认
  `history_lookback` 由 60 → 250；`_build_candidate_row` 内部切片处理。
  候选行新增 `high_120d` / `high_250d` / `low_120d` /
  `dist_to_120d_high_pct` / `dist_to_250d_high_pct` /
  `is_above_120d_high` / `is_above_250d_high` / `pos_in_120d_range`。
  E3-A：仅 120d 区间位置，不补 `low_250d`。E7-C：任一窗口数据不足则
  对应字段降级为 `None`，不阻塞主流程。
- **首次冷拉延迟**：扩展 250d 窗口的首次 fetch 约多 30–45 秒
  （Tushare RPS=6 默认下），后续走 cache 零增量。

**PR-3 — LLM Prompt 对齐**

- **Few-Shot 锚定**（P0-5）：新增 `prompts_examples.py::VA_TREND_FEWSHOT`，
  在 `VA_TREND_SYSTEM` 末尾拼接两个示例（VCP 突破 / 高位上影诱多），
  引导 LLM 在不同 provider 间保持一致的 `launch_score` 尺度。
- **维度 A 提示扩展**：在【判断维度】A 段中加入"整理期波动率收敛"指引，
  显式提示 LLM 引用 `atr_10d_quantile_in_60d` / `bbw_compression_ratio`。
- **字段一致性单测**：`test_prompt_consistency.py` 用正则提取
  示例中的 `"field": "<X>"`，断言每个 X 都属于
  `_build_candidate_row` 输出键集合 OR screen hit 行字段集合。
  防止后续字段重命名时 prompt 失配。

### Default behavior change (heads-up)

- 默认开启 **上影线过滤**：`ScreenRules.upper_shadow_ratio_max = 0.35`。
  如需保持旧行为，在 `screen_rules` 配置中显式传 `"upper_shadow_ratio_max": null`。
- 默认开启 **分桶换手率**：`ScreenRules.turnover_buckets = DEFAULT_TURNOVER_BUCKETS`。
  如需保持旧行为，在 `screen_rules` 配置中显式传 `"turnover_buckets": null`，
  此时回退到 `[turnover_min, turnover_max]` 全局阈值。
- 默认 analyze 历史窗口由 60 → 250 个交易日（E2-A 单 fetch 复用）。
  首次冷启动会多消耗 30–45s（RPS=6 默认），后续走 cache 零增量。

### Implementation notes

- `turnover_buckets` JSON 配置中最后一档可用 `null` 表示无穷上界，
  `from_dict` 内转 `math.inf`。`as_dict()` 反向把 `math.inf` 序列化为 `null`，
  使 `screen_stats.json` 仍是合规的标准 JSON（不依赖 `Infinity` 扩展）。
- 不动 framework；不新增 tushare API 权限（`circ_mv` 已包含在
  `daily_basic` 字段中；250d 长窗口仍走 `daily`）。
- VCP 计算 `_compute_atr_series` 用简单平均 TR（与 Wilder 公式相比差异
  在 60d 窗口尺度内不显著）；BBW 用 4σ / MA20 × 100 表达带宽占均价比例。

## [limit-up-board v0.4.0] — 2026-05-07 — 候选股市值/股价过滤 + 持久化设置

> 仅升级 limit-up-board 插件版本（0.3.2 → 0.4.0）；框架版本不变。
> 升级方式：`deeptrade plugin upgrade <plugin-source>`，会自动应用新增的
> migration（`20260508_005_lub_config.sql`）。

### Added (limit-up-board plugin)

- **Step 1 候选股漏斗增加两条筛选条件**：在主板 + 涨停 join 之后、ST/停牌之前
  按 *流通市值* 与 *当前股价* 上限再筛一层。null 在任一字段 → 过滤（保守语义）。
  默认 `流通市值 < 100亿` + `股价 < 15元`。`bundle.market_summary` 新增
  `candidate_filter_summary` 字段（before / after / 阈值），LLM prompt 中可见。
- **新子命令 `deeptrade limit-up-board settings`**：交互式编辑两个上限阈值，
  回车保留当前值，写入新表 `lub_config`。
- **新子命令 `deeptrade limit-up-board settings show`**：表格展示当前生效设置，
  source ∈ {`default`, `persisted`}。
- **运行时打印当前配置**：`run` / `sync` 在 Step 1 之前 emit 一条 LOG 事件
  （`运行配置: 流通市值 < ...亿、股价 < ...元`），dashboard / 日志中可见。

### Implementation notes

- 配置存储：插件自有 `lub_config` 表（与 `lub_runs` / `lub_events` 同 Plan A
  纯隔离层级），不复用框架级 `app_config` —— 避免 `_DOT_TO_FIELD` 白名单
  扩张，框架保持轻量。
- 默认值唯一来源：`limit_up_board.config:LubConfig` 的 dataclass default；
  SQL / CLI / 文档不重复声明。

## [limit-up-board v0.3.0] — 2026-05-07 — Phase A + Phase B 因子补齐

> 仅升级 limit-up-board 插件版本（0.2.1 → 0.3.0）；框架版本不变。
> 升级方式：`deeptrade plugin upgrade <plugin-source>`，会自动应用本版本新增的两条
> migration（`20260508_001_lub_lhb_tables.sql` / `20260508_002_lub_cyq_perf.sql`）。

参考 `docs/limit-up-board-optimization-plan.md`。对照 Gemini 给出的"游资思路"因子
清单，分两阶段补齐：A 阶段为派生因子（不增 tushare API），B 阶段接入龙虎榜与筹码。

### Added (limit-up-board plugin)

**A1 — 派生因子（候选股层，pure compute）**
- `amplitude_pct`：T 日振幅 = (high − low) / pre_close × 100。
- `fd_amount_ratio`：封单比 = fd_amount / amount × 100，>10% 为强势封板。
- `ma5` / `ma10` / `ma20` + `ma_bull_aligned`：基于 prev_daily 的简单移动平均；
  `ma_bull_aligned = (close > ma5 > ma10 > ma20)`。历史不足窗口期返回 null。
- `up_count_30d`：近 30 个交易日 pct_chg ≥ 9.8 的天数；不足 30 日返回 null。

**A2 — 市场情绪三件套（market_summary 层）**
- `limit_step_distribution_prev` + `limit_step_trend`：T 与 T-1 的连板梯队对比，
  interpretation ∈ {`spectrum_lifting`, `spectrum_collapsing`, `stable`}。
- `yesterday_failure_rate`：T-1 全市场炸板率（z / (u + z)），interpretation ∈
  {`high` ≥25%, `moderate`, `low` ≤10%}。
- `yesterday_winners_today`：T-1 涨停股在 T 日的连板率与平均涨幅，
  interpretation ∈ {`strong_money_effect`, `neutral`, `weak_money_effect`}。

**B1 — 龙虎榜（top_list / top_inst → required）**
- 候选股层字段：`lhb_net_buy_yi`（龙虎榜净买入，亿元；正/负均如实给出）、
  `lhb_inst_count`（机构席位 unique 数）、`lhb_famous_seats`（命中游资白名单的
  exalter 原文数组）。
- `FAMOUS_SEATS_HINTS`：~15 条主流游资席位子串白名单（拉萨系、宁波系、章盟主、
  赵老哥、厦门帮等）。匹配逻辑大小写不敏感；只给 exalter 原文，不暴露白名单标签。
- 新表：`lub_top_list`（key trade_date+ts_code）、`lub_top_inst`
  （key trade_date+ts_code+exalter+side）。
- "未上榜 ≠ 数据缺失"：candidate 不在当日 top_list 中时 `lhb_*` 为 null，但
  `data_unavailable` 中**不会**出现 top_list/top_inst（接口本身可用）。

**B2 — 筹码（cyq_perf → required）**
- 候选股层字段：`cyq_winner_pct`（获利盘比例 %）、`cyq_top10_concentration`
  （100 − (cost_95pct − cost_5pct) / weight_avg × 100，clip [0,100]）、
  `cyq_avg_cost_yuan`（weight_avg）、`cyq_close_to_avg_cost_pct`
  （(close − weight_avg) / weight_avg × 100）。
- 新表：`lub_cyq_perf`（key trade_date+ts_code）。

**Prompt 改动**
- R1_SYSTEM【分析维度】新增形态、历史基因、市场情绪三类；evidence 新增"missing_data
  字段不得引用"硬约束。
- R2_SYSTEM【判断重点】追加：亏钱效应下自动下调 confidence 一档；梯队拉升期允许
  上调 continuation_score；筹码维度三阈值（70/60/-10）+ 龙虎榜维度（未上榜 ≠ 缺失；
  负净买入不可作为正面 evidence；不可推断游资身份）。
- R1 **不引入** chip / LHB 维度（控制 prompt 噪声，B 阶段仅 R2 使用）。

### Changed (BREAKING)

**Tushare 权限要求扩张**（用户必须确保 tushare 账户已开通以下权限）：
- `top_list` / `top_inst` / `cyq_perf` 由 `permissions.tushare_apis.optional` 上移
  到 `required`；任一缺失 → run failed。**未引入新权限分类**——"接口可用但
  candidate 未上榜"用 null + 数据层 inner-join 自然表达。

**默认值 / 内部行为变更**
- `RunParams.daily_lookback` / CLI `--daily-lookback` 默认值 10 → **30**（满足
  ma20 + up_count_30d）。
- `collect_round1` 新增 `prev_trade_date: str | None` 入参；3 个 runner 调用点
  通过 `_safe_prev_trade_date(cal, T)` 计算并传入。
- daily 历史窗口的日历缓冲由 `lookback + 5` 改为 `lookback × 2`，确保 30 个交易
  日 lookback 在过节窗口下仍能命中 ≥30 行真实交易日。

### Migrations

- `20260508_001_lub_lhb_tables.sql` — 创建 `lub_top_list` / `lub_top_inst`。
- `20260508_002_lub_cyq_perf.sql` — 创建 `lub_cyq_perf`。

### Tests

- `tests/strategies_builtin/limit_up_board/test_phase_a_factors.py`：33 个单元
  测试，覆盖 A1/A2 全部派生函数 + 边界（不足窗口期、零分母、空 frame、阈值跳点）。

- `tests/strategies_builtin/limit_up_board/test_phase_b_factors.py`：20 个单元
  测试，覆盖白名单匹配（大小写、去重、非字符串）、LHB rollup（空帧 / None /
  仅 top_list）、cyq 集中度（紧/宽/截断到 0）、close_to_avg_cost_pct 边界。

## [v0.7.0] — 2026-05-01 — Stage 概念归插件 + 配置键改名

清理 v0.6 留下的 stage 硬编码技术债。Stage 名字、preset → stage tuning 表全
部移入插件；框架的 `LLMClient.complete_json` 不再认识 stage，由调用方直接传
入 `StageProfile`。配置键 `deepseek.profile` 同步重命名为 `app.profile`。
**Breaking change**（项目仍在 dev/iteration 期）。设计原文：DESIGN.md §10.1。

### Changed (BREAKING)

**框架瘦身 — stage 退出 LLMClient**

- 删除 `core.llm_client.KNOWN_STAGES` / `LLMUnknownStageError` /
  `_stage_profile()` / 全局 `_CURRENT_STAGE`。
- `LLMClient.complete_json` 签名变化：
  - 删除 `stage: str` 入参；framework 不再写 `llm_calls.stage` 列。
  - 新增 `profile: StageProfile`（必填）— 调用方直接传入已解析的调参档。
  - `LLMClient.__init__` 删除 `profiles=` 入参。
  - `LLMManager.get_client()` 不再绑 profile。
- `RecordedTransport` 改为纯 FIFO：`register(response)` 不再带 stage 标签。
- 删除 `core.config.DS_STAGES` / `DeepSeekProfileSet` / `PROFILES_DEFAULT`
  / `ConfigService.get_profile()`。
- `StageProfile` 升格为公共契约，搬到 `deeptrade.plugins_api.llm`，由
  `from deeptrade.plugins_api import StageProfile` 公开导出。

**配置键改名 — `deepseek.profile` → `app.profile`**

- `AppConfig.deepseek_profile` → `AppConfig.app_profile`；`_DOT_TO_FIELD`
  同步更新。preset 仍是 `Literal["fast","balanced","quality"]`，语义全局，
  但键名 vendor-agnostic。
- DB 行自动迁移：`config_migrations.migrate_legacy_deepseek_profile_key`
  幂等地把 `deepseek.profile` 行改写为 `app.profile`，并删除旧行。
- **环境变量直接断代**：`DEEPTRADE_DEEPSEEK_PROFILE` 不再被识别。
  `ConfigService.get_app_config()` 启动时若检测到旧 env 而新 env 未设，
  抛 `RuntimeError` 退出 — 避免静默用错配置（默认会回落到 "balanced"，
  让用户以为生效但其实并没有）。请改为 `DEEPTRADE_APP_PROFILE`。

**DB schema — `llm_calls.stage` 列删除**

- 新 SQL 迁移 `20260501_002_drop_llm_calls_stage.sql` `ALTER TABLE
  llm_calls DROP COLUMN IF EXISTS stage`（DuckDB 1.0+）。
- `core/migrations/core/20260427_001_init.sql` 同步去掉 `stage` 字段，
  让 fresh DB 直接落到 v0.7 期望状态。
- 历史 run 的 stage 信息仍可在
  `~/.deeptrade/reports/<run_id>/llm_calls.jsonl` 中查阅；v0.7 起新写入的
  jsonl 行也不再含 `stage` 键。

**插件改造（两个内建插件）**

- 新增 `<plugin>/profiles.py`：本地维护 preset → stage tuning 表 +
  `resolve_profile(preset, stage)`。
- `runner.py` 读取 `cfg.app_profile`（preset 字符串），传给 pipeline 函数；
  pipeline 内部调 `resolve_profile()` 得到 `StageProfile`。
- `volume-anomaly` 借此改造把语义错误的 stage 名 `continuation_prediction`
  改回 `trend_analysis`。

### Added

- `deeptrade/plugins_api/llm.py` — 公共 `StageProfile` 契约（4 字段：
  thinking / reasoning_effort / temperature / max_output_tokens）。
- `config_migrations.migrate_legacy_deepseek_profile_key` + 4 条单测
  （rename / 幂等 / fresh DB no-op / new-already-set 跳过）。
- `tests/core/test_config.py` 新增两条 env 行为测试（旧 env 报错、新旧并存
  以新为准）。

### Engineering

- 136 pytest tests passing (无变更)。
- 偿还 v0.6 RV6-4 / RV6 §10.2 已知技术债。

## [v0.6.0] — 2026-05-01 — LLM Manager 化（多 Provider）

The LLM client is no longer DeepSeek-specific — it is now a framework-level
service that lets a single plugin call multiple OpenAI-compatible LLMs in
the same run. **Breaking change** (project remains in dev/iteration phase).
Full rationale: `DESIGN.md` §0.7 + §10.

### Changed (BREAKING)

**Configuration model — `deepseek.*` → `llm.*`**

- `llm.providers` (JSON dict, app_config) — `{name: {base_url, model, timeout}}`.
  Multiple providers coexist; each plugin picks by name at call time.
- `llm.<name>.api_key` (secret_store) — one secret per provider. The
  `is_secret_key()` predicate (replacing the old `SECRET_KEYS` constant)
  matches `tushare.token` plus this dynamic prefix.
- `llm.audit_full_payload` (bool, app_config) — replaces
  `deepseek.audit_full_payload`.
- `deepseek.profile` is **kept** as the global stage-profile name (rename
  deferred to v0.7 per §10.1 note).
- The four legacy keys `deepseek.base_url` / `deepseek.model` /
  `deepseek.timeout` / `deepseek.audit_full_payload` are removed from
  `AppConfig`. There is **no `llm.default`** — callers must pass a name.

**Auto-migration**

- On first `apply_core_migrations()` after upgrade, legacy `deepseek.*`
  rows are migrated into `llm.providers["deepseek"]` + the renamed secret
  `llm.deepseek.api_key` + `llm.audit_full_payload`. Idempotent: re-runs
  on already-migrated DBs are no-ops. Code in
  `deeptrade.core.config_migrations.migrate_legacy_deepseek_keys`.

**LLM client / new manager**

- New `core/llm_manager.py::LLMManager` — the only path plugins should use:
  `list_providers()`, `get_provider_info(name)`, `get_client(name, *,
  plugin_id, run_id, reports_dir=None)`. Caches clients per
  `(name, plugin_id, run_id)`. Documented as not thread-safe.
- `core/deepseek_client.py` → `core/llm_client.py`;
  `DeepSeekClient` → `LLMClient`. `OpenAIClientTransport` keeps its name
  (it really is the OpenAI-compatible transport).
- `LLMNotConfiguredError` (new) — raised by manager when a provider is
  missing or its api_key is unset.

**CLI command surface**

- Added: `config set-llm` (interactive new / edit / delete one provider),
  `config list-llm` (show usable providers), `config test-llm [name]`
  (per-provider connectivity check; tests all when omitted).
- Removed: `config set-deepseek`, `config test`. The init-time prompt
  ("Configure deepseek now?") is now "Configure an LLM provider now?".
- `config show` expands `llm.providers` so each provider's `api_key` slot
  is its own masked row.

**Plugin runtime collapse**

- `volume-anomaly` and `limit-up-board` runtimes lose their per-plugin
  `build_llm_client(rt)`; both now declare `llms: LLMManager` and call
  `rt.llms.get_client(provider_name, plugin_id=, run_id=, reports_dir=)`.
  Provider selection helper `pick_llm_provider(rt)` ships in each
  plugin's `runtime.py` (prefers `deepseek`, falls back to first
  available); a per-plugin `default_llm` config key is deferred to v0.7.

### Added

- `LLMProviderConfig` Pydantic model (per-provider connection record).
- `ConfigService.set_llm_provider(name, *, base_url, model, timeout,
  api_key=None)` and `delete_llm_provider(name)` — CRUD helpers used by
  the CLI.
- `tests/core/test_llm_manager.py` — list/info/get_client + cache + missing
  api_key/provider errors + multi-provider coexistence (11 cases).
- `tests/core/test_config_migrations.py` — idempotency + happy path +
  partial-legacy-state edge case (7 cases).

### Engineering

- 136 pytest tests passing (was 130; +11 manager + 7 migration − 12 retired
  duplicates).
- ruff + mypy clean on touched files.
- DESIGN §0.7 + §7.1 + §7.3 + §10 rewritten; PLAN §11 adds the v0.6 work
  breakdown.

### Known design debts (deferred)

- `KNOWN_STAGES` (`strong_target_analysis / continuation_prediction /
  final_ranking`) is still hardcoded in the framework — leaks plugin
  semantics into `core/llm_client.py`. v0.7 will let plugins declare their
  own stage names + per-stage profile overrides.
- `deepseek.profile` key name retained for backward compat within the
  current dev cycle; rename to `llm.profile` planned for v0.7.
- No transport plugin type yet — Anthropic native / Gemini native cannot be
  added without code changes. Will land as `type=llm-transport` plugins
  when first user need surfaces.

## [v0.5.0] — 2026-04-30 — Plugin CLI dispatch + pure data isolation

Breaking-change reshape per `docs/plugin_cli_dispatch_evaluation.md`. The
project is in dev/iteration phase; **no backward compatibility**.

### Changed (BREAKING)

**Framework command surface — closed**
- Top-level CLI is now `init / config / plugin / data` ONLY. `strategy` and
  `channel` command groups removed.
- Pure pass-through: any unknown first token is looked up as a `plugin_id`;
  if installed + enabled, framework calls `Plugin.dispatch(remaining_argv)`
  and is otherwise dumb.
- `deeptrade --help` no longer enumerates plugin subcommands. `--help`
  inside a plugin is the plugin's own responsibility.
- `deeptrade hello` removed. Interactive main menu removed.
- Reserved plugin_ids: `init`, `config`, `plugin`, `data`.

**Plugin contract — minimal**
- `StrategyPlugin` Protocol removed. New unified `Plugin` Protocol:
  `metadata` + `validate_static(ctx)` + `dispatch(argv) -> int`.
- `ChannelPlugin` extends `Plugin` and adds `push(ctx, payload)`.
- `StrategyContext` / `StrategyParams` / `StrategyRunner` / TUI dashboard
  removed. Each plugin owns its own runtime + run lifecycle internally.
- `ChannelContext` renamed to `PluginContext` (still narrow: db + config +
  plugin_id).

**Data isolation — Plan A (pure)**
- Framework owns ONLY: `app_config`, `secret_store`, `schema_migrations`,
  `plugins`, `plugin_tables`, `plugin_schema_migrations`, `llm_calls`,
  `tushare_sync_state`, `tushare_calls`. No business tables.
- Tushare-derived shared market tables (`stock_basic`, `trade_cal`, `daily`,
  `daily_basic`, `moneyflow`) removed from core. Each strategy plugin
  declares its own prefixed copies (e.g. `lub_stock_basic`, `va_*`).
- `tushare_sync_state` PK now `(plugin_id, api_name, trade_date)`. Each
  plugin tracks its own sync state and cache; no cross-plugin sharing.
- `tushare_calls` and `llm_calls` add `plugin_id` column.
- `TushareClient.__init__` requires `plugin_id`. Framework probes use the
  reserved `FRAMEWORK_PLUGIN_ID = "__framework__"` sentinel.

**Notification API**
- `core/notifier.py` exposes top-level `notify(db, payload)` and
  `notification_session(db)`. Re-exported as `from deeptrade import
  notify, notification_session`. NoopNotifier when no channel enabled.
- `strategy_runs` / `strategy_events` tables removed. Each plugin defines
  its own `<prefix>_runs` / `<prefix>_events` if it wants run history.

### Added

- Built-in plugins reshaped to v0.2.0:
  - `limit-up-board`: own `cli.py` + `plugin.py` + `runner.py` + `runtime.py`;
    new migration with 10 `lub_*` tables.
  - `volume-anomaly`: same pattern; 5 `va_*` tables; subcommands `screen`
    / `analyze` / `prune` / `history` / `report`.
  - `stdout-channel`: implements new `Plugin` + `ChannelPlugin` contracts;
    own `dispatch` for `test` / `log`.
- Tests: framework routing tests (`tests/cli/test_routing.py`); Plugin
  Protocol contract tests (`tests/plugins_api/test_protocol.py`); plugin
  install + migration isolation tests (`tests/core/test_plugin_install.py`).

### Removed

- `cli_strategy.py`, `cli_channel.py`, `tui/` package, `core/strategy_runner.py`,
  `core/context.py`, old plugin `strategy.py` files.
- `textual` dependency.
- `_interactive_main_menu`, `hello` command.

## [v0.1.0] — 2026-04-28

First public release. Baseline implementation of DESIGN.md v0.3.1.

### Added

**Framework**

- `deeptrade init` — DuckDB layout + 11-table core schema migrations (idempotent).
- `deeptrade config show / set / set-tushare / set-deepseek / test` — layered config (env > db > default), keyring + plaintext fallback for secrets.
- `deeptrade plugin install / list / info / disable / enable / upgrade / uninstall [--purge]` — three-stage lifecycle (install **never** touches network; validate is connectivity-only; run does the strict checks).
- `deeptrade strategy list / run / history / report <run_id>` — Live EVA-themed dashboard (header / progress / events / analysis / footer); `--no-dashboard` for non-tty.
- Plugin api_version "1": `StrategyPlugin` Protocol, `StrategyContext`, `StrategyEvent` enum, Pydantic `PluginMetadata` (YAML).

**Core services**

- `Database` — single-process, single-writer DuckDB; reentrant write lock; short transactions.
- `SecretStore` — keyring-first, plaintext fallback with explicit warning.
- `TushareClient` — token-bucket rate limit, tenacity retries, 4 cache classes (static / trade_day_immutable / trade_day_mutable / hot_or_anns), `data_completeness` (final / intraday) for intraday isolation.
- `DeepSeekClient` — JSON-mode + Pydantic double-validate; profile triple (`fast` / `balanced` / `quality`); stage-level `max_output_tokens` (R1/R2 default 32k, final_ranking 8k); **never** passes `tools` / `tool_choice` / `functions`.
- `StrategyRunner` — status state machine (running → success / failed / partial_failed / cancelled); KeyboardInterrupt → cancelled; any `VALIDATION_FAILED` event flips success → partial_failed.
- `setup_logging()` — stderr handler + rotating file under `~/.deeptrade/logs/`.

**Built-in strategy: limit-up-board**

- Step 0 `resolve_trade_date()` — most-recent-closed trade day; `app.close_after` configurable threshold; `--allow-intraday` opt-in.
- Step 1 data assembly — main board filter (Q2: SSE/SZSE only), ST/suspended exclusion, three-tier `sector_strength` fallback (`limit_cpt_list` → `lu_desc_aggregation` → `industry_fallback`), normalized prompt fields (亿/万 + 2dp) while DB keeps raw.
- Step 2 R1 — `plan_r1_batches()` with input + output token DUAL budget (F5); `EvidenceItem.unit` mandatory.
- Step 4 R2 — single batch by default; auto multi-batch when input exceeds budget.
- Step 4.5 `final_ranking` — only triggered on multi-batch R2; `select_finalists()` keeps top + watchlist + boundary avoid samples.
- Step 5 reports — 5-file dump under `~/.deeptrade/reports/<run_id>/`; banner stack rules (red for partial_failed/failed/cancelled, yellow for INTRADAY MODE, both stack).

### Fixed (v0.3 review round 2 → v0.3.1)

- **F1** `limit_step` was duplicated in optional table → removed; required-only.
- **F2** `limit_cpt_list` `✅` mismatched optional status → unified to `optional+fallback` everywhere; `sector_strength_source` label propagated to prompts.
- **F3** Fast profile R2 `thinking: true` contradicted "all off" docs → flipped to `false`.
- **F4** `--allow-intraday` would have polluted EOD caches → added `data_completeness` column; daily-mode reader rejects intraday-cached rows; UI/report INTRADAY MODE banner.
- **F5** Default `max_output_tokens=8192` would truncate R1 → moved to per-stage profile (R1/R2 32k, final_ranking 8k); R1 evidence cap 8 → 4; rationale length-capped via prompt.
- **S1** Migrations are now the **sole** DDL source; `tables` only declares names + purge flag.
- **S2** `app.close_after` configurable (default 18:00); install never touches tushare.
- **S3** `strategy_runs.status` CHECK constraint removed (DuckDB ALTER limitation); validation moved to Pydantic layer.
- **S4** `row_count=0` is a legal outcome (extreme tape day); fallback predicate accepts it.
- **S5** `final_ranking` only ranks finalists; non-finalists keep `batch_local_rank`; both surface in `round2_predictions.json`.

### Engineering

- 163 pytest tests passing; ruff + mypy clean.
- 1 real concurrency bug fixed during development: `Database.transaction()` + `execute()` self-deadlock with non-reentrant `threading.Lock` → switched to `threading.RLock`.
- 1 pandas 2.x compat fix: `pd.read_json(json_str)` deprecated → wrap in `io.StringIO()`.

### Known design debts (planned for v0.4)

- **D1** Replace `configure(ctx) -> dict` with schema-driven `get_param_schema() -> type[BaseModel]`; CLI auto-renders questionary forms; non-interactive mode supports `--params-file`.
- **D2** Per-API `probes` in plugin metadata; `validate` becomes two layers (`validate_connectivity` + `validate_required_apis`).

[v0.1.0]: https://github.com/example/deeptrade/releases/tag/v0.1.0
