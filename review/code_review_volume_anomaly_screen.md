# 「成交量异动策略」screen 模式实现复盘评审报告

| 项目 | 内容 |
|---|---|
| 评审对象 | `volume_anomaly` 插件 screen 模式（`data.py:screen_anomalies` + `_fetch_daily_history_by_date` + `strategy.py:_run_screen`） |
| 评审重点 | 数据完整性、规则执行强度、静默降级、可观测性 |
| 评审方式 | 代码静态走查 + 流水线逐步推演 + 边界数据假设 |
| 总体结论 | **存在 4 处中-高严重度静默降级**，会导致 (a) 规则比纸面更松，(b) 部分标的被错误剔除而无任何告警。当前命中结果可信但**命中数量不够稳定**，需要补防御 + 暴露诊断。 |

---

## 一、执行概要（TL;DR）

screen 模式的 5 步漏斗中，每一步都存在数据缺失的可能。框架对**完全失败**做了合理处理（提前 return + `data_unavailable` 标记），但对**部分失败 / 缓慢降级**几乎没有可观测性，存在以下 4 类被掩盖的偏差：

| 类别 | 问题 | 净效应 | 严重度 |
|---|---|---|---|
| 历史窗口某天数据缺失 | 静默跳过该天 | 规则**变松**（潜在假阳性） | 🔴 高 |
| 个股历史不足 60 天 | `len(h) ≥ 6` 即可通过 | 规则**变松** | 🔴 高 |
| `daily_basic` 部分股票缺失 turnover | NaN 失败比较被静默剔除 | 规则**变严**（误杀） | 🟠 中 |
| `stock_st` 异常返空 | 不剔除任何 ST 股 | **语义违反**（ST 进入候选） | 🟠 中 |

——按目前的实现，"虽然制定了标准"是事实，"是否完全执行标准"则**不能在结果里证伪**。

---

## 二、复盘范围

### 受审代码

```
deeptrade/strategies_builtin/volume_anomaly/volume_anomaly/
├── data.py              ← screen_anomalies, _fetch_daily_history_by_date, _try_optional, ScreenRules
├── strategy.py          ← _run_screen, _build_tushare_client
└── render.py            ← write_screen_report (审计可见性)
```

### 审计假设清单

- tushare 单次调用可能因限频/服务异常**失败**（已通过 `_try_optional` 捕获 OPTIONAL，REQUIRED 抛错）
- tushare 单次调用可能**返空**（合法返回，无异常）
- 个股可能**新上市**（历史 < 60 日）
- 个股可能**长期停牌**后复牌（历史不连续）
- 缓存 JSON 反序列化的 int/str 漂移（已修）

---

## 三、流水线逐步审计

### Step 1 — 主板池

```python
stock_basic = tushare.call("stock_basic", force_sync=force_sync)
main_pool = main_board_filter(stock_basic)
main_codes = set(main_pool["ts_code"].astype(str))
```

| 审计点 | 状态 |
|---|---|
| stock_basic 抛异常 | ✅ 直接 propagate，不会静默 |
| stock_basic 返空 | ⚠ main_pool 为空 → eligible 为空 → 全流程返 0 命中（无告警） |
| 7 天 TTL 缓存可能漏 IPO | ⚠ 影响极小（IPO 当周很少出"异动"，且不会假阳性） |
| 主板过滤口径 | ✅ `market='主板' AND exchange in ('SSE','SZSE')` 正确 |

### Step 2 — ST / 停牌排除

```python
st_df = tushare.call("stock_st", trade_date=trade_date, ...)
st_codes = set(st_df["ts_code"].astype(str)) if not st_df.empty else set()

susp_df, susp_err = _try_optional(tushare, "suspend_d", ...)
susp_codes = set(susp_df["ts_code"]) if susp_df is not None and not susp_df.empty else set()
```

| 审计点 | 状态 |
|---|---|
| stock_st 抛异常（无权限） | ✅ propagate（REQUIRED 接口） |
| **stock_st 返空（异常情况）** | 🟠 **静默：st_codes 为空集 → 不剔除任何 ST 股**。按 A 股实际情况，每日 ST 股数量稳定在 100+，返空一定是数据异常 |
| suspend_d 失败 | ✅ 记 `data_unavailable`；且停牌股自然不会出现在 daily(T) → 实际影响小 |

### Step 3 — T 日 K 线规则

```python
daily_t = tushare.call("daily", trade_date=trade_date, ...)
daily_t = _normalize_id_cols(daily_t)
if daily_t is None or daily_t.empty:
    return ScreenResult(... all zeros)
daily_t = daily_t[daily_t["ts_code"].isin(eligible)].copy()
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

| 审计点 | 状态 |
|---|---|
| daily(T) 抛异常 | ✅ propagate |
| daily(T) 返空 | ✅ 提前 return 0 命中 |
| daily(T) **缺少部分股票**（intraday 模式或 IPO） | ⚠ 那些股票被静默排除（净效应：规则**变严**而非降级，可接受） |
| pct_chg / close / open / high / low 缺 NaN | ⚠ NaN 比较返回 False → 静默剔除（影响极小，主板停牌恢复后第一日可能 NaN） |
| `clip(lower=1e-9)` | ✅ 防 high==low 一字板 0 除零 |

### Step 4 — 换手率规则 ⚠ 关键风险点

```python
db_t = tushare.call("daily_basic", trade_date=trade_date, ...)
if db_t is not None and not db_t.empty and "turnover_rate" in db_t.columns:
    db_lookup = db_t.set_index("ts_code")["turnover_rate"].to_dict()
else:
    db_lookup = {}
    data_unavailable.append("daily_basic.turnover_rate")
t_day_hits["turnover_rate"] = t_day_hits["ts_code"].map(db_lookup)  # ← NaN if not in lookup
turnover_hits = t_day_hits[
    (t_day_hits["turnover_rate"] >= rules.turnover_min)
    & (t_day_hits["turnover_rate"] <= rules.turnover_max)
].copy()
```

| 审计点 | 状态 |
|---|---|
| daily_basic(T) 抛异常 | ✅ propagate |
| daily_basic(T) **完全返空** | 🟠 db_lookup 为空 → 全部 NaN → 0 通过；记 `data_unavailable` |
| **daily_basic(T) 部分缺失（单只股票没有 turnover_rate）** | 🔴 **静默：该股 turnover_rate=NaN → 比较返 False → 被剔除**，但 `data_unavailable` 不会标记。**用户无法知道有几只候选因数据缺失被错杀** |
| daily_basic 单位错位 | ✅ turnover_rate 直接是百分比，单位一致 |

### Step 5 — vol 历史规则 ⚠⚠ 最高风险点

```python
survivor_codes = set(turnover_hits["ts_code"].astype(str))
history_dates = _last_n_trade_dates(calendar, trade_date, rules.lookback_trade_days)
history_df = _fetch_daily_history_by_date(
    tushare, history_dates, survivor_codes, force_sync=force_sync
)
```

```python
def _fetch_daily_history_by_date(...):
    frames = []
    for d in trade_dates:
        df = tushare.call("daily", trade_date=d, force_sync=force_sync)
        if df is None or df.empty:
            continue                        # ← 静默跳过！无事件、无 data_unavailable
        df = _normalize_id_cols(df)
        if df is None or df.empty:
            continue
        if candidate_codes:
            df = df[df["ts_code"].isin(candidate_codes)]
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)
```

```python
for row in turnover_hits.itertuples(index=False):
    code = str(row.ts_code)
    h = history_df[history_df["ts_code"].astype(str) == code].sort_values("trade_date")
    if h.empty or len(h) < 6:               # ← 6 行就放行
        continue
    ...
    vols_long = h["vol"].astype(float).tolist()
    vols_short = [float(v) for v in h.tail(rules.vol_max_short_window)["vol"].tolist()]
    vol_max_long = max(vols_long)
    vol_max_short = max(vols_short)
```

| 审计点 | 状态 |
|---|---|
| 60 次 `daily(trade_date=X)` 任一抛异常 | ✅ propagate（停止运行） |
| **60 次中任一返空** | 🔴 **静默跳过：vol_max_long / vol_max_short 算在更少天数上 → 规则严重变松**。例如某只股票实际历史 60 天有 200_000 vol，但那一天 daily 返空，被算成 vol_max_long=120_000，T 日 vol=130_000 就通过了 |
| **个股历史 < 60 天**（新股 / 长期停牌复牌） | 🔴 **静默：6 行即过门槛**。 60 日规则降级为 6 日规则，几乎没有过滤效力 |
| **个股历史 30-59 天**（不在前两类极端，但中间区段） | 🟠 静默使用全部可用历史；`vols_short` = `tail(30)` 当不足 30 天时取所有可用，相当于 vol_max_short = vol_max_long → "短窗口最大量"和"长窗口最大量"退化为同一条件 |
| vol_ratio_5d 计算 | 🟠 `prior.tail(5)` 取最近 5 个**有数据**的交易日，若中间有缺失，实际跨度可能 > 5 日 |
| vol 不复权 | 🟡 拆股/送转期间前期 vol 偏小 → 当前 vol 更易胜出（规则变松，但场景罕见） |

---

## 四、发现的问题（按严重度排序）

### 🔴 H1 — 60 日历史窗口任一日 daily() 返空被静默跳过

**位置**：`data.py:_fetch_daily_history_by_date` 第 4 行 `continue`

**症状**：vol_max_long / vol_max_short 在不完整数据上计算，规则被悄悄放松。

**典型触发场景**：
- 某历史交易日 tushare 数据未完整发布（罕见但发生过）
- 缓存写入时被中断 → 该 trade_date 的缓存条目损坏 → 重读返空
- 网络瞬时抖动导致单次调用返空（tushare SDK 行为）

**影响估计**：1 天数据缺失 → 假阳性率提升 ~1.7% / 5 天缺失 → ~8%。

### 🔴 H2 — 个股历史不足 60 天即通过门槛

**位置**：`data.py:screen_anomalies` 中 `if h.empty or len(h) < 6: continue`

**症状**：新股、长期停牌后复牌的股票，"近 3 个月最大成交量"实际只比对了几天到几十天，规则形同虚设。

**典型触发场景**：
- T 之前 60 个交易日内才上市的新股
- 之前停牌 1-2 个月的股票
- 之前停牌 > 60 天的股票（历史 < 60 天）

**影响估计**：A 股每月新股 + 复牌股共 30-60 只，其中能巧合通过 T 日规则的 1-3 只 / 月。

### 🟠 M1 — daily_basic 部分股票缺 turnover_rate 被静默剔除

**位置**：`data.py:screen_anomalies` 第 4 步 `t_day_hits["turnover_rate"] = ... .map(db_lookup)` 后未检查 NaN

**症状**：T 日规则通过的候选中，daily_basic 表里没有对应行的股票，turnover_rate=NaN，所有比较为 False，被无声剔除。`data_unavailable` 只在**完全空**时记录。

**典型触发场景**：
- 个别新股第一日没有 daily_basic（极少见）
- daily_basic 缓存与 daily 缓存不同步（一个新一个旧）

**影响估计**：日均 0-3 只候选受影响，但用户完全看不到。

### 🟠 M2 — stock_st 异常返空时 ST 股进入候选

**位置**：`data.py:screen_anomalies` Step 2 `st_codes = set(...) if not st_df.empty else set()`

**症状**：A 股每日 ST 股稳定在 100+ 只，stock_st(T) 返空只可能是**数据异常**，但代码当成"今日无 ST"处理。

**典型触发场景**：
- 缓存损坏
- 该日 tushare 接口暂时性问题

**影响估计**：每年发生 1-2 次的概率，命中后果是 ST 股进入异动追踪池（违反"主板非 ST"基本要求）。

### 🟡 L1 — vol_ratio_5d 取"5个有数据日"而非"5个连续交易日"

**位置**：`data.py:screen_anomalies` `vol_mean_prev5 = float(prior.tail(5)["vol"].mean())`

**症状**：当历史窗口不连续时，"前 5 日均量"实际跨度可能超过 5 个交易日。

**影响估计**：极小，仅在历史稀疏时偏离原意。

### 🟡 L2 — 阈值无边界校验

**位置**：`strategy.py:_configure_screen_rules` + `data.ScreenRules`

**症状**：用户输入 `pct_chg_min=10, pct_chg_max=5`、`turnover_max=-1` 等无意义组合，无校验直接执行 → 0 命中。

### 🟡 L3 — vol 不复权在拆分送转期偏离实际

**位置**：vol 全程使用原始字段，无 `adj_factor` 处理

**症状**：除权日前后的 vol 不可比；对于 60 天内做过 10:5 高送转的股票，前期 vol 是不复权口径下"看似偏小"，T 日相对更容易胜出。

**影响估计**：A 股每年送转股约 200 只，分散在 60 天窗口里影响很小，且方向是"放松"不是"拒绝合法标的"。

---

## 五、建议修复方案

修复按"必须做 / 建议做 / 可选"三档列出，**每个都给出最小改动方案**。

### 必须做（修 H1, H2, M1, M2）

#### 修复 1（H1）：把历史日缺失暴露出来

```python
# data.py:_fetch_daily_history_by_date 改造
def _fetch_daily_history_by_date(
    tushare, trade_dates, candidate_codes, *, force_sync=False
) -> tuple[pd.DataFrame, list[str]]:
    """Returns (concat_df, missing_dates)."""
    frames: list[pd.DataFrame] = []
    missing_dates: list[str] = []
    for d in trade_dates:
        df = tushare.call("daily", trade_date=d, force_sync=force_sync)
        if df is None or df.empty:
            missing_dates.append(d)
            continue
        df = _normalize_id_cols(df)
        if df is None or df.empty:
            missing_dates.append(d)
            continue
        if candidate_codes:
            df = df[df["ts_code"].isin(candidate_codes)]
        frames.append(df)
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return out, missing_dates
```

调用方记入 `data_unavailable`：
```python
history_df, missing_history_dates = _fetch_daily_history_by_date(...)
if missing_history_dates:
    data_unavailable.append(
        f"daily history missing on {len(missing_history_dates)} days: "
        f"{missing_history_dates[:5]}{'...' if len(missing_history_dates) > 5 else ''}"
    )
```

#### 修复 2（H2）：个股历史覆盖率门槛参数化 + 剔除时记录

新增 `ScreenRules.min_history_coverage: float = 0.8`（要求至少有 80% 的 lookback 天数才参评）：

```python
# 在 screen_anomalies 个股循环里
required_days = int(rules.lookback_trade_days * rules.min_history_coverage)
insufficient_history: list[dict[str, Any]] = []
for row in turnover_hits.itertuples(...):
    code = str(row.ts_code)
    h = history_df[...].sort_values("trade_date")
    if len(h) < required_days:
        insufficient_history.append({
            "ts_code": code,
            "available_days": len(h),
            "required_days": required_days,
        })
        continue
    ...

# 报告里展示
result.insufficient_history = insufficient_history
```

ScreenResult 加 `insufficient_history` 字段，render 写入 summary.md 一节。

#### 修复 3（M1）：暴露 daily_basic 部分缺失

```python
t_day_hits["turnover_rate"] = t_day_hits["ts_code"].map(db_lookup)
n_missing_turnover = int(t_day_hits["turnover_rate"].isna().sum())
if n_missing_turnover > 0:
    missing_codes = t_day_hits[t_day_hits["turnover_rate"].isna()]["ts_code"].tolist()
    data_unavailable.append(
        f"daily_basic.turnover_rate missing for {n_missing_turnover} candidates: "
        f"{missing_codes[:5]}{'...' if n_missing_turnover > 5 else ''}"
    )
```

#### 修复 4（M2）：stock_st 返空时声明可疑

```python
st_df = tushare.call("stock_st", trade_date=trade_date, ...)
st_codes = set(st_df["ts_code"].astype(str)) if not st_df.empty else set()
if not st_codes:
    data_unavailable.append(
        "stock_st(T) returned 0 ST codes — abnormal for A股, "
        "ST stocks may have leaked into candidates; verify data freshness"
    )
```

### 建议做（修 L1, L2）

#### 修复 5（L1）：vol_ratio_5d 严格 5 连续日

```python
expected_5d = history_dates[-6:-1]  # T 不含
prior_5d = h[h["trade_date"].isin(expected_5d)]
if len(prior_5d) < 5:
    insufficient_history.append({"ts_code": code, "reason": "missing prev-5d"})
    continue
vol_mean_prev5 = float(prior_5d["vol"].mean())
```

#### 修复 6（L2）：参数后置校验

`ScreenRules.__post_init__`：
```python
def __post_init__(self) -> None:
    if not (0 <= self.pct_chg_min <= self.pct_chg_max):
        raise ValueError(f"invalid pct_chg range [{self.pct_chg_min}, {self.pct_chg_max}]")
    if not (0 <= self.turnover_min <= self.turnover_max):
        raise ValueError(f"invalid turnover range")
    if not (0 <= self.body_ratio_min <= 1):
        raise ValueError(f"body_ratio_min must be in [0, 1]")
    if self.vol_max_short_window <= 0 or self.vol_top_n_long <= 0:
        raise ValueError("vol windows must be positive")
    if self.vol_max_short_window > self.lookback_trade_days:
        raise ValueError("vol_max_short_window must be ≤ lookback_trade_days")
    if self.vol_ratio_5d_min < 0:
        raise ValueError("vol_ratio_5d_min must be ≥ 0")
```

### 可选（修 L3）

#### 修复 7（L3）：复权 vol

引入 `adj_factor` 接口，在 `_fetch_daily_history_by_date` 内对 vol 做前复权。代价：每只股票多一次接口或维持一张大表的 join。**收益 / 成本不高**，建议暂缓。

---

## 六、可观测性增强（横向方案）

无论 H1-M2 是否修复，建议增加一个**全局诊断输出**进入 `screen_summary.md`：

```markdown
## 数据完整性诊断
- 主板池 stock_basic 行数: 5234（缓存日期: 2026-04-22）
- stock_st(T) ST 标的数: 168
- suspend_d(T) 停牌标的数: 47 / OPTIONAL状态: ok
- daily(T) 全市场行数: 5018 (主板覆盖: 3038/3195 = 95.1%)
- daily_basic(T) 全市场行数: 5018 (主板覆盖: 3038/3195 = 95.1%)
- 60日历史窗口: 60 天计划 / 60 天实际拉取 / 0 天缺失
- 个股历史覆盖: ≥48天(80%)的有 487 只 / 不足者已剔除 12 只
- 长窗口 daily 调用缓存命中率: 60/60 (cache hit) 或 X/60 (live)
```

落地到 ScreenResult + render 内即可，预计 50 行代码。

---

## 七、结论与建议优先级

| 优先级 | 项 | 工时估计 | 立即收益 |
|---|---|---|---|
| **P0** | H1 + H2 + M1 + M2 (修复 1-4) | ~2 小时 | 消除 4 处静默降级，结果可信度大幅提升 |
| **P0** | 可观测性增强（六） | ~1 小时 | 用户能在每次 screen 后**自证数据完整性** |
| **P1** | L1 + L2（修复 5-6） | ~30 分钟 | 边界鲁棒性，防止配置错误 |
| **P2** | L3（修复 7） | ~3 小时 | 边角场景，可暂缓 |

### 当前命中可信度评估

针对之前命中的 `601058.SH`（赛轮轮胎）：
- 主板大盘股，历史长度充足（远超 60 天）→ H2 不影响
- daily_basic 显然有 turnover_rate（5%-级别合理数据）→ M1 不影响
- 60 日历史是否完整未知，但成交量比值 4.96 远超 2 倍门槛 → 即便缺 1-2 天，结论稳定
- 显示出的 `max_vol_60d=1291710.19` 合理 → 大概率历史窗口完整

**该命中本身可信。** 但后续放宽阈值后命中数翻倍，新增的命中里不排除被 H1/H2 误放进来的样本，这是真实风险。
