# 成交量异动策略 · 波次 2 详细设计与迭代计划

> 本文档承接 `docs/volume_anomaly_optimization_review.md` 的"波次 2（P1）"三项优化，给出可执行的工程化设计与开发迭代计划。
>
> **状态**：设计稿 v3（定稿）。F1–F15 + G1–G12 全部已定，PR-4/5/6 checklist 全部就绪。**实现暂缓**——按用户安排，等待整体节奏指令后统一进入实现阶段。
> **前置依赖**：波次 1（v3 定稿）将先完成实现并合并；波次 2 部分字段（如 `dimension_scores` 中引用波次 1 新增的 `atr_10d_quantile_in_60d` / `dist_to_250d_high_pct` / `upper_shadow_ratio`）依赖波次 1 已落地。

## 文档修订记录

- **v1**（初稿）：3 项设计 + 3 PR 切分 + 15 项关键决策点（F1–F15）
- **v2**：F1–F15 定稿；新增 §4b 提出 12 个实现层面细节（G1–G12）；按 G6 修订 §2.2 migration SQL（拆 6 列）；按 G2/G3/G5/G8/G10 修订 §1.2 / §3.2 内部描述
- **v3**（当前）：G1–G12 全部按倾向定稿（§4b 表格更新）；PR-4/5/6 checklist 终版固化

---

## 0. 设计目标与约束

### 0.1 本波次目标

| # | 优化项 | 类别 | 触达层 | 战略权重 |
|---|--------|------|--------|----------|
| P1-1 | RPS / 大盘相对 alpha | LLM 输入特征 | `data.py::collect_analyze_bundle` | 信号差异化 |
| P1-2 | 显式分维度评分 | LLM Schema + Prompt | `schemas.py` + `prompts.py` | 可解释性 |
| P1-3 | T+N 自动回测闭环 | **新基础设施** | 新表 + 新 CLI 子命令 | **后续 ML/RAG 演进的前置基础设施** |

P1-1 与 P1-2 是当期信号质量提升；**P1-3 是战略基建**——本波次的最高战略权重项。它本身不直接提升当期信号，但是后续所有 ML/RAG/Multi-Agent 演进的数据闭环底座（评估文档第 8 节"可证伪性"中的"事实评估能力"亦由它承担）。

### 0.2 工程原则（与既往偏好对齐）

1. **不引入新的 framework concept**：所有改动落在 `volume-anomaly` 插件内；P1-3 的新表走插件自有 `va_*` 前缀（与现有 `va_watchlist` / `va_anomaly_history` 同 Plan A 纯隔离层级，不复用框架级表）。
2. **向后兼容 schema 变化**：P1-2 的 `dimension_scores` 字段在 `VATrendCandidate` 中作为**可选字段**加入；旧 LLM 响应仍可解析；下游消费者按字段存在性判断。
3. **数据成本可控**：
   - P1-1 新增 1 类 API 调用（`index_daily`），单次 60–250d 窗口，cache 友好
   - P1-3 在已有的 `_fetch_daily_history_by_date` 之上做 T+N 拉取，复用 cache
4. **Tushare 权限补全**：本波次需要在 `deeptrade_plugin.yaml::permissions.tushare_apis` 中新增 `index_daily`（推荐放 optional，因为可降级——P1-1 失败时 alpha 字段返 None 不阻塞 analyze）。

### 0.3 显式不做什么

- ❌ 不做"行业相对 alpha"（设计上有 F2 决策点，初步倾向暂缓——单独 PR 视效果再加）
- ❌ 不做调度器集成（cron / daemon）—— P1-3 的 evaluate 命令仅作为 CLI 子命令；调度由用户外部配置（如 Linux cron / Windows Task Scheduler / 或框架未来支持的 schedule 命令）
- ❌ 不做 LightGBM / Multi-Agent / RAG —— 这些是波次 4，本波次只为它们打地基
- ❌ 不做 LLM `launch_score` 公式重写（dimension_scores 与 launch_score 的关系问题在 F3 中决策）
- ❌ 不做框架层 `tushare_client` 改造，不动 `deeptrade.core` 任何文件

---

## 1. P1-1 · RPS / 大盘相对 alpha

### 1.1 现状

`_build_candidate_row` 当前没有任何相对市场或行业的强度对比指标。LLM 在判断"主升浪启动概率"时，无法区分：
- 大盘上涨 5%、个股涨 6% → 跟随性反弹（弱信号）
- 大盘下跌 1%、个股涨 6% → 抗跌强势（强信号）

这两种行情的"放量异动"含义完全不同，但当前模型只能看到 `pct_chg`，无法推断市场背景。

### 1.2 设计

#### 数据需求

新增一次 Tushare `index_daily` 调用，拉取沪深 300（`000300.SH`）过去 250 个交易日 daily（与波次 1 P0-4 的 250d 窗口对齐）。

**注意**：`index_daily` 的 cache 语义同 `daily`——`trade_day_immutable`，跨 run 完全共享，所以稳态零增量调用。

#### 输出字段（候选行）

```python
"alpha_5d_pct":           float | None,  # 个股 5 日累计收益 - baseline 5 日累计收益
"alpha_20d_pct":          float | None,  # 同上 20d（中期相对强度）
"alpha_60d_pct":          float | None,  # 同上 60d（长期相对强度）
"baseline_index_code":    str,            # "000300.SH" — 让 LLM 知道 baseline 是哪个
"rel_strength_label":     Literal["leading", "in_line", "lagging"],
                                          # leading: alpha_20d > +5%
                                          # lagging: alpha_20d < -5%
                                          # in_line: 介于 ±5% 之间
                                          # alpha_20d 缺失时不输出该字段
```

#### 计算公式

```
ret_n_pct = (close_T / close_T-n - 1) × 100
alpha_n_pct = stock_ret_n_pct - baseline_ret_n_pct
```

baseline 用对数收益 vs 简单收益的差异在 60 日尺度上不显著，本波次用**简单收益**（与现有 `pct_chg_60d` 计算口径一致）。

#### 配置

```python
def collect_analyze_bundle(
    *,
    ...,
    baseline_index_code: str = "000300.SH",
    ...,
) -> AnalyzeBundle:
```

不放进 `ScreenRules`——这是 analyze 阶段的特征，不参与筛选。

#### 失败兜底

- `index_daily` 不可用（权限缺失 / API error）→ 三个 `alpha_*` 字段返 `None`、`rel_strength_label` 不输出，写入 `bundle.data_unavailable`，**同时 emit 一条 `EventLevel.WARN` 的 LOG 事件**（G8）：`"index_daily 不可用 (alpha 字段降级为 None)；如需启用，请确认 Tushare 账户已开通 index_daily 权限"`
- baseline 历史不足（< 60d）→ 缺失尺度的 alpha 返 None
- 个股历史不足（< 60d）→ 同上

#### LLM Prompt 适配（与 P1-2 / P0-5 协同）

在 `VA_TREND_SYSTEM` 的判断维度中：
- 维度 D（板块强度）扩展为 "**板块与市场相对强度**"，增加对 `alpha_*_pct` / `rel_strength_label` 的判断指引
- few-shot 示例（PR-3 已合并的 `prompts_examples.py`）补充示例引用 `alpha_20d_pct`

### 1.3 兼容性

- 完全向后兼容（新增字段，旧消费者不受影响）
- token 增量：每候选 +30–40 tokens

### 1.4 单测要点

- baseline 上涨 10%、个股上涨 15% → `alpha_20d_pct ≈ 5`、`rel_strength_label = "in_line"`（5% 边界，需明确）
- baseline 下跌 5%、个股上涨 5% → `alpha_20d_pct ≈ 10`、`rel_strength_label = "leading"`
- 个股历史不足 → `alpha_20d_pct = None`、`rel_strength_label` 不输出
- baseline 完全缺失 → 所有 alpha 字段均 None

---

## 2. P1-2 · 显式分维度评分

### 2.1 现状

`VATrendCandidate` 中 `launch_score` 是单一标量（0–100），LLM 在判断维度（A 洗盘 / B 形态 / C 资金 / D 板块 / E 历史 / F 风险）之间游移、可解释性差。

调试时无法回答"为什么这个标的 launch_score 是 65 而不是 75"，导致后续 prompt 迭代盲目。

### 2.2 设计

#### Schema 改造

`schemas.py` 中新增子模型：

```python
class VADimensionScores(BaseModel):
    """LLM 对各维度的显式打分（0-100）。

    `risk` 是反向维度——分越高代表风险越大；其余维度都是正向。
    `launch_score` 与本子模型并存，由 LLM 自行保证大致一致；
    我们不强制 launch_score = f(dimension_scores)（见 §F3 决策）。
    """
    model_config = ConfigDict(extra="forbid")
    washout: int = Field(ge=0, le=100)      # 洗盘充分度
    pattern: int = Field(ge=0, le=100)      # 形态突破有效性
    capital: int = Field(ge=0, le=100)      # 资金验证（moneyflow / volume）
    sector: int = Field(ge=0, le=100)       # 板块强度 + 大盘相对（合并 P1-1 后）
    historical: int = Field(ge=0, le=100)   # 历史浪型位置（越早越好；二浪起涨 > 二浪末段）
    risk: int = Field(ge=0, le=100)         # 风险（越高越烂；high upper-shadow / late-stage）
```

`VATrendCandidate` 新增字段：

```python
class VATrendCandidate(BaseModel):
    ...
    dimension_scores: VADimensionScores  # P1-2 — 必填字段
```

**做必填还是选填？** 见 §F8 决策。我倾向**必填 + migration**——schema 一致性比向前兼容老旧响应更重要；旧响应已经持久化在 `va_stage_results.raw_response_json` 中，不需要解析。

#### Prompt 改造

`VA_TREND_SYSTEM` 中：

1. 在【输出语义】节后增加：

   > 【dimension_scores 评分尺度】
   > - 0–30：明显不利 / 不充分
   > - 30–60：中性 / 部分满足
   > - 60–80：明显有利 / 较充分
   > - 80–100：教科书级 / 极充分（保留给罕见的极端正例 / 极端风险）
   > 
   > **risk 维度的方向相反**——分越高代表风险越大。

2. 在【判断维度】每个维度（A–F）末尾增加一行 "→ 对应 `dimension_scores.<name>`"。

3. 输出 JSON Schema 部分把 `dimension_scores` 加进示例。

#### Few-Shot 同步更新

`prompts_examples.py` 的两个示例（PR-3 已合并）补充 `dimension_scores`：
- 示例 A（imminent_launch）：`{washout: 80, pattern: 75, capital: 70, sector: 75, historical: 60, risk: 25}`
- 示例 B（not_yet）：`{washout: 30, pattern: 35, capital: 25, sector: 50, historical: 30, risk: 75}`

#### 持久化 schema 变更（v2 — G6 决策：拆 6 列而非单 JSON 列）

`va_stage_results` 表新增 6 个 DOUBLE 列（migration `20260601_002_dimension_scores.sql`）：

```sql
ALTER TABLE va_stage_results ADD COLUMN dim_washout    DOUBLE;
ALTER TABLE va_stage_results ADD COLUMN dim_pattern    DOUBLE;
ALTER TABLE va_stage_results ADD COLUMN dim_capital    DOUBLE;
ALTER TABLE va_stage_results ADD COLUMN dim_sector     DOUBLE;
ALTER TABLE va_stage_results ADD COLUMN dim_historical DOUBLE;
ALTER TABLE va_stage_results ADD COLUMN dim_risk       DOUBLE;
```

`runner.py::_write_stage_results` 中按子字段写入对应列；旧行所有 `dim_*` 列为 `NULL`，stats SQL 用 `WHERE dim_washout IS NOT NULL` 过滤。

**为什么拆列而非 JSON**：`stats --by dimension_scores` 要做 6 个维度与 ret_t3 的 Pearson 相关，纯 SQL 聚合在拆列模式下简洁高效（`AVG((dim_washout - mean) * (ret_t3 - ret_mean))`），JSON_EXTRACT 在 SQL 层昂贵且 DuckDB 跨版本兼容性差。原始 JSON 已在 `raw_response_json` 中保留作为审计数据。

#### 渲染层

`render.py::write_analyze_report` 中，预测表格增加一列 "维度评分（W/P/C/S/H/R）"，紧凑表达：

```
80/75/70/75/60/25
```

完整 dimension_scores JSON 写到详细页，不挤占 summary 行。

### 2.3 兼容性

- LLM Schema 不向后兼容旧响应（看 §F8 决策）
- 现有 `va_stage_results` 行不动（新列 NULL）
- 新插件版本号 bump（见 §F11）

### 2.4 单测要点

- `VADimensionScores`：单字段越界（>100, <0）抛 ValidationError
- `VATrendCandidate`：`dimension_scores` 缺失抛 ValidationError
- prompt 一致性：新增的 `prompts_examples.py` 示例中 dimension_scores 字段存在且每个子字段都在 [0, 100]

---

## 3. P1-3 · T+N 自动回测闭环【基础设施】

### 3.1 现状

- `va_anomaly_history` 表存所有历史 hits（异动筛选命中明细）
- `va_stage_results` 表存所有历史 LLM 预测（含 `prediction` / `pattern` / `launch_score`）
- **缺失**：事后实际收益（T+1 ~ T+N close）未被自动 fetch、未被持久化、未与预测做 join
- **后果**：策略效果只能人工抽样验证；后续 ML/RAG 没有训练数据；评估文档第 8 节"可证伪性"无法落地

### 3.2 设计

#### 新增数据表 `va_realized_returns`

新增 migration `20260601_001_realized_returns.sql`：

```sql
CREATE TABLE IF NOT EXISTS va_realized_returns (
    anomaly_date    VARCHAR NOT NULL,    -- 异动 T 日（YYYYMMDD）
    ts_code         VARCHAR NOT NULL,
    -- T 日基准价（用于计算 ret_*）
    t_close         DOUBLE,
    -- 各 horizon 的事后实际收盘价（单位：元；NULL 表示 horizon 尚未到 / 数据缺失）
    t1_close        DOUBLE,
    t3_close        DOUBLE,
    t5_close        DOUBLE,
    t10_close       DOUBLE,
    -- 对应收益（百分比；ret = (tn_close / t_close - 1) × 100）
    ret_t1          DOUBLE,
    ret_t3          DOUBLE,
    ret_t5          DOUBLE,
    ret_t10         DOUBLE,
    -- 5 日 / 10 日窗口内极值（捕捉真实主升浪幅度，独立于 horizon 端点）
    max_close_5d    DOUBLE,
    max_close_10d   DOUBLE,
    max_ret_5d      DOUBLE,    -- 5 日内最大涨幅（峰值收益）
    max_ret_10d     DOUBLE,
    max_dd_5d       DOUBLE,    -- 5 日内最大回撤：(min(close[T+1..T+5]) - t_close) / t_close × 100；负数或 0（G2 决策口径）
    -- 元数据
    computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    data_status     VARCHAR NOT NULL,    -- 'complete' | 'partial' | 'pending'
    PRIMARY KEY (anomaly_date, ts_code)
);

-- 索引：按 anomaly_date 和 data_status 查询频繁
CREATE INDEX IF NOT EXISTS idx_va_realized_returns_date
    ON va_realized_returns(anomaly_date, data_status);
```

`data_status` 语义（v2 — G5 决策的严格三态）：
- `pending`：T+1 还没到 today（行刚被插入，无任何 close 可填）
- `partial`：T+1 已到 today 但 max_horizon (T+10) **未到 today**，**OR** 任一 horizon 在对应 trade_date 上有数据缺失（停牌等）
- `complete`：max_horizon 已到 today **AND** 所有 4 个 horizon 都成功拉到了 close

这能让 data_status 真实反映"是否还能补数据"——后续 evaluate 重跑时，仅 `partial`（且非停牌缺失永久无法补的）和 `pending` 行需要重算。

#### 新增 CLI 子命令 `evaluate`

`cli.py` 增加 `evaluate` 子命令（与 `screen / analyze / prune` 同级）：

```bash
deeptrade volume-anomaly evaluate [--lookback-days N] [--trade-date YYYYMMDD]
```

**职责**（G3 决策：评估对象覆盖到 `va_anomaly_history` 全集，独立于 analyze 是否运行）：
1. 从 **`va_anomaly_history`** 中找出 `anomaly_date >= today - lookback_days` 的所有 hits（不依赖 `va_stage_results`）
2. 对每条 hit，按 horizon (1, 3, 5, 10 trade days) 计算应有的目标 trade_date
3. fetch 这些 (ts_code, target_date) 对应的 daily close（复用 `_fetch_daily_history_by_date`）
4. 计算 ret_* / max_ret_* / max_dd_* 指标
5. UPSERT 到 `va_realized_returns`（`anomaly_date, ts_code` 已存在则 UPDATE）
6. 写入 evaluate 报告（与 screen/analyze 报告同目录结构）
7. **写 `va_runs` / `va_events`**（G10 决策：`mode='evaluate'`，与 screen/analyze/prune 一致）

**默认参数**：
- `--lookback-days 30`（评估近 30 日的 hits；旧的认为已稳定不重算）
- `--trade-date today`（用于决定"今天最多能算到 T+几"）

**幂等性**：每次运行覆盖 `data_status != 'complete'` 的行；`complete` 行不重算（除非 `--force-recompute`）。

#### 新增 CLI 子命令 `stats`

`cli.py` 增加 `stats` 子命令：

```bash
deeptrade volume-anomaly stats [--from YYYYMMDD] [--to YYYYMMDD] [--by prediction|pattern|launch_score_bin]
```

**职责**：聚合 `va_stage_results` JOIN `va_realized_returns`，输出按维度的命中率统计：

```
按 prediction 维度（2026-04-01 ~ 2026-05-08，N=87）:
                  样本数   T+3 平均收益   T+3 胜率   T+5 最大涨幅平均
imminent_launch     32       +4.8%        62%         +9.3%
watching            41       +1.2%        51%         +5.1%
not_yet             14       -0.5%        43%         +3.2%

按 launch_score_bin 维度（同上）:
80-100              5        +7.2%        80%         +12.5%
60-80               28       +3.1%        58%         +7.8%
40-60               36       +0.5%        47%         +4.2%
0-40                18       -1.3%        38%         +2.1%
```

如果 P1-2 已合并（dimension_scores 已落地），`stats` 进一步支持：

```bash
deeptrade volume-anomaly stats --by dimension_scores
```

输出每个维度评分与 T+3 收益的相关系数（Pearson / Spearman）。

#### 新增 runner 类 `VaEvaluator`

`runner.py` 中新增 `class VaEvaluator`（与 `VaRunner` 平行），承担 evaluate 逻辑：

```python
@dataclass
class EvaluateParams:
    trade_date: str | None = None
    lookback_days: int = 30
    force_recompute: bool = False

class VaEvaluator:
    def __init__(self, rt: VaRuntime) -> None:
        self._rt = rt

    def execute_evaluate(self, params: EvaluateParams) -> RunOutcome:
        return self._drive("evaluate", params, self._iter_evaluate(params))
```

`_iter_evaluate` 内部步骤：
1. 解析 today / trade_calendar
2. 查 `va_anomaly_history` 获取目标 hits
3. 计算每个 horizon 对应的 target_dates（按 `calendar.next_open()`）
4. 批量 fetch daily（按 trade_date 分组，复用 cache）
5. 计算指标 + 写表
6. emit RESULT_PERSISTED

#### 新增报告渲染

`render.py` 新增 `write_evaluate_report(run_id, ...)`：
- 输出每个 hit 的实际收益对照
- 输出本次 evaluate 的 summary（总 hits、新增 complete、新增 partial、跳过 complete）

#### 配置项

```python
@dataclass
class VaConfig:           # 新建（参照 limit_up_board::LubConfig 模式）
    evaluate_horizons: tuple[int, ...] = (1, 3, 5, 10)
    evaluate_default_lookback_days: int = 30
```

但若不希望引入新插件配置表，也可以直接用 module-level 常量（见 §F6 决策）。

#### 与 stats 的关系

`stats` 子命令是**只读查询**，不 fetch 数据、不写表；只读 `va_stage_results` JOIN `va_realized_returns`。因此 stats 命令很快，可以频繁运行。

### 3.3 兼容性

- 新增表，旧用户升级时 migration 自动建表
- 新增 CLI 子命令，旧 CLI 用户不受影响
- 不改 screen / analyze / prune 行为

### 3.4 单测要点

- `_compute_realized_returns(t_close, future_closes)`：纯函数，输入 OHLC 序列 → 输出 ret_* / max_ret_* / max_dd_*
- 边界：T+1 数据缺失（停牌） → ret_t1 = None，data_status = 'partial'
- 边界：T 当天还没到 today + N → 对应 horizon 字段 None，data_status = 'pending'
- evaluate 的幂等性：连续两次 evaluate 同一 trade_date，第二次应跳过 `complete` 行
- stats SQL 正确性：mock va_stage_results + va_realized_returns 测试 JOIN 与聚合

---

## 4. 关键决策点（已定稿 — v2）

| # | 决策点 | 取值 |
|---|--------|------|
| **F1** | RPS baseline 指数 | **`000300.SH` 沪深 300** |
| **F2** | 是否做行业相对 alpha | **不做**，仅大盘 alpha；行业 alpha 留波次 3 视效果再加 |
| **F3** | `dimension_scores` 与 `launch_score` 一致性 | **模型自洽**（prompt 软约束，不强制公式） |
| **F4** | 评估 horizons | **T+1 / T+3 / T+5 / T+10** |
| **F5** | `va_realized_returns` 收益类型 | **仅绝对收益**；相对收益 stats 时 JOIN 计算 |
| **F6** | `evaluate_horizons` 配置存放 | **module-level 常量**，不引入 `va_config` 新表 |
| **F7** | 调度集成 | **仅 CLI**，调度由用户外部配置 |
| **F8** | `dimension_scores` 必填/选填 | **必填**（schema 严格；旧响应已持久化在 `raw_response_json`） |
| **F9** | 行业 baseline 计算 | **跳过**（与 F2 联动） |
| **F10** | RPS alpha 输出窗口 | **5d + 20d + 60d 三档** |
| **F11** | 插件版本号节奏 | **每个 PR 独立 minor bump**（PR-4 → 0.4.0、PR-5 → 0.5.0、PR-6 → 0.6.0；SemVer 1.0 前 minor 可含 schema 变更，与 limit_up_board 节奏一致） |
| **F12** | T+N 评估 backfill 策略 | **默认仅评估近 30 日 + `--backfill-all` 选项** |
| **F13** | `dimension_scores` 维度数 | **6 维（W/P/C/S/H/R）**，不加 liquidity |
| **F14** | Prompt 中 launch_score / dimension_scores 引导 | **软约束**：launch_score 应大致反映各维度综合，不强制公式 |
| **F15** | 验收时间窗口 | **PR 合并后 3 个月**，等 evaluate 累积 ≥ 100 样本 |

定稿后的衍生约束：
- F8 必填 → 必须配套 `prompts_examples.py` 同步更新（PR-6 中），否则 LLM 可能漏字段失败
- F11 每 PR 独立 minor → CHANGELOG.md 顶部需要 3 个独立段（v0.4.0 / v0.5.0 / v0.6.0），每段都要明确列出该 PR 的功能与回滚方式
- F12 `--backfill-all` → 在 `evaluate` 子命令的 argparse 中暴露此标志，文档明确"首次升级后建议跑一次"

---

## 4b. 实现层面细节（已定稿 — v3）

> v2 提出的 12 个实现层面细节已全部按倾向定稿。原选项对照表归档保留以便回溯，但每条已确定取舍。

| # | 决策点 | 取值（v3 定稿） |
|---|--------|------------------|
| **G1** | `index_daily` 拉取窗口 | **250d**（与波次 1 stock daily 长度对称、cache 友好、未来扩展零代价） |
| **G2** | `max_dd_5d` 口径 | **从 T 出发的最大下跌**：`(min(close[T+1..T+5]) - t_close) / t_close × 100`（与 `max_ret_5d` 对称） |
| **G3** | evaluate 评估对象 | **`va_anomaly_history` 全集**；PK = `(anomaly_date, ts_code)`；stats 时再 LEFT JOIN `va_stage_results` |
| **G4** | `launch_score_bin` 默认分箱 | **0-40 / 40-60 / 60-80 / 80-100**（实战分布）；自定义 `--bins` 留 v0.7+ |
| **G5** | `data_status` 三态边界 | **A + 严格补丁**：pending = T+1 未到；partial = max_horizon 未到 **OR** 任一 horizon 数据缺失；complete = max_horizon 已到 **AND** 所有 horizon 拉到 |
| **G6** | `dimension_scores` 持久化 | **拆 6 列 DOUBLE**（`dim_washout / dim_pattern / dim_capital / dim_sector / dim_historical / dim_risk`）；JSON 备份不冗余存 |
| **G7** | CLI 参数对称性 | **不强行对称**：evaluate 用 `--lookback-days`，stats 用 `--from/--to` |
| **G8** | `index_daily` 缺权限提示 | **emit `EventLevel.WARN` LOG**，不静默降级 |
| **G9** | `va_realized_returns.t_close` | **冗余存**（独立可读；防 va_anomaly_history 行被异常删除的边界） |
| **G10** | `evaluate` 写 `va_runs` / `va_events` | **写**（`mode='evaluate'`，与 screen/analyze/prune 一致） |
| **G11** | `stats` 输出格式 | **仅终端表格**；markdown/csv/json 留下游需求出现再加 |
| **G12** | PR-5 是否预埋 `dimension_scores` 占位 | **不预埋**：PR-5 仅 alpha；PR-6 才加 dimension_scores（避免 prompt-schema 失配） |

<details>
<summary>v2 选项对照（点击展开）</summary>

| # | 细节 | 选项 |
|---|------|------|
| **G1** | `index_daily` 的拉取窗口 | A. 250d / B. 仅 60d |
| **G2** | `max_dd_5d` 口径 | A. 从 T 出发的最大下跌 / B. 峰值-后回撤 |
| **G3** | evaluate 评估对象 | A. va_anomaly_history 全集 / B. 仅 va_stage_results |
| **G4** | launch_score_bin 默认分箱 | A. 0-40/40-60/60-80/80-100 / B. 均匀 4 档 / C. 自定义 |
| **G5** | data_status 边界 | A. 严格三态 / B. 只看 max_horizon |
| **G6** | dimension_scores 持久化 | A. 单 JSON 列 / B. 拆 6 列 / C. 同时存 |
| **G7** | CLI 参数对称性 | A. 不对称 / B. 都用 from/to |
| **G8** | index_daily 缺权限提示 | A. 静默降级 / B. emit WARN |
| **G9** | t_close 冗余 | A. 存 / B. 不存 |
| **G10** | evaluate 写 va_runs | A. 写 / B. 不写 |
| **G11** | stats 输出格式 | A. 仅终端 / B. 多格式 |
| **G12** | PR-5 预埋 dimension_scores | A. 不预埋 / B. 预埋 |

</details>

---

## 5. 开发迭代计划（PR 切分）

### 5.1 总览

按战略权重（P1-3 基建优先）+ 风险隔离（schema 改动放最后）排序：

| PR | 范围 | 主要文件 | 依赖 | 预估工作量 |
|----|------|----------|------|------------|
| **PR-4** | P1-3 T+N 自动回测闭环 | 新 migration、新 `evaluate` 子命令、新 `stats` 子命令、新 `runner.VaEvaluator` | 波次 1 全部合并（共享 `_fetch_daily_history_by_date`） | 4–5 人日 |
| **PR-5** | P1-1 RPS / 大盘相对 alpha | `data.py`（bundle + candidate row）、`prompts.py`（维度 D 文字）、`prompts_examples.py`（示例补字段） | 波次 1 全部合并 | 1.5–2 人日 |
| **PR-6** | P1-2 显式分维度评分 | `schemas.py`（新子模型）、`prompts.py`（评分尺度 + 维度对应）、`prompts_examples.py`、`runner.py`（持久化）、新 migration（va_stage_results 加列）、`render.py`（紧凑表达） | PR-5 合并（few-shot 示例同时引用 alpha 和 dimension_scores） | 3 人日 |

**总计**：~9 人日工程量；按 1 人 1 周节奏，约 2 周完成。

### 5.2 PR 之间的并行性

- PR-4（基建）与 PR-5（alpha）**完全独立**，可并行开发
- PR-6（dimension_scores）依赖 PR-5（few-shot 联合更新），需要在 PR-5 合并后再合 PR-6
- 推荐顺序：**PR-4 与 PR-5 并行准备 → 先 merge 哪个看 review 进度 → PR-6 最后**

### 5.3 PR-4 详细 checklist（P1-3 T+N 自动回测闭环）

**新增/修改文件**
- [ ] `deeptrade/strategies_builtin/volume_anomaly/migrations/20260601_001_realized_returns.sql`：新建表 `va_realized_returns`、索引；checksum 写入 `deeptrade_plugin.yaml::migrations`
- [ ] `data.py`：新增纯函数
  - `_compute_realized_returns(t_close, future_closes_by_horizon) -> dict`：返回 ret_t1/t3/t5/t10、max_ret_5d/10d、max_dd_5d
  - `_resolve_horizon_dates(calendar, anomaly_date, horizons) -> dict[int, str]`：每个 horizon 解析为 trade_date
- [ ] `runner.py`：
  - 新增 `EvaluateParams` dataclass
  - 新增 `class VaEvaluator(rt)`，仿 `VaRunner` 模式
  - `_iter_evaluate` 实现：按 lookback 取 hits → 解析 horizon dates → fetch daily → 计算指标 → UPSERT
  - 处理 `force_recompute` 与幂等性（默认跳过 `data_status='complete'` 行）
- [ ] `cli.py`：注册 `evaluate` 子命令（参数：`--lookback-days` / `--trade-date` / `--force-recompute` / `--backfill-all`）
- [ ] `cli.py`：注册 `stats` 子命令（参数：`--from` / `--to` / `--by`）；实现纯只读 SQL 聚合
- [ ] `render.py`：
  - 新增 `write_evaluate_report(run_id, ...)`
  - 新增 `render_stats_table(rows, by)`（终端表格）
- [ ] `runtime.py`：`VaRuntime` 不动；`VaEvaluator` 复用 `tushare` / `db` / `config`
- [ ] `deeptrade_plugin.yaml`：
  - `version: 0.3.0` → `0.4.0`（参考 §F11）
  - `tables` 列表新增 `va_realized_returns`
  - `migrations` 列表新增 `20260601_001_realized_returns.sql` + checksum
- [ ] `CHANGELOG.md`：顶部新增 `## [volume-anomaly v0.4.0] — YYYY-MM-DD — T+N 自动回测闭环`
- [ ] 新单测：`tests/strategies_builtin/volume_anomaly/test_realized_returns.py`
  - `_compute_realized_returns`：完整序列 / T+1 缺失 / T+10 未到
  - `_resolve_horizon_dates`：跨周末、跨节假日
  - 幂等性：mock evaluate 跑两次，第二次跳过 complete 行
- [ ] 新单测：`tests/strategies_builtin/volume_anomaly/test_stats_query.py`
  - mock 表数据 → 验证 by-prediction / by-launch_score_bin SQL 聚合正确性

**验收**
- 新单测全部通过
- 在已有 `va_anomaly_history` 上跑 `evaluate --backfill-all`，所有 30 天前的 hits 都能写出 `data_status='complete'`
- 跑 `stats --by prediction`，输出可读、列对齐

**回滚预案**
- 新表是新增，drop 即可（migration 文件保留以防回滚后再升级）
- evaluate / stats 是新子命令，不影响现有 screen/analyze/prune

---

### 5.4 PR-5 详细 checklist（P1-1 RPS / 大盘相对 alpha）

**新增/修改文件**
- [ ] `deeptrade_plugin.yaml::permissions.tushare_apis.optional`：新增 `index_daily`
- [ ] `data.py::collect_analyze_bundle`：
  - 新增参数 `baseline_index_code: str = "000300.SH"`
  - 新增 fetch 步骤：`tushare.call("index_daily", ts_code=baseline_index_code, start_date=..., end_date=...)`
  - 失败兜底：写入 `bundle.data_unavailable`
  - 把 baseline close 序列传入 `_build_candidate_row`
- [ ] `data.py::_build_candidate_row`：新增计算
  - `alpha_5d_pct` / `alpha_20d_pct` / `alpha_60d_pct`
  - `baseline_index_code`（原样传出）
  - `rel_strength_label`（按 alpha_20d 分档）
- [ ] `prompts.py::VA_TREND_SYSTEM`：维度 D 改为 "**板块与市场相对强度**"，提示文字引用新字段
- [ ] `prompts_examples.py`：示例 A 补 `alpha_20d_pct`、`rel_strength_label`；示例 B 补对应负向值
- [ ] `tests/strategies_builtin/volume_anomaly/test_alpha_features.py`：纯函数测试 alpha 计算 + 边界标签
- [ ] `tests/strategies_builtin/volume_anomaly/test_prompt_consistency.py`：扩展字段一致性 schema 涵盖新增字段
- [ ] `CHANGELOG.md`：合入 v0.4.0 节同段，或单独 v0.4.1（依 PR 顺序，见 §F11）

**验收**
- 新单测全部通过
- 历史 1 个交易日数据上跑一次 analyze，确认 candidate row 中三个 alpha 字段全部输出
- LLM 报告中维度 D 引用 `alpha_*_pct` 至少一次

**回滚预案**
- `index_daily` 不可用时自动降级（alpha 字段返 None）
- 代码 revert 不影响其他 PR

---

### 5.5 PR-6 详细 checklist（P1-2 显式分维度评分）

**新增/修改文件**
- [ ] `deeptrade/strategies_builtin/volume_anomaly/migrations/20260601_002_dimension_scores.sql`：6 条 `ALTER TABLE va_stage_results ADD COLUMN dim_<name> DOUBLE`（washout / pattern / capital / sector / historical / risk）— 见 §2.2 v2 修订
- [ ] `schemas.py`：
  - 新增 `class VADimensionScores(BaseModel)`
  - `class VATrendCandidate` 新增字段 `dimension_scores: VADimensionScores`（必填，§F8-A）
- [ ] `prompts.py::VA_TREND_SYSTEM`：
  - 增加【dimension_scores 评分尺度】节
  - 各维度（A–F）末尾标注 → `dimension_scores.<name>` 对应
  - 输出 schema 示例补 `dimension_scores`
- [ ] `prompts_examples.py`：示例 A / 示例 B 各补 `dimension_scores` 对象
- [ ] `runner.py::_write_stage_results`：将 `dimension_scores` 子字段分别写入 `dim_washout / dim_pattern / dim_capital / dim_sector / dim_historical / dim_risk` 6 列
- [ ] `render.py::write_analyze_report`：summary 表加 "维度 W/P/C/S/H/R" 紧凑列；详细页输出完整 JSON
- [ ] `cli.py::stats`：扩展 `--by dimension_scores`，输出 6 个维度与 ret_t3 的 Pearson 相关
- [ ] `deeptrade_plugin.yaml`：
  - `version`: `0.4.x` → `0.5.0`（schema 变更）
  - `migrations`: 新增 `20260601_002_*` + checksum
- [ ] `CHANGELOG.md`：新增 `## [volume-anomaly v0.5.0]` 段，**明确标注** "VATrendCandidate.dimension_scores 现为必填字段；旧 LLM 响应不再可解析（但持久化到 raw_response_json 不受影响）"
- [ ] `tests/strategies_builtin/volume_anomaly/test_dimension_scores.py`：
  - VADimensionScores 边界（>100 / <0 / 缺失）抛错
  - VATrendCandidate dimension_scores 缺失抛错
  - prompt 一致性：示例中 dimension_scores 字段存在且合法
- [ ] `tests/strategies_builtin/volume_anomaly/test_stats_query.py`：扩展覆盖 `--by dimension_scores`

**验收**
- 新单测全部通过
- 跑一次完整 analyze，确认输出包含 dimension_scores
- evaluate 后跑 `stats --by dimension_scores`，验证相关性表能渲染

**回滚预案**
- schema 改动较大，回滚需要：(a) revert PR-6；(b) 不需要 drop 列（NULL 列对旧逻辑无影响）；(c) 但**已经按新 schema 持久化的行**，旧版本读取时会因 `extra='forbid'` 失败 → 因此 PR-6 一旦合并，**降级路径需要保留 raw_response_json 而忽略 dimension_scores_json**
- 在 `runner._write_stage_results` 中，dimension_scores 同时写到 raw_response_json，确保降级时也有数据可读

---

## 6. 验收指标

### 6.1 工程验收（PR 合并门槛）

- [ ] 三轮 PR 各自单测全绿、CI 通过
- [ ] migration 在 fresh install 与 upgrade 路径都能正常 apply
- [ ] `evaluate` / `stats` CLI 输出可读、列对齐
- [ ] LLM 输出包含 dimension_scores（PR-6 后）
- [ ] CHANGELOG 段落明确列出 schema 变更与 backfill 建议

### 6.2 信号质量验收（合并后 3 个月观察 — §F15-B）

| 指标 | 期望方向 | 数据来源 |
|------|----------|----------|
| `va_realized_returns` 累积 complete 样本 | ≥ 100 行 | DB |
| `imminent_launch` 标的的 T+3 平均收益 | > `watching` > `not_yet` | `stats --by prediction` |
| `launch_score 80-100` 桶的 T+3 胜率 | > `40-60` 桶的胜率（验证 LLM 评分有区分度） | `stats --by launch_score_bin` |
| 各维度评分与 T+3 收益的 Spearman 相关 | `washout / pattern / capital / sector / historical` 为正；`risk` 为负 | `stats --by dimension_scores` |
| `alpha_20d_pct > 5%` 的标的 T+3 胜率 | 高于 `alpha_20d_pct < -5%` 的标的 | 自 SQL |

### 6.3 失效信号

- 累积 3 个月样本，`launch_score 高分桶` 的 T+3 胜率与低分桶**无显著差异** → LLM 评分无效，需要回头优化 prompt
- 任一维度评分与 T+3 收益**反向相关**（如 `washout` 高反而收益低）→ 维度定义有误，需要重新校准 few-shot

---

## 7. 风险与回滚

| 风险 | 触发场景 | 回滚动作 |
|------|----------|----------|
| `index_daily` 权限不足 | 用户 Tushare 等级低 | 已设计降级（alpha 字段返 None）；通知用户 |
| evaluate 长时间运行（首次 backfill） | 历史 hits 多、horizon 长 | `--backfill-all` 仅在用户主动调用时启用；默认 lookback=30 |
| `dimension_scores` 让 LLM 输出复杂度超 token 上限 | 大批次 + 高 thinking | 与现有 `max_output_tokens=32768` 对照；`plan_batches` 中 `avg_out` 上调到 1100 |
| 维度评分校准漂移（不同 LLM 模型给分尺度差异大） | 跨模型迁移 | few-shot 示例锚定；3 个月观察期确认稳定后再做 prompt 微调 |
| stats SQL 在大表下慢 | hits + stage_results 累积超 1 万行 | 加索引（DDL 已含 `idx_va_realized_returns_date`）；进一步可加 `va_stage_results` 的 `(prediction, launch_score)` 索引 |
| schema 变更导致下游消费者破坏 | PR-6 合并 | 在 raw_response_json 中保留完整原始响应（已是现状），下游可降级读取 |

---

## 8. 与波次 3 / 波次 4 的衔接

本波次产出的基础设施直接为后续波次铺路：

- **波次 3 候选项**（`docs/volume_anomaly_optimization_review.md` §P2 列表）：
  - `bull_trap_risk` 结构化字段 / 防守锚点 / 板块动态图谱 / 多日量价堆积模式
  - 这些都不依赖 P1-3 基建，可以独立启动
- **波次 4 战略项**（评估文档 §P3）：
  - **LightGBM 前置打分**：直接消费 `va_realized_returns` 作为 label，消费 `va_stage_results.dimension_scores_json` 作为辅助特征
  - **Multi-Agent 辩论**：基础设施已就绪，prompt 改造工作即可启动
  - **Post-Mortem RAG**：`va_realized_returns` 中失败案例 → 复盘 Agent → 向量库；本波次的 evaluate 命令直接给出"哪些是失败案例"
  - **日内分时特征**：与本波次正交，独立判断

---

## 9. 下一步（v3 定稿 — 实现暂缓）

波次 2 设计已完成 3 轮对齐（v1 → v2 → v3），所有大方向决策（F1–F15）和实现细节（G1–G12）全部固化。

**当前整体状态**：
- ✅ 波次 1 设计稿 v3 定稿（D1–D10 + E1–E8 全确定，PR-1/2/3 checklist 终版就绪）
- ✅ 波次 2 设计稿 v3 定稿（F1–F15 + G1–G12 全确定，PR-4/5/6 checklist 终版就绪）
- ⏸ 实现阶段：**暂缓**，等待用户指令统一启动

**进入实现阶段时的建议顺序**：

```
波次 1：PR-1 (ScreenRules) → PR-2 (候选行特征) → PR-3 (Few-Shot)
              ↓ 全部 merge
波次 2：PR-4 (基建闭环) ‖ PR-5 (RPS alpha) → PR-6 (dimension_scores)
              ↓ 全部 merge
3 个月观察期 → 验收 → 决定是否启动波次 3
```

PR-4 与 PR-5 完全独立可并行（`‖`），PR-6 依赖 PR-5（few-shot 联合更新）。每个 PR 独立 review/merge，独立 minor 版本号 bump。

如有任何点需要返工或新增决策，可直接提出再走一轮 review。否则等待你的指令进入实现。
