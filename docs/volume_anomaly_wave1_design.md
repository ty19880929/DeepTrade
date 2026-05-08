# 成交量异动策略 · 波次 1 详细设计与迭代计划

> 本文档承接 `docs/volume_anomaly_optimization_review.md` 的"波次 1（P0）"五项优化，给出可执行的工程化设计与开发迭代计划。
>
> **状态**：设计稿 v3（定稿）。D1–D10 + E1–E8 全部已定，PR-1/2/3 checklist 全部就绪。**实现暂缓**——按用户安排，先完成波次 2 设计后再统一开始实现。

## 文档修订记录

- **v1**（初稿）：5 项设计 + 3 PR 切分 + 10 项关键决策点（D1–D10）
- **v2**：D1–D10 定稿；新增 §6b 提出 8 个实现层面细节（E1–E8）
- **v3**（当前）：E1–E8 全部按倾向定稿（§6b 表格更新）；PR checklist 终版固化

---

## 0. 设计目标与约束

### 0.1 本波次目标

用 1–2 周的工程量，以**最小风险**把以下 5 项 P0 优化落地：

| # | 优化项 | 类别 | 触达层 |
|---|--------|------|--------|
| P0-1 | 上影线过滤 | Screen 规则 | `data.py::screen_anomalies` |
| P0-2 | 按流通市值分桶换手率 | Screen 规则 | `data.py::screen_anomalies` |
| P0-3 | VCP / ATR / BBW 因子 | LLM 输入特征 | `data.py::_build_candidate_row` |
| P0-4 | 120/250 日阻力位距离 | LLM 输入特征 | `data.py::collect_analyze_bundle` |
| P0-5 | Few-Shot 示例对齐 | LLM Prompt | `prompts.py::VA_TREND_SYSTEM` |

### 0.2 工程原则（与用户既往偏好对齐）

1. **不引入新的 framework concept**：本波次完全在 `volume-anomaly` 插件内完成，不动框架层（`deeptrade.core` / `deeptrade.plugins_api`）。
2. **向后兼容**：所有新配置项有合理默认；用户已有的 `screen_rules` JSON 配置无需修改即可继续工作；启用新行为可以通过开关关闭。
3. **可证伪**：每项变更必须在 `ScreenDiagnostics` 或 `_build_candidate_row` 输出里**显式可观察**——能回答"这条规则今天淘汰了谁、为什么"。
4. **数据成本可控**：
   - P0-1 / P0-2 / P0-3 **零新 API 调用**（数据已在内存中）
   - P0-4 新增 1 类 API 调用（`daily` 长窗口），**复用现有 cache**（`trade_day_immutable`），稳态零增量
   - P0-5 仅增加 prompt token，每批次约 +800–1200 tokens
5. **无新 Tushare 权限**：本波次依赖的 API（`stock_basic / daily / daily_basic / adj_factor`）已在 `deeptrade_plugin.yaml::permissions.tushare_apis` 中声明。

### 0.3 显式不做什么

- ❌ 不动 RPS / 行业相对 alpha（属于波次 2，需新增 `index_daily` 权限）
- ❌ 不改 LLM 输出 schema（保留波次 2 的"显式分维度评分"一并改）
- ❌ 不动 `ScreenRules` 的 `vol_*` 暴量逻辑（波次 1 只在 ScreenRules 上做加法，不动暴量阈值）
- ❌ 不调整 `pct_chg_min/pct_chg_max`（评估意见里建议保守，但本波次先冻结，避免命中率多变量耦合）
- ❌ 不补 `volume_anomaly` 的全套单测（仅为本波次新增逻辑写最小覆盖；存量代码补测放波次 2）

---

## 1. P0-1 · 上影线过滤

### 1.1 现状

`data.py::screen_anomalies` 在 T-day 阶段已计算 `body_ratio`，但**未过滤上影线**——`body_ratio = 0.6` 允许 0–40% 的振幅落在影线上，极端情况下可能是纯上影线（"避雷针"）。

```python
# 现状（data.py L458-467）
daily_t["body"] = daily_t["close"] - daily_t["open"]
daily_t["range"] = (daily_t["high"] - daily_t["low"]).clip(lower=1e-9)
daily_t["body_ratio"] = daily_t["body"] / daily_t["range"]
t_day_hits = daily_t[
    (daily_t["close"] > daily_t["open"])
    & (daily_t["body_ratio"] >= rules.body_ratio_min)
    & (daily_t["pct_chg"] >= rules.pct_chg_min)
    & (daily_t["pct_chg"] <= rules.pct_chg_max)
].copy()
```

### 1.2 设计

#### 数据流变化
增加一列 `upper_shadow_ratio = (high − max(open, close)) / range`，加入过滤链。

#### `ScreenRules` 字段
```python
@dataclass
class ScreenRules:
    ...
    # P0-1 — None 表示关闭过滤（向后兼容）
    upper_shadow_ratio_max: float | None = 0.35
```

- `0.35` = 上影线最多占当日振幅 35%
- `None` 时跳过过滤（用于 A/B 验证或回退）
- `__post_init__` 校验：当不为 None 时，需 `0 < upper_shadow_ratio_max ≤ 1`

#### 诊断字段
```python
@dataclass
class ScreenDiagnostics:
    ...
    # P0-1
    n_after_upper_shadow: int = 0  # T-day 规则之后、turnover 之前的中间值
```

#### `ScreenResult` 字段
新增 `n_after_upper_shadow: int`，与现有 `n_after_t_day_rules` / `n_after_turnover` 对齐，让漏斗渲染层（`render.py::write_screen_report`）能展示这一步的淘汰量。

#### 单 hit 输出
每条 hit 增加 `upper_shadow_ratio: float`，与 `body_ratio` 并列。

### 1.3 向后兼容

- `from_dict()` 中新增字段，缺失时使用默认 0.35
- 已有用户配置（不指定 `upper_shadow_ratio_max`）：自动启用过滤——**这是行为变化**，需要在 release notes 中点出
- 用户若不希望启用：在 `screen_rules` 中显式传 `"upper_shadow_ratio_max": null`

### 1.4 单测要点

- 三组人造数据：纯阳线（影线 0）、避雷针（影线 50%）、合理影线（影线 25%）→ 期望 通过 / 拒绝 / 通过
- `null` 时跳过过滤的回归测试

---

## 2. P0-2 · 按流通市值分桶换手率

### 2.1 现状

固定 `turnover_min=3.0 / turnover_max=10.0`。这导致：
- 大盘股 5% 已经天量，被规则视为正常活跃，造成"伪异动"采样
- 小盘股 10% 是日常震荡，没真正异动也容易超过下限

`daily_basic` 在 `screen_anomalies` 中已被调用，已读取了 `turnover_rate`，但**没读 `circ_mv` / `total_mv`**。

### 2.2 设计

#### `ScreenRules` 字段
```python
@dataclass
class ScreenRules:
    ...
    # P0-2 — 按流通市值分桶的相对换手率阈值
    # 列表元素：(circ_mv_yi_max, turnover_min, turnover_max)
    # circ_mv_yi_max 升序排列；查找时取第一个 ≥ 个股 circ_mv_yi 的桶
    # None 时退化到旧逻辑（turnover_min / turnover_max）
    turnover_buckets: list[tuple[float, float, float]] | None = None
```

#### 默认表（提案）

| 流通市值（亿元） | turnover_min | turnover_max |
|------------------|--------------|--------------|
| ≤ 50（微盘）     | 5.0          | 15.0         |
| 50–200（中小盘） | 3.5          | 12.0         |
| 200–1000（中盘） | 2.5          |  9.0         |
| > 1000（大盘）   | 1.5          |  6.0         |

代码形式：
```python
DEFAULT_TURNOVER_BUCKETS = [
    (50.0,    5.0, 15.0),
    (200.0,   3.5, 12.0),
    (1000.0,  2.5,  9.0),
    (float("inf"), 1.5, 6.0),
]
```

#### 启用方式（决策点见 §6）

两种方案：
- **方案 A（保守，向后兼容）**：默认 `turnover_buckets = None`，沿用旧逻辑；用户在 `screen_rules` 显式开启
- **方案 B（开箱即用，行为变化）**：默认 `turnover_buckets = DEFAULT_TURNOVER_BUCKETS`；用户传 `null` 退回旧逻辑

#### 数据流变化

`daily_basic(T)` 调用结果当前只取 `turnover_rate`，需扩到 `circ_mv`：

```python
# 现状（data.py L483-486）
db_lookup = db_t.set_index("ts_code")["turnover_rate"].to_dict()

# 改造后
db_lookup = db_t.set_index("ts_code")[["turnover_rate", "circ_mv"]].to_dict("index")
# 在过滤循环里按 circ_mv 取对应桶
```

`circ_mv` 单位是"万元"，转成亿元后查桶：`circ_mv_yi = circ_mv_万 / 1e4`。

#### 诊断字段
```python
@dataclass
class ScreenDiagnostics:
    ...
    # P0-2 — 按桶记录命中分布
    turnover_bucket_hits: dict[str, int] = field(default_factory=dict)
    # 例：{"≤50亿": 12, "50-200亿": 5, "200-1000亿": 2, ">1000亿": 0}
    n_missing_circ_mv: int = 0
    circ_mv_missing_codes: list[str] = field(default_factory=list)
```

`circ_mv` 缺失时（虽极少见）应进入 `data_unavailable`，并对该 hit **使用全局 fallback** 阈值 `[turnover_min, turnover_max]`，避免静默淘汰。

#### 单 hit 输出
每条 hit 增加：
- `circ_mv_yi: float`（与 `_build_candidate_row` 输出字段同名同语义，方便下游统一）
- `turnover_bucket: str`（如 `"≤50亿"`）

### 2.3 向后兼容

- 方案 A：完全兼容
- 方案 B：用户旧配置（不传 `turnover_buckets`）会进入分桶模式——**显式行为变化**

### 2.4 单测要点

- 三只虚构股票：30 亿微盘 turnover=14% / 500 亿中盘 turnover=10% / 2000 亿大盘 turnover=5% → 在分桶模式下分别 通过 / 拒绝 / 通过；在旧模式（`turnover_max=10`）下 通过 / 通过 / 通过
- `circ_mv` 缺失时回退到全局阈值的回归测试

---

## 3. P0-3 · VCP / ATR / BBW 因子

### 3.1 现状

`_build_candidate_row` 已经计算了 `base_max_drawdown_pct`（价格收敛）和 `base_vol_shrink_ratio`（量收敛），但缺**波动率收敛**这一第三维度。这是 VCP 理论的关键缺口。

### 3.2 设计

#### 新增字段（输出到候选行 → 喂 LLM）

```python
# 在 _build_candidate_row 中新增计算
"atr_10d_pct":            float | None,  # 当前 10 日 ATR 占当前 close 的百分比
"atr_10d_quantile_in_60d": float | None,  # 0-1，越小代表越收敛
"bbw_20d":                 float | None,  # 当前 20 日 Bollinger Band Width（占均价百分比）
"bbw_compression_ratio":   float | None,  # 当前 BBW / 60 日 BBW 均值（< 1 表示收敛中）
```

#### 计算规则

- **ATR(10)**：经典 Wilder 公式或简单平均都可。本波次用**简单平均**（计算简单、对趋势无明显偏向）：
  ```
  TR_t = max(high_t − low_t, |high_t − close_{t-1}|, |low_t − close_{t-1}|)
  ATR_10_t = mean(TR_{t-9..t})
  ```
- **ATR 序列分位数**：把过去 60 日每日的 `ATR_10` 排序，当前值的分位数（0 = 历史最低，1 = 历史最高）
- **BBW(20)**：
  ```
  MA_20_t = mean(close_{t-19..t})
  STD_20_t = std(close_{t-19..t})
  BBW_t = (4 × STD_20_t) / MA_20_t × 100   # 百分比表示，上下轨各 2σ → 总宽 4σ
  ```
- **`bbw_compression_ratio`**：`BBW_t / mean(BBW over last 60 days)`

#### 数据需求
**零新数据**——所需 `high / low / close` 已在 `daily_by_code[code]` 的 60 日 history 里。

#### 健壮性
- 当 history 不足 60 天时（新股），`atr_10d_quantile_in_60d` 设为 `None`；ATR/BBW 自身只要有足够基础窗口（10 / 20 天）就计算
- 全部 `None` 进入 `missing_data`，由 LLM 显式声明缺失

#### LLM Prompt 配套调整（属于 P0-5 的覆盖范围，但此处提及）
在 `VA_TREND_SYSTEM` 的判断维度 A "是否经过充分洗盘"中点出新字段：

> - 整理期间的波动率是否收敛（`atr_10d_quantile_in_60d` / `bbw_compression_ratio`）；越低越好
> - 三维齐降（价收敛 + 量缩 + 波动率收敛）是 VCP 的教科书形态

### 3.3 向后兼容

完全兼容——只是输出多了字段。Token 增量：每只候选 +4 个标量 ≈ 30-40 tokens。

### 3.4 单测要点

- 单元测试 `_build_candidate_row`：构造一段先剧烈、后收敛的 history → 期望 `bbw_compression_ratio < 1`、`atr_10d_quantile_in_60d` 接近 0
- 反向：先收敛、后剧烈 → 期望 `bbw_compression_ratio > 1`、分位数接近 1
- 短历史（< 20 天）：BBW = None，函数不抛异常

---

## 4. P0-4 · 120/250 日阻力位距离

### 4.1 现状

`_build_candidate_row` 用 60 日 history 算 `high_60d`，但 60 日窗口太短——很多异动股的 60 日新高就是异动当天本身，该字段失去区分能力。

### 4.2 设计

#### 选型：**单独拉一段轻量长窗口**

> ❌ 不扩大现有 60 日 verbatim 窗口（会让 LLM 输入 token 翻 4 倍）
> ✅ 单独跑一段 250 日 daily，**只取 high/close**，算完极值后丢弃，**不喂 LLM**

#### 实现位置
新增 `_fetch_extended_high_lookup(tushare, calendar, trade_date, codes, lookback_days, force_sync)`，返回 `{ts_code: {"high_120d": float, "high_250d": float, "low_120d": float}}`。

参数：`lookback_days=250`（>120 才能同时算两个窗口）。

可复用现有 `_fetch_daily_history_by_date`（已经支持按日批量、cache 友好）。

#### 配置

```python
@dataclass
class ScreenRules:
    ...
    # 注意：仅 analyze 阶段使用，screen 阶段不读
    # 在 collect_analyze_bundle 中作为参数传入
```

不放进 `ScreenRules`——这是 analyze 阶段的特征参数，不参与筛选。改为 `collect_analyze_bundle` 的关键字参数：

```python
def collect_analyze_bundle(
    *,
    ...,
    extended_lookback_trade_days: int = 250,
    ...
) -> AnalyzeBundle:
```

#### 新增字段（候选行）

```python
"high_120d":                float | None,
"high_250d":                float | None,
"low_120d":                 float | None,
"dist_to_120d_high_pct":    float | None,  # (last_close − high_120d) / high_120d × 100，0 = 创新高
"dist_to_250d_high_pct":    float | None,
"is_above_120d_high":       bool,           # last_close > high_120d
"is_above_250d_high":       bool,
"pos_in_120d_range":        float | None,   # (last_close − low_120d) / (high_120d − low_120d)，0 = 区间最低，1 = 最高
```

#### 数据成本

- 新增 1 次 `daily(trade_date=X)` 调用 × 250 天 ≈ 250 次调用
- **但** 与现有 60 日窗口完全重叠（60 ⊂ 250），cache 命中率高；首次冷跑约多 190 次调用，后续稳态零增量
- Tushare RPS 限制下，250 次调用大约多 30 秒（在缓存命中前）

#### 失败兜底

- 任一窗口拉不全（`missing_history_dates` > 阈值）→ 该窗口字段返 `None`，并写入 `bundle.data_unavailable`
- 不阻塞 analyze 主流程

### 4.3 向后兼容

完全兼容。analyze 阶段额外消耗 ≈ 30s 冷启动 + ≈ 60 tokens/候选。

### 4.4 单测要点

- 构造 250 天 history、当前 close 处于 250 日新高位置 → 期望 `is_above_250d_high = True`、`dist_to_250d_high_pct` 略 < 0
- 历史不足（150 天）→ `high_250d = None`、`dist_to_250d_high_pct = None`、`high_120d` 仍可计算

---

## 5. P0-5 · Few-Shot 示例对齐

### 5.1 现状

`VA_TREND_SYSTEM` 只有 schema 规范、没有"判断尺度"的 anchoring。同样输入数据，DeepSeek / Claude / GPT 给出的 `launch_score` 可能差 20+ 分。

### 5.2 设计

#### 在 `prompts.py` 中新增

```python
VA_TREND_FEWSHOT = """\
【参考示例】（仅展示判断尺度与字段引用规范，不是输入的一部分）

示例 A — 教科书式 VCP 缩量充分洗盘后放量突破
{
  "candidate_id": "000XXX.SZ",
  "ts_code": "000XXX.SZ",
  "name": "示例 A",
  "rank": 1,
  "launch_score": 78,
  "confidence": "high",
  "prediction": "imminent_launch",
  "pattern": "breakout",
  "washout_quality": "sufficient",
  "rationale": "整理 24 日，回撤 12%，波动率分位 0.08；T 日放量 2.4 倍站上 MA20；moneyflow 5 日累计净流入；板块为当日主线。三维 VCP 齐降 + 主线共振 → 启动概率高。",
  "key_evidence": [
    {"field": "base_days",                  "value": 24,    "unit": "日",  "interpretation": "整理周期较长，洗盘相对充分"},
    {"field": "base_max_drawdown_pct",      "value": 12.3,  "unit": "%",   "interpretation": "回撤幅度足以清洗短线浮筹"},
    {"field": "atr_10d_quantile_in_60d",    "value": 0.08,  "unit": "无",  "interpretation": "波动率处于近 60 日 8% 分位，VCP 收敛"},
    {"field": "vol_ratio_5d",               "value": 2.4,   "unit": "倍",  "interpretation": "放量倍数确认放量异动"},
    {"field": "dist_to_250d_high_pct",      "value": -3.5,  "unit": "%",   "interpretation": "距 250 日新高仅 3.5%，临近年线突破口"}
  ],
  "next_session_watch": ["次日开盘是否站稳 MA10", "板块强度是否延续主线地位"],
  "invalidation_triggers": ["收盘跌破 MA10 且 moneyflow 转为净流出", "板块跌出主线前 5"],
  "risk_flags": [],
  "missing_data": []
}

示例 B — 高位长上影线 + 资金外流
{
  "candidate_id": "600YYY.SH",
  "ts_code": "600YYY.SH",
  "name": "示例 B",
  "rank": 2,
  "launch_score": 22,
  "confidence": "medium",
  "prediction": "not_yet",
  "pattern": "unclear",
  "washout_quality": "insufficient",
  "rationale": "异动当天上影线占振幅 0.42，body_ratio 仅 0.55；过去 60 日已两次涨停且距 120 日新高 < 1%；moneyflow 趋势 falling 且 5 日累计净流出。高位放量诱多概率高。",
  "key_evidence": [
    {"field": "upper_shadow_ratio",   "value": 0.42,  "unit": "无",  "interpretation": "上影线偏长，疑似试盘失败"},
    {"field": "prior_limit_up_count_60d", "value": 2, "unit": "次", "interpretation": "近 60 日已 2 次涨停，浪型偏后"},
    {"field": "dist_to_120d_high_pct", "value": -0.8, "unit": "%",  "interpretation": "贴近 120 日新高，套牢盘压力大"},
    {"field": "net_mf_trend",          "value": "falling", "unit": "无", "interpretation": "资金 5 日趋势性流出"}
  ],
  "next_session_watch": ["放量回踩 MA10 是否守住", "moneyflow 是否转为净流入"],
  "invalidation_triggers": ["再放量阴线击穿 MA20"],
  "risk_flags": ["High Bull Trap Risk", "Late-stage pattern"],
  "missing_data": []
}
"""
```

`VA_TREND_SYSTEM = (existing_text + "\n" + VA_TREND_FEWSHOT)`。

#### 实现层选项（决策点见 §6）

- **A**：示例直接拼到 `VA_TREND_SYSTEM` 字符串末尾（简单、改动最小）
- **B**：单独放 `prompts_examples.py`，main prompt 在 build 时拼接（更整洁、便于将来按 stage 切换）

#### 字段一致性约束

示例中引用的字段（`atr_10d_quantile_in_60d` / `dist_to_250d_high_pct` / `upper_shadow_ratio`）**必须真实存在于本波次输出**。这就要求 P0-5 必须在 P0-1（输出 `upper_shadow_ratio` 到 hit）+ P0-3 + P0-4 落地之后再合并。

#### Token 成本

`VA_TREND_FEWSHOT` 约 800–1000 tokens，每批 system prompt 都消耗一次。在 200K 输入预算下可接受。

### 5.3 向后兼容

完全兼容（仅扩展 system prompt）。

### 5.4 单测要点

- 字段名一致性测试：从 `VA_TREND_FEWSHOT` 中正则提取 `"field": "<X>"`，断言每个 X 都是 `_build_candidate_row` 的合法输出键 OR `screen` 阶段输出 hit 的合法键。这条测试**防止后续重命名字段时 prompt 失配**。

---

## 6. 关键决策点（已定稿 — v2）

| # | 决策点 | 取值 |
|---|--------|------|
| D1 | 上影线默认阈值 | **`0.35`** |
| D2 | 上影线默认开/关 | **默认开启**（`upper_shadow_ratio_max=0.35`；用户可显式传 `null` 关闭） |
| D3 | 分桶换手率默认开/关 | **默认开启**（`turnover_buckets=DEFAULT_TURNOVER_BUCKETS`；用户传 `null` 退回旧逻辑） |
| D4 | 默认分桶表 | **采纳 4 档**：`(50亿, 5.0, 15.0)` / `(200亿, 3.5, 12.0)` / `(1000亿, 2.5, 9.0)` / `(>1000亿, 1.5, 6.0)` |
| D5 | 阻力位窗口实现 | **单独拉 250d 轻量窗口**（不喂 LLM verbatim） |
| D6 | 是否一并调 `pct_chg_max` | **不调**，保持 `[5.0, 8.0]`，波次 2 再视命中分布观察 |
| D7 | Few-Shot 示例存放 | **单独文件 `prompts_examples.py`**，`prompts.py` build 时拼接 |
| D8 | PR 切分粒度 | **3 个独立 PR**（PR-1 ScreenRules → PR-2 候选行特征 → PR-3 Prompt） |
| D9 | 测试范围 | **只测本波次新增逻辑**，存量回归测试推迟到波次 2 |
| D10 | 行为变化可见性 | **仅 CHANGELOG.md release notes**，不加 CLI flag |

定稿后的衍生约束：
- D2 + D3 决定了**默认行为变化**——必须在 CHANGELOG.md 顶部 `## [volume-anomaly v0.3.0]` 段落中明确列出，并提示"如需保持旧行为，传 `null` 即可"
- D7 决定 PR-3 新增文件 `deeptrade/strategies_builtin/volume_anomaly/volume_anomaly/prompts_examples.py`
- D9 决定测试目录路径 `tests/strategies_builtin/volume_anomaly/`（参照 limit_up_board 既有约定，纯函数单测，不跑 tushare/DB/LLM）

---

## 6b. 实现层面细节（已定稿 — v3）

> v2 提出的 8 个实现层面细节已全部按倾向定稿。原选项对照表归档保留以便回溯，但每条已确定取舍。

| # | 决策点 | 取值（v3 定稿） |
|---|--------|------------------|
| **E1** | `turnover_buckets` JSON 格式 | **`null` 显式表达无穷**：`[[50, 5, 15], ..., [null, 1.5, 6]]`；`from_dict` 内转 `math.inf`，dataclass 内仍是 list-of-tuple |
| **E2** | 250d 扩展窗口与 60d analyze 窗口 | **复用一次 fetch**：`_fetch_daily_history_by_date` 拉 250d，`_build_candidate_row` 按窗口切片喂不同消费者（5d verbatim / 60d ATR/BBW / 120d/250d 极值）；**不新增专用 helper** |
| **E3** | 阻力位字段对称性 | **不补 `low_250d` / `pos_in_250d_range`**；保留 `low_120d` / `pos_in_120d_range` |
| **E4** | 分桶边界判定 | **`circ_mv_yi ≤ bucket_max`**（边界值归"较小桶"），单测覆盖 `circ_mv_yi == 50.0` |
| **E5** | release notes 位置 | **仓库根 `CHANGELOG.md` 顶部新增 `## [volume-anomaly v0.3.0]` 段** |
| **E6** | 插件版本号 | **`0.2.0` → `0.3.0`**（minor，对应 D2 + D3 的默认行为变化） |
| **E7** | 扩展窗口冷拉降级 | **A+C**：默认 `extended_lookback_trade_days=250`（冷拉 ~42s @ rps=6）+ 任一窗口 fetch 不全时对应字段降级为 `None` |
| **E8** | 端到端集成测试 | **本波次不加**；端到端 mock fixture 留给波次 2 一并交付 |

<details>
<summary>v2 选项对照（点击展开）</summary>

| # | 细节 | 选项 |
|---|------|------|
|---|------|------|----------|
| **E1** | `turnover_buckets` 在用户 JSON 配置中的传入格式 | A. `[[50, 5, 15], [200, 3.5, 12], [1000, 2.5, 9], [null, 1.5, 6]]`（最后一档用 null 表示无穷）<br/>B. `[[50, 5, 15], [200, 3.5, 12], [1000, 2.5, 9]]`（最后一档由代码兜底为 `inf`） | **A** —— 显式标记"最后一档"，配置可读性高；`from_dict` 中 `null → math.inf`，dataclass 内仍是 list-of-tuple |
| **E2** | 扩展 250d 窗口与现有 analyze 60d 窗口是否复用一次 fetch | A. 复用 — `collect_analyze_bundle` 改为只拉一次 250d，60d verbatim 部分从 250d 切片<br/>B. 分开拉 — 60d + 250d 两份，逻辑分离 | **A** —— 减少一次完整 fetch 循环；`_fetch_daily_history_by_date` 拉的是 250d，按现有 60d 切片喂 LLM verbatim、按 120d/250d 切片算极值；**但保留现有的"history_lookback=60d"作为 LLM verbatim 切片宽度**，即扩展窗口 ≥ verbatim 窗口时才合并 |
| **E3** | 阻力位字段是否补 250d 区间位置 / `low_250d` | A. 仅 `low_120d` / `pos_in_120d_range`<br/>B. 同时补 `low_250d` / `pos_in_250d_range` | **A** —— 250d 区间位置的判断意义比 120d 弱（A 股 250d ≈ 1 年，区间位置容易被趋势主导而非震荡区间），同时省 token |
| **E4** | 分桶查找的边界判定语义 | A. `circ_mv_yi ≤ bucket_max` 取第一个命中桶（边界值归"较小桶"）<br/>B. `circ_mv_yi < bucket_max` 取第一个命中桶（边界值归"较大桶"） | **A** —— 与"≤ 50亿 = 微盘"的口语理解一致；单测覆盖 `circ_mv_yi == 50.0` 的边界 |
| **E5** | release notes 落地位置 | A. 在仓库根 `CHANGELOG.md` 顶部新增 `## [volume-anomaly v0.3.0]` 段<br/>B. 新建 `docs/release_notes/volume_anomaly_v0.3.md` | **A** —— 与现有 `## [limit-up-board v0.4.0]` 风格一致；不引入新文档目录 |
| **E6** | 插件版本号 bump | A. `0.2.0` → `0.3.0`（minor，新功能 + 默认行为变化）<br/>B. `0.2.0` → `0.2.1`（patch，仅当作改进） | **A** —— D2 + D3 是默认行为变化，按 SemVer 应 minor bump（与 limit-up-board v0.3.0 / v0.4.0 节奏一致） |
| **E7** | 扩展窗口冷拉的 RPS 预算与降级 | A. 默认 `extended_lookback_trade_days=250`，冷拉约 42s（`tushare_rps=6.0`），文档标注即可<br/>B. 默认收紧到 200d，约 33s 冷拉<br/>C. 提供降级——任一桶/窗口 fetch 不全时 `dist_to_*` 字段降为 `None`，不阻塞 analyze | **A + C** —— 250d 冷拉延迟一次性、cache 后归零，可接受；降级路径 C 是必须的，已在 §4.2 "失败兜底" 中明确 |
| **E8** | 是否新增端到端 (screen → analyze) 集成测试 | A. 不加，本波次仅纯函数单测<br/>B. 加一个最小集成测试（mock tushare） | **A** —— 与 D9 一致；端到端测试基础设施投入大，留给波次 2 的"T+N 自动回测闭环"工作合并交付（共享 mock tushare fixture） |

</details>

---

## 7. 开发迭代计划（PR 切分）

### 7.1 总览

| PR | 范围 | 主要文件 | 依赖 | 预估工作量 |
|----|------|----------|------|------------|
| PR-1 | P0-1（上影线）+ P0-2（分桶换手率） | `data.py` + 新单测 | 无 | 1–2 人日 |
| PR-2 | P0-3（ATR/BBW）+ P0-4（阻力位） | `data.py`（candidate row + bundle）+ 新单测 | PR-1 合并（避免冲突） | 2–3 人日 |
| PR-3 | P0-5（Few-Shot） | `prompts.py`（或 `prompts_examples.py`） + 字段一致性单测 | PR-2 合并（示例引用新字段） | 0.5 人日 |

### 7.2 PR-1 详细 checklist

**改动范围（基于 E1 / E4 / E5 / E6 决策）**
- [ ] `data.py::ScreenRules`：新增 `upper_shadow_ratio_max: float | None = 0.35` / `turnover_buckets: list[tuple[float, float, float]] | None = DEFAULT_TURNOVER_BUCKETS`
- [ ] `data.py`：模块级 `DEFAULT_TURNOVER_BUCKETS` 常量，最后一档用 `math.inf` 表示无穷
- [ ] `data.py::ScreenRules.from_dict`：
  - `upper_shadow_ratio_max`: float / null 反序列化
  - `turnover_buckets`: 接受 list-of-list（JSON 无 tuple），其中**每个内层元素第一位为 null 时转 `math.inf`**（E1）；list-of-tuple 也应原样接受（兼容程序内构造）
- [ ] `data.py::ScreenRules.__post_init__`：
  - `upper_shadow_ratio_max`：非 None 时需 `0 < x ≤ 1`
  - `turnover_buckets`：非 None 时需非空、每档 `0 ≤ min ≤ max`、`circ_mv_yi_max` 严格升序
- [ ] `data.py::ScreenDiagnostics`：新增 `n_after_upper_shadow` / `turnover_bucket_hits: dict[str, int]` / `n_missing_circ_mv` / `circ_mv_missing_codes`
- [ ] `data.py::ScreenResult`：新增 `n_after_upper_shadow`
- [ ] `data.py::screen_anomalies`：
  - 计算 `upper_shadow_ratio = (high - max(open, close)) / range`，加入过滤链；写入 `n_after_upper_shadow`
  - `daily_basic` 读取扩展为 `[turnover_rate, circ_mv]`；通过 `normalize_to_yi("circ_mv", ...)` 复用现有亿元换算（**不新增 helper**）
  - 分桶查找：取第一个使 `circ_mv_yi ≤ bucket.max` 的桶（E4 边界规则）；`circ_mv` 缺失时降级为全局 `[turnover_min, turnover_max]` 并计入 `circ_mv_missing_codes`
- [ ] hit 行新增字段：`upper_shadow_ratio` / `circ_mv_yi` / `turnover_bucket`（如 `"≤50亿"`）
- [ ] `render.py::write_screen_report`：报告中
  - 漏斗增加"上影线" 节点
  - 增加"分桶命中分布"小表
- [ ] 新单测目录 + 文件：
  - `tests/strategies_builtin/volume_anomaly/__init__.py`（空文件）
  - `tests/strategies_builtin/volume_anomaly/test_screen_rules.py`，覆盖：
    1. 上影线三组合（纯阳 / 避雷针 / 合理影线）
    2. 分桶三档命中（30 亿 turnover=14% / 500 亿 turnover=10% / 2000 亿 turnover=5%）→ 通过 / 拒绝 / 通过；同样数据在分桶 None 模式（旧逻辑）→ 通过 / 通过 / 通过
    3. 边界值：`circ_mv_yi == 50.0` 归"≤50亿"桶（E4）
    4. `circ_mv` 缺失 → fallback 到全局阈值
    5. `ScreenRules.from_dict` 反序列化：`{"turnover_buckets": [[50, 5, 15], [null, 1.5, 6]]}` → 内层 null 转 `math.inf`
- [ ] **release notes**：在仓库根 `CHANGELOG.md` 顶部新增段落
  - 标题：`## [volume-anomaly v0.3.0] — YYYY-MM-DD — 上影线过滤 + 流通市值分桶换手率`
  - 明确列出**默认行为变化**：上影线默认开启 0.35、分桶换手率默认开启
  - 提示回退方式：`screen_rules` 中传 `"upper_shadow_ratio_max": null` / `"turnover_buckets": null`
- [ ] `deeptrade_plugin.yaml::version`：`0.2.0` → `0.3.0`（E6）

**验收**
- 新单测全部通过
- 已有 limit_up_board / 框架层单测无回归
- 在历史 1–2 个交易日数据上跑一遍 screen，对比改造前后的漏斗：上影线节点应有非零淘汰；分桶模式下大盘股淘汰量明显高于旧模式

**回滚预案**
- 用户在 `screen_rules` 中传 `"upper_shadow_ratio_max": null`、`"turnover_buckets": null` 即可完全回到旧逻辑
- 代码 revert：`git revert <PR-1 commit>` 不影响 PR-2 / PR-3（PR-2 不依赖 PR-1 的 ScreenRules 字段）

---

### 7.3 PR-2 详细 checklist

**改动范围（基于 E2 / E3 / E7 决策）**
- [ ] `data.py`：新增内部纯函数 `_compute_atr_series(history) -> list[float | None]`（10 日窗口、简单平均 TR）
- [ ] `data.py`：新增内部纯函数 `_compute_bbw_series(history) -> list[float | None]`（20 日 BBW）
- [ ] `data.py::collect_analyze_bundle`：
  - **不新增 `_fetch_extended_high_lookup` 专用函数**（E2-A 复用原语）—— 直接把 `history_lookback` 的默认值从 60 提到 250；现有 `_fetch_daily_history_by_date` 调一次返回 250d
  - 新增可选参数 `extended_lookback_trade_days: int = 250`
  - **保留** `verbatim_lookback: int = 60` 作为切片宽度，`_build_candidate_row` 接受全量 250d history 后内部切 60d 用作 ATR/BBW 输入、切 5d 用作 verbatim、切 120d/250d 用作极值
- [ ] `data.py::_build_candidate_row`：
  - 新增 4 个 VCP 字段：`atr_10d_pct` / `atr_10d_quantile_in_60d` / `bbw_20d` / `bbw_compression_ratio`
  - 新增 5 个阻力位字段（E3-A 不补 250d 区间位）：`high_120d` / `high_250d` / `low_120d` / `dist_to_120d_high_pct` / `dist_to_250d_high_pct` / `is_above_120d_high` / `is_above_250d_high` / `pos_in_120d_range`
  - 短历史降级：history < 250 → `*_250d` 字段返 None；< 120 → `*_120d` 字段返 None；< 60 → `atr_10d_quantile_in_60d` 返 None；< 20 → BBW 返 None
- [ ] `data.py::AnalyzeBundle.data_unavailable`：扩展窗口缺失天数过多（>20%）时写入告警，但不阻塞主流程（E7-C 降级路径）
- [ ] 新单测：`tests/strategies_builtin/volume_anomaly/test_candidate_features.py`
  - `_compute_atr_series`：构造一段先剧烈、后收敛的 history → 期望 ATR 序列后半段单调递减
  - `_compute_bbw_series`：同上对应 BBW
  - `_build_candidate_row`：
    - 250d 完整 history：所有字段非空；当前 close 处于 250d 新高 → `is_above_250d_high=True`、`dist_to_250d_high_pct ≈ 0`
    - 150d history（不足 250d，足够 120d）：`high_250d / dist_to_250d_high_pct = None`、`high_120d` 仍可计算
    - 50d history：BBW 可计算（≥ 20）、ATR 分位数 = None（< 60）、阻力位字段全 None
    - 短历史不抛异常

**验收**
- 新单测全部通过
- 在已有 watchlist 上跑一次 analyze 冷启动，确认扩展窗口冷拉时间增量 ≤ 1 分钟（按 `tushare_rps=6.0`，250 次调用约 42 秒；cache 后归零，E7-A）
- LLM 输入 prompt 大小增量在预期内（每候选 +50–80 tokens；不增加 verbatim 部分）

**性能注意（E7-A）**
- 扩展窗口的 250 次 daily 调用走 cache，跨 run 命中率高，**只在首次冷跑慢一次**（约 42s @ rps=6）
- 若用户反馈冷拉延迟不可接受，可降级 `extended_lookback_trade_days=200`（约 33s @ rps=6）

**回滚预案**
- 代码 revert PR-2 即可恢复 60d 窗口；不影响 PR-1
- PR-3 依赖 PR-2 输出的字段，若 PR-2 revert 必须同时 revert PR-3

---

### 7.4 PR-3 详细 checklist

**改动范围（基于 D7 决策）**
- [ ] 新建 `deeptrade/strategies_builtin/volume_anomaly/volume_anomaly/prompts_examples.py`：
  - 导出常量 `VA_TREND_FEWSHOT: str`，内容如 §5.2 的两个示例 JSON
  - 模块文档字符串说明：示例引用的字段必须真实存在于 `_build_candidate_row` 输出或 `screen_anomalies` hit 行
- [ ] `prompts.py`：
  - 顶部 `from .prompts_examples import VA_TREND_FEWSHOT`
  - `VA_TREND_SYSTEM` 末尾（在【字段值约束】之后）追加 `VA_TREND_FEWSHOT`
  - 在【判断维度】A 段中增加一行提示：「整理期间的波动率是否收敛（`atr_10d_quantile_in_60d` / `bbw_compression_ratio`）；越低越好」
- [ ] 新单测：`tests/strategies_builtin/volume_anomaly/test_prompt_consistency.py`
  - 从 `VA_TREND_FEWSHOT` 中用正则 `r'"field":\s*"([^"]+)"'` 提取所有引用字段名
  - 用受控人造 input 跑一次 `_build_candidate_row` 拿到所有合法 candidate 字段名集合
  - 用受控人造 input 跑一次 `screen_anomalies` 的内部计算分支，拿到所有合法 hit 字段名集合（也可硬编码 hit schema 字段集合）
  - 断言：示例中每个 `"field": "<X>"` 都属于以上两个合法集合之一
  - **防御目的**：以后任何字段重命名都会立刻让此测试红，避免 prompt 失配未被发现

**验收**
- 新单测通过
- 跑一次完整 analyze（冷启动），从 `va_runs` / `va_events` / 报告中确认：
  - 同一批候选下，新 prompt 比旧 prompt 在 `key_evidence.field` 中引用 `atr_10d_quantile_in_60d` / `dist_to_250d_high_pct` / `upper_shadow_ratio` 的频次提升

**回滚预案**
- 代码 revert PR-3 即可恢复旧 prompt；不影响 PR-1 / PR-2 的字段输出（仅 LLM 不再被引导引用）

---

## 8. 验收指标（whole-wave）

> 这一节回答原方案被点出的"缺乏可证伪性"问题。

### 8.1 工程验收（PR 合并门槛）

- [ ] 三轮 PR 各自单测全绿、CI 通过
- [ ] 已有功能（screen / analyze / prune）端到端冒烟通过
- [ ] 报告渲染（screen / analyze）正常显示新字段
- [ ] release notes 明确列出**默认行为变化**（D2 + D3）

### 8.2 信号质量验收（合并后 1–2 周观察）

> 需在 PR 全部合并后跑一段历史回放，**与改造前同区间对比**。

| 指标 | 期望方向 | 备注 |
|------|----------|------|
| Screen 漏斗 `n_after_t_day_rules → n_after_upper_shadow` 淘汰率 | > 0% | 上影线规则有真实淘汰 |
| Screen `turnover_bucket_hits` 大盘桶（>1000 亿）占比 | 显著 > 0 | 分桶纠正了大盘股漏报 |
| Analyze 阶段，`key_evidence.field` 引用新增字段的批均次数 | ≥ 2 次/批 | 新因子被 LLM 实际使用 |
| Imminent_launch 标的的 T+3 平均收益（如已有回测能力） | 改造后 ≥ 改造前 | 信号质量整体提升 |
| 平均 input_tokens / 批 | 增量 ≤ 8% | token 预算可控 |

> **注意**：T+3 收益指标依赖**波次 2 的"T+N 自动回测闭环"**，本波次只能做事后人工抽样。

### 8.3 失效信号

如果任一项触发，应**立即冻结后续合并**并复盘：

- 漏斗经上影线节点后 hit 数掉 80% 以上 → 阈值过严，需要回 0.40
- `n_missing_circ_mv` 占主板池 > 5% → `daily_basic` 数据完整度问题，需要先排查
- LLM 在 `key_evidence` 中**完全不引用**新字段（连续 3 批） → Few-Shot 示例位置或字段命名需要再调

---

## 9. 风险与回滚

| 风险 | 触发场景 | 回滚动作 |
|------|----------|----------|
| 上影线阈值过严，过滤掉大量真主升浪 | hit 数显著下降 | 调 `upper_shadow_ratio_max` 至 0.40 / null |
| 分桶表与实际市场分布不匹配 | 某档桶持续 0 命中 | 调整桶边界 / 阈值，无需改代码 |
| 扩展窗口 API 调用拖慢首次 analyze | 冷启动 > 5 分钟 | 默认 250 → 200 / 改异步预热 |
| Few-Shot 示例引偏 LLM 评分 | imminent_launch 数量异常 | 临时回退 `prompts.py`（git revert PR-3 即可，不影响 PR-1/2） |
| `circ_mv` 字段在某些日期缺失 | `n_missing_circ_mv` 高 | 已设计 fallback，自动退回全局阈值 |

---

## 10. 与波次 2 的衔接

为了减少波次 2 的返工，本波次落地时已经为以下后续工作做好铺垫：

- **波次 2 的"显式分维度评分"**：本波次只动 system prompt，**不动 schema**，留出干净的二期改造面
- **波次 2 的"RPS / 行业相对 alpha"**：本波次新增的 `industry` 已在候选行；二期可直接接入 `index_daily`
- **波次 2 的"T+N 自动回测闭环"**：本波次扩展的 `_fetch_extended_high_lookup` 复用同一套 `_fetch_daily_history_by_date`，二期回测取 T+1..T+5 close 时可直接调用同一接口
- **波次 2 的"防守锚点字段"**：本波次未触碰 `next_session_watch` / `invalidation_triggers` 字符串字段，留给二期升级为枚举

---

## 11. 下一步（v2 — 等待 E1–E8 对齐）

D1–D10 已全部定稿；现在等用户对 §6b 中 8 个**实现层面**细节（E1–E8）给取舍。我会按你的回应：

1. 把 E1–E8 的取舍收敛到设计稿（v3 定稿，不再有未决项）
2. 直接进入 PR-1 实现 —— 设计对齐已完成 3 轮（v1 / v2 / v3）
3. PR-1 落地后再做 PR-2 / PR-3，按 §7 的依赖顺序推进

如果对 E1–E8 的我的倾向都认可，可以直接回复"全部按你的倾向"，我即可定稿 v3 进入实现阶段。

如果对设计稿仍有质疑或新增决策点，请直接指出——我们再多走一轮 review 也比写完代码再返工便宜。
