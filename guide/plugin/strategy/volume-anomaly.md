# 成交量异动策略（volume-anomaly）使用说明

> 内置策略插件 · v0.6.0 · `plugin_id = volume-anomaly`
> 主板成交量异动筛选 + LLM 主升浪启动预测，配套 T+N 自动回测闭环。

---

## 目录

- [一、插件功能总览](#一插件功能总览)
- [二、典型工作流](#二典型工作流)
- [三、命令与选项详解](#三命令与选项详解)
  - [3.1 `screen` — 异动筛选](#31-screen--异动筛选)
  - [3.2 `analyze` — LLM 走势分析](#32-analyze--llm-走势分析)
  - [3.3 `prune` — 剔除老旧追踪标的](#33-prune--剔除老旧追踪标的)
  - [3.4 `evaluate` — T+N 实际收益评估](#34-evaluate--tn-实际收益评估)
  - [3.5 `stats` — 实证统计聚合](#35-stats--实证统计聚合)
  - [3.6 `history` — 历次 run 列表](#36-history--历次-run-列表)
  - [3.7 `report` — 重新渲染报告](#37-report--重新渲染报告)
- [四、安装与升级](#四安装与升级)
- [五、数据需求与权限](#五数据需求与权限)
- [六、产物与持久化](#六产物与持久化)
- [七、常见问题](#七常见问题)

---

## 一、插件功能总览

本插件围绕"主板成交量异动"主题提供**端到端**的策略闭环，包含 5 个工作动词（`screen` / `analyze` / `prune` / `evaluate` / `stats`）+ 2 个查阅动词（`history` / `report`）。整体定位是 **本地、规则可审计、LLM 决策可追溯、事后可回测**。

### 1.1 核心能力

| 能力 | 子命令 | 是否调用 LLM | 说明 |
|---|---|:---:|---|
| 异动筛选（local rules） | `screen` |  ✘ | 主板每日扫描，应用涨幅 / K线实体 / 上影线 / 换手率 / 量价规则，命中标的写入 `va_watchlist` 待追踪池 |
| 走势分析（LLM 主升浪预测） | `analyze` | ✔ | 读 `va_watchlist` → 装配 250d 历史 + moneyflow + 板块强度 + 沪深300 alpha → LLM 输出 `imminent_launch / watching / not_yet` 三档预测 + 6 维评分 |
| 待追踪池清理 | `prune` | ✘ | 剔除追踪 ≥ N 日历日的老旧 watchlist 行 |
| T+N 实际收益评估 | `evaluate` | ✘ | 对 `va_anomaly_history` 中过去命中条目，自动拉取 T+1/T+3/T+5/T+10 收盘 + 5d/10d 窗口 max_close / max_dd，写入 `va_realized_returns` |
| 实证统计聚合 | `stats` | ✘ | 在 `va_stage_results × va_realized_returns` 上做 `prediction / pattern / launch_score_bin / dimension_scores` 维度聚合，输出胜率、均值、Pearson 相关 |
| Run 历史 | `history` | ✘ | 列出最近 N 次 run（任意 mode）|
| 报告回看 | `report` | ✘ | 重新渲染历史 run 的终端摘要或完整 Markdown 报告 |

### 1.2 关键特性

#### A. 多重过滤漏斗（`screen`）

按"先廉价 → 后昂贵"原则串联：

```
主板池 → 排除 ST/停牌
       → T 日阳线 + 涨幅 [pct_chg_min, pct_chg_max] + 实体占比 ≥ body_ratio_min
       → 上影线占振幅 ≤ upper_shadow_ratio_max（v0.3.0 新增，避雷针过滤）
       → 换手率（按流通市值分桶 / 全局区间）
       → 量价规则双判定：
          (a) vol_t == max(vol_max_short_window 日内最大量) 或
          (b) vol_t 处于 lookback_trade_days 日内 top vol_top_n_long
          且 vol_t ≥ vol_ratio_5d_min × mean(严格前 5 个交易日 vol)
```

- **复权一致性**：`vol_adjust=true`（默认）时，使用 `adj_factor` 前向调整历史 vol，避免送转 / 拆股造成的伪信号。
- **历史覆盖率门槛**：`min_history_coverage`（默认 80%）保证个股有足够历史才参与量价判定，否则进入诊断区 `insufficient_history`。
- **可观测性**：每次 `screen` 都输出"数据完整性诊断"段（`stock_st` 是否异常空、`turnover_rate` 缺失代码、`adj_factor` 缺失日期等），以便用户自证本次结果未受静默降级影响。

#### B. LLM 走势分析的 6 维评分（`analyze`，v0.6.0 新增）

每只候选输出一个 `dimension_scores` 子对象，6 个维度独立打分（0-100）：

| 维度 | 含义 | 极性 |
|---|---|---|
| `washout` | 洗盘充分度 | 正向（高 = 充分） |
| `pattern` | 形态突破有效性 | 正向 |
| `capital` | 资金验证（moneyflow / volume） | 正向 |
| `sector` | 板块 + 大盘相对强度（含沪深 300 alpha） | 正向 |
| `historical` | 历史浪型位置（越早越好） | 正向 |
| `risk` | 风险（**反向极性**：高分 = 高风险） | 反向 |

报告中以紧凑形式 `W/P/C/S/H/R` 显示（如 `80/75/70/75/60/25`）。

#### C. 沪深 300 相对 alpha（v0.5.0 新增）

每只候选附 3 个 alpha 字段（5d / 20d / 60d）+ `rel_strength_label ∈ {leading, in_line, lagging}`（基于 `alpha_20d_pct ±5%` 分档），帮助 LLM 区分"跟随性反弹"与"抗跌强势"。需要 Tushare 已开通 `index_daily` 权限；否则字段降级为 `None`，runner 会显式抛 `EventLevel.WARN` 提示。

#### D. VCP / 阻力位特征（v0.3.0 新增）

候选输入额外携带：

- `atr_10d_pct` / `atr_10d_quantile_in_60d` — ATR 收敛指标
- `bbw_20d` / `bbw_compression_ratio` — Bollinger 宽度收敛
- `high_120d` / `high_250d` / `low_120d` + `dist_to_120d_high_pct` / `dist_to_250d_high_pct` / `is_above_120d_high` / `is_above_250d_high` / `pos_in_120d_range` — 中长期阻力位

#### E. T+N 自动回测闭环（v0.4.0 新增）

`evaluate` 子命令把 `va_anomaly_history` 里**任意历史日期**的命中条目自动展开 T+1/T+3/T+5/T+10 收盘价、5d/10d 窗口 max_close / max_dd_5d，写入 `va_realized_returns`。`data_status` 三态（`pending` / `partial` / `complete`）支持幂等增量；每天补一次即可让历史样本逐步收敛。

#### F. Stats 聚合（v0.4.0 / v0.6.0 增强）

`stats --by` 支持 4 种聚合维度：

- `prediction` — 按 `imminent_launch / watching / not_yet` 分档，看 T+3 均收益、胜率
- `pattern` — 按形态（`breakout / consolidation_break / first_wave / second_leg / unclear`）分组
- `launch_score_bin` — 按 launch_score 4 档（0-40 / 40-60 / 60-80 / 80-100）分桶
- `dimension_scores` — 6 维评分与 `ret_t3` 的 Pearson 相关系数

#### G. 盘中 / 日终模式严格隔离

`--allow-intraday` 是所有运行型动词都接受的开关。开启后报告头会渲染 `⚠ INTRADAY MODE` 横幅，且本次 run 的 `va_runs.is_intraday = TRUE`，避免与日终结果混用。默认（不带该参数）只在 `now ≥ close_after`（默认 18:00）后认可"今日"为 T。

#### H. 全 run 可观测

每次 run 都自动写入：

- `va_runs` — 一行/run（包含 mode / trade_date / status / params_json / summary_json / error）
- `va_events` — N 行/run（每个 step 的 START / FINISH 事件 + 失败堆栈）
- `~/.deeptrade/reports/<run_id>/` — Markdown 摘要 + JSON 明细 + `llm_calls.jsonl`（analyze 模式）

---

## 二、典型工作流

### 2.1 每日例行（推荐 cron / 手工三步）

```bash
# ① 收盘后筛选异动 → 写入待追踪池
deeptrade volume-anomaly screen

# ② 让 LLM 对当前 watchlist 做主升浪启动预测
deeptrade volume-anomaly analyze

# ③ 周末 / 月末做一次清理（可选）
deeptrade volume-anomaly prune --days 30
```

### 2.2 实证回测（增量补样本）

```bash
# 把过去 30 天命中过的标的全量算 T+N
deeptrade volume-anomaly evaluate --lookback-days 30

# 看 LLM 预测在 T+3 上的胜率分布
deeptrade volume-anomaly stats --by prediction

# 看 6 维评分与 T+3 收益的相关性
deeptrade volume-anomaly stats --by dimension_scores --from 20260101
```

### 2.3 盘中观察（仅供参考，不做日终决策）

```bash
deeptrade volume-anomaly screen  --allow-intraday --force-sync
deeptrade volume-anomaly analyze --allow-intraday --force-sync
```

---

## 三、命令与选项详解

> **统一前缀**：所有插件命令都通过 `deeptrade volume-anomaly <subcommand> ...` 调用。
> **统一帮助**：任意子命令加 `--help` 由插件自身渲染，不经框架。

### 3.1 `screen` — 异动筛选

**用途**：按本地规则扫描主板，所有命中标的 upsert 进 `va_watchlist`，并 append 到审计表 `va_anomaly_history`。**无 LLM 调用**。

```
deeptrade volume-anomaly screen [OPTIONS]
```

#### 选项

| 选项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `--trade-date <YYYYMMDD>` | str | `None`（自动取最近已收盘交易日） | 显式指定要扫描的交易日 T。**未提供且非盘中模式且当前时间 < 18:00** 时自动回退到 `pretrade(today)` |
| `--allow-intraday` | flag | False | 允许扫描"今日"未收盘数据（盘中模式）。报告会渲染 `⚠ INTRADAY MODE` 横幅；`va_runs.is_intraday=true` |
| `--force-sync` | flag | False | 强制忽略 tushare 本地缓存，重新拉取所有 API 调用。一般不需要；缓存怀疑被污染时使用 |

#### 输出

- 终端：漏斗摘要（每一关剩余数量）+ 命中表（`Code/Name/Industry/Pct%/Body/Turn%/VolRatio5d/...`）。
- 报告目录：`~/.deeptrade/reports/<run_id>/`
  - `summary.md` — 完整 Markdown 报告（含筛选阈值、漏斗、数据完整性诊断、命中明细、被排除候选）
  - `screen_hits.json` — 本次命中的明细数组
  - `screen_stats.json` — 漏斗各阶段计数 + rules 快照 + diagnostics

#### 退出码

- `0` — `success` / `partial_failed`
- `1` — `failed` / `cancelled`

#### 使用示例

```bash
# 例 1：默认扫描最近已收盘交易日（最常见）
deeptrade volume-anomaly screen

# 例 2：补扫某个历史交易日（比如周一忘了跑）
deeptrade volume-anomaly screen --trade-date 20260506

# 例 3：盘中扫描（盘中调试用，不可与日终混用）
deeptrade volume-anomaly screen --allow-intraday

# 例 4：怀疑缓存污染（如 tushare 修订了某天的数据），强制重拉
deeptrade volume-anomaly screen --trade-date 20260506 --force-sync
```

#### 漏斗解读示例

```
funnel: 3175 → 2989 → 142 → 138 → 31 → 8
        │       │       │      │      │     │
        │       │       │      │      │     └─ 量价规则（最终命中）
        │       │       │      │      └─ 换手率分桶
        │       │       │      └─ 上影线过滤
        │       │       └─ T 日阳线 + 涨幅 + 实体占比
        │       └─ 排除 ST + 停牌
        └─ 主板池
```

---

### 3.2 `analyze` — LLM 走势分析

**用途**：读取 `va_watchlist` 的当前快照，为每只候选装配上下文（250d OHLCV / moneyflow 5d / 沪深 300 alpha / 板块强度 / VCP-阻力位特征），分批调用 LLM 输出**主升浪启动预测**与 6 维评分。

```
deeptrade volume-anomaly analyze [OPTIONS]
```

#### 选项

| 选项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `--trade-date <YYYYMMDD>` | str | 同 `screen` | 显式指定 T，决定 `next_trade_date`、历史窗口右端 |
| `--allow-intraday` | flag | False | 同 `screen` |
| `--force-sync` | flag | False | 同 `screen` |

> 默认使用全局 `app.profile`（`fast` / `balanced` / `quality`）解析 LLM stage profile。`balanced` / `quality` 默认开启 `thinking=True, reasoning_effort=high`；`fast` 关闭 thinking。

#### 输出

- 终端：三档 `imminent_launch / watching / not_yet` 候选表，含紧凑 `W/P/C/S/H/R` 6 维评分。
- 报告目录：`~/.deeptrade/reports/<run_id>/`
  - `summary.md` — 三档表 + 市场背景 + 风险提示
  - `analyze_predictions.json` — 每只候选的完整 LLM 输出（含 `dimension_scores` / `key_evidence` / `next_session_watch` / `invalidation_triggers` / `risk_flags`）
  - `data_snapshot.json` — 入参快照（候选清单 + market_summary + sector_strength + data_unavailable）
  - `llm_calls.jsonl` — 本次所有 LLM 调用元数据（含 token / latency / validation_status）

#### 单批 / 多批

- 默认按 token 预算（输入 200K + 输出参考 stage profile.max_output_tokens）自动切批。
- 多批失败：失败批会写入 `va_runs.summary_json` 与 banner 中的 `失败批次` 列表；不影响其他批的成功结果（terminal_status = `partial_failed`）。

#### 数据降级提示

- `index_daily`（沪深 300）权限缺失：报告 `data_unavailable` 段提示，runner emit 一条 `EventLevel.WARN` 日志：`alpha 字段降级为 None；如需启用 alpha，请确认 Tushare 账户已开通 index_daily 权限`。
- `limit_cpt_list` 缺失：板块强度自动 fallback 到 `industry_fallback`（按 watchlist 行业聚合），prompt 中明确声明 `sector_strength_source`，让 LLM 自动调低置信度。

#### 退出码

- `0` — `success` / `partial_failed`
- `1` — `failed` / `cancelled`

#### 使用示例

```bash
# 例 1：例行收盘后分析（紧跟 screen 之后）
deeptrade volume-anomaly analyze

# 例 2：补分析某个历史交易日（前提：当时 watchlist 状态尚未 prune 掉）
deeptrade volume-anomaly analyze --trade-date 20260506

# 例 3：盘中观察（仅参考；报告会标 INTRADAY 横幅）
deeptrade volume-anomaly analyze --allow-intraday

# 例 4：怀疑昨日 daily 缓存有问题，强制重拉再分析
deeptrade volume-anomaly analyze --force-sync
```

---

### 3.3 `prune` — 剔除老旧追踪标的

**用途**：从 `va_watchlist` 中删除入池日期 `tracked_since` 距今 ≥ N 个日历日的标的。**不影响 `va_anomaly_history`**（审计表保留全量）。

```
deeptrade volume-anomaly prune [OPTIONS]
```

#### 选项

| 选项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `--days <int>` | int | `30` | 阈值：剔除追踪 ≥ N 日历日（**注意是日历日，不是交易日**）的标的。`0` 表示清空整个 watchlist |
| `--trade-date <YYYYMMDD>` | str | `None` | 显式指定"今日"（影响 calendar-day 计算） |
| `--allow-intraday` | flag | False | 同前 |

#### 输出

- 终端：剔除数量 + 剩余池规模 + 被剔除标的表（`Code/Name/Industry/Tracked Since/已追踪/Last Screened`）。
- 报告目录：
  - `summary.md`
  - `pruned_codes.json` — 被剔除标的明细

#### 退出码

- `0` — 成功；其它 → 异常

#### 使用示例

```bash
# 例 1：默认（剔除追踪满 30 日历日的标的）
deeptrade volume-anomaly prune

# 例 2：更激进，10 日就剔除（适合短线策略）
deeptrade volume-anomaly prune --days 10

# 例 3：清空整个 watchlist（重置策略状态，谨慎使用）
deeptrade volume-anomaly prune --days 0

# 例 4：基于某个具体"今日"做剔除（用于回放）
deeptrade volume-anomaly prune --days 30 --trade-date 20260430
```

---

### 3.4 `evaluate` — T+N 实际收益评估

**用途**：对 `va_anomaly_history` 中过去命中条目，自动拉取 T+1 / T+3 / T+5 / T+10 各 horizon 收盘价 + 5d/10d 窗口 max_close / max_dd，计算实际收益指标，写入 `va_realized_returns`。**幂等**：默认跳过 `data_status='complete'` 行。

```
deeptrade volume-anomaly evaluate [OPTIONS]
```

#### 选项

| 选项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `--lookback-days <int>` | int | `30` | 仅评估 `anomaly_date` 在过去 N 个日历日内的 hit。**典型用法：每天跑一次 `--lookback-days 30`，让滚动窗口内的样本逐步收敛到 `complete`** |
| `--trade-date <YYYYMMDD>` | str | `None` | 显式指定"今日"（决定哪些 horizon 已到达） |
| `--backfill-all` | flag | False | **覆盖** `--lookback-days`，对历史全量重算（`lookback = 365×10`）。一次性补齐用 |
| `--force-recompute` | flag | False | 重新评估已 `data_status='complete'` 的行（用于修正过去的错值或 schema 升级）|
| `--force-sync` | flag | False | 强制忽略 tushare 缓存重拉 |
| `--allow-intraday` | flag | False | 盘中模式（罕见——只影响今日 T+1 是否纳入） |

#### `data_status` 三态

| 状态 | 含义 | 下次 evaluate 行为 |
|---|---|---|
| `pending` | 连 T+1 都还在未来 | 重算 |
| `partial` | max_horizon (T+10) 未到 today，或某个已到的 horizon 因停牌缺数据 | 重算 |
| `complete` | T+10 已到 today **且**所有 horizon 都拉到收盘 | 默认跳过（除非 `--force-recompute`）|

#### 输出

- 终端：状态分布（`complete / partial / pending` 各多少条）。
- 报告目录：
  - `summary.md`
  - `evaluate_summary.json`

#### 使用示例

```bash
# 例 1：日常增量（每天 cron 跑一次）
deeptrade volume-anomaly evaluate --lookback-days 30

# 例 2：首次启用本功能，对过去半年 hit 全量补样本
deeptrade volume-anomaly evaluate --backfill-all

# 例 3：发现历史 evaluate 算错了（如曾经用错的 t_close），强制重算最近 60 天
deeptrade volume-anomaly evaluate --lookback-days 60 --force-recompute

# 例 4：基于某个特定"今日"做评估（用于复现）
deeptrade volume-anomaly evaluate --trade-date 20260430 --lookback-days 90
```

---

### 3.5 `stats` — 实证统计聚合

**用途**：在 `va_stage_results × va_realized_returns` JOIN 上做聚合查询，输出 LLM 预测在不同维度下的事后表现。**纯只读，不拉取数据**——必须先有 `analyze` 的预测样本与 `evaluate` 的实际收益样本。

```
deeptrade volume-anomaly stats [OPTIONS]
```

#### 选项

| 选项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `--from <YYYYMMDD>` | str | `None` | `anomaly_date` 下界（含）|
| `--to <YYYYMMDD>` | str | `None` | `anomaly_date` 上界（含）|
| `--by <key>` | str | `prediction` | 聚合维度：`prediction` / `pattern` / `launch_score_bin` / `dimension_scores` |

#### `--by` 取值详解

| 取值 | 含义 | 输出列 |
|---|---|---|
| `prediction` | 按 LLM 三档（`imminent_launch / watching / not_yet`）分组 | bucket / 样本数 / T+3 均收益 / T+3 胜率 / T+5 最大涨幅均 |
| `pattern` | 按形态分组（`breakout` / `consolidation_break` / `first_wave` / `second_leg` / `unclear`）| 同上 |
| `launch_score_bin` | 按 launch_score 4 档（`0-40` / `40-60` / `60-80` / `80-100`）分桶 | 同上 |
| `dimension_scores` | 6 维评分（`washout / pattern / capital / sector / historical / risk`）与 `ret_t3` 的 Pearson 相关 | bucket / 样本数 / Pearson r（占用 `T+3 均收益` 列）/ — / T+5 最大涨幅均 |

> v0.6.0：`dimension_scores` 在 `va_stage_results` 拆 6 列（`dim_washout` / `dim_pattern` / `dim_capital` / `dim_sector` / `dim_historical` / `dim_risk`）持久化，故 Pearson 在 SQL 层用 DuckDB 内置 `CORR(...)` 直接算，无需扫描 JSON。

#### 使用示例

```bash
# 例 1：看 LLM 三档预测的整体胜率（最常用）
deeptrade volume-anomaly stats --by prediction

# 例 2：看不同形态的预测准度
deeptrade volume-anomaly stats --by pattern

# 例 3：看 launch_score 与实际收益的单调性（应当：高分档胜率 > 低分档）
deeptrade volume-anomaly stats --by launch_score_bin

# 例 4：看哪些维度对 T+3 收益最有解释力（Pearson 相关）
deeptrade volume-anomaly stats --by dimension_scores

# 例 5：限定时间窗（例如只看 v0.6.0 上线后的样本）
deeptrade volume-anomaly stats --from 20260501 --to 20260531 --by prediction

# 例 6：只看上界（早期样本会逐步过期）
deeptrade volume-anomaly stats --to 20260101 --by prediction
```

#### 解读建议

- `prediction = imminent_launch` 这一档的 T+3 胜率应显著高于 `not_yet`，否则说明 LLM 校准失效，需要回顾 prompt。
- `dimension_scores --by` 中 Pearson |r| < 0.05 的维度参考价值低；`risk` 应是**负相关**（高风险分 → 低收益）。
- 样本数 `n_samples < 30` 时各项统计量极易被噪声主导，谨慎下结论。

---

### 3.6 `history` — 历次 run 列表

**用途**：列出最近 N 个 run（任意 mode），用于回看 / 排查。

```
deeptrade volume-anomaly history [OPTIONS]
```

#### 选项

| 选项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `--limit <int>` | int | `20` | 最多列出多少条（按 `started_at` 倒序）|

#### 输出

每行一个 run：

```
<run_id>  <mode>  <trade_date>  <status>  <started_at> → <finished_at>
```

#### 使用示例

```bash
# 例 1：默认（最近 20 个 run）
deeptrade volume-anomaly history

# 例 2：只看最近 5 个
deeptrade volume-anomaly history --limit 5

# 例 3：看完整历史（结合 grep 过滤 mode）
deeptrade volume-anomaly history --limit 200 | grep analyze
```

---

### 3.7 `report` — 重新渲染报告

**用途**：根据 `run_id` 重新输出某个历史 run 的报告——默认终端紧凑摘要，`--full` 渲染完整 Markdown。

```
deeptrade volume-anomaly report <RUN_ID> [OPTIONS]
```

#### 参数 / 选项

| 名 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `RUN_ID`（位置参数） | str | — | 来自 `history` 列表的 `run_id` |
| `--full` | flag | False | 渲染完整 Markdown（包含 `summary.md` 全文） |

#### 退出码

- `0` — 成功
- `2` — 找不到对应的 `summary.md` 文件

#### 使用示例

```bash
# 例 1：终端紧凑摘要（推荐日常用，速度快）
deeptrade volume-anomaly report 4f3a2b1c-...

# 例 2：完整 Markdown（含数据完整性诊断 / 漏斗 / 命中明细）
deeptrade volume-anomaly report 4f3a2b1c-... --full

# 例 3：组合 history + report 看最近一次 analyze 完整结果
RUN_ID=$(deeptrade volume-anomaly history --limit 1 | awk '{print $1}')
deeptrade volume-anomaly report "$RUN_ID" --full
```

---

## 四、安装与升级

### 4.1 安装

```bash
deeptrade plugin install ./deeptrade/strategies_builtin/volume_anomaly -y
```

安装时会按 `deeptrade_plugin.yaml` 中的 `migrations` 顺序应用：

- `20260430_001_init.sql` — 创建 `va_watchlist` / `va_anomaly_history` / `va_stage_results` / `va_runs` / `va_events`
- `20260601_001_realized_returns.sql` — 创建 `va_realized_returns`（v0.4.0 引入）
- `20260601_002_dimension_scores.sql` — 给 `va_stage_results` 增加 6 个 `dim_*` 列（v0.6.0 引入）

### 4.2 升级

```bash
deeptrade plugin upgrade ./deeptrade/strategies_builtin/volume_anomaly
```

只会增量应用新增的 migration，已有数据不会丢失。**v0.6.0 升级注意**：旧 LLM 响应（无 `dimension_scores`）不再可被新 schema 解析；但 `va_stage_results.raw_response_json` 中的历史 JSON 仍完整保留，仅新写入 require 新 schema。

### 4.3 卸载

```bash
# 默认：仅 disable，保留所有数据
deeptrade plugin uninstall volume-anomaly

# 同时 DROP 所有 va_* 表（不可恢复！）
deeptrade plugin uninstall volume-anomaly --purge -y
```

---

## 五、数据需求与权限

### 5.1 必需 Tushare API（任一缺失则 run 终止）

| API | 用途 |
|---|---|
| `stock_basic` | 主板池过滤、行业、上市状态 |
| `trade_cal` | 计算 T / T+1 / 历史窗口 |
| `daily` | 价格 / 涨跌幅 / vol / amount（T 日 + 历史窗口） |
| `daily_basic` | turnover_rate / circ_mv / pe / pb |
| `stock_st` | 排除 ST / *ST |

### 5.2 可选 API（缺失则降级 + 报告提示）

| API | 用途 | 缺失影响 |
|---|---|---|
| `suspend_d` | 停牌排除 | 只能依靠 `daily` 是否返回判定，`screen` 在 `data_unavailable` 段标注 |
| `moneyflow` | 5 日资金流（analyze） | candidate 的 `moneyflow_5d_summary.rows_used = 0` |
| `limit_list_d` | 60 日内涨停标记（analyze） | candidate 的 `prior_limit_up_count_60d = 0`、`days_since_last_limit_up = None` |
| `limit_cpt_list` | 板块强度 tier 1 | 自动 fallback 到 `industry_fallback`，prompt 中显式声明 |
| `top_list` | 龙虎榜（暂未消费，预留） | — |
| `anns_d` | 公告（暂未消费，预留） | — |
| `adj_factor` | 复权调整（screen 量价比对） | `vol_adjust` 自动降级为"raw vol"，diagnostics 区段标注 `degraded`|
| `index_daily` | 沪深 300 alpha（analyze） | `alpha_*_pct = None`，runner emit `EventLevel.WARN` 日志 |

### 5.3 LLM 配置

`analyze` 需要至少一个可用的 LLM provider（`deeptrade config set-llm` 配置 / `deeptrade config list-llm` 查看）。挑选哪个 provider 由插件 runtime 的 `pick_llm_provider` 决定（一般取 default）。

---

## 六、产物与持久化

### 6.1 数据库表（DuckDB，`~/.deeptrade/deeptrade.duckdb`）

| 表 | PK | 说明 | `--purge` 删除? |
|---|---|---|:---:|
| `va_watchlist` | `ts_code` | 当前待追踪标的池（一只一行）| ✔ |
| `va_anomaly_history` | `(trade_date, ts_code)` | 历次 screen 命中明细（审计 / 评估用） | ✔ |
| `va_stage_results` | `(run_id, stage, ts_code)` | LLM analyze 结构化结果（含 6 维评分拆列）| ✔ |
| `va_runs` | `run_id` | 本插件 run 历史（替代框架 `strategy_runs`）| ✔ |
| `va_events` | `(run_id, seq)` | 本插件 run 事件流（替代框架 `strategy_events`）| ✔ |
| `va_realized_returns` | `(anomaly_date, ts_code)` | T+N 实际收益指标 | ✔ |

### 6.2 报告目录

每个 run 会创建 `~/.deeptrade/reports/<run_id>/`：

```
<run_id>/
├── summary.md                  # 主报告（人读）
├── llm_calls.jsonl             # LLM 调用元数据（含 token / latency / 校验状态）
├── screen_hits.json            # screen 模式
├── screen_stats.json           # screen 模式
├── analyze_predictions.json    # analyze 模式
├── data_snapshot.json          # analyze 模式（输入快照）
├── pruned_codes.json           # prune 模式
└── evaluate_summary.json       # evaluate 模式
```

---

## 七、常见问题

### Q1. `screen` 命中数为 0，怎么排查？

按报告 `## 数据完整性诊断` 区段从上到下看：

1. `stock_st(T) ST 标的数 = 0` → ⚠ 可能是 tushare 当日 ST 接口返回异常，建议加 `--force-sync` 重跑。
2. `主板覆盖率 < 95%` → daily / daily_basic 数据不全；可能是盘中调用尚未结束。
3. `vol_adjust = degraded` → `adj_factor` 大量缺失，量价规则在送转日附近会失效；考虑临时关闭 vol_adjust（暂未暴露 CLI 选项，需改 ScreenRules）。
4. 漏斗"实体≥0.6 + 涨幅 5-8%"剩余非常少 → 当日没有窄涨阳线题材，是市场原因，非数据问题。

### Q2. `analyze` 报错 `partial_failed`，是结果可用吗？

报告头会渲染 `🚨 PARTIAL — 本次结果不完整，不可作为有效筛选结果` 横幅，并列出失败的 batch ID。**应当避免**直接使用本次输出做决策——重跑或放弃本次。失败原因详见 `llm_calls.jsonl`（一般是 `validation_failed` / `transport_error` / `set_mismatch_after_retry`）。

### Q3. `evaluate` 第一次跑完，几乎全是 `partial`，正常吗？

正常。`partial` 表示 max_horizon (T+10) 未到 today。每天 cron 跑一次 `evaluate --lookback-days 30`，10 个交易日后这些行就会自然变成 `complete`。**不要**用 `--force-recompute` 反复刷已 `partial` 的行——下次 evaluate 默认就会重算它们。

### Q4. 想观察某只标的为什么没被 LLM 标 `imminent_launch`？

打开 `~/.deeptrade/reports/<analyze-run-id>/analyze_predictions.json`，找到对应 `ts_code`，关注：

- `dimension_scores.washout` 偏低 → LLM 认为洗盘未充分
- `dimension_scores.risk` 偏高 → 风险信号过强（结合 `risk_flags` 列表看具体哪些）
- `key_evidence` 列表 → LLM 引用了哪些字段做判定
- `invalidation_triggers` → LLM 给出的"放弃信号"列表，未来观察这些指标

### Q5. 报告里 `sector_strength_source = industry_fallback` 是不是意味着结果不可靠？

意味着 `limit_cpt_list`（同花顺涨停概念排行）当日不可用，板块强度退化到"按 watchlist 行业聚合"。LLM 在 prompt 中已被显式告知该 source，会自动降低 `confidence`。可信度排序：`limit_cpt_list > industry_fallback`。

### Q6. 我自己的策略想复用 `va_anomaly_history` 表，可以吗？

可以但**不推荐**。表所有权属本插件，`deeptrade plugin uninstall volume-anomaly --purge` 会一并 DROP。如有跨插件依赖需求，建议自己的插件单独维护一份镜像表。

---

*免责声明：本插件仅用于策略研究、数据整理与候选标的分析，**不构成投资建议**，**不进行自动交易**。所有 LLM 输出基于提交的结构化数据，不引用任何外部信息源；用户应自行核验候选标的的最新状态后再做决策。*
