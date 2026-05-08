# 打板策略 (Limit-Up Board) AI量化进阶优化方案 (V2)

基于对当前 `limit-up-board` 插件代码的深度调研（当前已实现了 Phase A 和 Phase B 的诸多高阶因子，如筹码集中度 `cyq_perf`、龙虎榜 `lhb` 席位解析、市场情绪三件套、多阶段 LLM 漏斗及 R3 辩论等），现从 **更深度的 AI 与量化结合视角** 提出下一阶段的进阶优化项。

目前的架构在工程严谨性（严格的 JSON Schema 校验、数据对齐、容错处理）上已经非常成熟，以下优化建议主要集中在 **提升 LLM 推理上限**、**引入细粒度微观数据** 以及 **多智能体博弈（Multi-Agent）深度化** 上。

---

## 1. LLM Prompt 与 Schema 结构优化 (Chain-of-Thought 强制化)

### 1.1 调整 Pydantic Schema 字段顺序（关键优化）
**现状问题**：在当前的 `StrongCandidate` 和 `ContinuationCandidate` schema 中，结论性字段（如 `selected`, `prediction`, `continuation_score`）排在解释性字段（`rationale`, `key_evidence`）**之前**。对于自回归（Autoregressive）大语言模型而言，这意味着模型必须先在没有输出推理过程的情况下"拍脑袋"给结论，然后再强行解释其结论，这会极大地限制模型的逻辑推理能力。
**优化建议**：
将 Schema 中的推理字段前置，强制 LLM 执行思维链（CoT）：
```python
class ContinuationCandidate(BaseModel):
    # ...
    # 1. 强制先列出证据
    key_evidence: list[EvidenceItem] 
    # 2. 强制进行综合推演 (新增一个专门的 CoT 字段)
    step_by_step_reasoning: str = Field(..., max_length=300)
    # 3. 最后再输出预测结果
    prediction: Literal["top_candidate", "watchlist", "avoid"]
    continuation_score: float
    confidence: Literal["high", "medium", "low"]
    # ...
```
**收益**：通过先生成 Evidence 和 Reasoning，模型的注意力机制（Attention）将被充分激活，最终输出的 `prediction` 和 `score` 的准确率和稳定性将得到质的飞跃。

### 1.2 动态少样本提示 (Dynamic Few-Shot Prompting)
**现状问题**：当前采用的是 Zero-Shot 设定，辅以大量的硬性纪律约束。
**优化建议**：构建一个轻量级的历史案例库（Vector DB 或简单的内存队列）。在组装 R2 Prompt 时，根据当前的市场情绪（如 `limit_step_trend`）动态检索 1-2 个近期相似环境下的“成功连板”与“晋级失败（如大面）”的真实个例，作为 Few-Shot 样本注入 Prompt。
*示例*：“参考案例：在相似的亏钱效应下，上周的 [个股X] 同样具备高集中度筹码和知名游资买入，但次日因板块无跟随而断板跌停。”
**收益**：让 LLM 能够感知“当前市场风格/近期记忆”，从而减少在极端行情下的刻舟求剑。

---

## 2. 量化因子扩展 (微观结构与周期动量)

### 2.1 引入集合竞价因子 (Auction Data - 原计划的 Phase C)
**现状问题**：目前主要依赖盘后（EOD）数据（`daily`, `moneyflow`），虽然有 `first_time` 等涨停时间，但缺乏对“次日接力意愿”最核心的指标——集合竞价。
**优化建议**：引入 T+1 日（或 T 日）的 `stk_auction_o` 数据。
*   **竞价爆量比**：集合竞价成交额 / T-1日总成交额。
*   **竞价涨幅**：反映真实承接力度。
**收益**：这是打板策略中胜率最高的因子之一。通过将其加入 R2 甚至新增的 R2.5 阶段，LLM 可以更精准地筛选出开盘即具备弱转强或一字预期的标的。

### 2.2 细粒度分时动量 (Intraday Momentum)
**优化建议**：接入 1分钟 或 5分钟 线数据（`stk_mins`）。
*   计算 **涨停前 30 分钟的成交量占比**。
*   衍生因子：`limit_up_steepness`（拉升斜率，是缓慢推升还是秒板）。
*   将这些分时特征抽象为定性标签（如 "脉冲拉升", "分歧烂板", "缩量秒板"）喂给 LLM。

### 2.3 板块生命周期判定 (Sector Lifecycle)
**现状问题**：`sector_strength` 目前仅计算 T 日的横截面强度（前 10 大概念）。
**优化建议**：计算目标板块在过去 3-5 天的涨停家数一阶导数（变化率）。
*   由 `data.py` 判定并打标板块阶段：`initiating` (发酵期), `accelerating` (高潮期), `exhausting` (退潮期)。
*   **收益**：LLM 在处理“退潮期”板块内的涨停股时，将能够更理智地给出 `avoid` 判定，规避板块补跌风险。

---

## 3. 多智能体架构优化 (Multi-Agent R3 Enhancement)

### 3.1 赋予 R3 辩论显式的专业 Persona (角色扮演)
**现状问题**：目前的 R3 辩论中，各个 LLM (peer_a, peer_b) 虽然可能底层调用的模型不同，但它们的 System Prompt 是一致的。
**优化建议**：在多并发预测阶段（或者专设的辩论阶段），显式地为不同节点分配不同的分析视角（Persona）：
1.  **趋势龙头拥趸 (The Trend Speculator)**：System Prompt 强化关注 `limit_times`, `up_stat`, 龙虎榜净买入，鼓励高风险高收益。
2.  **风控与筹码专家 (The Risk Manager)**：System Prompt 强化关注 `cyq_winner_pct` (获利盘抛压), `amplitude_pct` (炸板风险), 历史阻力位。
3.  **情绪博弈大师 (The Sentiment Analyst)**：专注于挖掘 `yesterday_failure_rate` 与日内封单比（`fd_amount_ratio`）之间的背离。
**收益**：引入正交维度的分析框架。当“风控专家”与“趋势拥趸”达成共识时，该候选股的可靠度将远超同质化模型的重复验证。

### 3.2 逻辑自洽性校验 (Self-Correction Loop)
**优化建议**：在 R2 阶段输出后，引入一个基于规则的轻量级“打回器”（Bouncer）。
*   *校验规则示例*：如果 LLM 给出的 `confidence` 为 `high` 且 `prediction` 为 `top_candidate`，但其引用的 `key_evidence` 中包含了 `cyq_winner_pct > 80` 且 `lhb_net_buy_yi < 0`（机构净流出）。
*   *动作*：不立即接受该结果，而是构建一条纠错 Prompt（"你的证据显示抛压极重且资金流出，但你给出了最高级别的看好，请重新审视你的逻辑漏洞"），强制 LLM 进行一轮 Self-Correction。

---

## 总结

当前项目已经打下了极其扎实的数据工程和 LLM Pipeline 基础。下一步的优化方向不再是单纯增加日线指标，而是：
1. **重构 Prompt 结构**，顺应大模型的 CoT 运作机制（结论后置）。
2. **升维数据视角**，从日线（EOD）下沉到分时（Intraday）和竞价（Auction）。
3. **深化 Agent 博弈**，利用多维度 Persona 消除单一 AI 视角的盲区。