# 5 分钟上手

本文带你从零完成：安装 → 配置 → 安装内置插件 → 跑一次 → 看报告。

## 0. 前置

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) 或 pip
- Tushare 账号 + token（≥ 8000 积分以解锁 `limit_list_d`/`limit_step` 等核心接口）
- DeepSeek API key（[官网](https://platform.deepseek.com/) 申请）

## 1. 安装

```bash
git clone <repo>
cd DeepTrade

# 推荐
uv sync --all-extras

# 兜底
python -m venv .venv
source .venv/bin/activate     # Windows: .\.venv\Scripts\activate
pip install -e ".[dev]"
```

验证：

```bash
$ uv run deeptrade --version
DeepTrade 0.0.1
```

## 2. 初始化

```bash
$ uv run deeptrade init
✔ Database created: ~/.deeptrade/deeptrade.duckdb
✔ Schema applied: 20260427_001
? Configure tushare now? Y
? Tushare token: ********
? Tushare RPS [6.0]:
? Tushare timeout (s) [30]:
✔ Saved tushare config
? Configure deepseek now? Y
? DeepSeek API key: ********
? Base URL [https://api.deepseek.com]:
? Model [deepseek-v4-pro]:
? Profile: › balanced
✔ Saved deepseek config
```

非交互（CI / 脚本）：

```bash
deeptrade init --no-prompts
deeptrade config set tushare.token <YOUR_TOKEN>
deeptrade config set deepseek.api_key <YOUR_KEY>
```

## 3. 自检

```bash
$ uv run deeptrade config test
✔ Tushare: stock_basic returned 5247 rows in 320ms
✔ DeepSeek: echo ok (1234ms)
```

如有任一项失败：

- `tushare.token not configured` → 重跑 `config set-tushare`
- `Permission denied` 类错误 → 检查积分等级 / token 是否正确
- `Connection refused` → 检查网络

## 4. 安装官方插件

官方插件维护在 [DeepTradePluginOfficial](https://github.com/ty19880929/DeepTradePluginOfficial) 仓库。框架通过短名查注册表，自动拉取最新 release tarball 并安装。

```bash
# 浏览注册表
deeptrade plugin search

# strategy 插件
deeptrade plugin install limit-up-board
deeptrade plugin install volume-anomaly

# channel 插件（推送通道；可选）
deeptrade plugin install stdout-channel

# 查看
deeptrade plugin list
```

每个插件在自己的 `deeptrade_plugin.yaml` + `migrations/*.sql` 里声明它需要的全部表（每插件自己拥有自己 tushare 派生数据，零跨插件耦合）。

> **离线 / 自定义版本**：本地路径仍可装：`deeptrade plugin install ./path/to/plugin`。指定 ref：`deeptrade plugin install limit-up-board --ref limit-up-board/v0.3.0`。

## 5. 跑一次

每个插件**自己拥有**它的命令树。框架只做 `deeptrade <plugin_id> ...argv` 透传，`--help` 由插件自管：

```bash
$ deeptrade limit-up-board --help
Commands:
  run      Run the full打板策略 pipeline.
  sync     Fetch + persist data only (no LLM stages).
  history  List recent runs of this plugin.
  report   Re-display a finished run's report.
```

跑一次：

```bash
# 默认日终模式
$ deeptrade limit-up-board run

# 盘中模式（数据可能不完整）
$ deeptrade limit-up-board run --allow-intraday

# 指定历史日 + 强制重新同步
$ deeptrade limit-up-board run --trade-date 20260424 --force-sync
```

成交量异动有三种子命令：

```bash
$ deeptrade volume-anomaly screen           # 异动筛选 → 加入待追踪池
$ deeptrade volume-anomaly analyze          # LLM 主升浪启动预测
$ deeptrade volume-anomaly prune --days 30  # 剔除追踪超过 30 日的标的
```

## 6. 看报告

```bash
$ ls ~/.deeptrade/reports/<run_id>/
summary.md                  # 全文 markdown
round1_strong_targets.json  # R1 完整 JSON
round2_predictions.json     # R2 全量候选
round2_final_ranking.json   # 仅 R2 多批时存在
data_snapshot.json          # 输入快照
llm_calls.jsonl             # LLM 调用流水
```

历史 / 重看，由插件自己的子命令提供（不再由框架命令提供）：

```bash
$ deeptrade limit-up-board history
$ deeptrade limit-up-board report <run_id>
$ deeptrade limit-up-board report <run_id> --full   # 完整 markdown

$ deeptrade volume-anomaly history
$ deeptrade volume-anomaly report <run_id>
```

## 7. 推送通知（可选）

如安装了 channel 插件，任何插件调 `deeptrade.notify(...)` 都会自动派发到所有 enabled 渠道，无需指定具体渠道：

```bash
# 自检：从 stdout-channel 自己合成一条 payload，通过 notifier 链路发回 stdout-channel.push()
$ deeptrade stdout-channel test
✔ push success (channel=stdout-channel run_id=... status=success)

# 看通道审计日志
$ deeptrade stdout-channel log --limit 5
```

## 8. 调试 tips

- 加 `DEEPTRADE_LOG_LEVEL=DEBUG` 查看详尽日志：

  ```bash
  DEEPTRADE_LOG_LEVEL=DEBUG deeptrade limit-up-board run
  ```

- 强制重新同步行情（忽略缓存）：`--force-sync`

- 关闭 thinking 加速（profile=fast，全程关思维链）：

  ```bash
  deeptrade config set app.profile fast
  ```

- 看每次 LLM 调用的原始请求/响应：

  ```sql
  -- 直接 duckdb-cli 打开 DB
  SELECT validation_status, input_tokens, output_tokens
    FROM llm_calls
    WHERE plugin_id = 'limit-up-board'
    ORDER BY created_at DESC LIMIT 20;
  ```

下一步：[plugin-development.md](plugin-development.md) 写一个自己的插件。
