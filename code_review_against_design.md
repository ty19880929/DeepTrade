# DeepTrade 代码评审报告

评审对象：`E:\personal\DeepTrade`  
设计基准：`DESIGN.md` v0.3.1  
评审日期：2026-04-28  
说明：用户消息写的是 `DESING.md`，项目中实际存在的是 `DESIGN.md`，本报告以 `DESIGN.md` 为准。

## 1. 总体结论

当前代码已经实现了较完整的框架骨架：Typer CLI、DuckDB migration、配置管理、keyring/plaintext secret fallback、插件元数据与 migration、Rich dashboard、DeepSeek JSON/Pydantic 客户端、Tushare 缓存层、内置打板策略 R1/R2/final_ranking 管线和较完整测试套件。

自动化验证结果良好：

```text
uv run pytest       -> 163 passed
uv run ruff check . -> All checks passed
uv run mypy deeptrade -> Success: no issues found
```

但从“是否符合 DESIGN.md v0.3.1”角度看，仍有若干高优先级偏差。最关键的问题集中在：

- strategy run 没有执行 `validate()` / `configure()`。
- 打板策略声明 required 的数据接口没有全部采集，且 `stock_st` required 被按 optional 降级。
- Tushare 5xx fallback 设计未接入实际调用路径。
- LLM transport 失败会直接使 run failed，而不是按 batch failed / partial_failed 处理。
- plugin install 的 static validation 失败不会阻止安装。
- 策略数据没有落入设计声明的共享行情表和插件业务表，主要进了通用 blob cache。
- final_ranking 失败时报告可能写成 success，但 runner 终态是 partial_failed。

这些问题不影响当前测试通过，但会影响真实运行、审计可重放性和设计承诺的可靠性。

## 2. 关键发现

### Critical 1：`strategy run` 没有执行插件 `validate()` 和 `configure()`

位置：

- [deeptrade/cli_strategy.py](E:/personal/DeepTrade/deeptrade/cli_strategy.py:71)
- [deeptrade/cli_strategy.py](E:/personal/DeepTrade/deeptrade/cli_strategy.py:79)
- [deeptrade/cli_strategy.py](E:/personal/DeepTrade/deeptrade/cli_strategy.py:98)

设计要求：

- `install` 不联网。
- `validate` 在 `plugin info` 手动触发，`strategy run` 自动触发。
- `strategy run` 应进入 plugin `configure()` 子问卷收参。
- run 阶段严格检查 required 接口可用。

实际实现：

- `cmd_run()` 只 `_load_entrypoint()`，构造 `StrategyParams`，然后直接 `runner.execute()`。
- 没有调用 `plugin.validate(ctx)`。
- 没有调用 `plugin.configure(ctx)`。
- CLI 要求 `plugin_id` 必填，也没有实现不传时 questionary 单选。

影响：

- required API、DeepSeek、Tushare 配置问题会推迟到 pipeline 中爆炸，不符合三阶段分层。
- 插件无法声明自己的运行参数问卷。
- 设计中的交互体验和前置失败提示没有落地。

建议：

1. `cmd_run()` 加载插件后先构造只含 config/db 的 ctx，调用 `plugin.validate(ctx)`。
2. 合并 CLI 参数和 `plugin.configure(ctx)` 返回值。
3. 支持 `deeptrade strategy run` 不带 plugin_id 时从已启用策略插件中 questionary 单选。
4. 让 `validate()` 失败在创建 `strategy_runs` 前终止，并给出清晰错误。

### Critical 2：打板策略 required 数据接口没有全部采集

位置：

- [deeptrade/strategies_builtin/limit_up_board/deeptrade_plugin.yaml](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/deeptrade_plugin.yaml:11)
- [deeptrade/strategies_builtin/limit_up_board/data.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/data.py:247)
- [deeptrade/strategies_builtin/limit_up_board/data.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/data.py:252)
- [deeptrade/strategies_builtin/limit_up_board/data.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/data.py:273)
- [deeptrade/strategies_builtin/limit_up_board/data.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/data.py:314)

设计和 metadata 声明 required：

```text
stock_basic, trade_cal, daily, daily_basic, stock_st, limit_list_d, limit_step, moneyflow
```

实际 `collect_round1()` 采集：

```text
stock_basic, limit_list_d, stock_st, suspend_d, limit_list_ths, limit_cpt_list, limit_step
```

缺失：

- `daily`
- `daily_basic`
- `moneyflow`

影响：

- R1/R2 设计中要求的近 N 日走势、换手率、量比、市值、资金流确认没有进入上下文。
- LLM 实际基于更薄的数据做强势分析和连板预测，策略质量与设计目标不一致。
- metadata required 与真实执行不一致，用户即使缺少 `moneyflow` 权限也不会在当前流程暴露。

建议：

1. 在 Step 1 补齐 `daily`、`daily_basic`、`moneyflow` 的 T-N 到 T 数据。
2. 将近 5 日 daily、近 3/5 日 moneyflow 聚合进候选 context。
3. 如果 MVP 暂不实现，应把 `daily`、`daily_basic`、`moneyflow` 从 required 降级并修改设计和 prompt，不能保留虚假 required。

### Critical 3：`stock_st` 是 required，但 unauthorized 被软跳过

位置：

- [deeptrade/strategies_builtin/limit_up_board/deeptrade_plugin.yaml](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/deeptrade_plugin.yaml:16)
- [deeptrade/strategies_builtin/limit_up_board/data.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/data.py:273)
- [deeptrade/strategies_builtin/limit_up_board/data.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/data.py:275)

设计要求：

- required 接口未授权：终止 run。
- optional 接口未授权：记录 `data_unavailable` 并降级。

实际实现：

```python
try:
    st_df = tushare.call("stock_st", trade_date=trade_date)
except TushareUnauthorizedError:
    data_unavailable.append("stock_st")
    st_codes = set()
```

影响：

- 无法排除 ST / *ST 股票，却仍继续进入 LLM 分析。
- 这会破坏“沪深主板打板候选”的基础风险过滤。
- 与 metadata 和 DESIGN 的 required 语义冲突。

建议：

- `stock_st` unauthorized 应直接抛出，让 runner 标记 `failed`。
- 只对 `suspend_d`、`limit_list_ths`、`limit_cpt_list`、热榜、龙虎榜等 optional 做软跳过。

### Critical 4：Tushare 5xx fallback 函数存在，但未接入实际调用路径

位置：

- [deeptrade/core/tushare_client.py](E:/personal/DeepTrade/deeptrade/core/tushare_client.py:321)
- [deeptrade/core/tushare_client.py](E:/personal/DeepTrade/deeptrade/core/tushare_client.py:369)
- [deeptrade/core/tushare_client.py](E:/personal/DeepTrade/deeptrade/core/tushare_client.py:572)

设计要求：

- Tushare 5xx 重试耗尽后有条件 fallback 到 DB。
- fallback 需要检查 trade_date、status、cache_class、data_completeness。

实际实现：

- `can_fallback()` 已实现。
- `call()` 在 cache miss 后直接 `_fetch_and_store()`。
- `_fetch_and_store()` 只捕获 `TushareUnauthorizedError`。
- `TushareServerError` / `TushareRateLimitError` 重试耗尽后直接向外抛出，没有尝试读已有缓存。

影响：

- 设计中的 “5xx fallback 到本地已同步数据” 实际不可用。
- 真实 Tushare 临时故障会直接导致策略失败，即使本地已有同日完整数据。

建议：

1. `_fetch_and_store()` 捕获重试耗尽后的 `TushareServerError`。
2. 读取对应 `SyncState`，调用 `can_fallback()`。
3. fallback 成功时读取 cached payload 并发 `tushare.fallback` 事件或至少写审计。
4. fallback 失败时按 required/optional 由调用层决定终止或降级。

### High 1：LLM transport 失败未按 batch failed / partial_failed 处理

位置：

- [deeptrade/strategies_builtin/limit_up_board/pipeline.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/pipeline.py:197)
- [deeptrade/strategies_builtin/limit_up_board/pipeline.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/pipeline.py:333)
- [deeptrade/strategies_builtin/limit_up_board/pipeline.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/pipeline.py:450)

设计要求：

- LLM 超时 / 5xx：tenacity 重试；最终失败则该批 failed。
- 任一 batch 最终失败：run.status = `partial_failed`，报告红色横幅。

实际实现：

- R1/R2/final_ranking 只捕获 `LLMValidationError`。
- `LLMTransportError` 重试耗尽后会向外抛出。
- `StrategyRunner` 捕获后将整个 run 标为 `failed`。

影响：

- 单个 LLM batch 网络失败会导致全局 failed，其他 batch 不会继续完成审计。
- 与设计中“批次失败不伪装 success，但继续保留审计”的容错模型不一致。

建议：

- R1/R2/final_ranking 同时捕获 `LLMTransportError`。
- 对当前 batch 发 `validation.failed` 或新增 `llm.batch.failed` 事件。
- 增加 `failed_batches`，继续后续批次。

### High 2：plugin install 的 static validation 失败不会阻止安装

位置：

- [deeptrade/core/plugin_manager.py](E:/personal/DeepTrade/deeptrade/core/plugin_manager.py:205)
- [deeptrade/core/plugin_manager.py](E:/personal/DeepTrade/deeptrade/core/plugin_manager.py:207)
- [deeptrade/core/plugin_manager.py](E:/personal/DeepTrade/deeptrade/core/plugin_manager.py:221)
- [deeptrade/core/plugin_manager.py](E:/personal/DeepTrade/deeptrade/core/plugin_manager.py:223)

设计要求：

- install 阶段包含 entrypoint import 和 `validate_static(ctx)`。
- 任一失败应事务回滚、清理拷贝目录。

实际实现：

- DB 写入和 migration 提交后，才执行 static self-check。
- entrypoint 加载失败只 `logger.warning`，仍返回 installed record。
- `validate_static()` 抛异常也只 warning，插件仍安装成功。

影响：

- 不可导入或静态自检失败的插件会显示为已安装、enabled。
- 用户直到运行时才遇到加载失败。
- 破坏 install 阶段对本地插件基本有效性的保证。

建议：

- 在事务提交前或提交后失败回滚清理时执行 entrypoint import + `validate_static()`。
- static validation 失败应抛 `PluginInstallError`，删除 install copy，并撤销 registry/migration 记录。
- 如果担心 plugin code 运行副作用，至少 import entrypoint 失败必须阻止安装。

### High 3：策略数据没有落入设计声明的共享行情表和插件表

位置：

- [deeptrade/core/tushare_client.py](E:/personal/DeepTrade/deeptrade/core/tushare_client.py:393)
- [deeptrade/core/tushare_client.py](E:/personal/DeepTrade/deeptrade/core/tushare_client.py:501)
- [deeptrade/core/tushare_client.py](E:/personal/DeepTrade/deeptrade/core/tushare_client.py:529)
- [deeptrade/strategies_builtin/limit_up_board/migrations/20260427_001_init.sql](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/migrations/20260427_001_init.sql:7)

设计要求：

- DuckDB 是持久化存储。
- 通用行情进入 `stock_basic`、`daily`、`daily_basic` 等共享表。
- 插件声明表安装后自动建表；打板策略同步所需数据持久化到插件表。
- 结果写入 `lub_stage_results`。

实际实现：

- `TushareClient` 将 DataFrame 存为 `tushare_cache_blob.payload_json`。
- `collect_round1()` 没有将 `limit_list_d` 写入 `lub_limit_list_d`。
- `limit_list_ths` 没有写入 `lub_limit_ths`。
- R1/R2/final_ranking 的结构化结果没有写入 `lub_stage_results`，只写报告文件。

影响：

- 策略结果和中间数据无法通过 DuckDB 表稳定查询、复盘、比对。
- 插件 metadata 中声明的表基本只被创建，没有承担设计职责。
- “从本地提取预先同步好的数据”尚未真正成立，当前更像接口 response blob cache。

建议：

1. 保留 `tushare_cache_blob` 作为通用原始缓存可以接受，但不要替代业务表。
2. 在数据同步层将标准接口写入共享表和插件表。
3. R1/R2 每批结果写 `lub_stage_results`。
4. 报告导出只作为读模型，不应是唯一结果持久化。

### High 4：final_ranking 失败时报告可能写成 success

位置：

- [deeptrade/strategies_builtin/limit_up_board/pipeline.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/pipeline.py:450)
- [deeptrade/strategies_builtin/limit_up_board/strategy.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/strategy.py:194)
- [deeptrade/strategies_builtin/limit_up_board/strategy.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/strategy.py:200)
- [deeptrade/strategies_builtin/limit_up_board/strategy.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/strategy.py:222)

设计要求：

- 任一 batch 最终失败，run.status = `partial_failed`。
- `partial_failed` 报告顶部必须红色横幅。

实际实现：

- `run_final_ranking()` 失败会 yield `VALIDATION_FAILED`，runner 终态会变 `partial_failed`。
- 但 `strategy.py` 写报告时只看 `r1_result.failed_batches` 和 `r2_result.failed_batches`。
- final_ranking 失败不会设置 `terminal_status = PARTIAL_FAILED`。
- 注释提到 render_result 后重渲染，但 `render_result()` 是 `pass`。

影响：

- DB 中 run 可能是 `partial_failed`，但 `summary.md` 仍显示 `success` 且没有红色横幅。
- 用户查看报告会误认为结果完整有效。

建议：

- `run_final_ranking()` 返回 `failed` 状态或 result object。
- strategy 在写报告前纳入 final_ranking 失败状态。
- 或让 runner 在终态确定后调用插件 `render_result()` 重写报告。

### High 5：CLI 未实现设计中的默认交互入口和数据同步命令

位置：

- [deeptrade/cli.py](E:/personal/DeepTrade/deeptrade/cli.py:22)
- [deeptrade/cli.py](E:/personal/DeepTrade/deeptrade/cli.py:25)
- [deeptrade/cli_strategy.py](E:/personal/DeepTrade/deeptrade/cli_strategy.py:46)

设计要求：

- `deeptrade` 默认进入主菜单。
- 支持 `deeptrade data sync --strategy ...`。
- `deeptrade strategy run` 不传策略时可从已安装策略中选择。

实际实现：

- root app `no_args_is_help=True`。
- 没有 `data` subcommand。
- `strategy run` 的 `plugin_id` 是必填参数。

影响：

- 核心交互体验偏离 TradingAgents 风格。
- 用户无法先同步数据再运行策略。
- 插件选择流程不符合设计。

建议：

- 增加 root 默认主菜单命令或 callback 中的交互入口。
- 增加 `data sync` 命令并复用插件 `sync_data` / data layer。
- `strategy run` 改为 `plugin_id: str | None = None`，为空时 questionary 单选。

## 3. 中优先级发现

### Medium 1：secret_store 中 keyring 记录在 keyring 不可用时会触发断言

位置：

- [deeptrade/core/secrets.py](E:/personal/DeepTrade/deeptrade/core/secrets.py:75)
- [deeptrade/core/secrets.py](E:/personal/DeepTrade/deeptrade/core/secrets.py:80)

问题：

- 如果某次运行用 keyring 保存了 secret，后续环境 keyring 不可用，`SecretStore.get()` 读到 `method == "keyring"` 后直接 `assert self._keyring is not None`。
- 这会导致 `config show`、策略运行等路径崩溃。

建议：

- 如果记录为 keyring 但当前 keyring 不可用，应返回 `None` 并给出可读错误：“secret stored in keyring but keyring unavailable”。
- CLI 可以提示用户重新配置或切换 plaintext fallback。

### Medium 2：`validate_static` 没有注入 plugin metadata

位置：

- [deeptrade/core/plugin_manager.py](E:/personal/DeepTrade/deeptrade/core/plugin_manager.py:131)
- [deeptrade/strategies_builtin/limit_up_board/strategy.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/strategy.py:61)

问题：

- Protocol 要求 `metadata: PluginMetadata`。
- `LimitUpBoardStrategy.metadata = None`。
- `_load_entrypoint()` 实例化后没有把 YAML metadata 注入实例。

影响：

- 插件运行时无法通过 `self.metadata` 读取权限、版本、表声明等信息。
- 第三方插件如果依赖 metadata 会失败。

建议：

- `_load_entrypoint(..., meta)` 或加载后统一 `instance.metadata = meta`。
- 对 Protocol 做运行时结构校验。

### Medium 3：Tushare cache state 与 payload 不在同一事务

位置：

- [deeptrade/core/tushare_client.py](E:/personal/DeepTrade/deeptrade/core/tushare_client.py:383)
- [deeptrade/core/tushare_client.py](E:/personal/DeepTrade/deeptrade/core/tushare_client.py:393)
- [deeptrade/core/tushare_client.py](E:/personal/DeepTrade/deeptrade/core/tushare_client.py:318)

问题：

- `_fetch_and_store()` 先写 `tushare_sync_state(status=ok)`，再 `_write_cached()`。
- 如果写 payload 失败，会留下 `status=ok` 但没有 cached payload。
- 后续 `_cache_hit()` 为 true，`_read_cached()` 返回空 DataFrame，调用层可能把空结果当真实数据。

建议：

- state 与 payload 写入放入同一事务。
- `_cache_hit()` 不应只看 state，还应确认 payload 存在。
- `_read_cached()` 找不到 payload 时应视为 cache miss，而不是返回空 DataFrame。

### Medium 4：LLM 审计表保存完整 prompt 和 response，可能导致数据库快速膨胀

位置：

- [deeptrade/core/deepseek_client.py](E:/personal/DeepTrade/deeptrade/core/deepseek_client.py:382)
- [deeptrade/core/deepseek_client.py](E:/personal/DeepTrade/deeptrade/core/deepseek_client.py:387)
- [deeptrade/core/deepseek_client.py](E:/personal/DeepTrade/deeptrade/core/deepseek_client.py:400)

问题：

- `request_json` 存完整 system/user prompt，候选量大时单条记录可能非常大。
- `response_json` 存完整响应。
- 设计强调审计和可重放，但也要求轻量本地工具。长期运行会让 DuckDB 文件快速膨胀。

建议：

- DB 中保留 `prompt_hash`、token、stage、response summary。
- 完整 prompt/response 存到 reports/<run_id>/llm_calls.jsonl 或压缩文件。
- 至少提供配置项控制是否保存完整 prompt。

### Medium 5：插件代码加载方式破坏独立安装语义

位置：

- [deeptrade/core/plugin_manager.py](E:/personal/DeepTrade/deeptrade/core/plugin_manager.py:99)
- [deeptrade/core/plugin_manager.py](E:/personal/DeepTrade/deeptrade/core/plugin_manager.py:125)
- [deeptrade/strategies_builtin/limit_up_board/strategy.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/strategy.py:36)

问题：

- 内置插件 YAML entrypoint 是 `strategy:LimitUpBoardStrategy`。
- 已安装 copy 中的 `strategy.py` 内部 import 使用 `deeptrade.strategies_builtin.limit_up_board.*`。
- 这意味着实际运行依赖当前包内代码，而不是安装目录中的插件副本。

影响：

- 插件版本化、upgrade、install_path 隔离语义被削弱。
- 如果安装 copy 与当前源码不同，运行结果不可预期。

建议：

- 内置插件也按标准包结构使用相对 import。
- entrypoint 使用包名，例如 `limit_up_board.strategy:LimitUpBoardStrategy`。
- 安装目录应包含完整插件包，运行应只从 install_path 解析。

### Medium 6：字段单位转换硬编码为“元 -> 亿/万”

位置：

- [deeptrade/strategies_builtin/limit_up_board/data.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/data.py:185)
- [deeptrade/strategies_builtin/limit_up_board/data.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/data.py:192)
- [deeptrade/strategies_builtin/limit_up_board/data.py](E:/personal/DeepTrade/deeptrade/strategies_builtin/limit_up_board/data.py:336)

问题：

- `yi()` 和 `wan()` 假设输入 raw 单位都是“元”。
- Tushare 不同接口字段单位并不统一，设计要求 DB 保留 raw 单位，prompt normalized 时要基于字段原义转换。

影响：

- 如果 `amount`、`total_mv`、`float_mv`、`free_float` 等字段原始单位不是元，LLM 看到的证据值会失真。

建议：

- 建立字段级单位映射，例如 `FIELD_UNITS = {"fd_amount": "元", "total_mv": "万元" ...}`。
- normalized 过程按字段名转换，而不是对所有数字统一除以 1e8。
- EvidenceItem 中填入转换后的 unit，并在 data_snapshot 中保留 raw/normalized 对照。

## 4. 低优先级发现

### Low 1：dashboard 运行期间 run_id 一直是 `(starting)`

位置：

- [deeptrade/cli_strategy.py](E:/personal/DeepTrade/deeptrade/cli_strategy.py:89)
- [deeptrade/cli_strategy.py](E:/personal/DeepTrade/deeptrade/cli_strategy.py:98)
- [deeptrade/cli_strategy.py](E:/personal/DeepTrade/deeptrade/cli_strategy.py:101)

问题：

- DashboardState 在运行前用 `(starting)`。
- 真正 run_id 在 `runner.execute()` 返回后才写回 state。
- 所以运行过程中 header 的 run_id 不是真实 ID。

建议：

- run_id 由 CLI/runner 预生成并传入 runner。
- 或 runner 在 `_record_run_start()` 后立即通过事件通知 dashboard。

### Low 2：`config test` 绕过封装客户端

位置：

- [deeptrade/cli_config.py](E:/personal/DeepTrade/deeptrade/cli_config.py:151)
- [deeptrade/cli_config.py](E:/personal/DeepTrade/deeptrade/cli_config.py:171)

问题：

- `config test` 直接使用 Tushare SDK 和 OpenAI SDK。
- 没有复用 `TushareClient` / `DeepSeekClient` 的限流、错误翻译、审计和 no-tools 约束。

建议：

- `config test` 改用正式客户端 transport。
- 连接测试是否写审计可配置，但错误分类应复用正式路径。

## 5. 设计符合度矩阵

| 模块 | 符合度 | 说明 |
| --- | --- | --- |
| 项目结构 | 高 | 源码结构基本匹配设计。 |
| CLI 基础命令 | 中 | 有 init/config/plugin/strategy，但缺默认主菜单和 data sync。 |
| 配置管理 | 高 | app_config/secret_store/环境变量优先级基本实现。 |
| 密钥管理 | 中 | keyring/plaintext fallback 已有，但 keyring 记录跨环境不可用时会断言。 |
| DuckDB core schema | 高 | v0.3.1 主要表已实现。 |
| 插件 metadata | 高 | migrations 唯一 DDL 源、llm_tools=false 等实现较好。 |
| 插件安装 | 中 | migrations/表登记完整，但 static validation 失败不阻断安装。 |
| StrategyContext no-tools | 高 | 没有暴露 tool call 接口。 |
| DeepSeek JSON/Pydantic | 高 | stage profile、no-tools、JSON validation、审计都实现。 |
| LLM 批次容错 | 中 | validation error 可 partial_failed，但 transport error 会 failed。 |
| Tushare 缓存与限流 | 中 | 缓存、intraday 隔离、限流存在，但 fallback 未接入，业务表未落地。 |
| 打板策略数据层 | 低到中 | 候选池和板块 fallback 有，但 required 行情/资金数据缺失，ST required 被降级。 |
| R1/R2 prompt/schema | 高 | candidate_id、extra forbid、evidence/unit、长度约束已实现。 |
| final_ranking | 中 | schema/prompt 存在，但失败状态没有正确反映到报告。 |
| 报告输出 | 中 | 文件输出完整，但 DB stage result 未落表，状态可能不一致。 |
| 测试覆盖 | 高（代码行为）/ 中（设计契约） | 163 tests passed，但未覆盖若干设计级约束。 |

## 6. 测试覆盖缺口建议

建议新增以下测试，防止上述问题回归：

1. `strategy run` 必须调用 `plugin.validate()`，validate 抛错时不创建 run。
2. `strategy run` 必须调用 `plugin.configure()` 并合并参数。
3. `stock_st` unauthorized 时内置策略必须 failed，而不是 data_unavailable。
4. `collect_round1()` 必须调用 `daily`、`daily_basic`、`moneyflow`。
5. Tushare 5xx + 已有同日 final cache 时必须 fallback。
6. Tushare state ok 但 payload 缺失时不得返回空 DataFrame。
7. LLMTransportError 在某批失败后应产生 partial_failed，而不是整体 failed。
8. final_ranking validation failed 时 `summary.md` 必须显示 partial_failed 红色横幅。
9. plugin entrypoint import 失败或 validate_static 失败时 install 必须失败。
10. 安装后的插件实例 `metadata` 不应为 None。
11. `deeptrade strategy run` 无 plugin_id 时应进入策略选择。
12. `--allow-intraday` 写入的缓存不得被日终模式命中。

## 7. 修复优先级建议

### 第一批：阻断真实运行误判

1. 修 `strategy run` 的 validate/configure。
2. 修打板策略 required 数据采集和 `stock_st` required 语义。
3. 修 Tushare 5xx fallback 接入。
4. 修 LLMTransportError batch 级处理。
5. 修 final_ranking 失败报告状态。

### 第二批：持久化与复盘能力

1. 将 Tushare 数据落到共享行情表和 `lub_*` 插件表。
2. 将 R1/R2/final_ranking 结果写 `lub_stage_results`。
3. 将 cache state 与 payload 原子写入。
4. 调整 llm_calls 大字段存储策略。

### 第三批：体验与扩展性

1. 增加 root 主菜单和 `data sync` 命令。
2. 修 plugin metadata 注入和 entrypoint 隔离。
3. `config test` 复用正式客户端。
4. 字段单位映射表化。

## 8. 结论

项目已经具备良好的骨架和测试纪律，当前代码质量本身不差；主要风险在于实现还没有完全兑现 `DESIGN.md v0.3.1` 对“数据完整性、required/optional 权限语义、批次级容错、插件安装有效性、可重放持久化”的承诺。

建议不要直接进入真实 Tushare/DeepSeek 长流程试运行。应先修复 Critical 和 High 项，尤其是数据采集与状态处理，否则很容易生成形式完整但事实基础不足的选股报告。
