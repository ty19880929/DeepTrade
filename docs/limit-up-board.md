# 打板策略（limit-up-board）

> 内置策略：双轮 LLM 漏斗（强势标的分析 → 连板预测），多批时自动 final_ranking 全局校准。

## 工作流程

```
T 日 = 最近一个【已收盘】交易日
        ┌─ today           if today.is_open AND now ≥ close_after (默认 18:00)
   T = ┤
        └─ pretrade(today) 否则
T+1 日 = trade_cal 中 T 之后第一个开市日

Step 1  数据装配（required → 强校验；optional → 缺失 + 提示）
        ├─ 主板 + limit_list_d.U + 非 ST + 非停牌
        └─ 流通市值 < N 亿 + 股价 < M 元（v0.4，可通过 `settings` 子命令调整）
Step 2  R1 强势标的分析       stage = strong_target_analysis     batch by token budget
Step 3  增量数据装配（仅 R1 selected）
Step 4  R2 连板预测           stage = continuation_prediction    单批默认
Step 4.5 final_ranking 全局校准   stage = final_ranking         仅 R2 多批触发
Step 5  落库 + 渲染 + reports/<run_id>/
```

## 数据需求矩阵（DESIGN §11.2）

### 必需接口

| 接口 | 用途 | 积分门槛 |
|---|---|---|
| `stock_basic` | 沪深主板池过滤、行业、上市状态 | 2000 |
| `trade_cal` | 找最近交易日 / 前后交易日 | 2000 |
| `stock_st` | 排除 ST / *ST | 3000 |
| `daily` | 价格、近 N 日走势 | 基础 |
| `daily_basic` | 换手 / 量比 / 估值 / 市值 | 2000 |
| `limit_list_d` | 涨停明细：封单 / 首末封板 / 炸板 / 连板 | 5000 |
| `limit_step` | 全市场连板天梯 | 8000 |
| `moneyflow` | 当日 + 近 3 日资金流 | 2000 |
| `top_list` | 龙虎榜个股净买入（v0.8 起）| 2000 |
| `top_inst` | 龙虎榜机构席位（v0.8 起，配合白名单匹配） | 2000 |
| `cyq_perf` | 筹码分布：获利盘 / 集中度 / 平均成本（v0.8 起） | 5000 |

任一缺失 → run 立即终止，提示用户去 tushare 申请权限。

> 注：`top_list` / `top_inst` 接口可用但 candidate 没有触发龙虎榜（未上榜）时，
> 该候选股的 `lhb_*` 字段为 null——这是合法事实，**不进入 `data_unavailable`**。
> 同理，`cyq_perf` 返回中无某 candidate 的记录（罕见）→ 该 candidate 的 `cyq_*`
> 为 null，由 LLM 自动填入 `candidate.missing_data`，不影响其他候选股或 run 整体。

### 可选增强（缺失则降级）

`limit_list_ths` / `limit_cpt_list` / `ths_hot` / `dc_hot` / `stk_auction_o` / `anns_d` / `suspend_d` / `stk_limit`

## 板块强度三级 fallback

```
limit_cpt_list  →  lu_desc_aggregation  →  industry_fallback
   (官方权威)         (同花顺涨停原因聚合)      (行业粗聚合，可信度最低)
```

prompt 中显式声明 `sector_strength_source` 字段，让 LLM 看到当前数据来源后**自动**降低 `confidence`（DESIGN §11.3 / F2）。

## 单位约定（C5）

| 字段 | DB 存储（raw） | Prompt 字段（normalized） |
|---|---|---|
| 封单 / 板上成交 / 总市值 / 流通市值 | 元 | `*_yi`（亿） |
| 主力净额 / 大单净额 | 万元 | 保留原 `万` 或 `*_wan` |
| 涨跌幅 / 换手率 | % | 同名，保留 2 位小数 |

EvidenceItem 强制带 `unit` 字段，LLM 不会混淆量纲。

## 提示词节选

### R1 — 强势标的分析（DESIGN §12.4.3）

system prompt 包含：

- 严禁外部搜索 / 编造的硬性纪律
- 5 项分析维度（封板强度 / 板块 / 梯队 / 量价 / 风险）
- evidence 1-4 条（F5 收紧）+ 每条必引输入字段
- rationale ≤ 80 字

user prompt 包含：

- trade_date / batch_no / batch_total / data_unavailable
- 市场摘要 JSON（limit_step 分布等）
- 板块强度摘要（含 `sector_strength_source`）
- 候选清单（normalized 字段已就绪）

### R2 — 连板预测（DESIGN §12.5.5）

判断重点：

- 是否处于主线强势板块（`sector_strength_source` 越靠 limit_cpt_list 越权威）
- 龙头 / 空间板地位（参考 limit_step 全市场最高连板数）
- 封板质量、资金延续性、过度一致性风险

输出含 `next_day_watch_points`（次日观察具体指标）+ `failure_triggers`（放弃信号）。

### final_ranking — 全局校准（仅 R2 多批触发，DESIGN §12.5.4）

system prompt：

- 严禁引入新事实，仅基于 finalists 的摘要 + 市场环境重排
- `final_rank` 必须是 1..N 连续置换
- `delta_vs_batch` ∈ {upgraded, kept, downgraded}

输入 finalists = 各批次 `top_candidate` + `watchlist` + 各批 `avoid` 中分数最高的若干（边界样本）。

## DeepSeek profile 三档（DESIGN §10.1）

| profile | R1 thinking | R2 thinking | final_ranking thinking | 推荐场景 |
|---|---|---|---|---|
| `fast` | ✘ | ✘ | ✘ | 成本敏感 / 快速调试 |
| `balanced` (默认) | ✘ | ✔ | ✔ | 推荐：R1 候选量大需要快，R2 决策少需要深 |
| `quality` | ✔ | ✔ | ✔ | 关键决策日 / 复盘 |

**stage 级 max_output_tokens**（F5）：R1/R2 默认 32k，final_ranking 8k。

## 报告产出

```
~/.deeptrade/reports/<run_id>/
├── summary.md                       # 完整 markdown，含红/黄横幅
├── round1_strong_targets.json       # R1 完整结果
├── round2_predictions.json          # R2 全量预测（含 batch_local_rank）
├── round2_final_ranking.json        # 仅多批时存在（M4）
├── data_snapshot.json               # 市场摘要 + 候选输入快照
└── llm_calls.jsonl                  # 每次 LLM 调用一行
```

横幅规则（DESIGN §12.8.3 + F4 / M5）：

| status | is_intraday | 横幅 |
|---|---|---|
| `success` | `false` | 无 |
| `success` | `true` | 黄色 INTRADAY MODE |
| `partial_failed` | `false` | 红色 PARTIAL |
| `partial_failed` | `true` | 红 + 黄两条叠加 |
| `failed` | `*` | 红色 FAILED |
| `cancelled` | `*` | 红色 CANCELLED |

## 关键参数（CLI）

v0.5 起，CLI 由插件自管（框架仅做 `deeptrade <plugin_id> ...` 透传）：

```bash
deeptrade limit-up-board run \
    [--trade-date YYYYMMDD] \
    [--allow-intraday] \
    [--force-sync] \
    [--daily-lookback N]      # 默认 30（满足 ma20 + up_count_30d）
    [--moneyflow-lookback N]

deeptrade limit-up-board sync       # 仅拉数+落库，不调 LLM
deeptrade limit-up-board history    # 本插件 run 历史（lub_runs 表）
deeptrade limit-up-board report <run_id> [--full]
```

- `--trade-date`：显式指定 T 日（最常用于回看 / 调参）。
- `--allow-intraday`：盘中模式，写 `data_completeness='intraday'`，日终模式严格拒绝命中（防数据污染）。
- `--force-sync`：忽略所有缓存，强制重拉。
- `--daily-lookback`：daily / daily_basic 历史窗口（trade days）。**默认 30**，
  支撑 ma20 与近 30 日涨停次数计算；调小到 < 20 会让形态因子降级为 null。
- `--no-dashboard`：禁用 Live Layout，回退到纯文本输出（适合管道 / CI）。

## 候选股市值/股价过滤（v0.4）

Step 1 在「主板 + 涨停 + 非 ST + 非停牌」漏斗末尾再加一层市值与股价上限过滤：

| 维度 | 字段（来源） | 默认上限 | null 行为 |
|---|---|---|---|
| 流通市值 | `limit_list_d.float_mv`（元 → 亿） | 100 亿 | 过滤掉 |
| 当前股价 | `limit_list_d.close`（元） | 15 元 | 过滤掉 |

阈值持久化在插件自有的 `lub_config` 表（与框架级 `app_config` 完全隔离），
通过 `settings` 子命令交互式管理：

```bash
deeptrade limit-up-board settings        # 交互修改两个阈值，回车保留当前值
deeptrade limit-up-board settings show   # 表格展示当前生效设置 + source
```

`source` 列含义：

- `default` — 表中无对应行，使用 `LubConfig` dataclass 中声明的默认值。
- `persisted` — 表中有行，使用持久化后的值。

`run` / `sync` 在 Step 1 之前会 emit 一条 LOG 事件展示当前生效阈值
（`运行配置: 流通市值 < ...亿、股价 < ...元`），dashboard / 日志中可见。
prompt 端通过 `market_summary.candidate_filter_summary` 暴露过滤前后数量
+ 阈值，LLM 看到后不会误以为候选池小是因为接口异常。

## Phase A 派生因子（v0.7+）

详见 `docs/limit-up-board-optimization-plan.md` §1。

候选股层（写入 prompt 候选行）：

| 字段 | 含义 | 边界行为 |
|---|---|---|
| `amplitude_pct` | T 日振幅（%） | high/low/pre_close 任一缺失 → null |
| `fd_amount_ratio` | 封单/成交额（%） | amount 为 0 / null → null |
| `ma5` / `ma10` / `ma20` | 简单移动平均（元） | 历史不足窗口期 → null |
| `ma_bull_aligned` | 多头排列（bool） | 任一 ma 为 null → null |
| `up_count_30d` | 近 30 日涨停次数 | 历史不足 30 日 → null |

市场情绪层（写入 market_summary）：

| 字段 | interpretation 取值 |
|---|---|
| `limit_step_trend` | `spectrum_lifting` / `spectrum_collapsing` / `stable` |
| `yesterday_failure_rate` | `high`（≥25%）/ `moderate` / `low`（≤10%） |
| `yesterday_winners_today` | `strong_money_effect` / `neutral` / `weak_money_effect` |

T-1 数据通过 `cal.pretrade_date(T)` 派生；若 T-1 子接口失败，对应 section 退化为
null + 加入 `data_unavailable`，不阻断 run。

## Phase B 因子（v0.8+，仅 R2 使用）

详见 `docs/limit-up-board-optimization-plan.md` §2。R1 不引入 chip / LHB 维度
（避免增加 R1 prompt 噪声）。

候选股层新增字段：

| 字段 | 来源 | 含义 / 阈值 |
|---|---|---|
| `lhb_net_buy_yi` | top_list.net_amount → 亿 | 龙虎榜净买入；负值不可作为正面 evidence |
| `lhb_inst_count` | top_inst | 该股当日龙虎榜机构席位数（unique exalter） |
| `lhb_famous_seats` | top_inst.exalter × 白名单 | 命中 ~15 条游资席位的子串匹配；命中即给 exalter 原文。**注：** 数据层为 `list[str]`，进入 R2 prompt 前被拍平为 `lhb_famous_seats_count` (int) + `lhb_famous_seats_text` (str，分号分隔)，以满足 `EvidenceItem.value` 的标量约束 |
| `cyq_winner_pct` | cyq_perf.winner_rate | 获利盘比例；> 70% 视为抛压重 |
| `cyq_top10_concentration` | cost_5pct/cost_95pct/weight_avg 派生 | `100 - (cost_95 - cost_5) / weight_avg × 100`，> 60% 视为高度集中 |
| `cyq_avg_cost_yuan` | cyq_perf.weight_avg | 加权平均成本 |
| `cyq_close_to_avg_cost_pct` | (close - weight_avg) / weight_avg × 100 | < -10% 视为严重套牢盘解套 |

**未上榜 ≠ 数据缺失**：candidate 不在当日 top_list 中时 `lhb_*` 为 null，但
`data_unavailable` 中**不会**出现 top_list/top_inst（接口本身是 ok 的）。LLM 应将
"全部为 null" 解读为"未触发龙虎榜异动"，而不是数据缺失。

## 已知限制 / 设计债

- **D1**（v0.4 计划）：`configure()` 改为 schema 驱动，CLI 自动生成 questionary 表单。
- **D2**（v0.4 计划）：每个 required API 按 metadata.probes 单独探测，提供更精细的 `validate` 阶段。
- 龙虎榜 / 公告分析 / 集合竞价增强模式（v0.2 后续接入）。
