# Changelog

All notable changes to DeepTrade. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and SemVer.

## [v0.9.1] — 2026-05-16 — `set-llm` 默认值表新增 OpenAI 官方端点

交互式 `deeptrade config set-llm` 在用户敲入 provider 名称后会用首段（split-by-dash 第一段）去 `_DEFAULT_BASE_URLS` 查一个默认 base URL 预填到下一步表单。此前该表只覆盖 deepseek / qwen / kimi / doubao 四家国内供应商，命名 `openai` 时默认值为空字符串，用户需要自己记忆 / 粘贴 `https://api.openai.com/v1` 才能继续。

底层 `OpenAIOfficialTransport` 早已存在并通过 `_TRANSPORT_BY_BASE_URL` 上的 `api.openai.com` substring 命中（v0.6 起），是唯一会把 `StageProfile.reasoning_effort` 真正写到 wire 的 transport（o1 / o3 reasoning 系列依赖）。这一版纯粹是补齐 wizard 的可发现性，运行时无任何行为差异。

### Changed

- `deeptrade/cli_config.py::_DEFAULT_BASE_URLS` 新增 `"openai": "https://api.openai.com/v1"`。命名为 `openai` / `openai-*`（如 `openai-o3`）的 provider 会自动预填该 base URL。
- `_set_llm_new` 的 provider 名称提示串补上 `openai` 作为示例。

### Migration notes

- 已有的 OpenAI provider 配置（如果用户在更早版本手动建过）不受影响——`_DEFAULT_BASE_URLS` 仅在新增时作为表单默认值使用，存量记录不会被回写。
- transport 路由、`reasoning_effort` 转发逻辑、`llm_calls` 审计字段全部不变。

## [v0.9.0] — 2026-05-16 — LLM transport 改流式，规避网关 idle-timeout

打板等长生成场景下，`kimi-k2.6` 等 thinking 模型的非流式调用 100% 触发 `LLMTransportError: Request timed out.`，单次失败耗时 ~27 分钟（外层 tenacity 3 次 × openai SDK 3 次 × 180 s）。Moonshot 官方文档明确：思考模型在 server 端先思考再生成，**任何中间网关（包括 Moonshot 自家网关）只要看到长时间无 header 返回就会把 TCP 当僵尸连接切掉**，这是非流式的设计性缺陷，与 base_url / DNS / TLS 均无关。

实证：同一 prompt + `stream=True` 下 TTFC 1.3 s、总耗时 42 s、`content_len=240`、JSON 合法、usage 完整。

### Changed

- `deeptrade/core/llm_client.py::OpenAICompatTransport.chat()` 改为流式：`stream=True` + `stream_options={"include_usage": True}`，逐 chunk 累积 `delta.content` 拼成完整文本，从末 chunk 取 usage。`create()` 与迭代两阶段的 `APITimeoutError` / `APIError` 都包成 `LLMTransportError`，原 tenacity 重试路径不变。
- 保留 v0.6 / v0.8.1 的两段逻辑：`_adjust_temperature()` 钩子（Moonshot 强制 1.0）与 `supports_reasoning_effort` 门控（仅 `OpenAIOfficialTransport` 翻 True）继续生效。
- 子类（`GenericOpenAITransport` / `DashScopeTransport` / `MoonshotTransport` / `OpenAIOfficialTransport`）零修改，继承新基类实现。`RecordedTransport`、`LLMClient`、`LLMResponse`、`llm_calls` 表结构、`reports/<run>/llm_calls.jsonl` 格式全部不变。

### Why streaming-only, no fallback

内测期约定：**不留 `stream=False` 开关、不加配置项、不做降级**。理由：

- timeout 语义自然从「整次调用墙钟」变为「单 chunk 间最大不活跃间隔」，更宽容、更符合 LLM 长生成的实际形态；`provider.timeout=180` 字面值无需调整。
- 流式中途断 → 截断 JSON → 走 `LLMClient._with_retry` 已有的 `LLMValidationError` 1 次 repair-retry，不必单独走非流式 fallback。
- include_usage 是 OpenAI 协议 2024-07 起的官方约定，in-scope 的全部 provider（OpenAI / Moonshot / DeepSeek / DashScope / Doubao / GLM / Yi / OpenRouter / SiliconFlow）均已支持；万一某末 chunk 缺 usage，审计记 0 不抛。

### Migration notes

- 插件零改动。`LLMClient.complete_json()` 接口、异常类型、返回类型、重试语义、审计格式全部不变。
- `app_config.llm_providers[*].timeout` 字段保留，语义如上；用户原本配的数值无需调整。
- 行为差异详见 `docs/fix/2026-05-16-llm-streaming-transport.md` §4.3。

## [v0.8.1] — 2026-05-16 — Moonshot reasoning 模型 temperature 兼容性

`limit-up-board` 等插件接入 Kimi K2.6（``base_url = https://api.moonshot.cn/v1``）后，**所有** LLM 调用 100% 命中 ``HTTP 400 invalid temperature: only 1 is allowed for this model``。根因：Kimi K2 系列的 thinking / reasoning 变体（与 OpenAI o1/o3、Anthropic Sonnet thinking 同侧设计）在服务端硬约束 ``temperature``——仅接受模型专属的固定值，而插件 ``StageProfile`` 出于复现性给的是 ``0.0 ~ 0.2``。

修复职责完全在框架：插件不应感知具体 provider/model 的服务端约束，框架的契约是「插件给一个温度意图，框架在真正发出请求前 sanitize 到目标 provider/model 能接受的取值」。

### Changed

- ``deeptrade/core/llm_client.py::OpenAICompatTransport``：新增 ``_adjust_temperature(model, temperature) -> float`` 钩子，默认 identity；``chat()`` 在写 kwargs 前调用钩子，并在改写时打一行 ``logger.info`` 便于排查。非 Moonshot 路径完全无感。
- 新增 ``MoonshotTransport(OpenAICompatTransport)``：``_FORCED_TEMPERATURE`` prefix 表强制 ``kimi-k2-thinking`` / ``kimi-k2.5`` / ``kimi-k2.6`` 到 ``1.0``、``kimi-for-coding`` 到 ``0.6``；fallthrough 走 ``min(temperature, 1.0)`` 兼顾非 reasoning 模型（``moonshot-v1-*`` / ``kimi-k2-instruct-*``）的 ``[0, 1]`` 上限——Pydantic 字段允许到 2.0，超界一样 400。
- ``_TRANSPORT_BY_BASE_URL`` 新增 ``("api.moonshot.cn", MoonshotTransport)``。substring 匹配自动覆盖 ``api.moonshot.cn`` / ``api.moonshot.cn/v1`` 所有形式；国际站 ``api.moonshot.ai`` 暂未支持，若后续需要追加一行即可。

### Why prefix match, not exact / regex

Moonshot 命名空间 ``<major>.<minor>[-<dated-revision>]`` 的天然分界就在 prefix。exact 会让 ``kimi-k2.6-1106`` / ``kimi-k2-thinking-128k`` 这类 dated revision 漏网，触发 0day 失败；regex 转义复杂度抬高 review 成本，收益不抵。

### Migration notes

- 插件零改动。``limit_up_board`` / 其他第三方插件的 ``profiles.py`` 不需要感知该约束。
- 用户原本在 Kimi reasoning 模型上设的 ``temperature=0.0`` 在改写后会被强制为 ``1.0``——这本来就是服务端唯一允许的取值，不改写就是 100% 失败。
- ``app.profile`` / ``llm.providers`` 配置无变动。

## [v0.8.0] — 2026-05-16 — 插件 install / upgrade 走 CDN，零 GitHub API 调用

`deeptrade plugin install` 与 `deeptrade plugin upgrade` 此前在解析"最新版本"与下载 tarball 时各打一次 ``api.github.com``，未认证用户共享 60/h 的 IP 级配额。一旦插件用户数上来，或者用户与浏览器 / `gh` CLI / `git clone` 公共仓库共用同一公网 IP，``HTTP 403: rate limit exceeded`` 就会把 install / upgrade 直接打死。共享 token 会违反 GitHub ToS，且配额仍会在那个 token 上聚合——不是解。

本版改造将插件分发热路径全部搬到 CDN 端点：

- **Tarball 下载**改走 ``codeload.github.com/<owner>/<repo>/tar.gz/<ref>``。codeload 是 CDN-backed 静态端点，不计入 REST API 限流。
- **"最新版本"**改从注册表 ``index.json`` 的新字段 ``latest_version`` 读取（``raw.githubusercontent.com`` 同样不限流，且已有 ETag 缓存）。
- **URL 形式安装**（``deeptrade plugin install https://github.com/...``）无 ``--ref`` 时默认 ``main`` 分支；其他默认分支需显式 ``--ref``。

净效果：未认证用户走 ``install`` / ``upgrade`` 全流程**零 API 调用**，60/h 限制对默认路径不再存在。``GITHUB_TOKEN`` 不再被框架读取，文档不再推荐设置。

### Changed

- ``deeptrade/core/github_fetch.py``：删除 ``latest_release_tag`` / ``NoMatchingReleaseError``；``fetch_tarball`` 改写为 codeload 单端点（移除 ``GITHUB_TOKEN`` / ``X-GitHub-Api-Version`` header）。
- ``deeptrade/core/registry.py``：``RegistryEntry`` 新增可选字段 ``latest_version: str | None``；解析器引入 ``_OPTIONAL_FIELDS`` 集合，含字段时填入，缺省 ``None``。schema_version 不变（仍为 1，向后兼容旧注册表文件）。
- ``deeptrade/core/plugin_source.py``：``_resolve_short_name`` 用 ``entry.latest_version`` 替代 ``latest_release_tag``；缺字段且无 ``--ref`` 时抛 ``SourceResolveError`` 提示需补 ``--ref``。``_resolve_url`` 无 ``--ref`` 默认 ``main``；codeload 失败时错误信息提示用户切换 ``--ref``。
- ``deeptrade/cli_plugin.py``：移除 ``NoMatchingReleaseError`` 引用。

### Removed

- ``latest_release_tag`` 公共函数与 ``NoMatchingReleaseError`` 异常类。
- 对 ``GITHUB_TOKEN`` 环境变量的读取（github_fetch.py 中）。

### Migration notes

- **注册表维护者**：在 ``DeepTradePluginOfficial/registry/index.json`` 每个 plugin entry 中加 ``latest_version`` 字段，每次插件发版后同步该字段。可由 plugin 仓库 release workflow 自动 PR 到注册表仓库。
- **存量旧版框架用户**：旧版（v0.7 及以下）仍会调 ``api.github.com``。``latest_version`` 字段是可选的、旧框架读取时会被忽略，不破坏现有用户；旧用户升级到本版后限流问题自动消失。
- **URL 形式高级用户**：若你 host 的仓库默认分支不是 ``main``，``deeptrade plugin install https://github.com/<your>/<repo>`` 需显式 ``--ref <branch>``。

### Why not "use a shared token"

GitHub ToS 明确禁止 token 跨用户共享（被发现 token 会被吊销）；即便允许，5000/h 也会被规模化使用快速吃掉，且 token 泄露需要维护者承担权限滥用风险。CDN 改造是唯一既不需要每用户认证、又能稳定服务的方案。未来若需要支持私有仓库或企业内部分发，再走 OAuth Device Flow（``gh auth login`` 同款流程）让每个用户认证自己的 token。

## [v0.7.0] — 2026-05-15 — 依赖隔离稳健化 + timezone IANA 校验

本版按 2026-05-15 评审研判文档 §5 v0.7 路线落地 2 项主线，至此 v0.5 § 5 全部"采纳项"清零：

1. **H6 依赖隔离稳健化**：
   - **dry-run 预检**：``run_install`` 之前先跑 ``uv pip install --dry-run`` 抓 uv 的 change plan；解析 ``~`` (update) / ``-`` (remove) 行，与 ``framework_core_canonicals()`` 求交集。若插件依赖会顺带升级 / 降级 / 移除任何框架核心 dep，默认拒绝，要求显式 ``--allow-core-bump`` 才放行。``uv`` 不在 PATH 时降级为不做检查（``pip`` 没有对等结构化 dry-run），保持 v0.4 装机不动手的承诺。
   - **dep snapshot**：``run_install`` 之前把 ``pip list --format=freeze`` 输出落到 ``~/.deeptrade/dep_snapshots/<plugin_id>/pre-install-<UTC>.txt``。失败时错误信息把这个目录甩到用户面前，便于 ``diff`` 回滚。
   - 仍**不做**：venv-per-plugin / subprocess 隔离（重型方案，违反"框架轻"）；框架 deps 的 pin lockfile（会污染用户项目的解析）。
2. **L1 timezone IANA 校验**：``AppConfig.app_timezone`` 改用 ``zoneinfo.available_timezones()`` 做硬校验，把 ``Asia/Shangai`` 这种近似拼写在写时拦住，而不是延迟到第一次 ``ZoneInfo(...)`` 调用爆 ``ZoneInfoNotFoundError``。``base_url`` HTTPS 强制按方案 §4 评估**仍不做**（本地 LLM 网关 / Ollama 常用 http）。

### Added

- ``deeptrade/core/dep_installer.py``：
  - ``framework_core_canonicals()``——读取 ``deeptrade-quant`` distribution 的 ``Requires``，过滤掉 ``extra`` marker，输出框架核心 dep 的 canonical 名字集合。从源码运行（未 ``pip install -e``）时返回空集合，让预检自动降级为 no-op。
  - ``preflight_dry_run(reqs, *, watched)``——best-effort 跑 ``uv pip install --dry-run``；解析 stderr / stdout 中的 ``~`` 与 ``-`` 行；返回与 ``watched`` 相交的 canonical 名字集合。``uv`` 缺席、resolution 失败、超时——一律返回空集合，让真正的 ``run_install`` 路径报底层错误，预检不抢话语权。
  - ``_parse_dry_run_changes(text, watched)``——dry-run 输出解析的纯函数版本，便于单测覆盖。
  - ``write_dep_snapshot(plugin_id, dir_root)``——优先 ``uv pip list``，回退 ``python -m pip list``（uv 管理的 venv 通常不带 pip）；写 ``<dir_root>/<plugin_id>/pre-install-<UTC>.txt``，返回 ``Path`` 或失败时 ``None``。
  - ``DRY_RUN_TIMEOUT_SECONDS`` 短超时常量，避免被卡住的 uv 阻塞真正的 install。
- ``deeptrade/core/paths.py::dep_snapshots_dir()``——``~/.deeptrade/dep_snapshots/``；纳入 ``ensure_layout()`` 自动创建。
- ``deeptrade/core/plugin_manager.py::PluginManager.install`` / ``upgrade`` 接 ``allow_core_bump: bool = False``；``_handle_dependencies`` 在 ``run_install`` 之前先跑 preflight 与 snapshot，并在 install error 信息里附上 snapshot 目录 hint。
- ``deeptrade/cli_plugin.py``：``plugin install`` 与 ``plugin upgrade`` 加 ``--allow-core-bump`` flag（中文 help 文案）。
- ``deeptrade/core/config.py::AppConfig._validate_app_timezone``——基于 ``zoneinfo.available_timezones()`` 的 ``@field_validator``；非法值抛 Pydantic ``ValidationError`` 并指出如何打印当前主机支持的全部 zones。
- 测试：``tests/core/test_config.py`` 5 个新用例（默认值通过、典型 IANA 名通过、typo / 空串 / 任意字符串拒绝）。``tests/core/test_plugin_dependencies.py`` 10 个新用例（``framework_core_canonicals`` 列出 must-have、``_parse_dry_run_changes`` 识别 update / remove、忽略 install 行、uv 缺席返回空集、空 requirements 返回空集、watched 为空跳过 subprocess、``_handle_dependencies`` 默认拒绝 core bump 与 ``--allow-core-bump`` 放行、``write_dep_snapshot`` 写文件、pip 缺失返回 ``None``、CLI 双向）。

### Changed

- ``deeptrade/core/plugin_manager.py::_handle_dependencies``：新增 ``allow_core_bump`` kwarg；``run_install`` 调用包了 ``try/except DepInstallError``，把异常包裹一层附上 snapshot 目录提示，便于排障。
- ``deeptrade/core/dep_installer.py::write_dep_snapshot``：拆出 ``_snapshot_argv()`` 帮助选择 ``uv pip list`` vs ``python -m pip list``，与 ``detect_installer()`` 的优先级一致。

### Migration notes

- **用户视角零强制动作**：默认行为是更严的（核心 dep 改动需要显式确认），但常见插件不会动框架核心 dep——绝大多数 ``deeptrade plugin install <短名>`` 命令仍然零摩擦。
- **遇到核心依赖冲突如何处理**：v0.7 之后若看到 ``plugin install would change framework core dep(s) [...]`` 的拒绝信息，先看 dry-run 报告的具体包：
    - 如果是 ``duckdb`` / ``pydantic`` / ``click`` / ``typer`` 等加载链路上的运行时关键件——大概率是插件 spec 写得太死，反馈给插件作者放宽；强行 ``--allow-core-bump`` 可能让框架进程立刻 import 失败。
    - 如果只是 minor 版本提升且已读过 release note——加 ``--allow-core-bump`` 显式确认放行。
- **uv 缺席的主机**：``preflight_dry_run`` 自动跳过，行为与 v0.4 一致。``write_dep_snapshot`` 也能用 ``python -m pip list`` 兜底（虽然 uv-only venv 上 pip 通常缺席，此时 snapshot 静默 no-op）。
- **dep_snapshots 目录的清理**：框架本身不做轮转。如果该目录变大，可以放心 ``rm`` 老的 ``pre-install-*.txt`` 文件——它们只用于回溯，不影响运行时。
- **timezone**：升级到 v0.7 之后第一次 ``Database()`` 打开会触发自动迁移，迁移本身不动 ``app.timezone``；但如果你之前不小心存了 typo 进 ``app_config``，下一次 ``ConfigService.get_app_config()`` 会抛 ``ValidationError``。修法：``deeptrade config set app.timezone Asia/Shanghai``（或你本地的正确 IANA 名）。

## [v0.6.0] — 2026-05-15 — 插件运行时 API + LLM 方言 + 杂项收尾

本版按 2026-05-15 评审研判文档 `DeepTrade-review-assessment-and-fix-plan-2026-05-15.md` §5 中 v0.6 路线落地 6 项主线：

1. **H4 插件运行时 API**：``api_version="2"`` 的插件改用 ``dispatch(ctx, argv)`` 接收 ``PluginContext``，无需再 ``import deeptrade.core.*`` 私有路径取 db / config。``api_version="1"`` 永久向后兼容，运行时按 metadata 决定调用形态。``plugins_api/__init__.py`` 把 ``LLMManager`` / ``TushareClient`` 提升为公共 API。
2. **H5 LLM 方言收口**：``OpenAICompatTransport`` 加 class 属性 ``supports_reasoning_effort`` 默认 False；新增 ``OpenAIOfficialTransport``（base_url 含 ``api.openai.com``）改 True。其余 OpenAI-compat provider 不再裸送 ``reasoning_effort``，消除国内非推理模型常见的 400 与忽略两种失败模式。
3. **H1 hard error**：``table_prefix`` 派生不匹配从 v0.5 的 ``DeprecationWarning`` 转 ``ValueError``，与显式声明路径合流，让"插件表必须落在派生命名空间"成为可执行约束。
4. **M6**：``env_var_for(key)`` 把 provider 名中的 ``-`` 归一化为 ``_``，避免 ``DEEPTRADE_LLM_QWEN-PLUS_API_KEY`` 这种 bash/sh 非法标识符；``set_llm_provider`` 在归一化后冲突时硬拒绝，避免 ``qwen-plus`` / ``qwen_plus`` 共用同一个 env var 时的静默 shadowing。
5. **M8**：``_try_load_keyring`` 改用 ``keyring.get_keyring()`` 的非写入式 backend 探测，只在 ``backends.fail.*`` / ``backends.null.*`` 类时返回 None；结果缓存到模块级变量，配 ``_invalidate_keyring_cache`` 给测试用。彻底消除 ``SecretStore.__init__`` 每次都向 OS 凭据存储写一个 ``__probe__`` 的副作用。
6. **M9**：``TushareClient`` 首次遇未知 API 改打 INFO 日志（进程级 + 实例级双 dedup，不再每次默认就刷屏）；``PluginMetadata.permissions.tushare_apis.cache_overrides: dict[str, str]`` 让插件作者声明非默认缓存策略，``TushareClient(cache_overrides=...)`` 在分类决策中优先采纳。

### Added

- ``deeptrade/plugins_api/base.py``：docstring 标注 ``api_version`` 与 ``dispatch`` 签名的对应关系；``Plugin.dispatch`` 改写为变参 Protocol 占位（``runtime_checkable`` 只校验属性存在，签名由框架按 metadata 决定）。
- ``deeptrade/core/plugin_manager.py::SUPPORTED_API_VERSIONS``（``frozenset({"1", "2"})``）；install / upgrade 路径用集合包含判断替换原先的 ``!= CURRENT_API_VERSION``，对未来扩 ``"3"`` 留接口。
- ``deeptrade/cli.py::_dispatch``：按 ``rec.api_version`` 选择 ``plugin.dispatch(argv)`` 或 ``plugin.dispatch(ctx, argv)``；v2 路径由框架开 ``Database`` + 构 ``PluginContext`` + 收尾关库，插件不再操心生命周期。
- ``deeptrade/plugins_api/__init__.py``：重导出 ``LLMManager`` 与 ``TushareClient``，正式纳入公共 surface 的 ``__all__``。
- ``deeptrade/plugins_api/metadata.py::TushareApiPermissions.cache_overrides: dict[str, str]``，配 ``_cache_overrides_values_valid`` model_validator 强制值落在 ``{static, trade_day_immutable, trade_day_mutable, hot_or_anns}``；非法值在 yaml 解析阶段拒绝，避免运行时缓存行为静默退化。
- ``deeptrade/core/tushare_client.py``：``TushareClient.__init__`` 新增 ``cache_overrides`` kwarg；新增 ``_resolve_cache_class`` 把"插件 overrides → 框架表 → 默认 + 一次性 INFO 日志"三级优先级集中表达；模块级 ``_UNKNOWN_API_LOGGED`` 做进程级 dedup。
- ``deeptrade/core/llm_client.py``：``OpenAICompatTransport.supports_reasoning_effort: bool = False`` 类标志位；``OpenAIOfficialTransport`` 子类（``api.openai.com`` base_url 触发）将其覆盖为 True；``chat()`` 仅在 ``supports_reasoning_effort and reasoning_effort`` 时把字段写入 ``kwargs``。``_TRANSPORT_BY_BASE_URL`` 新增 ``("api.openai.com", OpenAIOfficialTransport)`` 路由。
- ``deeptrade/core/secrets.py``：模块级 ``_keyring_cache`` / ``_keyring_probed`` 缓存；``_invalidate_keyring_cache`` 测试钩子；``_try_load_keyring`` 重写为按 backend FQCN 子串识别 fail/null 类。
- ``deeptrade/core/config.py::ConfigService.set_llm_provider``：归一化后冲突检测；``env_var_for`` 文档说明 ``-``→``_`` 规则的动机。
- 测试新增：``tests/plugins_api/test_api_version_2.py``（6 用例，覆盖双 api_version 安装、unknown api_version 拒绝、v1/v2 dispatch arity、公开类导出）、``tests/core/test_secrets.py`` v0.6 M8 节 3 用例（fail backend 拒绝、real backend 不写探针、cache 可重置）、``tests/core/test_config.py`` 3 用例（env 归一化、归一化冲突拒绝、同名更新放行）、``tests/core/test_plugin_security.py`` 3 用例（cache_overrides 默认空 / 合法值 / 非法值）、``tests/core/test_tushare_client.py`` 3 用例（overrides 优先、未知 API override 路径、INFO 日志一次性）、``tests/core/test_llm_client.py`` 4 用例（supports_reasoning_effort 默认 False、OpenAI-official 路由、Generic transport 不发送、empty 字符串不发送）。

### Changed

- ``deeptrade/plugins_api/metadata.py::_tables_match_prefix``：``table_prefix`` 省略且不匹配派生时从 ``warnings.warn(DeprecationWarning)`` 转 ``raise ValueError``；删除现已无用的 ``import warnings``。模块顶部 docstring 把 v0.5 advisory / v0.6 enforcement 的边界明确标注。
- ``deeptrade/plugins_api/metadata.py``：``TushareApiPermissions`` 加 ``cache_overrides`` 字段；定义 ``_CACHE_CLASS_VALUES`` 内部常量便于校验。
- ``deeptrade/core/tushare_client.py``：``cache_class = API_CACHE_CLASS.get(api_name, "trade_day_immutable")`` 改为 ``cache_class = self._resolve_cache_class(api_name)``，集中所有分类决策。
- ``deeptrade/cli.py``：v2 路径在 dispatch 周边内联 ``ConfigService`` / ``PluginContext`` 导入，避免顶层冷启动负担；其余跨 dispatch 路径行为不变。
- ``tests/core/test_plugin_security.py``：``test_plugin_table_prefix_warns_when_omitted_and_mismatched`` 改名 ``test_plugin_table_prefix_hard_fails_when_omitted_and_mismatched``，正向断言 ``ValidationError``；``test_plugin_table_prefix_no_warning_when_tables_match_derived`` 改名 ``test_plugin_table_prefix_passes_when_tables_match_derived``，保留 ``simplefilter("error", DeprecationWarning)`` 作为反向断言。
- ``tests/core/test_plugin_install.py::_make_minimal_plugin_dir``：``table_name`` 默认值从硬编码 ``test_table`` 改为按 ``plugin_id`` 派生（``<pkg>_t``），满足 v0.6 派生前缀约束；``table_ddl`` 默认也从默认值同步派生。
- ``tests/core/test_plugin_dependencies.py::_minimal_meta_dict``：表名 ``x_t`` 改为 ``minimal_x_t`` 以匹配 ``plugin_id="minimal-x"`` 的派生前缀。
- ``tests/core/test_llm_client.py::test_select_transport_class_defaults_to_generic``：``api.openai.com`` 从"回退到 Generic"断言改为"回退用 Generic / OpenAI-official 走专属路由"的双行声明；docstring 标注 v0.6 行为变化。

### Migration notes

- **插件作者侧（推荐路径）**：迁移到 ``api_version="2"``——
    1. ``deeptrade_plugin.yaml::api_version`` 改为 ``"2"``。
    2. ``def dispatch(self, argv):`` 改写为 ``def dispatch(self, ctx, argv):``。
    3. 把 ``Database(paths.db_path())`` / ``ConfigService(...)`` 等手动构造替换为 ``ctx.db`` / ``ctx.config``；之前从 ``deeptrade.core.*`` import 的 ``LLMManager`` / ``TushareClient`` 改为 ``from deeptrade.plugins_api import LLMManager, TushareClient``。
- **插件作者侧（最低改动路径）**：不动 ``api_version="1"``，所有既有插件**零代码改动**继续工作；唯一硬约束是 ``metadata.tables`` 必须落在 ``<plugin_id 派生前缀>_*`` 命名空间内（或显式声明 ``table_prefix``）。
- **多 LLM provider 命名**：使用 ``qwen-plus`` / ``qwen-max-2024`` 这类带连字符的 provider 名时，``DEEPTRADE_LLM_QWEN_PLUS_API_KEY`` 形式的 env var 现在可以正常生效；不要再同时注册归一化后冲突的名字（``qwen-plus`` 与 ``qwen_plus``）。
- **官方 OpenAI 推理模型**：``reasoning_effort`` 现在只在 ``base_url`` 含 ``api.openai.com`` 时上行。其他 OpenAI-compat 网关如需该字段（少见），临时方案是 fork 当前 transport 类并覆写 ``supports_reasoning_effort = True``；后续版本会按需要在路由表里追加。
- **Tushare 缓存策略覆盖**：插件作者可在 ``deeptrade_plugin.yaml`` 写
  ```yaml
  permissions:
    tushare_apis:
      cache_overrides:
        moneyflow_industry_ths: trade_day_mutable
  ```
  把框架表里没列出的 API 显式钉到正确的缓存类，避免依赖默认 ``trade_day_immutable``。运行时由插件自己把这份 dict 作为 ``cache_overrides=...`` 传给 ``TushareClient``。

## [v0.5.0] — 2026-05-15 — 插件信任边界 + CLI 中文化

本版主线落地 2026-05-15 评审研判文档 `DeepTrade-review-assessment-and-fix-plan-2026-05-15.md` 中的 v0.5 路线：把"插件可以声明并清掉框架核心表"这条被忽视的信任边界明确收口，让框架的 SQL 迁移有边界、插件加载有 sys.modules 守卫、升级路径不会被静默改动的历史 migration 蒙混过关；同时把命令行用户向文案统一为中文，建立首道防回退的回归测试。整体不引入新框架概念（拒绝了报告里的 RuntimeContext 大对象、独立 venv、SQL DSL 等重型方案），保持"框架轻、插件重"的原则。

实现按 5 个 PR 切片推进；既有插件不需要任何源码改动，但 v0.6 会把 v0.5 用 DeprecationWarning 提示的 `table_prefix` 缺省派生策略升级为硬失败——本版起插件作者应开始把声明的表名收敛到 `<plugin_id 派生前缀>_*` 命名空间。

### Added

- `deeptrade/plugins_api/metadata.py`：新增公开常量 `RESERVED_TABLE_NAMES`（10 个框架自有表）与 `RESERVED_TOP_PACKAGES`（15 个框架 / 公共依赖的顶层包名）。`PluginMetadata` 追加 4 个 `model_validator`：
  - `_tables_not_reserved`：拒绝声明任何 `RESERVED_TABLE_NAMES` 中的表（H1）。
  - `_tables_match_prefix`：`table_prefix` 显式声明则全表强制匹配；省略时按 `plugin_id.replace('-','_') + '_'` 派生并对不匹配的表发 `DeprecationWarning`。v0.6 转硬失败。
  - `_entrypoint_top_pkg_matches_plugin_id`：entrypoint 顶层包名必须等于 `plugin_id.replace('-','_')`，且不在 `RESERVED_TOP_PACKAGES` 中（H3 / T04）。
  - 新增 `table_prefix: str | None`（pattern `^[a-z][a-z0-9_]{0,15}_$`）字段。
- `deeptrade/core/migrations/core/20260515_002_affected_tables.sql`：给 `plugin_schema_migrations` 加 `affected_tables TEXT DEFAULT NULL` 列，由框架在每条 migration 应用后写入实际新增的表名（JSON 数组）。purge 路径优先按此列反查归属，`plugin_tables` 仅作为兼容老库的兜底（H2 / T03）。
- `deeptrade/core/plugin_manager.py::_apply_migrations`：重写为按每条 migration 做 schema-diff（`information_schema.tables` 前后快照）+ 3 道边界检查：
  - 新增表必须在 `meta.tables` 声明集合内（创建未声明表 → `PluginInstallError`）；
  - 删除表不可命中 `RESERVED_TABLE_NAMES`（DROP 核心表 → `PluginInstallError`）；
  - 删除表必须当前归属本插件（`plugin_tables` 反查）。
- `deeptrade/core/plugin_manager.py::_load_entrypoint`：新增 `sys.modules["deeptrade"]` 守卫（T07）—— import 前后比较框架模块的对象身份与 `__file__`，被替换则立即还原并 `PluginInstallError`；新增 `isinstance(instance, Plugin)` 校验（T08），缺 `dispatch` / `validate_static` / `metadata` 任一属性即拒绝。
- `deeptrade/core/plugin_manager.py::upgrade`：在跑任何迁移前先比对 `(version, checksum)`——若 metadata 中某条 migration 的 version 已在 DB 中但 checksum 不一致，直接 `PluginInstallError("checksum changed ... historical migrations are immutable")`（T09 / M4）。
- `deeptrade/core/plugin_manager.py::_purge_plugin_tables`：从 `uninstall(purge=True)` 中拆出独立方法。`affected_tables` 全部非空则按其与 `plugin_tables(purge=True)` 取交集；任一行为 NULL（v0.4 老库）则退化到全 `plugin_tables`；无论走哪条路径都在 DROP 前再次拦截 `RESERVED_TABLE_NAMES` 并 `logger.error` 留痕（H1 / T06）。
- `deeptrade/cli_plugin.py::cmd_enable`：补 `PluginInstallError` 捕获分支（T11），把 manager 给出的 "install_path missing, reinstall before enabling" 提示透传，`Exit(2)`。
- `deeptrade/cli_config.py::cmd_show`：表格之后追加黄色 `⚠ N 个 secret 以明文存储` 提示（T13）。
- `deeptrade/core/config.py::ConfigService.count_plaintext_secrets()`：上述 CLI 警告的支撑方法，封装 `SecretStore.list_records()` 过滤，避免 CLI 触私有属性。
- `tests/core/test_plugin_security.py`：v0.5 trust-boundary 全套负向回归，38 个用例覆盖 RESERVED 集合、`table_prefix` warn/hard、entrypoint 顶层包约束（参数化跑 14 个保留包名）、schema-diff 拒创建未声明表、拒 DROP 核心表、purge 即便 plugin_tables 被污染仍跳过核心表、Plugin Protocol 缺 dispatch 拒装、upgrade 拒收已篡改的历史 migration。
- `tests/cli/test_user_facing_strings_are_chinese.py`：T16 中文化回归锁，11 个用例覆盖 `--help / config show / config show --help / plugin list / plugin install --help` 的中文关键字断言 + 6 条参数化反向断言（钉死被替换掉的英文短语）。
- 新增覆盖：`tests/cli/test_plugin_cmd.py::test_enable_missing_install_path_message`（T24）、`tests/cli/test_config_cmd.py::test_config_show_warns_plaintext_secrets` 与 `_no_warning_when_no_secrets_set`（T26）。

### Changed

- `deeptrade/core/plugin_manager.py`：删除自实现的 `_iter_statements` SQL 多语句分割器，改为 `self._db.execute(sql)` 直接喂入整段——DuckDB 原生支持 `;` 分隔多语句执行（T10）。
- `deeptrade/cli.py` / `cli_config.py` / `cli_plugin.py` / `cli_data.py` + `plugin_manager.summarize_for_install`：用户向文案全面中文化（T15）。范围限定为 `typer.echo` / Rich Table 标题与列名 / `questionary.*` prompt / `typer.Option/Argument` 的 `help` / `@app.command` docstring 首行 / `✘ / ✔ / ⚠` 句子；明确**保留英文**：`raise XxxError(...)` 消息、`logger.*`、源码注释 + docstring 第二行及之后、命令名本身——保证异常 / 日志 / stack trace 在英文社区仍可搜可贴。
- `deeptrade/cli_plugin.py::cmd_uninstall`：docstring + `--purge` option help 重写为中文，准确描述实际两段语义（默认 = 删文件 + disable，可重装恢复；`--purge` = 同时 DROP 表与所有 metadata 行，不可恢复）。实际行为不动。
- `pyproject.toml::[tool.hatch.build.targets.sdist].include`：删除不存在的 `/DESIGN.md`、`/PLAN.md`（v0.2.0 删除的 docs 残留），改为收录 `/CHANGELOG.md`（T14）。
- `deeptrade/{cli_data, core/github_fetch, core/dep_installer, core/plugin_source, core/registry}.py` 五处模块 docstring 中的 `docs/*_design.md` 失效引用全部改指 `CHANGELOG.md` 对应版本段（项目 docs 目录已在 v0.2.0 删除，不予恢复）。
- `README.md` 保留 IDs 段落补一句：`db` 长度未达 plugin_id 正则下限（`^[a-z][a-z0-9-]{2,31}$`，至少 3 字符），由正则隐式拒绝；其余四个由 `RESERVED_PLUGIN_IDS` 在安装时显式拦截。

### Migration notes

- **插件作者侧零强制动作**：本版只对边界违例发 `DeprecationWarning`（仅 `table_prefix` 派生不匹配这一项）。v0.6 会把它转硬失败，因此官方插件 + 第三方插件**建议**在下个版本前把表名收敛到 `<plugin_id 派生前缀>_*` 命名空间（如 `limit-up-board` → 派生 `limit_up_board_`，所有表以此开头），或在 `deeptrade_plugin.yaml` 顶层显式声明 `table_prefix: <prefix>_`。
- **v0.4 已装插件平滑升级**：`plugin_schema_migrations.affected_tables` 是新列，老插件历史行将以 NULL 存在；`uninstall --purge` 检测到 NULL 自动退化到 v0.4 的 `plugin_tables` 路径，行为不变。重新跑一次 install/upgrade 会让新写入的 migration 行带上 `affected_tables`，自动进入更严格的路径。
- **DB 自动迁移**：v0.4.2 起 `Database()` 已默认 `auto_migrate=True`，本版新增的 `20260515_002_affected_tables.sql` 在用户下次执行任意 `deeptrade <...>` 命令时静默应用，无需手动 `db upgrade`。设置 `DEEPTRADE_SKIP_AUTO_MIGRATE=1` 仍是逃生通道。
- **CLI 输出语言切换**：异常 / 日志 / `--help` 中 Typer 自动生成的 `Usage:` / `Options:` 头仍是英文（Typer 行为，不打算改），其余命令输出全部中文化。监控 / 自动化脚本如有按英文短语 grep 输出的，需要按 `tests/cli/test_user_facing_strings_are_chinese.py` 中的中文关键字相应更新。

## [v0.4.2] — 2026-05-12 — Database 构造自动迁移 + DEEPTRADE_DEBUG=1 显示堆栈

定位到一个用户报错：升级到 0.4.1 后第一次跑 `deeptrade limit-up-board lgb train ...` 直接报 `✘ TypeError: list indices must be integers or slices, not str`。链路追到 `TushareClient._restore_cached_frame` —— 0.4.1 改用 `{"version","schema","data"}` 包裹格式读取，但旧裸数组的缓存行没被清掉。根因不是缓存代码本身，而是迁移触发链路：`apply_core_migrations` 只在 `deeptrade db init` / `db upgrade` 显式调用，包升级后用户不手动跑就一直拿不到 `20260512_001_drop_legacy_tushare_cache.sql` 的清空动作。同时报错只有一行无堆栈，定位也很慢——这是双层 swallow（插件 + 框架都没渲染 traceback）。

### Changed

- `deeptrade/core/db.py::Database.__init__` 新增 `auto_migrate: bool = True` kwarg；构造时若未关闭（且 `DEEPTRADE_SKIP_AUTO_MIGRATE` 未置位）就自动调一次 `apply_core_migrations(self)`。任何打开 DB 的代码路径（CLI 插件分发、PluginManager、未来的 SDK 调用）都因此跟进 schema，包升级后无需手动 `db upgrade` 即可避开 0.4.1 那种"新读路径碰旧数据"的尴尬。
- `deeptrade/cli.py` 的 `init` / `db init` / `db upgrade` 显式传 `auto_migrate=False`，自己调 `apply_core_migrations` 收集"本次新跑了哪些版本"以打印 `✔ Schema applied: <version>` 行；其他命令走默认 auto-migrate 路径。
- `deeptrade/cli.py::_dispatch` 给插件 `dispatch()` 包了一层兜底：捕获 `BaseException`（放过 `SystemExit` / `KeyboardInterrupt`）后用 `render_exception` 渲染、按 `typer.Exit(1)` 传播。已经自己 catch 的插件不受影响；不 catch 的插件也能在框架层看到一致的输出格式。

### Added

- `deeptrade/plugins_api/errors.py`：`render_exception(exc, *, header_glyph="✘")` 与 `debug_enabled()`。约定环境变量 `DEEPTRADE_DEBUG=1`（接受 `1` / `true` / `yes` / `on`，大小写不敏感）——开启时输出完整 `traceback.format_exception` 文本（自动包含 `__cause__` 链与 exception group），否则维持 `✘ {ExcType}: {msg}` 一行。两者均不带尾部换行，调用方自加。从 `plugins_api/__init__.py` 重导出。
- 逃生通道 `DEEPTRADE_SKIP_AUTO_MIGRATE=1`：当一次失败的迁移把所有 CLI 命令都堵住时，置位该变量绕过 auto-migrate 还原现场。文档位于 README troubleshooting 段（非主流程）。
- `tests/core/test_db.py`：5 个新用例覆盖 auto-migrate 行为——新建 DB 自动跑迁移、`auto_migrate=False` 跳过、`DEEPTRADE_SKIP_AUTO_MIGRATE=1` 跳过、迁移失败硬上抛、**预置旧裸数组 `tushare_cache_blob` 行 → 再开 DB 必须被 0.4.1 的 drop 迁移清掉**（本次故事的回归锁）。
- `tests/plugins_api/test_errors.py`：默认模式一行、`DEEPTRADE_DEBUG=1` 含 `Traceback` + 链式 `__cause__`、自定义 glyph、`0` 视为关闭、`debug_enabled` env 反映。

### Migration notes

- **用户侧零动作**：从 0.4.1 升上来的用户下一次跑任何 `deeptrade <...>` 命令时，`Database()` 都会自动应用 `20260512_001_drop_legacy_tushare_cache.sql`，TypeError 自愈。仍想留旧缓存（不推荐）的用户可用 `DEEPTRADE_SKIP_AUTO_MIGRATE=1 deeptrade ...` 临时绕过。
- **插件作者**：仍可保留自己的 `except Exception` 兜底；推荐改用 `from deeptrade.plugins_api import render_exception`，写成 `sys.stderr.write(render_exception(e) + "\n")` 即可让 `DEEPTRADE_DEBUG=1` 透传栈信息。`api_version` 不动（"1"）；旧插件不调新工具也照常工作。
- **测试夹具变更**：`tests/core/test_db.py::fresh_db` 改用 `auto_migrate=False` 以保留"未迁移"语义；其余 `apply_core_migrations(fresh_db)` 用例无须改动（幂等）。

## [v0.4.1] — 2026-05-12 — Tushare 缓存 dtype 还原 + 消除 pandas FutureWarning

插件在缓存命中路径上反馈 pandas 2.2+ 抛出 `FutureWarning: The behavior of 'to_datetime' with 'unit' when parsing strings is deprecated`。根因在框架侧 `TushareClient._read_cached`：`pd.read_json(..., orient="records")` 默认 `convert_dates=True`，按列名启发式（含 `date / _at / _time / timestamp / modified`）调 `pd.to_datetime(values, errors='ignore', unit='ms')`，tushare 列名几乎全部命中，每次缓存读取都会触发警告；更危险的是 pandas 未来版本会静默改变 string+`unit` 的语义。

### Changed

- `deeptrade/core/tushare_client.py::TushareClient._write_cached` / `_read_cached` 改用 `{"version":1, "schema":{col:dtype_str}, "data":[...records...]}` 的包裹格式存取缓存；读取走 `json.loads + DataFrame.from_records + _restore_cached_frame`，按 schema 显式还原 dtype（`datetime64[*]` → `pd.to_datetime`；非 `object` 数值/布尔列 → `astype`；`object` 跳过）。彻底绕开 `pd.read_json` 的列名启发式，根因消除。
- 框架不引入任何 tushare 业务字段名清单——`schema` 字典从 `df.dtypes.astype(str).to_dict()` 自动派生，保持"框架不持有业务知识"的原则。
- 依赖瘦身：删除 `import io`（旧实现唯一用途）。

### Added

- `deeptrade/core/migrations/core/20260512_001_drop_legacy_tushare_cache.sql`：升级时一次性 `DROP TABLE IF EXISTS tushare_cache_blob`；旧裸数组格式的缓存条目与新读取路径不兼容，惰性建表 `_ensure_cache_table` 会在下次写入时重建。
- `tests/core/test_tushare_client.py::test_cache_payload_round_trip_preserves_dtypes`：以 `trade_date(YYYYMMDD字符串)` + `ts_code(object)` + `close(float64)` + `vol(int64)` + `is_st(bool)` + `ann_dt(datetime64[ns])` 全字段 round-trip，`warnings.simplefilter("error", FutureWarning)` 把 warning 当错误抓，锁住回归。

### Migration notes

- **缓存清空一次**：升级到 0.4.1 后第一次 `apply_core_migrations` 会执行 `DROP TABLE IF EXISTS tushare_cache_blob`；下一次按 `trade_date` 拉取的 API 会走一次远程，之后正常命中新格式缓存。`tushare_sync_state` 不受影响（仍记录 status=ok），但 `_cache_hit` 在缓存表不存在时已会判为 miss，行为安全。
- **插件无需改动**：`TushareClient.call(...)` 返回的 DataFrame dtype 与首次远程拉取时一致——之前依赖 `.dt` 访问器或显式 dtype 的插件代码无须调整。

## [v0.4.0] — 2026-05-12 — 插件级依赖管理 + 框架依赖瘦身

两项主线变更合并发布：

1. **插件可以声明并由框架自动安装自己的 Python 依赖**。`deeptrade_plugin.yaml::dependencies` 接受 PEP 508 specifier；`plugin install / upgrade` 期间走 `uv pip install` → `python -m pip install` 两级回退，已装且满足跳过，不满足硬拒绝并归因到具体冲突方。设计文档：`docs/DeepTrade/plugin_dependency_management_design.md`（仓库外）。
2. **框架主依赖瘦身**：`pandas` / `tushare` 不再是 framework 的 `Requires-Dist`，仅作为 `optional-dependencies.plugin-runtime` / `dev` extras 存在。`deeptrade.core.tushare_client.TushareClient` 这一专为插件准备的 wrapper 行为不变，但其运行时依赖现在由插件自己声明。受影响的插件清单与改造指引见 `docs/DeepTrade/plugin_required_dependencies.md`（仓库外）。

### Added

- `PluginMetadata.dependencies: list[str]`（PEP 508 specifier；`extra="forbid"` 通过默认空列表向后兼容旧 yaml）；Pydantic 校验拒绝非法 spec、VCS/URL 形式、重名包（大小写无关）。
- `deeptrade/core/dep_installer.py`：`parse_specs / plan_install / detect_installer / run_install`；installer 探测优先 `uv`（带 `--python <sys.executable>` 强制装入框架解释器）→ 回退 `python -m pip`；环境变量 `DEEPTRADE_DEP_INSTALL_TIMEOUT` 覆盖默认 300s 超时；marker 不匹配的 requirement 自动跳过。
- `PluginManager._handle_dependencies` / `_build_dep_ownership`：在 `install` / `upgrade` 的 `copytree` 之后、migrations 事务之前解析并安装 deps；冲突错误信息归因到"框架核心依赖"或具体 `plugin <id>`（反查既装插件的 `metadata_yaml`）。
- `plugin install` / `plugin upgrade` CLI 新增 `--no-deps`、`--reinstall-deps`；`summarize_for_install` 增 `dependencies` 行；`--no-deps` 启用时摘要区会显式提示。
- `pyproject.toml::optional-dependencies.plugin-runtime` extras：把 `tushare>=1.4` / `pandas>=2.2` 显式收集到这个组，便于本地通过 `uv sync --extra plugin-runtime` 调试 TushareClient。
- `tests/core/test_plugin_dependencies.py`：29 个用例覆盖元数据校验、planning、installer 探测、PluginManager 集成（安装 / 跳过 / 冲突 / 失败清理 / `--no-deps` / `--reinstall-deps` / 归因到其他插件）、upgrade 行为、CLI 摘要 + flag 透传。

### Changed

- `pyproject.toml::dependencies`：移除 `tushare>=1.4` 与 `pandas>=2.2`；其余 11 条框架直接使用的依赖（typer / questionary / rich / duckdb / openai / pydantic / tenacity / pyyaml / keyring / click / packaging）保持不变。
- `pyproject.toml::optional-dependencies.dev`：补入 `tushare>=1.4` / `pandas>=2.2`，使 `uv sync --all-extras` 仍能跑通 `tests/core/test_tushare_client.py` 等触及 wrapper 的测试。
- `deeptrade/__init__.py::__version__` → `0.4.0`；`pyproject.toml::project.version` → `0.4.0`（两处同步更新，遵 CLAUDE.md 发版流程）。

### Migration notes

- **框架瘦身的兼容性边界**：升级到 0.4.0 框架但**沿用旧版本插件**的用户，如果之前是用 `pip install deeptrade-quant` 一并装下了 pandas / tushare，环境里这两个库仍在，旧插件依然能跑。但**重新部署 / 新建虚拟环境**时，如果插件 yaml 没声明 `pandas` / `tushare`，运行期会以 `ModuleNotFoundError` 暴露——这就是新的"插件应该自己声明依赖"的预期行为。
- **官方插件改造**：`DeepTradePluginOfficial` 仓中的 `limit_up_board` / `volume_anomaly` 等策略需要把 `pandas>=2.2` / `tushare>=1.4` 写进自家 `deeptrade_plugin.yaml::dependencies`。具体指引见 `plugin_required_dependencies.md`。
- **离线 / 私有源用户**：本版本不支持自定义 `index_url`；如需禁用网络安装，用 `deeptrade plugin install <source> --no-deps` 跳过 dep 安装步骤，自行 `pip install` 准备好环境。
- **回滚不卸 deps**：dep 安装成功但后续 migrations / `validate_static` 失败时，已装的依赖**不会被反向卸载**——共享依赖风险下"装哪些卸哪些"会误伤其他插件。设计文档 §4.5 有详细说明。

### Out of scope (recorded for follow-up)

- `deeptrade.core.tushare_client` 仍位于框架代码树；下一步可考虑把它整体迁出框架（独立 PyPI 包或新的 `type=service` 插件类型），届时框架 wheel 完全不带 tushare 相关代码。
- 不引入 `requirements.lock` 风格的依赖锁定；当前每次 install 都按 specifier 实时解析。
- 不引入企业级 `index_url` / 私有 PyPI 源；v1 直接走 PyPI 默认源。

## [v0.3.1] — 2026-05-11 — Tushare 传输层韧性修复

`TushareSDKTransport.call` 用字符串关键字判断异常类型，长跑训练下 httpx 的 `RemoteProtocolError("Response ended prematurely")` 等传输层瞬断错误被错归为不可重试的 `TushareError`，绕过了 tenacity 重试白名单和 5xx → 缓存兜底链路，导致打板策略 lightgbm 训练（单轮 3000+ 次 Tushare 调用）频繁因网络抖动终止。本版本重写分类器并扩展重试策略。设计文档见 `docs/DeepTrade/tushare_transport_resilience_plan.md`（仓库外）。

### Added

- `TushareTransportError`（`TushareServerError` 的子类）：传输层瞬态错误的专门异常类型；自动复用现有重试白名单与 5xx 缓存兜底路径，调用方零改动。
- `_classify_tushare_exception` / `_is_transient_transport_error` / `_extract_http_status` 三个模块级辅助函数，实现"按异常类型 → HTTP 状态码 → 字符串关键字"的三级分类，类型识别覆盖 httpx / requests / urllib3 / stdlib 三栈。
- `app_config.tushare_max_retries`（默认 7，范围 1-20，dotted key `tushare.max_retries`）：tenacity stop_after_attempt 的最大尝试次数；从原来硬编码 5 提到 7。
- `TushareClient.__init__` 新增可选 `max_retries: int = 7` 参数。
- `tests/core/test_tushare_classifier.py`：分类器全套单测（37 个用例），含 "Response ended prematurely" 回归保护。
- `tests/core/test_tushare_retry_r1.py`：硬约束 R1 回归测试——每次 tenacity 重试都必须重新经过 `_TokenBucket.acquire()`；任何把 `bucket.acquire()` 移出 `_do_fetch` 的重构都会被这个测试拦下。

### Changed

- `TushareSDKTransport.call`：原 `except Exception` 中的字符串匹配块全部替换为对 `_classify_tushare_exception` 的调用；不再有 `"5" in msg[:3]` 这种 in 检查的 bug；未识别的异常默认归类为 `TushareTransportError`（可重试），而非历史的 `TushareError`（终态）——这是核心设计反转，意图是远程网络服务的"未知错"绝大多数是瞬态。
- `TushareClient._fetch_with_retries`：从 `@retry` 装饰器形式改为显式 `Retrying(...)( _do_fetch, ...)` 调用，便于通过 `__init__` 注入 `max_retries`；函数体拆为 `_do_fetch`，`self._bucket.acquire()` 仍是 `_do_fetch` 的第一行——见 `__init__` 中的 R1 注释。
- 重试退避策略：`wait_exponential(multiplier=1, min=1, max=15)` → `wait_exponential_jitter(initial=1, max=30, jitter=2)`，加 jitter 散开并发重试的"羊群效应"；`stop_after_attempt(5)` → `stop_after_attempt(max_retries)`（默认 7），最坏等待预算从 ~30s 抬到 ~70s。
- Tushare 限流文案识别扩展：除 "频率"/"限流"/"rate"/"429" 外，新增对 "每分钟…次"（如 "抱歉，您每分钟最多访问该接口500次"）的匹配。
- `tests/core/test_tushare_client.py` 中 4 处 `monkeypatch.setattr(TushareClient._fetch_with_retries.retry, "sleep", ...)` 改为 `monkeypatch.setattr(cli._retrying, "sleep", ...)`，配合 `Retrying` 实例化下放到 `__init__`。
- `deeptrade/__init__.py`：版本 bump 至 `0.3.1`。

### Out of scope (recorded for follow-up)

- `_TokenBucket` 只 decay 不 recover：撞过几次 429 后 rps 单调下降到 0.1，`TushareClient` 实例生命周期内不自愈。lub 训练每轮新建 client 所以单轮内不至于雪崩，但跨长跑场景需在后续版本治理。
- plugin 侧 `collect_training_window` 缺 per-day try/except、缺中间检查点——属插件韧性，不在框架范围。

## [v0.3.0] — 2026-05-11 — 移除 channel 插件类型 + 内置 notifier

本版本移除 `channel` 插件类型与框架内置的 notifier 链。IM 推送在实测中需要登录、轮询、发送多步流程，插件一次性 `dispatch(argv)` 的生命周期与之不匹配，整体能力将以框架级 ChatGateway 模块的形式重做（**本版本不含 ChatGateway 实现**，仅完成清理）。

### Removed

- `deeptrade/plugins_api/channel.py`（`ChannelPlugin` Protocol）
- `deeptrade/plugins_api/notify.py`（`NotificationPayload` / `NotificationSection` / `NotificationItem`）
- `deeptrade/core/notifier.py`（`NoopNotifier` / `MultiplexNotifier` / `AsyncDispatchNotifier` / `build_notifier` / `notify` / `notification_session`）
- `tests/core/test_notifier.py` / `tests/plugins_api/test_notify.py` 整文件
- `tests/plugins_api/test_protocol.py` 中 `ChannelPlugin` 相关 case
- `deeptrade.notify` / `deeptrade.notification_session` 顶层导出

### Changed

- `PluginMetadata.type` 收窄为 `Literal["strategy"]`（字段保留，便于未来扩展新 plugin 类型）。
- `apply_core_migrations` 新增 v0.3.0 数据迁移 `migrate_purge_non_strategy_plugins`：启动时清理 `plugins.type != 'strategy'` 的历史记录、`plugin_tables` / `plugin_schema_migrations` 关联行，并删除对应 install 目录。该迁移必须先于 `PluginManager.list_all()` 执行，否则旧 `channel` 行会触发 Pydantic 校验失败。
- `deeptrade/__init__.py`：版本 bump 至 `0.3.0`，移除 notify 顶层 re-export。
- README / CLAUDE.md：删除 channel / notify 段落与架构图相关条目；CLAUDE.md 新增"IM / notifications"短段说明能力暂缺、ChatGateway 是计划替代物。
- 官方 strategy 插件 `limit_up_board` / `volume_anomaly` 的 `runtime.py`：删除未被调用的 `notify()` / `is_notify_enabled()` 方法与 `NotificationPayload` import（这些方法在实际业务流中无任何调用点）。

### Migration notes

- **从 0.2.x 升级**：`pipx upgrade deeptrade-quant`。下次任何 `deeptrade ...` 命令首次落地时，`apply_core_migrations` 会自动清理 `stdout-channel` 等 `type=channel` 的历史插件记录及其 install 目录（行为等价于 `deeptrade plugin uninstall <id> --purge`），并在日志输出对应警告。
- 官方注册表（`DeepTradePluginOfficial`）的 `stdout-channel` 条目将在配套发版中下线，请勿再尝试安装。
- 业务流仍需要 IM 推送的用户：临时方案是在策略代码内直接 `import httpx` 自行调用 webhook；统一的 ChatGateway 能力将在后续版本提供。

## [v0.2.0] — 2026-05-09 — 框架瘦身（builtin 插件物理移除）· PR-8 cutover

本版本完成了框架与插件的物理解耦——`deeptrade-quant` wheel 不再携带任何插件代码。所有官方插件（`limit-up-board` / `volume-anomaly` / `stdout-channel`）必须通过 `deeptrade plugin install <短名>` 从注册表安装。

### Removed

- `deeptrade/strategies_builtin/` 整目录（含 `limit_up_board` / `volume_anomaly`）
- `deeptrade/channels_builtin/` 整目录（含 `stdout`）
- `tests/strategies_builtin/` 整目录（140 个测试随插件代码迁移到 `DeepTradePluginOfficial`）
- `mypy.ini` 中针对 builtin 的 `ignore_errors`（已无需要忽略的目标）

### Changed

- README.md / docs/quick-start.md / docs/plugin-development.md /
  guide/plugin/strategy/volume-anomaly.md：命令示例从本地 builtin 路径切换为短名（`deeptrade plugin install limit-up-board` 等）。
- `deeptrade/core/notifier.py` docstring：移除 `channels_builtin/ mirrors strategies_builtin/` 的过期引用。

### Migration notes

- **从 0.1.x 升级**：`pipx upgrade deeptrade-quant`。已通过本地路径安装的 builtin 插件**不会**被升级删除（plugin 数据/代码独立于框架 wheel），可继续使用，也可执行 `deeptrade plugin upgrade <短名>` 切换到注册表版本。
- **历史快照可回溯**：`git checkout archive/with-builtin-plugins-v0.1.0-preview` 取回含 builtin 的最后状态。
- **回滚发版**：用户可 `pipx install deeptrade-quant==0.1.0` 锁定上一稳定版本。

### Wheel size impact

| 指标 | v0.1.0 | v0.2.0 |
|------|--------|--------|
| wheel 文件数 | 74 | 37 |
| Python 源文件（mypy 扫描） | 65 | 31 |
| 内置 .sql migration | 4 | 1（仅框架核心） |

## [v0.1.0] — 2026-05-09 — 框架与插件解耦 · PyPI 首个稳定发布（PR-1 ~ PR-6）

PyPI distribution name: **`deeptrade-quant`**（CLI 命令仍是 `deeptrade`）。

```bash
pipx install deeptrade-quant      # or: uv tool install deeptrade-quant
deeptrade plugin search           # browse the official registry
deeptrade plugin install limit-up-board
```

参考 `docs/distribution-and-plugin-install-design.md`。本版本是 §10 中
PR-1~PR-6 的合集；builtin 插件目录仍随 wheel 一起发布（兼容旧的本地路径
安装），下一个版本（PR-8 cutover）将完成瘦身。

### Added

- **从 PyPI 分发**：项目元数据、LICENSE、GitHub Actions（`ci.yml` /
  `release.yml`）就位；`tag v*` 触发 OIDC Trusted Publisher 直发 PyPI。
- **官方插件注册表客户端**（`deeptrade.core.registry`）：从
  `raw.githubusercontent.com/ty19880929/DeepTradePluginOfficial/main/registry/index.json`
  拉取，ETag 缓存到 `~/.deeptrade/plugins/registry-cache.json`；网络故障
  时回退到本地缓存。
- **GitHub tarball 拉取**（`deeptrade.core.github_fetch`）：stdlib 实现
  （`urllib` + `tarfile`），无第三方 HTTP 依赖；`GITHUB_TOKEN` 环境变量
  自动用于 rate-limit / 私有仓库（未来扩展）；解压做 path-traversal
  防护。
- **来源解析层**（`deeptrade.core.plugin_source`）：`SourceResolver` 把
  用户输入（短名 / GitHub URL / 本地路径）统一解析为本地目录，强制
  `min_framework_version` 校验。
- **新命令** `deeptrade plugin search [keyword] [--no-cache]`：列出注册
  表中可用的插件；`--no-cache` 旁路 ETag 缓存。
- **`plugin info` 注册表 fallback**：未安装但在注册表中时显示注册表条
  目 + 安装命令提示。

### Changed

- `deeptrade plugin install <SOURCE>` 现支持三种来源：短名（注册表）、
  完整 git 仓库 URL、本地路径。判定顺序：本地目录存在 → git URL → 短
  名。`--ref <tag|branch|sha>` 可指定具体 ref（默认拉该插件最新 release
  tag）。
- `deeptrade plugin upgrade <SOURCE>` 同上，且加入 SemVer 比较：
  - 待装 == 已装：`exit 0` + "已是最新版本 vX"
  - 待装 > 已装：执行升级
  - 待装 < 已装：`exit 2` + 提示先 `uninstall --purge`（**禁止降级**，
    因为框架未建模 migration 回滚）
- `PluginManager.upgrade()` 返回类型改为 `InstalledPlugin | UpgradeNoop`。

### Fixed

- `pyproject.toml` 删除了与 `packages = ["deeptrade"]` 冲突的
  `[tool.hatch.build.targets.wheel.force-include]` 段（曾导致 wheel 内
  `deeptrade/core/migrations/` 路径 duplicate name，让 PyPI 上传失败）。
- `deeptrade.core.notifier`：channel 实例 cast 到 `ChannelPlugin`（mypy
  narrowing 修复）。
- 一批预存在的 ruff lint（UP045 / F401 / I001 / W292 / B023）清理。

### Notes

- builtin 插件（`limit-up-board` / `volume-anomaly` / `stdout-channel`）
  代码已迁移到 [`DeepTradePluginOfficial`](https://github.com/ty19880929/DeepTradePluginOfficial)
  独立仓库，并发布了首个 release tag（`limit-up-board/v0.4.0` 等）。
  本版本框架仍然包含旧 builtin 子树作为兼容期保留；下一版本（瘦身后）
  将物理移除。
- archive tag `archive/with-builtin-plugins-v0.1.0-preview` 永久保留含
  builtin 子树的最后状态，可随时 `git checkout` 取回。

## [volume-anomaly v0.6.0] — 2026-05-08 — 显式分维度评分（PR-6）

> 仅升级 volume-anomaly 插件版本（0.5.0 → 0.6.0）；框架版本不变。
> 升级方式：`deeptrade plugin upgrade <plugin-source>`，会自动应用新增的
> migration（`20260601_002_dimension_scores.sql`）。

> ⚠ **Schema 变更**：`VATrendCandidate.dimension_scores` 现为**必填字段**；
> 旧 LLM 响应不再可解析（但持久化到 `va_stage_results.raw_response_json`
> 不受影响——升级前的历史数据完整保留可读，仅新写入要求新 schema）。

参考 `docs/volume_anomaly_wave2_design.md` § 2。

### Added (volume-anomaly plugin)

- **新子模型 `VADimensionScores`**：6 个维度（`washout` / `pattern` /
  `capital` / `sector` / `historical` / `risk`）每个 0–100 整数评分，
  其中 `risk` 为反向极性（高分 = 高风险），其余为正向极性。
- **`VATrendCandidate.dimension_scores` 必填**（F8）：每只候选都必须输出
  6 维评分，由 LLM 自洽与 `launch_score` 大致一致（F3 / F14 软约束，
  不强制公式）。
- **Prompt 评分尺度锚定**：`VA_TREND_SYSTEM` 加入【dimension_scores 评分尺度】
  段（0-30 / 30-60 / 60-80 / 80-100 四档语义），每个判断维度（A–F）末尾
  显式标注对应 `dimension_scores.<name>`。
- **Few-Shot 示例同步**：`prompts_examples.py` A/B 示例补 `dimension_scores`
  对象，与示例评分尺度一致。
- **拆 6 列持久化**（G6）：`va_stage_results` 新增 `dim_washout` /
  `dim_pattern` / `dim_capital` / `dim_sector` / `dim_historical` /
  `dim_risk` 6 个 `DOUBLE` 列。`runner._write_stage_results` 写入
  对应列；旧行 `dim_*` 全部为 `NULL`，stats SQL 自动过滤。
  原始 JSON 仍写入 `raw_response_json` 作为审计备份。
- **stats `--by dimension_scores`**：新支持子选项，输出 6 个维度评分与
  `ret_t3` 的 Pearson 相关系数（DuckDB `CORR(...)` 内置聚合）。
- **报告紧凑表达**：`write_analyze_report` 在 imminent_launch / watching
  表中新增"W/P/C/S/H/R"列，紧凑展示 6 维分数（如 `80/75/70/75/60/25`）。

### Implementation notes

- 拆列而非单 JSON 列的理由（G6）：`stats --by dimension_scores` 要求
  6 维与 `ret_t3` 做 Pearson，`AVG`/`CORR` 在拆列模式下纯 SQL 表达；
  `JSON_EXTRACT` 在 SQL 层昂贵且 DuckDB 跨版本兼容性差。
- 输出 token 预算上调：`DEFAULT_AVG_OUTPUT_TOKENS_PER_CANDIDATE`
  900 → 1100（吸纳 6 维评分 + alpha 字段），`plan_batches` 按新预算切批。
- F13 决策：6 维（W/P/C/S/H/R），不加 liquidity。

### Breaking changes

- 旧 LLM 响应（无 `dimension_scores`）会在 Pydantic 解析阶段抛
  `ValidationError`。已落地的批次再 retry 时需更新 prompt（已自动包含）。
- 回滚路径：revert PR-6 不需要 drop 列（`NULL` 列不影响旧逻辑），但
  已经按新 schema 持久化的行将无法被旧版本读取——因此降级时**保留
  `raw_response_json` 而忽略 `dimension_scores_json`**。

## [volume-anomaly v0.5.0] — 2026-05-08 — RPS / 大盘相对 alpha（PR-5）

> 仅升级 volume-anomaly 插件版本（0.4.0 → 0.5.0）；框架版本不变。
> 升级方式：`deeptrade plugin upgrade <plugin-source>`。本版本不新增 migration。

参考 `docs/volume_anomaly_wave2_design.md` § 1。

### Added (volume-anomaly plugin)

- **沪深 300 alpha 字段**（P1-1）：候选行新增 `alpha_5d_pct` / `alpha_20d_pct` /
  `alpha_60d_pct`（个股相对沪深 300 同期累计收益差，单位 %）+ `baseline_index_code`
  + `rel_strength_label ∈ {leading, in_line, lagging}`（基于 alpha_20d_pct
  ±5% 分档）。让 LLM 在判断"放量异动"时区分跟随性反弹与抗跌强势。
- **新 tushare 权限 `index_daily`（optional）**：250d 拉一次沪深 300 基准
  收盘价；G1—与个股 daily 长度对称、cache 友好。
- **维度 D 改名 / 扩写**：`VA_TREND_SYSTEM` 中维度 D 由"板块强度"改为
  "**板块与市场相对强度**"，提示 LLM 同时考虑板块层 (`sector_strength_*`)
  与市场层 (`alpha_*_pct` / `rel_strength_label`)。
- **Few-Shot 示例补充 alpha 引用**：`prompts_examples.py` 的 A/B 示例
  各加一条 `alpha_20d_pct` 引用，确保 anchoring 与新维度一致。
- **G8 显式降级提示**：`index_daily` 权限缺失或 fetch 失败时，runner emit
  一条 `EventLevel.WARN` LOG 事件，明确提示用户"alpha 字段降级为 None；
  如需启用 alpha，请确认 Tushare 账户已开通 index_daily 权限"。

### Behavior change

- analyze 阶段每次 run 多 1 次 `index_daily` 调用（cache 友好；首次冷拉
  ~5s），无新数据消费 SLA 影响。
- LLM 输入每候选 +30–40 tokens（5d/20d/60d alpha + label）；
  在 200K 输入预算下完全可接受。

### Implementation notes

- alpha 计算口径：`alpha_n = stock_pct_chg_n − baseline_pct_chg_n`（简单收益）。
- `rel_strength_label` 仅基于 `alpha_20d_pct`；alpha_20d 缺失时
  `rel_strength_label = None`，但 `baseline_index_code` 永远输出（元数据）。
- F2 决策：行业相对 alpha 暂不做，留给波次 3 视效果再加。

## [volume-anomaly v0.4.0] — 2026-05-08 — T+N 自动回测闭环（PR-4）

> 仅升级 volume-anomaly 插件版本（0.3.0 → 0.4.0）；框架版本不变。
> 升级方式：`deeptrade plugin upgrade <plugin-source>`，会自动应用新增的
> migration（`20260601_001_realized_returns.sql`）。

参考 `docs/volume_anomaly_wave2_design.md` § 3。

### Added (volume-anomaly plugin)

- **新表 `va_realized_returns`**：每条 hit 一行（PK=`anomaly_date+ts_code`），
  存 T+1/T+3/T+5/T+10 收盘价、对应收益、5d/10d 窗口最大涨幅与回撤。
  `data_status ∈ {pending, partial, complete}`（G5 严格三态：T+1 未到→pending；
  max_horizon 未到 OR 任一 horizon 数据缺失→partial；max_horizon 已到且全填→complete）。
- **新子命令 `evaluate`**：
  ```
  deeptrade volume-anomaly evaluate [--lookback-days N] [--trade-date YYYYMMDD]
                                    [--backfill-all] [--force-recompute]
  ```
  从 `va_anomaly_history` 全集（G3）里取 anomaly_date 在 lookback 内的 hits，
  按 trade-day horizon 解析 T+1/T+3/T+5/T+10 实际 trade_date，复用
  `_fetch_daily_history_by_date` 的 cache 拉取收盘价，UPSERT 进
  `va_realized_returns`。`complete` 行默认跳过；`--force-recompute` 强制重算。
- **新子命令 `stats`**：
  ```
  deeptrade volume-anomaly stats [--from] [--to] [--by prediction|pattern|launch_score_bin]
  ```
  纯只读 SQL 聚合 `va_stage_results JOIN va_realized_returns`，输出每桶的
  样本数 / T+3 平均收益 / T+3 胜率 / T+5 最大涨幅均。`launch_score_bin`
  默认分箱 `0-40 / 40-60 / 60-80 / 80-100`（G4）。
- **`evaluate` 写 va_runs / va_events**（G10）：`mode='evaluate'` 与
  screen / analyze / prune 同级，可在 `history` 子命令中查看。

### Implementation notes

- 所有 horizon 时间换算用 `TradeCalendar.next_open` 跳过周末/节假日。
- `va_realized_returns.t_close` 冗余存（G9）——让 stats 在 va_anomaly_history
  被异常清空时仍可读取。
- 不新增 tushare API 权限（仍只用 `daily`）。
- 不引入新插件级 config 表（F6）；horizon 列表写死在 module-level
  `EVALUATE_HORIZONS = (1, 3, 5, 10)`。

## [volume-anomaly v0.3.0] — 2026-05-08 — 波次 1（PR-1 + PR-2 + PR-3）

> 仅升级 volume-anomaly 插件版本（0.2.0 → 0.3.0）；框架版本不变。
> 升级方式：`deeptrade plugin upgrade <plugin-source>`。本版本不新增 migration。

参考 `docs/volume_anomaly_wave1_design.md`（§ 1–5 共五项 P0 优化）。

### Added (volume-anomaly plugin)

**PR-1 — Screen 规则**

- **上影线过滤**（P0-1）：T-day 规则后、换手率前新增一道过滤，
  `upper_shadow_ratio = (high − max(open, close)) / range > upper_shadow_ratio_max`
  的候选直接淘汰，避免"避雷针"型的纯上影 K 线进入候选。诊断字段
  `n_after_upper_shadow` 暴露在漏斗中；hit 行新增 `upper_shadow_ratio` 字段。
- **按流通市值分桶的换手率阈值**（P0-2）：用 `daily_basic.circ_mv` 把候选按
  流通市值（亿元）切 4 档，每档使用独立的 `[turnover_min, turnover_max]`：
  `≤50亿: [5, 15]` / `50–200亿: [3.5, 12]` / `200–1000亿: [2.5, 9]` /
  `>1000亿: [1.5, 6]`。边界值归"较小桶"（E4）。`circ_mv` 缺失时退化到
  全局 `turnover_min/max`，并写入 `data_unavailable`。诊断字段
  `turnover_bucket_hits` / `n_missing_circ_mv` / `circ_mv_missing_codes`
  暴露在 screen 报告中；hit 行新增 `circ_mv_yi` / `turnover_bucket` 字段。

**PR-2 — Analyze 候选行特征**

- **VCP 三维收敛指标**（P0-3）：候选行新增
  `atr_10d_pct` / `atr_10d_quantile_in_60d`（10 日 ATR 在近 60 日 ATR 序列中的分位数）/
  `bbw_20d`（20 日 Bollinger 带宽）/ `bbw_compression_ratio`
  （当前 BBW / 近 60 日 BBW 均值，<1 表示在收敛）。零新数据，复用现有
  60d 历史窗口（实际从 250d 切片）。
- **120/250 日阻力位距离**（P0-4）：`collect_analyze_bundle` 默认
  `history_lookback` 由 60 → 250；`_build_candidate_row` 内部切片处理。
  候选行新增 `high_120d` / `high_250d` / `low_120d` /
  `dist_to_120d_high_pct` / `dist_to_250d_high_pct` /
  `is_above_120d_high` / `is_above_250d_high` / `pos_in_120d_range`。
  E3-A：仅 120d 区间位置，不补 `low_250d`。E7-C：任一窗口数据不足则
  对应字段降级为 `None`，不阻塞主流程。
- **首次冷拉延迟**：扩展 250d 窗口的首次 fetch 约多 30–45 秒
  （Tushare RPS=6 默认下），后续走 cache 零增量。

**PR-3 — LLM Prompt 对齐**

- **Few-Shot 锚定**（P0-5）：新增 `prompts_examples.py::VA_TREND_FEWSHOT`，
  在 `VA_TREND_SYSTEM` 末尾拼接两个示例（VCP 突破 / 高位上影诱多），
  引导 LLM 在不同 provider 间保持一致的 `launch_score` 尺度。
- **维度 A 提示扩展**：在【判断维度】A 段中加入"整理期波动率收敛"指引，
  显式提示 LLM 引用 `atr_10d_quantile_in_60d` / `bbw_compression_ratio`。
- **字段一致性单测**：`test_prompt_consistency.py` 用正则提取
  示例中的 `"field": "<X>"`，断言每个 X 都属于
  `_build_candidate_row` 输出键集合 OR screen hit 行字段集合。
  防止后续字段重命名时 prompt 失配。

### Default behavior change (heads-up)

- 默认开启 **上影线过滤**：`ScreenRules.upper_shadow_ratio_max = 0.35`。
  如需保持旧行为，在 `screen_rules` 配置中显式传 `"upper_shadow_ratio_max": null`。
- 默认开启 **分桶换手率**：`ScreenRules.turnover_buckets = DEFAULT_TURNOVER_BUCKETS`。
  如需保持旧行为，在 `screen_rules` 配置中显式传 `"turnover_buckets": null`，
  此时回退到 `[turnover_min, turnover_max]` 全局阈值。
- 默认 analyze 历史窗口由 60 → 250 个交易日（E2-A 单 fetch 复用）。
  首次冷启动会多消耗 30–45s（RPS=6 默认），后续走 cache 零增量。

### Implementation notes

- `turnover_buckets` JSON 配置中最后一档可用 `null` 表示无穷上界，
  `from_dict` 内转 `math.inf`。`as_dict()` 反向把 `math.inf` 序列化为 `null`，
  使 `screen_stats.json` 仍是合规的标准 JSON（不依赖 `Infinity` 扩展）。
- 不动 framework；不新增 tushare API 权限（`circ_mv` 已包含在
  `daily_basic` 字段中；250d 长窗口仍走 `daily`）。
- VCP 计算 `_compute_atr_series` 用简单平均 TR（与 Wilder 公式相比差异
  在 60d 窗口尺度内不显著）；BBW 用 4σ / MA20 × 100 表达带宽占均价比例。

## [limit-up-board v0.4.0] — 2026-05-07 — 候选股市值/股价过滤 + 持久化设置

> 仅升级 limit-up-board 插件版本（0.3.2 → 0.4.0）；框架版本不变。
> 升级方式：`deeptrade plugin upgrade <plugin-source>`，会自动应用新增的
> migration（`20260508_005_lub_config.sql`）。

### Added (limit-up-board plugin)

- **Step 1 候选股漏斗增加两条筛选条件**：在主板 + 涨停 join 之后、ST/停牌之前
  按 *流通市值* 与 *当前股价* 上限再筛一层。null 在任一字段 → 过滤（保守语义）。
  默认 `流通市值 < 100亿` + `股价 < 15元`。`bundle.market_summary` 新增
  `candidate_filter_summary` 字段（before / after / 阈值），LLM prompt 中可见。
- **新子命令 `deeptrade limit-up-board settings`**：交互式编辑两个上限阈值，
  回车保留当前值，写入新表 `lub_config`。
- **新子命令 `deeptrade limit-up-board settings show`**：表格展示当前生效设置，
  source ∈ {`default`, `persisted`}。
- **运行时打印当前配置**：`run` / `sync` 在 Step 1 之前 emit 一条 LOG 事件
  （`运行配置: 流通市值 < ...亿、股价 < ...元`），dashboard / 日志中可见。

### Implementation notes

- 配置存储：插件自有 `lub_config` 表（与 `lub_runs` / `lub_events` 同 Plan A
  纯隔离层级），不复用框架级 `app_config` —— 避免 `_DOT_TO_FIELD` 白名单
  扩张，框架保持轻量。
- 默认值唯一来源：`limit_up_board.config:LubConfig` 的 dataclass default；
  SQL / CLI / 文档不重复声明。

## [limit-up-board v0.3.0] — 2026-05-07 — Phase A + Phase B 因子补齐

> 仅升级 limit-up-board 插件版本（0.2.1 → 0.3.0）；框架版本不变。
> 升级方式：`deeptrade plugin upgrade <plugin-source>`，会自动应用本版本新增的两条
> migration（`20260508_001_lub_lhb_tables.sql` / `20260508_002_lub_cyq_perf.sql`）。

参考 `docs/limit-up-board-optimization-plan.md`。对照 Gemini 给出的"游资思路"因子
清单，分两阶段补齐：A 阶段为派生因子（不增 tushare API），B 阶段接入龙虎榜与筹码。

### Added (limit-up-board plugin)

**A1 — 派生因子（候选股层，pure compute）**
- `amplitude_pct`：T 日振幅 = (high − low) / pre_close × 100。
- `fd_amount_ratio`：封单比 = fd_amount / amount × 100，>10% 为强势封板。
- `ma5` / `ma10` / `ma20` + `ma_bull_aligned`：基于 prev_daily 的简单移动平均；
  `ma_bull_aligned = (close > ma5 > ma10 > ma20)`。历史不足窗口期返回 null。
- `up_count_30d`：近 30 个交易日 pct_chg ≥ 9.8 的天数；不足 30 日返回 null。

**A2 — 市场情绪三件套（market_summary 层）**
- `limit_step_distribution_prev` + `limit_step_trend`：T 与 T-1 的连板梯队对比，
  interpretation ∈ {`spectrum_lifting`, `spectrum_collapsing`, `stable`}。
- `yesterday_failure_rate`：T-1 全市场炸板率（z / (u + z)），interpretation ∈
  {`high` ≥25%, `moderate`, `low` ≤10%}。
- `yesterday_winners_today`：T-1 涨停股在 T 日的连板率与平均涨幅，
  interpretation ∈ {`strong_money_effect`, `neutral`, `weak_money_effect`}。

**B1 — 龙虎榜（top_list / top_inst → required）**
- 候选股层字段：`lhb_net_buy_yi`（龙虎榜净买入，亿元；正/负均如实给出）、
  `lhb_inst_count`（机构席位 unique 数）、`lhb_famous_seats`（命中游资白名单的
  exalter 原文数组）。
- `FAMOUS_SEATS_HINTS`：~15 条主流游资席位子串白名单（拉萨系、宁波系、章盟主、
  赵老哥、厦门帮等）。匹配逻辑大小写不敏感；只给 exalter 原文，不暴露白名单标签。
- 新表：`lub_top_list`（key trade_date+ts_code）、`lub_top_inst`
  （key trade_date+ts_code+exalter+side）。
- "未上榜 ≠ 数据缺失"：candidate 不在当日 top_list 中时 `lhb_*` 为 null，但
  `data_unavailable` 中**不会**出现 top_list/top_inst（接口本身可用）。

**B2 — 筹码（cyq_perf → required）**
- 候选股层字段：`cyq_winner_pct`（获利盘比例 %）、`cyq_top10_concentration`
  （100 − (cost_95pct − cost_5pct) / weight_avg × 100，clip [0,100]）、
  `cyq_avg_cost_yuan`（weight_avg）、`cyq_close_to_avg_cost_pct`
  （(close − weight_avg) / weight_avg × 100）。
- 新表：`lub_cyq_perf`（key trade_date+ts_code）。

**Prompt 改动**
- R1_SYSTEM【分析维度】新增形态、历史基因、市场情绪三类；evidence 新增"missing_data
  字段不得引用"硬约束。
- R2_SYSTEM【判断重点】追加：亏钱效应下自动下调 confidence 一档；梯队拉升期允许
  上调 continuation_score；筹码维度三阈值（70/60/-10）+ 龙虎榜维度（未上榜 ≠ 缺失；
  负净买入不可作为正面 evidence；不可推断游资身份）。
- R1 **不引入** chip / LHB 维度（控制 prompt 噪声，B 阶段仅 R2 使用）。

### Changed (BREAKING)

**Tushare 权限要求扩张**（用户必须确保 tushare 账户已开通以下权限）：
- `top_list` / `top_inst` / `cyq_perf` 由 `permissions.tushare_apis.optional` 上移
  到 `required`；任一缺失 → run failed。**未引入新权限分类**——"接口可用但
  candidate 未上榜"用 null + 数据层 inner-join 自然表达。

**默认值 / 内部行为变更**
- `RunParams.daily_lookback` / CLI `--daily-lookback` 默认值 10 → **30**（满足
  ma20 + up_count_30d）。
- `collect_round1` 新增 `prev_trade_date: str | None` 入参；3 个 runner 调用点
  通过 `_safe_prev_trade_date(cal, T)` 计算并传入。
- daily 历史窗口的日历缓冲由 `lookback + 5` 改为 `lookback × 2`，确保 30 个交易
  日 lookback 在过节窗口下仍能命中 ≥30 行真实交易日。

### Migrations

- `20260508_001_lub_lhb_tables.sql` — 创建 `lub_top_list` / `lub_top_inst`。
- `20260508_002_lub_cyq_perf.sql` — 创建 `lub_cyq_perf`。

### Tests

- `tests/strategies_builtin/limit_up_board/test_phase_a_factors.py`：33 个单元
  测试，覆盖 A1/A2 全部派生函数 + 边界（不足窗口期、零分母、空 frame、阈值跳点）。

- `tests/strategies_builtin/limit_up_board/test_phase_b_factors.py`：20 个单元
  测试，覆盖白名单匹配（大小写、去重、非字符串）、LHB rollup（空帧 / None /
  仅 top_list）、cyq 集中度（紧/宽/截断到 0）、close_to_avg_cost_pct 边界。

## [v0.7.0] — 2026-05-01 — Stage 概念归插件 + 配置键改名

清理 v0.6 留下的 stage 硬编码技术债。Stage 名字、preset → stage tuning 表全
部移入插件；框架的 `LLMClient.complete_json` 不再认识 stage，由调用方直接传
入 `StageProfile`。配置键 `deepseek.profile` 同步重命名为 `app.profile`。
**Breaking change**（项目仍在 dev/iteration 期）。设计原文：DESIGN.md §10.1。

### Changed (BREAKING)

**框架瘦身 — stage 退出 LLMClient**

- 删除 `core.llm_client.KNOWN_STAGES` / `LLMUnknownStageError` /
  `_stage_profile()` / 全局 `_CURRENT_STAGE`。
- `LLMClient.complete_json` 签名变化：
  - 删除 `stage: str` 入参；framework 不再写 `llm_calls.stage` 列。
  - 新增 `profile: StageProfile`（必填）— 调用方直接传入已解析的调参档。
  - `LLMClient.__init__` 删除 `profiles=` 入参。
  - `LLMManager.get_client()` 不再绑 profile。
- `RecordedTransport` 改为纯 FIFO：`register(response)` 不再带 stage 标签。
- 删除 `core.config.DS_STAGES` / `DeepSeekProfileSet` / `PROFILES_DEFAULT`
  / `ConfigService.get_profile()`。
- `StageProfile` 升格为公共契约，搬到 `deeptrade.plugins_api.llm`，由
  `from deeptrade.plugins_api import StageProfile` 公开导出。

**配置键改名 — `deepseek.profile` → `app.profile`**

- `AppConfig.deepseek_profile` → `AppConfig.app_profile`；`_DOT_TO_FIELD`
  同步更新。preset 仍是 `Literal["fast","balanced","quality"]`，语义全局，
  但键名 vendor-agnostic。
- DB 行自动迁移：`config_migrations.migrate_legacy_deepseek_profile_key`
  幂等地把 `deepseek.profile` 行改写为 `app.profile`，并删除旧行。
- **环境变量直接断代**：`DEEPTRADE_DEEPSEEK_PROFILE` 不再被识别。
  `ConfigService.get_app_config()` 启动时若检测到旧 env 而新 env 未设，
  抛 `RuntimeError` 退出 — 避免静默用错配置（默认会回落到 "balanced"，
  让用户以为生效但其实并没有）。请改为 `DEEPTRADE_APP_PROFILE`。

**DB schema — `llm_calls.stage` 列删除**

- 新 SQL 迁移 `20260501_002_drop_llm_calls_stage.sql` `ALTER TABLE
  llm_calls DROP COLUMN IF EXISTS stage`（DuckDB 1.0+）。
- `core/migrations/core/20260427_001_init.sql` 同步去掉 `stage` 字段，
  让 fresh DB 直接落到 v0.7 期望状态。
- 历史 run 的 stage 信息仍可在
  `~/.deeptrade/reports/<run_id>/llm_calls.jsonl` 中查阅；v0.7 起新写入的
  jsonl 行也不再含 `stage` 键。

**插件改造（两个内建插件）**

- 新增 `<plugin>/profiles.py`：本地维护 preset → stage tuning 表 +
  `resolve_profile(preset, stage)`。
- `runner.py` 读取 `cfg.app_profile`（preset 字符串），传给 pipeline 函数；
  pipeline 内部调 `resolve_profile()` 得到 `StageProfile`。
- `volume-anomaly` 借此改造把语义错误的 stage 名 `continuation_prediction`
  改回 `trend_analysis`。

### Added

- `deeptrade/plugins_api/llm.py` — 公共 `StageProfile` 契约（4 字段：
  thinking / reasoning_effort / temperature / max_output_tokens）。
- `config_migrations.migrate_legacy_deepseek_profile_key` + 4 条单测
  （rename / 幂等 / fresh DB no-op / new-already-set 跳过）。
- `tests/core/test_config.py` 新增两条 env 行为测试（旧 env 报错、新旧并存
  以新为准）。

### Engineering

- 136 pytest tests passing (无变更)。
- 偿还 v0.6 RV6-4 / RV6 §10.2 已知技术债。

## [v0.6.0] — 2026-05-01 — LLM Manager 化（多 Provider）

The LLM client is no longer DeepSeek-specific — it is now a framework-level
service that lets a single plugin call multiple OpenAI-compatible LLMs in
the same run. **Breaking change** (project remains in dev/iteration phase).
Full rationale: `DESIGN.md` §0.7 + §10.

### Changed (BREAKING)

**Configuration model — `deepseek.*` → `llm.*`**

- `llm.providers` (JSON dict, app_config) — `{name: {base_url, model, timeout}}`.
  Multiple providers coexist; each plugin picks by name at call time.
- `llm.<name>.api_key` (secret_store) — one secret per provider. The
  `is_secret_key()` predicate (replacing the old `SECRET_KEYS` constant)
  matches `tushare.token` plus this dynamic prefix.
- `llm.audit_full_payload` (bool, app_config) — replaces
  `deepseek.audit_full_payload`.
- `deepseek.profile` is **kept** as the global stage-profile name (rename
  deferred to v0.7 per §10.1 note).
- The four legacy keys `deepseek.base_url` / `deepseek.model` /
  `deepseek.timeout` / `deepseek.audit_full_payload` are removed from
  `AppConfig`. There is **no `llm.default`** — callers must pass a name.

**Auto-migration**

- On first `apply_core_migrations()` after upgrade, legacy `deepseek.*`
  rows are migrated into `llm.providers["deepseek"]` + the renamed secret
  `llm.deepseek.api_key` + `llm.audit_full_payload`. Idempotent: re-runs
  on already-migrated DBs are no-ops. Code in
  `deeptrade.core.config_migrations.migrate_legacy_deepseek_keys`.

**LLM client / new manager**

- New `core/llm_manager.py::LLMManager` — the only path plugins should use:
  `list_providers()`, `get_provider_info(name)`, `get_client(name, *,
  plugin_id, run_id, reports_dir=None)`. Caches clients per
  `(name, plugin_id, run_id)`. Documented as not thread-safe.
- `core/deepseek_client.py` → `core/llm_client.py`;
  `DeepSeekClient` → `LLMClient`. `OpenAIClientTransport` keeps its name
  (it really is the OpenAI-compatible transport).
- `LLMNotConfiguredError` (new) — raised by manager when a provider is
  missing or its api_key is unset.

**CLI command surface**

- Added: `config set-llm` (interactive new / edit / delete one provider),
  `config list-llm` (show usable providers), `config test-llm [name]`
  (per-provider connectivity check; tests all when omitted).
- Removed: `config set-deepseek`, `config test`. The init-time prompt
  ("Configure deepseek now?") is now "Configure an LLM provider now?".
- `config show` expands `llm.providers` so each provider's `api_key` slot
  is its own masked row.

**Plugin runtime collapse**

- `volume-anomaly` and `limit-up-board` runtimes lose their per-plugin
  `build_llm_client(rt)`; both now declare `llms: LLMManager` and call
  `rt.llms.get_client(provider_name, plugin_id=, run_id=, reports_dir=)`.
  Provider selection helper `pick_llm_provider(rt)` ships in each
  plugin's `runtime.py` (prefers `deepseek`, falls back to first
  available); a per-plugin `default_llm` config key is deferred to v0.7.

### Added

- `LLMProviderConfig` Pydantic model (per-provider connection record).
- `ConfigService.set_llm_provider(name, *, base_url, model, timeout,
  api_key=None)` and `delete_llm_provider(name)` — CRUD helpers used by
  the CLI.
- `tests/core/test_llm_manager.py` — list/info/get_client + cache + missing
  api_key/provider errors + multi-provider coexistence (11 cases).
- `tests/core/test_config_migrations.py` — idempotency + happy path +
  partial-legacy-state edge case (7 cases).

### Engineering

- 136 pytest tests passing (was 130; +11 manager + 7 migration − 12 retired
  duplicates).
- ruff + mypy clean on touched files.
- DESIGN §0.7 + §7.1 + §7.3 + §10 rewritten; PLAN §11 adds the v0.6 work
  breakdown.

### Known design debts (deferred)

- `KNOWN_STAGES` (`strong_target_analysis / continuation_prediction /
  final_ranking`) is still hardcoded in the framework — leaks plugin
  semantics into `core/llm_client.py`. v0.7 will let plugins declare their
  own stage names + per-stage profile overrides.
- `deepseek.profile` key name retained for backward compat within the
  current dev cycle; rename to `llm.profile` planned for v0.7.
- No transport plugin type yet — Anthropic native / Gemini native cannot be
  added without code changes. Will land as `type=llm-transport` plugins
  when first user need surfaces.

## [v0.5.0] — 2026-04-30 — Plugin CLI dispatch + pure data isolation

Breaking-change reshape per `docs/plugin_cli_dispatch_evaluation.md`. The
project is in dev/iteration phase; **no backward compatibility**.

### Changed (BREAKING)

**Framework command surface — closed**
- Top-level CLI is now `init / config / plugin / data` ONLY. `strategy` and
  `channel` command groups removed.
- Pure pass-through: any unknown first token is looked up as a `plugin_id`;
  if installed + enabled, framework calls `Plugin.dispatch(remaining_argv)`
  and is otherwise dumb.
- `deeptrade --help` no longer enumerates plugin subcommands. `--help`
  inside a plugin is the plugin's own responsibility.
- `deeptrade hello` removed. Interactive main menu removed.
- Reserved plugin_ids: `init`, `config`, `plugin`, `data`.

**Plugin contract — minimal**
- `StrategyPlugin` Protocol removed. New unified `Plugin` Protocol:
  `metadata` + `validate_static(ctx)` + `dispatch(argv) -> int`.
- `ChannelPlugin` extends `Plugin` and adds `push(ctx, payload)`.
- `StrategyContext` / `StrategyParams` / `StrategyRunner` / TUI dashboard
  removed. Each plugin owns its own runtime + run lifecycle internally.
- `ChannelContext` renamed to `PluginContext` (still narrow: db + config +
  plugin_id).

**Data isolation — Plan A (pure)**
- Framework owns ONLY: `app_config`, `secret_store`, `schema_migrations`,
  `plugins`, `plugin_tables`, `plugin_schema_migrations`, `llm_calls`,
  `tushare_sync_state`, `tushare_calls`. No business tables.
- Tushare-derived shared market tables (`stock_basic`, `trade_cal`, `daily`,
  `daily_basic`, `moneyflow`) removed from core. Each strategy plugin
  declares its own prefixed copies (e.g. `lub_stock_basic`, `va_*`).
- `tushare_sync_state` PK now `(plugin_id, api_name, trade_date)`. Each
  plugin tracks its own sync state and cache; no cross-plugin sharing.
- `tushare_calls` and `llm_calls` add `plugin_id` column.
- `TushareClient.__init__` requires `plugin_id`. Framework probes use the
  reserved `FRAMEWORK_PLUGIN_ID = "__framework__"` sentinel.

**Notification API**
- `core/notifier.py` exposes top-level `notify(db, payload)` and
  `notification_session(db)`. Re-exported as `from deeptrade import
  notify, notification_session`. NoopNotifier when no channel enabled.
- `strategy_runs` / `strategy_events` tables removed. Each plugin defines
  its own `<prefix>_runs` / `<prefix>_events` if it wants run history.

### Added

- Built-in plugins reshaped to v0.2.0:
  - `limit-up-board`: own `cli.py` + `plugin.py` + `runner.py` + `runtime.py`;
    new migration with 10 `lub_*` tables.
  - `volume-anomaly`: same pattern; 5 `va_*` tables; subcommands `screen`
    / `analyze` / `prune` / `history` / `report`.
  - `stdout-channel`: implements new `Plugin` + `ChannelPlugin` contracts;
    own `dispatch` for `test` / `log`.
- Tests: framework routing tests (`tests/cli/test_routing.py`); Plugin
  Protocol contract tests (`tests/plugins_api/test_protocol.py`); plugin
  install + migration isolation tests (`tests/core/test_plugin_install.py`).

### Removed

- `cli_strategy.py`, `cli_channel.py`, `tui/` package, `core/strategy_runner.py`,
  `core/context.py`, old plugin `strategy.py` files.
- `textual` dependency.
- `_interactive_main_menu`, `hello` command.

## [v0.1.0] — 2026-04-28

First public release. Baseline implementation of DESIGN.md v0.3.1.

### Added

**Framework**

- `deeptrade init` — DuckDB layout + 11-table core schema migrations (idempotent).
- `deeptrade config show / set / set-tushare / set-deepseek / test` — layered config (env > db > default), keyring + plaintext fallback for secrets.
- `deeptrade plugin install / list / info / disable / enable / upgrade / uninstall [--purge]` — three-stage lifecycle (install **never** touches network; validate is connectivity-only; run does the strict checks).
- `deeptrade strategy list / run / history / report <run_id>` — Live EVA-themed dashboard (header / progress / events / analysis / footer); `--no-dashboard` for non-tty.
- Plugin api_version "1": `StrategyPlugin` Protocol, `StrategyContext`, `StrategyEvent` enum, Pydantic `PluginMetadata` (YAML).

**Core services**

- `Database` — single-process, single-writer DuckDB; reentrant write lock; short transactions.
- `SecretStore` — keyring-first, plaintext fallback with explicit warning.
- `TushareClient` — token-bucket rate limit, tenacity retries, 4 cache classes (static / trade_day_immutable / trade_day_mutable / hot_or_anns), `data_completeness` (final / intraday) for intraday isolation.
- `DeepSeekClient` — JSON-mode + Pydantic double-validate; profile triple (`fast` / `balanced` / `quality`); stage-level `max_output_tokens` (R1/R2 default 32k, final_ranking 8k); **never** passes `tools` / `tool_choice` / `functions`.
- `StrategyRunner` — status state machine (running → success / failed / partial_failed / cancelled); KeyboardInterrupt → cancelled; any `VALIDATION_FAILED` event flips success → partial_failed.
- `setup_logging()` — stderr handler + rotating file under `~/.deeptrade/logs/`.

**Built-in strategy: limit-up-board**

- Step 0 `resolve_trade_date()` — most-recent-closed trade day; `app.close_after` configurable threshold; `--allow-intraday` opt-in.
- Step 1 data assembly — main board filter (Q2: SSE/SZSE only), ST/suspended exclusion, three-tier `sector_strength` fallback (`limit_cpt_list` → `lu_desc_aggregation` → `industry_fallback`), normalized prompt fields (亿/万 + 2dp) while DB keeps raw.
- Step 2 R1 — `plan_r1_batches()` with input + output token DUAL budget (F5); `EvidenceItem.unit` mandatory.
- Step 4 R2 — single batch by default; auto multi-batch when input exceeds budget.
- Step 4.5 `final_ranking` — only triggered on multi-batch R2; `select_finalists()` keeps top + watchlist + boundary avoid samples.
- Step 5 reports — 5-file dump under `~/.deeptrade/reports/<run_id>/`; banner stack rules (red for partial_failed/failed/cancelled, yellow for INTRADAY MODE, both stack).

### Fixed (v0.3 review round 2 → v0.3.1)

- **F1** `limit_step` was duplicated in optional table → removed; required-only.
- **F2** `limit_cpt_list` `✅` mismatched optional status → unified to `optional+fallback` everywhere; `sector_strength_source` label propagated to prompts.
- **F3** Fast profile R2 `thinking: true` contradicted "all off" docs → flipped to `false`.
- **F4** `--allow-intraday` would have polluted EOD caches → added `data_completeness` column; daily-mode reader rejects intraday-cached rows; UI/report INTRADAY MODE banner.
- **F5** Default `max_output_tokens=8192` would truncate R1 → moved to per-stage profile (R1/R2 32k, final_ranking 8k); R1 evidence cap 8 → 4; rationale length-capped via prompt.
- **S1** Migrations are now the **sole** DDL source; `tables` only declares names + purge flag.
- **S2** `app.close_after` configurable (default 18:00); install never touches tushare.
- **S3** `strategy_runs.status` CHECK constraint removed (DuckDB ALTER limitation); validation moved to Pydantic layer.
- **S4** `row_count=0` is a legal outcome (extreme tape day); fallback predicate accepts it.
- **S5** `final_ranking` only ranks finalists; non-finalists keep `batch_local_rank`; both surface in `round2_predictions.json`.

### Engineering

- 163 pytest tests passing; ruff + mypy clean.
- 1 real concurrency bug fixed during development: `Database.transaction()` + `execute()` self-deadlock with non-reentrant `threading.Lock` → switched to `threading.RLock`.
- 1 pandas 2.x compat fix: `pd.read_json(json_str)` deprecated → wrap in `io.StringIO()`.

### Known design debts (planned for v0.4)

- **D1** Replace `configure(ctx) -> dict` with schema-driven `get_param_schema() -> type[BaseModel]`; CLI auto-renders questionary forms; non-interactive mode supports `--params-file`.
- **D2** Per-API `probes` in plugin metadata; `validate` becomes two layers (`validate_connectivity` + `validate_required_apis`).

[v0.1.0]: https://github.com/example/deeptrade/releases/tag/v0.1.0
