# 成交量异动策略（Volume Anomaly）优化方案代码审查报告

**审查日期**：2026年5月9日
**审查目标**：验证 `volume_anomaly` 策略的代码实现是否对齐了《成交量异动策略 AI 量化优化方案》(`docs\volume_anomaly_optimization_plan.md`) 中的设计要求。

---

## 1. 筛选条件优化 (Screening Rules) 审查

**结论：基本完全实现，设计了合理的后备与容错机制，但在“多日量价堆积识别”的放宽上采取了较为保守的折中方案。**

*   ✅ **动态换手率阈值 (Dynamic Turnover Rate)**
    *   **代码实现**：在 `data.py` 的 `ScreenRules` 中新增了 `turnover_buckets` 字段。代码通过 `_resolve_turnover_bucket` 方法，基于 `daily_basic` 提供的 `circ_mv_yi`（流通市值）来动态匹配换手率的 `[turnover_min, turnover_max]` 区间。当缺失市值数据时，会自动 fallback 到全局的换手率阈值，容错处理良好。
*   ✅ **形态过滤精细化 (Candlestick Shape Control)**
    *   **代码实现**：在 `data.py` 中增加了 `upper_shadow_ratio_max`（上影线占全天振幅比例）的硬性过滤条件（默认 0.35），成功限制了“避雷针”形态的标的入池，防止高位诱多的误判。
*   ⚠️ **多日量价堆积识别 (Volume Accumulation)**
    *   **代码实现**：代码在 `ScreenRules` 中引入了“双轨制”成交量判定（Plan B），允许 `vol_t` 是短期内的极大值，或者是长期窗口内的前 N 名。**然而**，代码中仍保留了对 `vol_ratio_5d_min` (默认 2.0) 的硬性检查 (`if vol_ratio_5d < rules.vol_ratio_5d_min: continue`)。
    *   **评估**：这意味着策略虽然在绝对放量天数上给予了宽容，但依然强求异动日当天（T日）相对于前5日有至少 2 倍的爆发。方案中提到的“连续3-5天阶梯式温和放量（不需要单日暴量）”的纯粹形态可能依然会被 `vol_ratio_5d` 拦在门外。这是一个偏向保守的实现策略，但在当前框架下是可以接受的。

## 2. 补充量化因子 (Supplementary Factors) 审查

**结论：完美实现。所有的量化因子都已被精确提取，并且被打包成了 LLM 易于理解的纯量量纲。**

*   ✅ **波动率收缩特征 (VCP, Volatility Contraction Pattern)**
    *   **代码实现**：在 `data.py` 的 `_build_candidate_row` 中，新增了 ATR 和布林带宽度（BBW）的计算逻辑。产出了 `atr_10d_pct`、`atr_10d_quantile_in_60d` 以及 `bbw_compression_ratio`，完美刻画了 VCP 的三维收缩特征。
*   ✅ **相对强弱指标 (RPS, Relative Price Strength)**
    *   **代码实现**：计算了 `alpha_5d_pct`、`alpha_20d_pct`、`alpha_60d_pct`（相对沪深300等基准指数的超额收益），并引入了 `rel_strength_label` (`leading`, `in_line`, `lagging`) 进行直观的定性打标。
*   ✅ **关键阻力位距离 (Distance to Resistance)**
    *   **代码实现**：成功提取了过去 120 日和 250 日的最高价/最低价，并输出了 `dist_to_120d_high_pct` 与 `dist_to_250d_high_pct`，帮助模型判断是“创新高突破”还是“超跌反弹”。

## 3. LLM 提示词与推理优化 (Prompt & Reasoning) 审查

**结论：完美实现。通过重构 Pydantic Schema 与引入 Few-shot 示例，大幅提升了输出的结构化程度和判断的一致性。**

*   ✅ **引入假突破风险评估 (Bull Trap Risk)**
    *   **代码实现**：在 `prompts.py` 中明确将 `risk` 作为反向打分的维度（分越高风险越大），并在 System Prompt 中要求识别高位放量出货、诱多等特征。
*   ✅ **结构化思维链 (Structured Chain-of-Thought)**
    *   **代码实现**：在 `schemas.py` 中新增 `VADimensionScores` 强制要求对 `washout`、`pattern`、`capital`、`sector`、`historical` 和 `risk` 六个维度独立打分 (0-100)，并在 `key_evidence` 中强制引用数据字段，确保了推理过程不悬空。
*   ✅ **交易视角的盈亏比预估 (Risk/Reward Estimation)**
    *   **代码实现**：`VATrendCandidate` Schema 中成功引入了 `next_session_watch` 和 `invalidation_triggers`（失效触发条件），迫使模型从“预测者”转化为具有风险意识的“交易员”。
*   ✅ **Few-Shot 示例对齐标准**
    *   **代码实现**：新增了专门的 `prompts_examples.py` 文件，提供了教科书级 VCP 突破（示例 A）和高位诱多（示例 B）两个 JSON 示例，这对于对齐不同大模型（如 Claude/GPT 等）的尺度极有价值。

## 4. 全局 AI 量化架构演进 (System-Level Architecture) 审查

**结论：基础闭环已搭建完成，为未来的高阶演进留出了接口。**

*   ✅ **构建闭环的自我进化系统基础 (Feedback Loop)**
    *   **代码实现**：通过 `stats.py` 和底层的 `realized_returns`（T+3/T+5收益）收集，系统已经具备了自动统计打分与胜率关联性的能力。`run_stats_query` 甚至实现了基于纯 SQL 的 Pearson 相关系数（CORR）计算，直接量化验证 LLM 的 `dimension_scores` 对真实收益的预测能力。
*   ⏳ **日内特征提取与多专家智能体 (Intraday & Multi-Agent)**
    *   **代码实现**：未在本次变更中发现。这属于长远规划，当前版本未实现符合预期。

---

## 总结
本次代码重构是一次**高质量的工程落地**。不仅原汁原味地还原了各项高阶量化因子，而且在 Prompt 工程上展现了极高的严谨性（通过严格的 JSON Schema 与字段引用强制约束 LLM）。策略在判断“主升浪启动点”的精确度上应当会有实质性的飞跃。