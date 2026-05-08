# 打板策略因子与 Prompt 优化方案（Phase A + Phase B）

> 配套阅读：`docs/limit-up-board.md`、`docs/gemin_anwser.md`
>
> 本方案对照 Gemini 给出的"游资思路"因子清单，对当前实现做**两阶段补齐**：
> Phase A 仅派生新因子（不引入新 tushare API），Phase B 引入龙虎榜与筹码数据。
> Phase C（盘前集合竞价二次预测）本次不实施，本文档不涉及。
>
> 现有架构保持不变：
> - 三级 sector_strength fallback、双轮漏斗、final_ranking 全局重排、R3 多 LLM 辩论。
> - DB 存 raw、prompt 给 normalized、EvidenceItem 强制带 unit、`extra='forbid'` 防新增字段。
> - 所有新增字段沿用 `FIELD_UNITS_RAW` 统一注册单位。

---

## 0. Gemini 因子覆盖差距速查

| Gemini 建议因子 | 现状 | 归属阶段 |
|---|---|---|
| 振幅 | 缺：daily 给了 high/low/close 但未派生 amplitude | **A** |
| 封单比（封单/成交额） | 缺：fd_amount_yi 与 amount_yi 都有，未给比率 | **A** |
| 均线多头（ma5/10/20） | 缺：prev_daily 是原始行情，未派生均线 | **A** |
| 近 30 日涨停次数 | 缺：daily_lookback 默认 10 日，无法覆盖 30 日窗口 | **A** |
| 涨停梯队趋势（连板率变化） | 缺：仅给 T 日 limit_step，未与 T-1 比较 | **A** |
| 昨日炸板率 | 缺：未取 T-1 limit_list_d 的炸板/失败统计 | **A** |
| 昨日涨停今日表现（赚钱效应） | 缺：未做 T-1 涨停 × T 日 daily 的 join | **A** |
| 龙虎榜席位 | 缺：metadata.optional 列出但 collect_round1 未实际拉取 | **B** |
| 筹码集中度 / 获利盘 | 缺：cyq_perf 未接入（账户已具权限，直接接入） | **B** |

未列入的因子（如 K 线形态识别、连板基因长尾）：当前 prev_daily 已足够 LLM 自行推理，且 DESIGN §2.8 明确"框架轻量、推理交给 LLM"，本次不做硬编码。

---

## 1. Phase A — 派生因子补齐

**性质：纯计算，不引入新 tushare API；只扩展现有 daily/limit_list_d/limit_step 的窗口与查询范围。**

### 1.1 数据窗口扩展

| 参数 | 当前默认 | 调整后 | 影响 |
|---|---|---|---|
| `daily_lookback` | 10 | **30** | 满足 ma20 + up_count_30d 计算 |
| `moneyflow_lookback` | 5 | 5（不变） | 已够用 |
| 新增 T-1 数据拉取 | — | **拉 T-1 的 `limit_list_d` 和 `limit_step`** | 用于"昨日炸板率"+"赚钱效应"+"梯队趋势" |

实现入口：`data.py::collect_round1` 在第 6 步（limit_step 之后、第 7 步 history window 之前）插入 `_collect_yesterday_context(tushare, prev_trade_date)`，返回 dataclass `YesterdayContext`。`prev_trade_date` 通过 `calendar.pretrade_date(trade_date)` 取，**不依赖任何新 API**。

CLI 同步暴露：

```bash
deeptrade limit-up-board run --daily-lookback 30   # 默认改为 30
```

### 1.2 新增派生字段定义

#### A1 — 振幅 `amplitude_pct`

| 项 | 值 |
|---|---|
| 计算公式 | `(high - low) / pre_close * 100` |
| 数据来源 | `daily` 当日行（已有） |
| 单位 | `%` |
| 注册位置 | 不进 `FIELD_UNITS_RAW`（派生字段，无 raw 单位概念）；prompt 直接给 `amplitude_pct: 7.32` |
| candidate 字段名 | `amplitude_pct` |
| 用途 | R1 量价维度评估"是否一字板/T字板/振幅炸板" |

#### A2 — 封单比 `fd_amount_ratio`

| 项 | 值 |
|---|---|
| 计算公式 | `fd_amount / amount * 100`（注意：两者都是 `limit_list_d` 字段，单位都是元，比值无量纲） |
| 单位 | `%` |
| candidate 字段名 | `fd_amount_ratio` |
| 用途 | Gemini "封单比 > 10% 通常代表强势"——给 LLM 一个**显式比率**，避免心算 |
| 边界 | `amount` 为 0 或 None 时输出 `null`，并写入 `missing_data` |

#### A3 — 均线 `ma5` / `ma10` / `ma20`（含多头排列标记）

| 项 | 值 |
|---|---|
| 计算 | 基于 prev_daily 30 行 `close` 序列的简单移动平均（含 T 日收盘） |
| 单位 | `元` |
| candidate 字段 | `ma5`、`ma10`、`ma20`、`ma_bull_aligned`（bool：`close > ma5 > ma10 > ma20`） |
| 用途 | 替 LLM 把"均线多头"这一形态判断显式化；rationale 中可直接引用 |
| 边界 | 历史不足 N 日时返回 `null` 并加 `missing_data` |

#### A4 — 近 30 日涨停次数 `up_count_30d`

| 项 | 值 |
|---|---|
| 计算 | 在 prev_daily 30 行中，对 `pct_chg ≥ 9.8` 的天数计数（10cm 主板，主板已被前置过滤） |
| 单位 | `次` |
| candidate 字段 | `up_count_30d` |
| 用途 | Gemini "历史连板基因"——`up_stat` 携带的是格式化字符串，这里给纯数值便于 LLM 引用 |
| 注 | 不区分一字/T字；Gemini 建议的"历史最大连板数"已在 `up_stat` 中携带 |

#### A5 — 涨停梯队趋势 `limit_step_trend`

写入 `market_summary`（不是 candidate 维度），结构如下：

```jsonc
"limit_step_distribution": { "1": 38, "2": 12, "3": 5, "4": 2 },          // T 日（已有）
"limit_step_distribution_prev": { "1": 25, "2": 10, "3": 6 },             // 新增 — T-1 日
"limit_step_trend": {
    "max_height": 4,           // T 日全市场最高板高
    "max_height_prev": 3,      // T-1 日
    "high_board_delta": +1,    // 4-3
    "total_limit_up_delta": +12,
    "interpretation": "spectrum_lifting" | "spectrum_collapsing" | "stable"
}
```

`interpretation` 由 `data.py` 派生（不让 LLM 起名）：

| 条件 | label |
|---|---|
| `high_board_delta > 0` AND `total_limit_up_delta > 0` | `spectrum_lifting`（拉升期） |
| `high_board_delta < 0` OR `total_limit_up_delta < -10` | `spectrum_collapsing`（退潮期） |
| 其他 | `stable` |

prompt 显式约束：**LLM 不可改写 interpretation 标签，但可解释其含义**。

#### A6 — 昨日炸板率 `yesterday_failure_rate`

写入 `market_summary`：

```jsonc
"yesterday_failure_rate": {
    "trade_date_prev": "20260506",
    "u_count": 65,                  // T-1 涨停封板数（limit='U'）
    "z_count": 18,                  // T-1 炸板数（limit='Z'）
    "rate_pct": 21.7,               // z / (u + z) * 100
    "interpretation": "high" | "moderate" | "low"
}
```

阈值（写在 `data.py`，不让 LLM 解读）：
- `rate_pct ≥ 25` → `high`（亏钱效应严重）
- `rate_pct ≤ 10` → `low`（赚钱效应良好）
- 否则 `moderate`

实现：`limit_list_d(trade_date=T-1)` 不再过滤 `limit='U'`，而是同时拉 U + Z 两类。

#### A7 — 昨日涨停今日表现 `yesterday_winners_today`

写入 `market_summary`：

```jsonc
"yesterday_winners_today": {
    "trade_date_prev": "20260506",
    "n_winners": 65,                // T-1 涨停封板数
    "n_continued_today": 42,        // 今日仍涨停的家数（≥9.8%）
    "continuation_rate_pct": 64.6,  // 连板率
    "n_negative_today": 8,          // 今日 pct_chg < -2% 的家数
    "avg_pct_chg_today": 4.8,       // 今日涨幅均值（%）
    "interpretation": "strong_money_effect" | "weak_money_effect" | "neutral"
}
```

阈值：
- `continuation_rate_pct ≥ 50` AND `avg_pct_chg_today ≥ 3` → `strong_money_effect`
- `continuation_rate_pct ≤ 25` OR `avg_pct_chg_today ≤ 0` → `weak_money_effect`
- 否则 `neutral`

实现：用 T-1 limit_list_d (U) 的 ts_code 集合，去 T 日 daily 中筛 pct_chg。

> A6 与 A7 同时给：A6 反映 T-1 当日的"主力被打"，A7 反映 T-1 涨停股次日（T）的"溢价能力"——Gemini 答复中两者都被强调，由 LLM 综合判断。

### 1.3 R1 / R2 prompt 改动

**R1 user prompt 变更（`prompts.py::r1_user_prompt`）**

每只 candidate 在原有字段后追加：

```
amplitude_pct, fd_amount_ratio, ma5, ma10, ma20, ma_bull_aligned, up_count_30d
```

R1_SYSTEM 的【分析维度】节扩为：

```diff
 - 封板强度：first_time / last_time / open_times / fd_amount_yi / limit_amount_yi / fd_amount_ratio
-- 量价：pct_chg / amount_yi / turnover_ratio
+- 量价：pct_chg / amount_yi / turnover_ratio / amplitude_pct（振幅过大警惕分歧炸板）
+- 形态：ma5 / ma10 / ma20 / ma_bull_aligned（多头排列时增强）
+- 历史基因：up_count_30d（近 30 日涨停次数）/ up_stat
+- 市场情绪：参考下方【市场摘要】中 limit_step_trend / yesterday_failure_rate / yesterday_winners_today
 - 风险：是否一字板 / 过度连板 / 题材孤立 / 缺数据
```

【evidence 要求】节追加一条硬约束：
> 当 candidate 的 `missing_data` 包含某字段时，evidence 中**不得**引用该字段。

**R2 user prompt 变更**

`market_context` 直接复用 R1 的 `market_summary`（已含 limit_step_trend / yesterday_failure_rate / yesterday_winners_today）；候选行同样附带 A1–A4 字段。

R2_SYSTEM 的【判断重点】节追加：

```
- 市场亏钱效应（yesterday_failure_rate.interpretation == 'high'）下，所有 confidence 自动下调一档（high → medium，medium → low）；rationale 需明示。
- 涨停梯队拉升（limit_step_trend.interpretation == 'spectrum_lifting'）下，最高板地位的标的可适度上调 continuation_score，但 score 仍受 0–100 上限约束。
- 不允许引用 missing_data 中的字段；可引用所有派生字段（amplitude_pct / fd_amount_ratio / ma_*  / up_count_30d）。
```

### 1.4 schema 改动

`schemas.py` **保持向后兼容**：所有 A1–A4 字段在 candidate dict 中是输入侧字段（prompt payload），**不进入 LLM 输出 schema**——LLM 仍只输出 `StrongCandidate` / `ContinuationCandidate`。EvidenceItem 的 `field` 字段已是 `str`，可自然引用新字段名。

### 1.5 表结构改动

无新表。`lub_limit_list_d` 已支持 limit ∈ {U, Z}（schema 上 `limit` 是 PK 一部分，写入时去掉前置过滤即可）。`lub_daily` 已存储完整 daily，A1/A3/A4 都是查询时计算，不落库。

### 1.6 测试与验收

| 项目 | 验收标准 |
|---|---|
| 单元测试 | `_collect_yesterday_context` 在缺 T-1（节后第一日，pretrade_date 仍可解析）情形下不抛异常；A6/A7 的 interpretation 边界值（10/25/50/3）有 case 覆盖 |
| 集成测试 | 用现有 fixture 跑 R1/R2 一遍，断言 prompt 中包含 `amplitude_pct` / `yesterday_failure_rate` 关键串 |
| 兼容性 | 老 run 的 reports 仍可 render（render 路径新字段缺失时显示 `—`） |
| 文档 | `docs/limit-up-board.md` 数据矩阵新增"近 N 日窗口默认 30"备注；CHANGELOG 记 Phase A |

---

## 2. Phase B — 新接入数据（龙虎榜 + 筹码）

**性质：增加 tushare API 依赖。**`cyq_perf` / `top_list` / `top_inst` 在当前账户权限下均直接调用，全部按 `required` 接入。
失败语义统一：unauthorized → run 终止并提示用户检查 tushare 权限；server / rate_limited → 触发现有重试机制；超过重试上限按 required 失败处理。
**不引入新的权限分类**（如 conditional_required）：现有 required/optional 二分法 + 正确的 join/null 语义已足以表达。

### 2.1 Phase B 数据接入

#### B1 — top_list / top_inst（龙虎榜）

| 项 | 设定 |
|---|---|
| 必需性 | **required**（账户已具权限） |
| 拉取范围 | `top_list(trade_date=T)` 和 `top_inst(trade_date=T)`，按 ts_code 与 candidate 集合 inner join |
| 落库 | 新增 `lub_top_list`（key: trade_date+ts_code）、`lub_top_inst`（key: trade_date+ts_code+exalter） |
| 派生字段（写到 candidate） | `lhb_net_buy_yi`（龙虎榜净买入，亿元）、`lhb_inst_count`（机构席位数）、`lhb_famous_seats`（知名游资席位字符串数组，由白名单匹配；进入 R2 prompt 时由 pipeline 拍平为 `lhb_famous_seats_count` + `lhb_famous_seats_text` 标量伴生字段，详见 §2.3） |
| **candidate 未上榜的语义** | candidate（R1 selected=true）若不在 top_list 返回中 → `lhb_*` 全部为 `null`；**这是合法事实，不进 `data_unavailable`，不进 candidate.missing_data**——因为它表示"该股没有触发异动上榜"，不是数据缺失 |
| 失败语义 | 接口 unauthorized / server / rate_limited 超出重试 → 按 required 抛出，run failed |

**知名游资席位白名单**（写在 `data.py` 常量中，便于扩展）：

```python
FAMOUS_SEATS_HINTS: list[str] = [
    "拉萨团结路", "拉萨东环路", "拉萨金融城南环路",   # 拉萨系
    "宁波桑田路", "宁波解放南路",                      # 宁波系
    "深圳益田路荣超商务中心",                          # 章盟主常用
    "中信证券上海溧阳路",                              # 赵老哥常用
    "华泰证券厦门厦禾路",                              # 厦门帮
    # ...（首版给 ~15 条，后续可由 channel 配置覆盖）
]
```

匹配逻辑：`top_inst.exalter` 字段做子串匹配（不区分大小写），命中即写入 `lhb_famous_seats`。

> 仅给"是否命中"，不让 LLM 推断"哪一位游资 = 谁"——保持匿名性，符合 R3 辩论的 peer 匿名化精神。

#### B2 — cyq_perf（筹码集中度 / 获利盘）

| 项 | 设定 |
|---|---|
| 必需性 | **required**（账户 8000 积分已具权限，按必需接口处理：失败 → run 终止 + 用户提示） |
| API | `cyq_perf(trade_date=T, ts_code=…)`（按 candidate ts_code 批量拉） |
| 落库 | `lub_cyq_perf`（key: trade_date+ts_code） |
| 派生字段（写到 candidate） | `cyq_winner_pct`（获利盘比例，%）、`cyq_top10_concentration`（前 10% 持仓集中度，%，由 `cost_5pct` / `cost_15pct` / `cost_85pct` / `cost_95pct` 派生）、`cyq_avg_cost_yuan`（平均成本，元；来自 `weight_avg`）、`cyq_close_to_avg_cost_pct`（当日 close 相对 weight_avg 的偏离 %） |
| 单位 | 直接 % / 元，无需 normalize |

> 不再设"路径 B（daily 累计近似）"和 `chip_data_source` 切换——cyq_perf 即为唯一路径。
> 单只 candidate 在 cyq_perf 返回中无记录（罕见）→ 该 candidate.missing_data 写入 cyq 字段名，不影响其他 candidate。

### 2.2 plugin metadata 改动

`deeptrade_plugin.yaml`：

```yaml
permissions:
  tushare_apis:
    required:
      # 既有清单不变
      - cyq_perf                  # 新增（筹码集中度 / 获利盘）
      - top_list                  # 新增（龙虎榜个股，从 optional 上移）
      - top_inst                  # 新增（龙虎榜机构明细，从 optional 上移）
    optional:
      # 既有可选项保留；删除 top_list / top_inst（已上移到 required）
      - limit_list_ths
      - limit_cpt_list
      - ths_hot
      - dc_hot
      - stk_auction_o
      - anns_d
      - suspend_d
      - stk_limit
```

**框架侧无改动**——保留现有 required / optional 二分法。
"接口可用但 candidate 未上榜"在数据层用空 join + null 表达；不需要新增权限分类。

### 2.3 R1 / R2 prompt 改动

**R1 不动**——chip data 在第二轮才用。R1 仍以"封板质量 + 板块 + 量价 + 形态"为主。

**R2 user prompt 候选行追加**：

```
# 筹码（cyq_perf）
cyq_winner_pct, cyq_top10_concentration, cyq_avg_cost_yuan, cyq_close_to_avg_cost_pct

# 龙虎榜（命中时填，未命中为 null）
lhb_net_buy_yi, lhb_inst_count, lhb_famous_seats_count, lhb_famous_seats_text
```

> **注：** 候选行数据层存的是 `lhb_famous_seats: list[str]`；进入 R2 prompt
> 前由 `_r2_row_from_selected` 拍平为 `lhb_famous_seats_count` (int) +
> `lhb_famous_seats_text` (str，分号分隔)，以满足 `EvidenceItem.value` 的
> `str|int|float|None` 标量约束。

R2_SYSTEM 的【判断重点】节追加：

```
- 筹码维度：
  · cyq_winner_pct > 70% 视为"获利盘抛压重"，下调 confidence；
    cyq_close_to_avg_cost_pct < -10% 视为"严重套牢盘解套"，谨慎评估；
    cyq_top10_concentration > 60% 视为"筹码高度集中"，可作为正面 evidence。
  · 仅当数据存在时引用；missing_data 中的字段不得引用、不得编造结论。
- 龙虎榜：
  · lhb_famous_seats_count > 0 时，可作为"游资认可"的正面 evidence；
    但 lhb_net_buy_yi < 0 时不得作为正面 evidence。
  · LLM 不可推断具体游资身份，仅可在 interpretation 中引用
    lhb_famous_seats_text 的字符串原文片段。
  · 作为 key_evidence 引用时，field 用 lhb_famous_seats_count（value 填整数）
    或 lhb_famous_seats_text（value 填字符串），严禁把席位列表当数组写入 value
    （违反 EvidenceItem.value 标量约束）。
```

### 2.4 schema 改动

R2 输出 schema 不变（`ContinuationCandidate` 已可用 EvidenceItem 引用任意输入字段名）。
不引入 run-level `chip_data_source` 字段——cyq_perf 是唯一路径，无需切换标识。

### 2.5 表结构与 migration

新增表：

```sql
-- migrations/20260508_001_lub_chips_and_lhb.sql
CREATE TABLE IF NOT EXISTS lub_top_list (
    trade_date VARCHAR NOT NULL,
    ts_code VARCHAR NOT NULL,
    name VARCHAR,
    close DOUBLE,
    pct_change DOUBLE,
    turnover_rate DOUBLE,
    amount DOUBLE,
    l_sell DOUBLE,
    l_buy DOUBLE,
    l_amount DOUBLE,
    net_amount DOUBLE,
    net_rate DOUBLE,
    amount_rate DOUBLE,
    float_values DOUBLE,
    reason VARCHAR,
    PRIMARY KEY (trade_date, ts_code)
);

CREATE TABLE IF NOT EXISTS lub_top_inst (
    trade_date VARCHAR NOT NULL,
    ts_code VARCHAR NOT NULL,
    exalter VARCHAR NOT NULL,
    side INTEGER,                  -- 0=buy, 1=sell
    buy DOUBLE,
    buy_rate DOUBLE,
    sell DOUBLE,
    sell_rate DOUBLE,
    net_buy DOUBLE,
    reason VARCHAR,
    PRIMARY KEY (trade_date, ts_code, exalter, side)
);

CREATE TABLE IF NOT EXISTS lub_cyq_perf (
    trade_date VARCHAR NOT NULL,
    ts_code VARCHAR NOT NULL,
    his_low DOUBLE,
    his_high DOUBLE,
    cost_5pct DOUBLE,
    cost_15pct DOUBLE,
    cost_50pct DOUBLE,
    cost_85pct DOUBLE,
    cost_95pct DOUBLE,
    weight_avg DOUBLE,
    winner_rate DOUBLE,
    PRIMARY KEY (trade_date, ts_code)
);
```

并在 `deeptrade_plugin.yaml::tables` 添加三张表的注册 + `purge_on_uninstall: true`。

### 2.6 测试与验收

| 项目 | 验收标准 |
|---|---|
| 三个 required 接口的失败语义 | cyq_perf / top_list / top_inst 任一 unauthorized → run 立即失败并提示用户检查 tushare 权限；server/rate_limited → 走现有重试 |
| 龙虎榜未上榜语义 | candidate 不在 top_list 返回中时，`lhb_*` 字段为 null；**断言** `data_unavailable` 不包含 top_list/top_inst（区别于"接口失败"） |
| 龙虎榜白名单 | exalter 子串匹配大小写不敏感、命中后写入 `lhb_famous_seats`；首版 ~15 条已覆盖测试用例 |
| missing_data 传播 | 当某 candidate 在 cyq_perf 返回中无记录时，仅该 candidate.missing_data 包含 cyq 字段，run 整体仍可完成 |
| 集成测试 | 跑一个 fixture run，断言 reports 中能看到龙虎榜与筹码 evidence 的 LLM 引用 |
| migration | 新表迁移 checksum 写入 yaml；已存在的 run history 不受影响 |

---

## 3. 实施顺序与回滚

| 步骤 | 涉及文件 | 可独立合入？ |
|---|---|---|
| **A1** 数据窗口扩展（lookback=30）+ A1–A4 派生字段 | `data.py`、`prompts.py` | ✅ 可独立 |
| **A2** 市场情绪三件套（A5/A6/A7）+ R1/R2 prompt 维度扩 | `data.py`、`prompts.py` | ✅ 可独立 |
| **A3** 测试 + 文档 + CHANGELOG | tests、docs | ✅ |
| **B1** 龙虎榜（top_list / top_inst，required） + 表迁移 + 白名单 | yaml、migrations、`data.py`、`prompts.py` | ✅ 可独立 |
| **B2** 筹码（cyq_perf，required） + 表迁移 | yaml、migrations、`data.py`、`prompts.py` | ✅ 可独立 |
| **B3** 测试 + 文档 + CHANGELOG | tests、docs | — |

每一步都对应一个独立 git commit，验证一步合一步；任一步骤如发现 prompt 退化（output_tokens 暴涨 / set_mismatch 飙升）可单独回滚。

## 4. 与现有设计原则的一致性检查

| 原则（DESIGN §） | 本方案是否满足 |
|---|---|
| §1 框架轻量 | ✅ 全部改动落在插件内；不新增框架分类、不修改 permissions / tushare_client |
| §2.8 plugin type 不扩张 | ✅ 仍是 strategy 类型，无新插件类型 |
| §11.2 数据矩阵显式声明 | ✅ Phase B 在 yaml 中将 cyq_perf / top_list / top_inst 全部登记为 required |
| §11.3 三级 fallback 模式 | ✅ 保留 sector_strength_source；本期不引入新的"source 切换"字段（cyq_perf 单一路径） |
| §12.4–12.5 R1/R2 prompt 结构 | ✅ 保留五维分析骨架，仅扩字段；R1 不引入筹码维度 |
| C5 单位约定 | ✅ 派生字段单位明确（%/元/亿/次），EvidenceItem 仍带 unit |
| F2 / M3 防幻觉 | ✅ missing_data 规则更严格（不得引用） |
| F-H1 set 等价校验 | ✅ 无 schema 输出层改动，校验逻辑不变 |

## 5. 不在本方案的范围

- **盘前集合竞价（stk_auction_o）二次预测** —— 即 Phase C，本次不做。
- **筹码数据的"daily 累计近似"路径** —— 当前账户已具 cyq_perf 权限，按 required 单一路径接入，不预留 fallback 路径。
- **K 线形态硬编码识别（"老鸭头"等）** —— 让 LLM 在 prev_daily + ma_* 上推理。
- **新插件类型（如"factor 插件"）** —— 派生因子保留在策略本体内。
- **Gemini 答复中提到的"15 年游资人设" prompt 改写** —— 当前 system prompt 的"研究助手 + 硬性纪律"已比"15 年游资"更适合防幻觉场景，不动。
