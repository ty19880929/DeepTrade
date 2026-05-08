# DeepTrade 文档与代码实现一致性评审报告

这是一份关于 DeepTrade 项目文档设计与实际代码实现之间不一致情况的详细评审报告。

经过对核心架构文档（`DESIGN.md`, `PLAN.md`）、策略与开发者说明文档（`docs/*.md`），以及框架和内建插件的实际源码（`deeptrade/core`, `deeptrade/plugins_api`, `deeptrade/strategies_builtin` 等）的深入交叉对比分析，我整理出了以下设计要求和代码实现不一致的具体内容。

考虑到 `DESIGN.md` 的 §0.x 章节已经申明了项目经历了 v0.5/v0.6 的破坏性重构，评审原则以**实际运行的代码逻辑为最终事实依据**。

---

## 一、 框架接口与上下文定义（API & Context）

### 1. `PluginContext` 与 `ChannelContext` 命名与位置异常
* **文档描述**：`DESIGN.md` §18.4 中明确定义了对于通知渠道插件，应使用 `ChannelContext` 这一更窄的服务束（`def validate_static(self, ctx: ChannelContext) -> None: ...`）。
* **代码实现**：
  * 在代码 `deeptrade/plugins_api/channel.py` 中，该上下文类被命名为 `PluginContext`，而非文档中的 `ChannelContext`。
  * 更具架构冲突性的是，基础插件协议文件 `deeptrade/plugins_api/base.py` 中的 `Plugin.validate_static` 方法，其类型提示所使用的 `PluginContext` 是**从 `deeptrade.plugins_api.channel` 中导入的**。这使得一个通用的顶层 Base Protocol 在结构上反向依赖了特定类型（Channel）下的上下文定义。

## 二、 策略插件权限与数据接口（Tushare APIs）

### 1. `limit-up-board` (打板策略) 接口的 Mandatory / Optional 级别冲突
* **文档描述**：`DESIGN.md` §11.2 明确将 `top_list`（龙虎榜每日明细）和 `top_inst`（龙虎榜机构席位）列为 **可选增强（`optional`）** 接口。
* **代码实现**：
  * 在 `deeptrade/strategies_builtin/limit_up_board/deeptrade_plugin.yaml` 中，`top_list` 和 `top_inst` 被硬性配置在 `required` 列表下。
  * 在 `data.py` 数据装配层，这两个接口失败会按 required 逻辑导致 run 终止。
  * *(注：在 `docs/limit-up-board.md` 的更新说明中也将其列为了 v0.8 起的必需接口，说明代码是最新状态，但核心 `DESIGN.md` §11.2 未同步更新。)*

### 2. `cyq_perf` (筹码分布) 接口在核心设计文档中的遗漏
* **文档描述**：整个 `DESIGN.md` 全文**未提及** `cyq_perf`（筹码分布）这一 Tushare API 的任何使用需求或设计。
* **代码实现**：
  * 插件元数据 `deeptrade_plugin.yaml` 将 `cyq_perf` 声明为 `required`。
  * 实际的 `data.py` 也在执行过程中强依赖此数据。
  * *(注：`docs/limit-up-board.md` 提及此接口为 v0.8 引入的必需接口，此更新同样未能反映在核心 `DESIGN.md` 中。)*

### 3. `volume-anomaly` (成交量异动策略) 的 `adj_factor` (复权因子) 实现状态冲突
* **文档描述**：在 `docs/code_review_volume_anomaly_screen.md` 的第五节修复建议中，对 `adj_factor`（处理 vol 复权）的结论是：“代价较高，收益/成本不高，建议暂缓”。
* **代码实现**：在 `deeptrade/strategies_builtin/volume_anomaly/deeptrade_plugin.yaml` 的 `permissions.tushare_apis.optional` 列表中，却赫然**声明并申请了** `adj_factor` 的访问权限。尽管代码逻辑中可能暂未深度使用，但权限申请列表未能与评审的“暂缓”结论保持同步。

## 三、 核心架构重构带来的文档自相矛盾（文档技术债）

虽然 `DESIGN.md` 在 §0.6 声明了“下文章节按时间序保留作为历史参考”，但为了报告的完整性，仍需指出以下对新开发者极易产生误导的代码与旧文档不一致之处：

### 1. 全局表 `strategy_runs` 和 `strategy_events` 的存在性
* **旧文档表述**：`DESIGN.md` §6.1 DDL 设计中包含了 `strategy_runs` 和 `strategy_events` 这两张框架级全局表。
* **代码实现**：在真正的 DuckDB 核心迁移文件 `20260427_001_init.sql` 中，这两张表已被完全移除。框架不再统一管理运行历史，而是遵照纯隔离原则，交由各插件自己创建和维护（例如：`lub_runs`, `lub_events`, `va_runs`, `va_events`）。

### 2. 废弃的 `StrategyContext`
* **旧文档表述**：`PLAN.md` 等开发计划文档中依然存在对 `StrategyContext` 的调用和测试用例引用。
* **代码实现**：代码中仅存在针对最新 Protocol 设计的 `PluginContext`，`StrategyContext` 已在重构中被彻底移除。

### 3. `llm_calls` 审计表的 `stage` 字段移除
* **旧文档表述**：`DESIGN.md` §6.1 定义 `llm_calls` 表具有 `stage` 字段。
* **代码实现**：在代码 `core/migrations/core/20260501_002_drop_llm_calls_stage.sql` 中，`stage` 字段已经被显式地 `DROP COLUMN`。这符合 §0.8 中“Stage 概念彻底归插件”的新设计，但进一步使得 §6.1 章节的 DDL 失去时效性。

---

## 四、 评审总结与建议

DeepTrade 的代码实现总体质量较高，特别是 v0.5/v0.6 的重构（如 LLM Manager 多提供商抽象、数据管线的隔离、Plugin 透传 CLI 机制等）在代码中得到了严格且精准的贯彻，没有发现运行时的严重结构性偏离。

**建议修复的行动点：**

1. **修正框架耦合问题（高优）：** 修正 `deeptrade/plugins_api/base.py` 中 `PluginContext` 的引入路径，应当将核心基础 Context 提升至更为中立的地方，或者解除基础 `Plugin` 对 `deeptrade.plugins_api.channel` 这一子模块的依赖，并根据文档修正名称为 `ChannelContext`，以保持架构的上下层级清晰。
2. **清理 YAML 声明：** 将 `volume-anomaly` 插件的 YAML 配置中的 `adj_factor` 移除，与 Review 结论保持一致，以避免未来权限申请引起歧义。
3. **消除文档认知差异：** 建议找时间系统性地将 `docs/limit-up-board.md` 中 v0.8 更新的数据矩阵要求（`top_list`, `top_inst`, `cyq_perf` 转为 required）反向合并到权威的 `DESIGN.md` 对应章节中，确保单一事实来源。