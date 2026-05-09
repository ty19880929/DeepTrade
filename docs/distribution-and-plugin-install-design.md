# DeepTrade 分发形式与插件安装机制设计

> **状态**：设计待确认
> **目标读者**：DeepTrade 维护者
> **范围**：框架本身的分发渠道；官方插件的组织、发布、安装、升级机制
>
> **核心承诺**：本方案的所有改动**严禁对现有功能产生任何影响、严禁引入降级 bug**。具体保障策略见 §8（迁移而非删除、archive tag 兜底、cutover 检查清单、回滚预案）。

---

## 1. 背景与目标

当前 DeepTrade 形态：

- 框架代码在主仓库；`pyproject.toml` 已是标准 hatchling 包，`[project.scripts] deeptrade = "deeptrade.cli:app"` 入口完整。
- 内置插件（`limit-up-board`、`volume-anomaly`、`stdout`）跟框架源码同住一棵树，分别在 `deeptrade/strategies_builtin/`、`deeptrade/channels_builtin/`，会随 wheel 一起装到 site-packages。
- `deeptrade plugin install` 仅支持本地路径安装（`PluginManager.install(source_path: Path)`）。

本设计要达到：

1. **框架与插件彻底解耦**：框架走 PyPI（`pipx install deeptrade-quant` / `uv tool install deeptrade-quant`，CLI 命令仍是 `deeptrade`），不携带任何插件代码。
2. **插件从 GitHub 获取**：`deeptrade plugin install <短名>` 通过官方注册表反查到 GitHub repo，拉取 tarball 安装。
3. **保留本地安装通道**：开发者写新插件、第三方插件未进官方注册表时，仍可 `deeptrade plugin install <local-path>` 本地装。
4. **第三方 GitHub URL 直装**：`deeptrade plugin install <git-url>` 作为兜底（要求是完整 git 仓库地址）。
5. **发布工程化**：框架通过 GitHub Actions 自动发布到 PyPI；官方插件仓库通过 GitHub Actions 校验注册表 + 触发 release。

---

## 2. 关键决策记录

| 决策点 | 选择 |
|--------|------|
| 内置插件是否随框架发？ | **不**。框架是纯运行时空壳，所有插件通过 `plugin install` 获取 |
| 用户怎么"指"一个插件？ | **短名 + 注册表**；同时支持本地路径和完整 git URL |
| 默认拉哪个 ref？ | **该插件的最新 release tag**（SemVer 排序最高） |
| 拉取手段 | **GitHub tarball API**（`/repos/{o}/{r}/tarball/{ref}`） + stdlib `tarfile`，无需用户装 git |
| 官方插件组织形式 | **Monorepo**：`DeepTradePluginOfficial` 仓库，子目录承载各插件，仓库本身不做版本管理 |
| 插件 Python 依赖 | **约定只能用框架已有依赖**，metadata schema 不留 `requirements` 字段 |
| 安装来源 | 短名（注册表） / 完整 git URL / 本地路径，三选一 |
| 升级语义 | 等版本提示并 `exit 0`；高版本执行升级；**禁止降级**（要求先 `uninstall --purge` 再 install） |
| Tag 与 release 策略 | 每插件独立 tag：`<plugin-id>/v<X.Y.Z>`，注册表通过 `tag_prefix` 关联 |
| 注册表存放位置 | 官方插件仓库的 `registry/index.json`，CLI 通过 `raw.githubusercontent.com` 拉取 |
| URL 直装范围 | 必须是完整 git 仓库地址（`https://github.com/<owner>/<repo>` 或 `git@...`） |
| 不依赖 Python 的分发？ | **不做**。坚持 `pipx` / `uv tool install` 路线 |
| 框架发布机制 | **GitHub Actions** + PyPI Trusted Publishers（OIDC，无 token 管理） |
| PyPI 项目名（distribution name） | `deeptrade-quant`（PyPI 上 `deeptrade` 名已被占用） |
| Python 包名（import name） | `deeptrade`（保持不变，`import deeptrade` 仍可用） |
| CLI 命令名 | `deeptrade`（保持不变） |

---

## 3. 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│  框架仓库:  github.com/ty19880929/deeptrade                       │
│   - 纯运行时（不含任何插件代码）                                │
│   - registry 客户端、tarball fetch、source resolver           │
│   - GitHub Actions: tag v* → 发布到 PyPI                     │
│                                                              │
│  分发：pipx install deeptrade-quant  (命令仍叫 deeptrade)    │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  官方插件仓库: github.com/ty19880929/DeepTradePluginOfficial    │
│   - Monorepo，无整仓版本号                                    │
│   - 子目录: limit_up_board/, volume_anomaly/, stdout/, ...   │
│   - 每插件独立 tag: <plugin-id>/vX.Y.Z                       │
│   - registry/index.json 索引所有插件                          │
│   - GitHub Actions: PR check + tag 触发 release              │
└─────────────────────────────────────────────────────────────┘

用户机:  ~/.deeptrade/plugins/
   ├── installed/<plugin_id>/<version>/    （现有，已落地）
   ├── registry-cache.json                 （新增，带 ETag）
   └── tarball-cache/                      （新增，可选）
```

**插件来源到 install 流水线的统一**：

```
plugin install limit-up-board                   plugin install ./my-plugin
       │                                                │
       ▼                                                ▼
 查注册表 → tarball API 拉 monorepo →            已是本地目录
 解压到临时目录的 <subdir>                              │
       │                                                │
       └──────────► 现有 install(tmp_path) ◄────────────┘
                    （checksum / migrations / static check）
```

设计意图：把"GitHub 拉取"做成 install 之前的**来源解析层**，现有 `PluginManager.install(source_path: Path)` **不需要改签名**。

---

## 4. CLI 接口

### 4.1 命令清单

```
deeptrade plugin install <SOURCE> [-y] [--ref <tag|branch|sha>]
deeptrade plugin upgrade <SOURCE> [--ref <tag|branch|sha>]
deeptrade plugin search [<keyword>]                       # 新增
deeptrade plugin info <plugin_id>                         # 已有，扩展为可查未安装的
deeptrade plugin list                                     # 已有
deeptrade plugin enable / disable / uninstall             # 已有，无变化
```

### 4.2 `<SOURCE>` 三种形式的判定顺序

| 形式 | 判定 | 例子 |
|------|------|------|
| 本地路径 | 路径存在且是目录 | `./my-plugin`、`/abs/path/plugin`、`C:\plugins\foo` |
| Git URL | 以 `http(s)://` 或 `git@` 开头，且能解析出 owner/repo | `https://github.com/foo/bar`、`git@github.com:foo/bar.git` |
| 短名 | 其他情况，查注册表 | `limit-up-board` |

### 4.3 `--ref` 默认值

- 本地路径：无意义（忽略）
- 短名：注册表条目 `tag_prefix` 下的最新 release tag
- Git URL：仓库的最新 release tag（不带 prefix 筛选）

`--ref <branch|tag|sha>` 显式覆盖。

### 4.4 退出码语义

| 命令 | 情况 | 输出 | exit |
|------|------|------|------|
| `install` | 成功 | `✔ 已安装: X v0.4.0` | 0 |
| `install` | 已安装 | `✘ X 已安装于 ...，请用 plugin upgrade` | 2 |
| `install` | 网络/校验失败 | `✘ <具体错误>` | 2 |
| `install` | 用户拒绝确认 | `Aborted.` | 1 |
| `upgrade` | 待装 > 已装 | `✔ upgraded: X → vY.Y.Y` | 0 |
| `upgrade` | 待装 == 已装 | `已是最新版本 vX.Y.Z` | **0** |
| `upgrade` | 待装 < 已装（禁止降级） | `✘ 待装版本 X 低于已装 Y...` | 2 |
| `upgrade` | 未安装 | `✘ X 未安装，请先 install` | 2 |

`upgrade` 等版本时退出 0，与 `pip install` 已装时的语义一致，便于脚本/CI 串联。

---

## 5. 注册表设计

### 5.1 存放位置

`DeepTradePluginOfficial` 仓库根下 `registry/index.json`。

CLI 拉取地址：

```
https://raw.githubusercontent.com/ty19880929/DeepTradePluginOfficial/main/registry/index.json
```

本地 ETag 缓存：`~/.deeptrade/plugins/registry-cache.json`。

### 5.2 文件 Schema

```json
{
  "schema_version": 1,
  "plugins": {
    "limit-up-board": {
      "name": "打板策略",
      "type": "strategy",
      "description": "A 股打板策略：双轮 LLM 漏斗（强势标的分析 → 连板预测）",
      "repo": "ty19880929/DeepTradePluginOfficial",
      "subdir": "limit_up_board",
      "tag_prefix": "limit-up-board/",
      "min_framework_version": "0.1.0"
    },
    "volume-anomaly": {
      "name": "成交量异动策略",
      "type": "strategy",
      "description": "主板成交量异动筛选 + LLM 主升浪启动预测（screen / analyze / prune 三模式）",
      "repo": "ty19880929/DeepTradePluginOfficial",
      "subdir": "volume_anomaly",
      "tag_prefix": "volume-anomaly/",
      "min_framework_version": "0.1.0"
    },
    "stdout-channel": {
      "name": "Stdout Channel",
      "type": "channel",
      "description": "Reference notification channel — fully consumes the payload but only prints '✔ push success' to stdout.",
      "repo": "ty19880929/DeepTradePluginOfficial",
      "subdir": "stdout",
      "tag_prefix": "stdout-channel/",
      "min_framework_version": "0.1.0"
    }
  }
}
```

> **注**：注册表 key（`stdout-channel`）必须等于子目录 `deeptrade_plugin.yaml` 中的 `plugin_id` 字段（§5.3 字段约束）；`subdir`（`stdout`）则是仓库内的目录名，两者不要求一致。`repo` 字段必须是完整的 `owner/repo` 形式，CLI 会用它拼接 GitHub API URL。

### 5.3 字段约束

| 字段 | 含义 | 约束 |
|------|------|------|
| `schema_version` | 索引文件版本号 | 当前固定 `1`；后续不兼容变更才递增 |
| `plugins.<key>` | key 即 `plugin_id` | 必须与子目录 `deeptrade_plugin.yaml` 中的 `plugin_id` 一致 |
| `name` | 中文展示名 | 与 `deeptrade_plugin.yaml.name` 一致 |
| `type` | `strategy` / `channel` | 与 `deeptrade_plugin.yaml.type` 一致 |
| `description` | 简短描述 | search 命令展示用 |
| `repo` | GitHub `owner/repo` | 当前都是 `ty19880929/DeepTradePluginOfficial`；保留字段以备未来跨仓库 |
| `subdir` | 仓库内的插件目录 | monorepo 必填，相对仓库根 |
| `tag_prefix` | release tag 前缀 | 必须以 `/` 结尾，例如 `limit-up-board/` |
| `min_framework_version` | 该插件可用的最低框架版本 | SemVer；CLI 在 install 前比对 |

### 5.4 加新插件流程

- **官方维护者**：直接向 `DeepTradePluginOfficial` 仓库提交 PR，新增子目录 + 修改 `registry/index.json`。
- **第三方贡献者**：向 `DeepTradePluginOfficial` 仓库提 PR；维护者审核后合并。

第三方插件不打算进官方注册表的，仍可通过本地路径或完整 git URL 直装，不进入 `plugin search` 列表。

---

## 6. 框架侧新增模块

### 6.1 `deeptrade/core/registry.py`

```python
class RegistryClient:
    URL = "https://raw.githubusercontent.com/ty19880929/DeepTradePluginOfficial/main/registry/index.json"
    CACHE = paths.user_data_dir() / "plugins" / "registry-cache.json"

    def fetch(self, *, force: bool = False) -> Registry:
        """ETag 缓存；force=True 旁路缓存。"""

    def resolve(self, plugin_id: str) -> RegistryEntry:
        """找不到 → RegistryNotFoundError。"""
```

错误类型：
- `RegistryFetchError`：网络/HTTP 错误
- `RegistryNotFoundError`：注册表里没有该 plugin_id
- `RegistrySchemaError`：拉到的 JSON 不符合 schema

### 6.2 `deeptrade/core/github_fetch.py`

```python
def latest_release_tag(repo: str, tag_prefix: str = "") -> str:
    """
    GET /repos/{repo}/releases (paginated)
    若 tag_prefix 非空：筛 tag.startswith(tag_prefix)；剩余部分按 SemVer 排序，取最高。
    若 tag_prefix 为空：所有 tag 按 SemVer 排序，取最高（用于 URL 直装场景）。
    没匹配 → NoMatchingReleaseError
    """

def fetch_tarball(repo: str, ref: str, dest_dir: Path) -> Path:
    """
    GET /repos/{repo}/tarball/{ref}, 流式写文件 → tarfile 解压到 dest_dir。
    返回 dest_dir 下唯一的顶级目录（GitHub tarball 顶层是 <owner>-<repo>-<sha7>/）。
    User-Agent: "deeptrade-cli/<version>"。
    可选环境变量 GITHUB_TOKEN 提升 rate limit / 访问私有仓库（未来扩展点）。
    错误 → TarballFetchError
    """
```

实现要点：
- 用 stdlib `urllib.request`，不引入 `requests`
- 流式下载 + 临时文件，避免大 repo 撑爆内存
- `tarfile` 使用 `data` filter（Python 3.12+）防止路径穿越；3.11 兼容路径校验

### 6.3 `deeptrade/core/plugin_source.py`（来源解析层）

```python
@dataclass
class ResolvedSource:
    path: Path           # 已就绪的本地源目录（含 deeptrade_plugin.yaml）
    origin: str          # "local" | "github_registry" | "github_url"
    origin_detail: dict  # {repo, ref, subdir} for github sources
    cleanup: Callable[[], None] | None = None  # 临时目录清理 hook

class SourceResolver:
    def __init__(self, registry: RegistryClient, framework_version: str): ...

    def resolve(self, raw: str, ref: str | None = None) -> ResolvedSource:
        if Path(raw).is_dir():
            return ResolvedSource(Path(raw).resolve(), "local", {})
        if _is_git_url(raw):
            return self._resolve_url(raw, ref)
        return self._resolve_short_name(raw, ref)
```

`_resolve_short_name` 流程：

1. `RegistryClient.fetch()` → `entry`
2. 校验 `framework_version >= entry.min_framework_version`，否则报错并提示 `请先升级 deeptrade`
3. `ref or latest_release_tag(entry.repo, entry.tag_prefix)`
4. `fetch_tarball(entry.repo, ref, tmp_dir)` → 解压
5. 进入 `tmp_dir/<top>/<entry.subdir>`
6. 返回 `ResolvedSource(path=..., origin="github_registry", origin_detail={...}, cleanup=tmp.cleanup)`

`_resolve_url` 流程：

1. 解析 `owner/repo`（支持 `https://github.com/o/r`、`https://github.com/o/r.git`、`git@github.com:o/r.git`）
2. `ref or latest_release_tag(repo, "")`
3. `fetch_tarball(repo, ref, tmp_dir)` → 解压
4. 校验 `tmp_dir/<top>/deeptrade_plugin.yaml` 存在（URL 直装假定**仓库根**就是插件目录；不支持指定 subdir，第三方 monorepo 自行拆分仓库）
5. 返回 `ResolvedSource(path=..., origin="github_url", ...)`

**临时目录管理**：用 `tempfile.TemporaryDirectory()`，CLI 命令结束（或 `mgr.install` 完成 `shutil.copytree` 后）调用 `cleanup`。`PluginManager` 内部已经把 source 复制到 `~/.deeptrade/plugins/installed/<id>/<version>/`，临时目录可立即清理。

### 6.4 CLI 改造（`deeptrade/cli_plugin.py`）

```python
@app.command("install")
def cmd_install(
    source: str = typer.Argument(..., help="短名 / 本地路径 / GitHub URL"),
    ref: str | None = typer.Option(None, "--ref"),
    yes: bool = typer.Option(False, "-y", "--yes"),
) -> None:
    resolver = SourceResolver(RegistryClient(), framework_version=__version__)
    try:
        resolved = resolver.resolve(source, ref)
    except (RegistryNotFoundError, TarballFetchError, NoMatchingReleaseError, RegistryFetchError) as e:
        typer.echo(f"✘ {e}")
        raise typer.Exit(2) from e

    try:
        meta = _load_metadata_yaml(resolved.path / "deeptrade_plugin.yaml")
    except PluginInstallError as e:
        typer.echo(f"✘ {e}")
        raise typer.Exit(2) from e

    typer.echo("─── 即将安装 ─────────────────────────────")
    typer.echo(f"来源: {_format_origin(resolved)}")
    typer.echo(summarize_for_install(meta, resolved.path))
    typer.echo("──────────────────────────────────────────")
    if not yes:
        ok = questionary.confirm("确认安装?", default=False).ask()
        if not ok:
            typer.echo("Aborted.")
            raise typer.Exit(1)

    db, mgr = _open()
    try:
        rec = mgr.install(resolved.path)
    except PluginInstallError as e:
        typer.echo(f"✘ Install failed: {e}")
        raise typer.Exit(2) from e
    finally:
        db.close()
        if resolved.cleanup:
            resolved.cleanup()

    typer.echo(f"✔ 已安装: {rec.plugin_id} v{rec.version}")
```

`cmd_upgrade` 同构改造（`source` 由 Path 改为 str），并叠加 §7 的版本比较语义。

`cmd_search` 新增：

```python
@app.command("search")
def cmd_search(
    keyword: str | None = typer.Argument(None),
    no_cache: bool = typer.Option(False, "--no-cache"),
) -> None:
    """列出注册表中的可安装插件。"""
    registry = RegistryClient().fetch(force=no_cache)
    rows = [...]  # 按 keyword 过滤 name/description/plugin_id
    # 表格输出：plugin_id / name / type / latest_version / description
```

`cmd_info` 扩展：未安装时也能展示注册表信息（fallback 到注册表查询）。

---

## 7. PluginManager 升级语义改造

### 7.1 版本比较

```python
from packaging.version import Version

def upgrade(self, source_path: Path) -> InstalledPlugin | UpgradeNoop:
    new_meta = _load_metadata_yaml(source_path / "deeptrade_plugin.yaml")
    cur = self._fetch_one_plugin(new_meta.plugin_id)
    if cur is None:
        raise PluginNotFoundError(...)

    cmp = (Version(new_meta.version) > Version(cur.version)) - (Version(new_meta.version) < Version(cur.version))
    if cmp == 0:
        return UpgradeNoop(plugin_id=new_meta.plugin_id, version=cur.version)
    if cmp < 0:
        raise PluginInstallError(
            f"待装版本 {new_meta.version} 低于已装 {cur.version}；"
            f"如需降级，请先 `deeptrade plugin uninstall {new_meta.plugin_id} --purge`"
        )
    # cmp > 0: 走现有 upgrade 主体
    ...
```

### 7.2 禁止降级的理由

当前 install/upgrade 流水线在 metadata 的 `migrations` 列表上**只做前向应用**，没有回滚机制。降级到旧版本时，旧版本 yaml 包含的 migration 列表更短，已经跑过的"未来" migration 不会被回滚，会留下"幽灵 schema"（DB 里存在新列，但旧版代码不知道）。

为了避免数据不一致：

- 本地降级直接报错，提示用户先 `uninstall --purge` 再 `install`
- GitHub 来源理论上不会触发降级（注册表只指向最新版），但 `--ref` 指定老 tag 时也会被这一层拦住
- 未来如需"安全降级"，需要插件 metadata 增加 down migration 概念，目前不做

### 7.3 `UpgradeNoop` 返回类型

新增轻量 dataclass：

```python
@dataclass
class UpgradeNoop:
    plugin_id: str
    version: str
```

CLI 层根据返回类型决定输出与 exit code：

```python
result = mgr.upgrade(resolved.path)
if isinstance(result, UpgradeNoop):
    typer.echo(f"已是最新版本 v{result.version}")
    return  # exit 0
typer.echo(f"✔ upgraded: {result.plugin_id} → v{result.version}")
```

---

## 8. 框架瘦身（**代码迁移而非删除** + 零回归保障）

> **强约束**：本节描述的所有动作必须满足"现有功能不降级"。任何一步如果可能破坏既有用户路径，**先建立替代路径并验证通过**，**再**移除旧路径。

### 8.1 用词澄清：迁移 ≠ 删除

"框架瘦身"在物理上分两个动作，**严格分离、不能合并**：

1. **迁移**：把 `deeptrade/strategies_builtin/limit_up_board/`、`deeptrade/strategies_builtin/volume_anomaly/`、`deeptrade/channels_builtin/stdout/` 的代码**完整复制**到新仓库 `DeepTradePluginOfficial`，并在新仓库为每个插件打 release tag、push 注册表。
2. **从主仓库工作树移除**：在新仓库的"等价路径"全链路验证通过（§8.5 cutover 检查清单逐项满足）后，**才**在主仓库 `git rm` 掉这两个 builtin 目录。

任意时刻，主仓库的 git history 都保留这些代码的完整历史；外加一个 archive tag（§8.4）作为"最后一个含 builtin 的版本"快照，永远可被 `git checkout` 取出。**这不是 destructive operation**。

### 8.2 插件代码迁移影响评估（已实证）

针对"迁移到新仓库后，插件代码本身是否需要改动"这个问题，对当前代码做了完整审查（grep 实证），结论：**插件代码零改动**。

支撑事实：

| 影响维度 | 现状 | 迁移后 | 是否需改动 |
|---------|------|-------|-----------|
| 插件内部模块互引（`from .data import ...`） | 36 处相对 import，落在内层包 `<plugin_id>/<plugin_id>/*.py` | 仍是相对 import；`PluginManager._load_entrypoint` 把 install_path 加到 sys.path 后 `import_module(top_pkg)`，与插件位于何处无关 | **否** |
| 插件引用框架（`from deeptrade.core import paths`、`from deeptrade.plugins_api import ...`） | 走 site-packages 中已安装的 `deeptrade` 包 | 完全相同；新仓库的插件假设 `pip install deeptrade` 已提供框架 | **否** |
| 插件之间互引 | 实证 grep：limit_up_board / volume_anomaly / stdout 三者**互不引用** | 不存在 | **否** |
| `deeptrade_plugin.yaml` 的 `entrypoint` | 形如 `limit_up_board.plugin:LimitUpBoardPlugin`（短包名） | 不变 | **否** |
| migration SQL 路径 | yaml 内相对 `migrations/<file>.sql` | 不变 | **否** |
| 框架代码引用插件目录 | grep 实证：`deeptrade/core/notifier.py:10` 仅在 docstring 注释中 mention，无实际 import；其他框架代码无任何 `strategies_builtin` / `channels_builtin` 硬引用 | 不变（注释可保留或顺手删除） | **否** |

**唯一需要跟随迁移的代码**：`tests/strategies_builtin/`（8 个测试文件），它们用 `from deeptrade.strategies_builtin.<plugin>.<plugin>.<module> import ...` 这种**绝对 import**形式访问插件实现，仅在"插件作为框架包子目录"时能工作。

处理方式：

- 这批测试**跟随插件代码一起迁移**到新仓库 `DeepTradePluginOfficial/<plugin_id>/tests/`
- import 路径改为相对（`from <plugin_id>.<module> import ...`），通过新仓库 CI 跑（`pip install deeptrade` 装框架 + `pip install -e ./<plugin_id>` 装插件 dev 模式，或 `PYTHONPATH=<plugin_id>` 直接运行 pytest）
- **主仓库的 `tests/strategies_builtin/` 目录在新仓库 CI 全 green 后才删除**

### 8.3 `pyproject.toml` 检查

需要改的字段（在 PR-1 一并完成）：

- `[project] name = "deeptrade"` → `name = "deeptrade-quant"`（distribution name；PyPI 上 `deeptrade` 已被占用）
- `[project] version`：PR-1 用 `0.0.2` 测发链路，PR-7 升到 `0.1.0`
- `[project.urls]`：`Documentation` / `Repository` 改为 `https://github.com/ty19880929/deeptrade`
- `[project] authors = [{ name = "DeepTrade" }]` → 视情况改为真实联系人

**不需要改的字段**（保持稳定）：

- `[project.scripts] deeptrade = "deeptrade.cli:app"` —— CLI 命令名不变
- `[tool.hatch.build.targets.wheel] packages = ["deeptrade"]` —— Python 包目录名不变

**必须删除的字段**（PR-1 实施时已踩坑、已修复，记此为戒）：

```toml
# 必须删除：
[tool.hatch.build.targets.wheel.force-include]
"deeptrade/core/migrations" = "deeptrade/core/migrations"
```

**为什么是坑**：`hatchling` 与 `setuptools` 的默认行为不同 ——

- `setuptools`：`packages` 默认只收 `.py` 文件，非 Python 资源（`.sql`、`.yaml`）需通过 `package_data` 或 `MANIFEST.in` 显式声明
- `hatchling`：`packages = ["deeptrade"]` **默认收 `deeptrade/` 下所有文件**（不区分扩展名），`.sql` / `.yaml` / `.md` 都自动进 wheel

因此原 `force-include` 段把同样路径**第二次**写入 zip，触发 `UserWarning: Duplicate name: '...'`。本地 build 仅是 warning，但 GitHub Actions runner 会因此把 release.yml 标为失败、阻止 PyPI publish。**PR-1 v0.0.2 tag 因此发布失败，v0.0.3 tag 删除该段后才发布成功**。

**经验**：插件迁移到新仓库（`DeepTradePluginOfficial`）后，**禁止**再为各插件子目录写类似 `force-include`；hatchling 会自动收所有非 `.py` 资源。

**用户体验**：`pipx install deeptrade-quant` 安装后，命令仍是 `deeptrade ...`、Python 中 `import deeptrade` 仍可用。这与 `pip install scikit-learn` → `import sklearn` 是同种关系。

### 8.4 三段式迁移流程（保护现有代码）

```
阶段 A: 主仓库归档（不动任何文件）
  ↓
  - 在主仓库当前 HEAD 打 archive tag: `archive/with-builtin-plugins-v0.0.x`
  - push 到 GitHub，作为"含内置插件的最后一个快照"，永久可回溯
  - 此阶段对工作树零修改

阶段 B: 新仓库建立 + 内容复制（主仓库仍然完整）
  ↓
  - 创建 github.com/ty19880929/DeepTradePluginOfficial 空仓库
  - 用 git filter-repo（保留 history）或 cp -r（不保留 history）把
    deeptrade/strategies_builtin/limit_up_board/        → DeepTradePluginOfficial/limit_up_board/
    deeptrade/strategies_builtin/volume_anomaly/        → DeepTradePluginOfficial/volume_anomaly/
    deeptrade/channels_builtin/stdout/                  → DeepTradePluginOfficial/stdout/
    tests/strategies_builtin/limit_up_board/            → DeepTradePluginOfficial/limit_up_board/tests/
    tests/strategies_builtin/volume_anomaly/            → DeepTradePluginOfficial/volume_anomaly/tests/
  - 调整测试 import 路径（绝对 → 相对）
  - 写 registry/index.json、CI workflows
  - 为每个插件打首个 tag（如 limit-up-board/v0.4.0），触发 plugin-release workflow
  - 此阶段主仓库零修改

阶段 C: 主仓库瘦身（cutover）
  ↓
  - 仅当 §8.5 检查清单全部 ✓ 才执行
  - 提一个独立 PR，git rm 主仓库的 builtin 目录 + tests/strategies_builtin/
  - 同步更新 README / docs / quick-start.md 中的命令示例（路径 → 短名）
  - PR merge 后再发布框架新版（如 v0.1.0），用户从 PyPI 升级即获得"瘦身后框架"
```

阶段 A、B 期间**主仓库可继续正常使用**：用户依然可以 `deeptrade plugin install ./deeptrade/strategies_builtin/limit_up_board -y` 装内置插件，所有现有 e2e 流程不受影响。

### 8.5 Cutover 检查清单（阶段 C 触发前必须全部 ✓）

- [ ] 新仓库 `DeepTradePluginOfficial` 已建立、可访问
- [ ] 三个内置插件的代码已完整迁移到新仓库（含 `deeptrade_plugin.yaml`、`migrations/*.sql`、Python 包、`__init__.py`）
- [ ] 每个插件至少有一个 release tag：`limit-up-board/v0.4.0`、`volume-anomaly/v<X>`、`stdout/v<X>`
- [ ] `registry/index.json` 已 push 到 main，能通过 `https://raw.githubusercontent.com/ty19880929/DeepTradePluginOfficial/main/registry/index.json` 拉取
- [ ] 框架已发布带 source resolver 的新版到 PyPI（PR-1 + PR-4 + PR-5 已合并并发版）
- [ ] **三向对比测试**：在干净环境下分别用以下两种方式装同一个插件，最终 `~/.deeptrade/plugins/installed/<id>/<version>/` 内容**字节级一致**（用 `diff -r` 验证）：
  - `deeptrade plugin install ./deeptrade/strategies_builtin/limit_up_board -y`（旧路径，主仓库现状）
  - `deeptrade plugin install limit-up-board`（新路径，注册表）
- [ ] 数据库结构对比：用上述两种方式各装一次后，各 `lub_*` / `va_*` / `stdout` 表的 schema 完全一致（`PRAGMA table_info(<table>)` 比对）
- [ ] 新仓库的迁移测试（原 `tests/strategies_builtin/*`）在新仓库 CI 全部 green
- [ ] 至少一名人工 reviewer 在干净测试机上跑通 `deeptrade init` → `plugin install limit-up-board` → 完整 `limit-up-board run` 端到端流程
- [ ] README / quick-start / plugin-development 文档示例已同步更新草稿（待阶段 C PR 中合入）

任意一项未 ✓，cutover PR **不允许合并**。

### 8.6 回滚预案

| 故障情景 | 回滚动作 |
|---------|---------|
| 阶段 C 合并后发现框架新版有问题 | `git revert` 瘦身 PR + PyPI yank 新版本 + 用户 `pipx install deeptrade==<上一稳定版>` 回退 |
| 用户用 `plugin install limit-up-board` 装到的"新版"插件出 bug | 用户可立即 `deeptrade plugin uninstall limit-up-board --purge` + `git clone` archive tag + `deeptrade plugin install <archive-checkout>/deeptrade/strategies_builtin/limit_up_board -y` 装回老版 |
| 注册表 JSON 损坏 / 拉取失败 | CLI 已设计为：本地 ETag 缓存命中即用旧版索引；本地路径安装与 URL 直装通道完全独立，不受影响 |
| 新仓库被误删 / GitHub 故障 | archive tag 在主仓库永久保留；本地路径安装通道始终可用 |

### 8.7 阶段 A 之前的状态收尾

当前 git status 显示 builtin 目录有未提交改动（migration 文件被合并成 `20260509_001_init.sql`）。阶段 A 之前：

- **先**把这批改动以"plugin 自身的最后一个版本"形式 commit 到主仓库（这也是阶段 A 打 archive tag 时 HEAD 包含的内容）
- 阶段 B 在新仓库的 README 注明"代码迁移自 deeptrade 主仓库 commit `<sha>`"，保留可追溯性

---

## 9. CI/CD 设计

### 9.1 框架仓库

#### `.github/workflows/ci.yml`（每 PR 跑）

```yaml
name: CI
on:
  pull_request:
  push: { branches: [main] }
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install -e '.[dev]'
      - run: ruff check .
      - run: mypy deeptrade
      - run: pytest
      - run: pip install build && python -m build  # 验证可打包
```

#### `.github/workflows/release.yml`（tag 触发）

```yaml
name: Release
on:
  push:
    tags: ['v*']
permissions:
  id-token: write   # OIDC，避免管理 PyPI token
  contents: write   # 创建 release
jobs:
  build-and-publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install build
      - run: python -m build           # 同时产出 sdist + wheel
      - run: pip install dist/*.whl && deeptrade --version
      - uses: pypa/gh-action-pypi-publish@release/v1
        # 依赖 PyPI Trusted Publishers (OIDC)，无需 secret
      - uses: softprops/action-gh-release@v2
        with: { files: dist/* }
```

#### 一次性配置

在 PyPI 上把 `deeptrade` 项目配置为 Trusted Publisher，绑定 GitHub repo + workflow 名称，免去 token 维护。

#### 版本号管理

`pyproject.toml` 的 `project.version` 与 git tag 对齐。建议手动改 `pyproject.toml` → commit → 打 tag → push tag，由 release workflow 接管发布。

### 9.2 官方插件仓库

#### `.github/workflows/registry-check.yml`（PR check）

```yaml
name: Registry Check
on: [pull_request]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install pyyaml jsonschema
      - run: python tools/check_registry.py
```

`tools/check_registry.py` 校验项：

- `registry/index.json` 符合 schema
- 每个条目的 `subdir` 目录存在
- 每个 `subdir/deeptrade_plugin.yaml` 的 `plugin_id` 与索引 key 一致
- 每个条目的 `name` / `type` 与 `deeptrade_plugin.yaml` 一致
- migration checksum 与文件实际内容一致（复用框架的 `_verify_migration_checksum` 逻辑，作为脚本依赖之一）

#### `.github/workflows/plugin-release.yml`（push tag 触发）

```yaml
name: Plugin Release
on:
  push:
    tags: ['*/v*']
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - id: parse
        run: |
          TAG="${GITHUB_REF_NAME}"
          PLUGIN_ID="${TAG%/v*}"
          VERSION="${TAG#*/v}"
          echo "plugin_id=$PLUGIN_ID" >> $GITHUB_OUTPUT
          echo "version=$VERSION" >> $GITHUB_OUTPUT
      - run: |
          # 校验 subdir/deeptrade_plugin.yaml 的 plugin_id/version 与 tag 一致
          python tools/check_release.py "${{ steps.parse.outputs.plugin_id }}" "${{ steps.parse.outputs.version }}"
      - uses: softprops/action-gh-release@v2
        with:
          name: ${{ steps.parse.outputs.plugin_id }} v${{ steps.parse.outputs.version }}
          # 不上传文件 — CLI 直接从 source tarball 拉取
```

#### Tag 命名约定

`<plugin-id>/v<X.Y.Z>`，例如：

- `limit-up-board/v0.4.0`
- `volume-anomaly/v0.6.0`
- `stdout/v0.1.0`

CLI 通过注册表的 `tag_prefix`（如 `limit-up-board/`）筛选属于该插件的所有 release tag，剥离前缀后按 SemVer 排序取最高。

---

## 10. 实施顺序

按 §8 的"迁移 ≠ 删除"原则，把改动拆为 7 个 PR + 1 个 cutover PR，合并顺序严格自上而下：

| # | PR | 仓库 | 内容 | 风险 |
|---|----|------|------|------|
| 1 | 框架 release workflow + PyPI Trusted Publisher | deeptrade | `release.yml` + `ci.yml`，先打一个 patch tag（如 `v0.0.2`）跑通 PyPI 发布链路；项目元数据（urls / author email / LICENSE）补齐 | 低 |
| 2 | **阶段 A**：归档当前主仓库状态 | deeptrade | 提交 git status 中的 builtin 改动（migration 合并），打 `archive/with-builtin-plugins-v0.0.x` tag 并 push | 极低，只 commit + tag |
| 3 | **阶段 B-1**：建立 `DeepTradePluginOfficial` 仓库 + 代码迁移 | new repo | 复制 `strategies_builtin/*`、`channels_builtin/*`、`tests/strategies_builtin/*` 到新仓库 monorepo 布局；调整 `tests/*` 的 import 路径为相对；删除外层冗余 `__init__.py`（让 `<plugin_id>/` 不是 Python 包，与运行时 import 模式一致）；写 `registry/index.json`（`min_framework_version` 直接用 `0.1.0`，与 §13 一致） | 低 — 主仓库零修改 |
| 4 | **阶段 B-2**：官方插件仓库的 CI | DeepTradePluginOfficial | `registry-check.yml` + `plugin-release.yml`；为三个插件打首个 tag（`limit-up-board/v0.4.0` 等），触发 release | 低 |
| 5 | 注册表客户端 + tarball fetch + source resolver | deeptrade | `registry.py` / `github_fetch.py` / `plugin_source.py` + 单元测试（mock urllib，**不打实际 GitHub**） | 低 — 全是新增模块，零现有功能改动 |
| 6 | CLI install/upgrade 改造 + `plugin search` | deeptrade | `cmd_install` / `cmd_upgrade` 改签名（`Path` → `str`）、加 search、版本比较、禁止降级、`UpgradeNoop`；保留所有原有错误信息文案 | 中 — 用户面，需通过完整 CLI 集成测试 |
| 7 | 框架发版（含新 CLI 能力，但 builtin 目录仍在） | deeptrade | 把 PR-5 + PR-6 合并后的 deeptrade 发到 PyPI 作为 `v0.1.0-rc1`（或正式 v0.1.0）；此时同时支持"短名"和"本地 builtin 路径"两种安装通道 | 中 |
| 8 | **阶段 C / Cutover**：主仓库 builtin 目录瘦身 | deeptrade | 严格执行 §8.5 检查清单全部 ✓ 后才合并；`git rm` 主仓库的 `strategies_builtin/`、`channels_builtin/`、`tests/strategies_builtin/`；同步更新 README / quick-start / plugin-development / DESIGN.md 中的命令示例（`./deeptrade/strategies_builtin/X` → `X`） | **高** — 唯一可能影响用户的步骤 |

**依赖关系**：

- PR-1 → PR-2 → PR-3 → PR-4 链式串行（建立基础设施 + 归档 + 新仓库）
- PR-5 不依赖 PR-2/3/4（纯新增模块），可与 PR-3/PR-4 并行
- PR-6 依赖 PR-5
- PR-7 依赖 PR-6 合并到 deeptrade main
- PR-8 依赖 PR-7 已发布且 §8.5 全部 ✓

**强制顺序**：PR-1 → PR-2 → PR-3 → PR-4 → PR-5 → PR-6 → PR-7 → **(暂停 + 跑 §8.5)** → PR-8。

**为什么 PR-8 单独成 PR**：

- PR-1 ~ PR-7 都是"新增能力"，不破坏任何现有路径，零回归风险
- PR-8 是唯一的 "destructive change"，必须独立、可被 `git revert` 单步回滚，且只在所有 §8.5 项目验证通过后才动手
- 这样一来即便 PR-8 发现遗漏，回滚成本最小

---

## 11. 刻意保留的小决定（非目标）

为了避免过度设计，以下能力**当前不做**：

| 能力 | 不做的原因 | 未来扩展点 |
|------|------------|-----------|
| 插件签名（cosign 等） | 现有 migration checksum + GitHub release 的 commit hash 已是事实上的不可篡改 | 注册表条目可加 `signing_key` 字段 |
| 插件自带 Python 依赖（extras / requirements） | 决策已锁定"插件只能用框架已有依赖" | 若放开，`deeptrade_plugin.yaml` 加 `requirements:` 字段 + install 时跑 pip |
| 私有 GitHub 仓库 | 当前用户群无明确需求 | `github_fetch` 加 `Authorization: Bearer $GITHUB_TOKEN` |
| URL 直装支持 monorepo subdir | 简化判定，第三方 monorepo 自行拆仓库 | `--subdir` 选项 |
| Migration 回滚 / 安全降级 | 工程量大，当前规模不值得 | `deeptrade_plugin.yaml` 加 `down_migrations` 字段 |
| 注册表 TTL 过期策略 | ETag 304 已经足够轻量 | 加 `--max-age` 参数 |
| 单文件二进制（PyInstaller） | 决策已锁定 pipx/uv 路线 | 独立 issue |
| Docker 镜像分发 | 交互式 CLI + 本地 DB 与 docker 体验割裂 | 独立 issue |

---

## 12. 用户视角的端到端流程

### 12.1 全新用户首次使用

```bash
# 1. 安装框架（PyPI 项目名是 deeptrade-quant，命令仍叫 deeptrade）
pipx install deeptrade-quant
# 或
uv tool install deeptrade-quant

# 2. 初始化（创建 ~/.deeptrade/，建库）
deeptrade init

# 3. 看看有哪些官方插件
deeptrade plugin search

# 4. 安装感兴趣的策略
deeptrade plugin install limit-up-board
deeptrade plugin install stdout

# 5. 配置 LLM / Tushare
deeptrade config llm
deeptrade config tushare

# 6. 跑策略
deeptrade limit-up-board run --date 2026-05-09
```

### 12.2 升级流程

```bash
# 框架升级
pipx upgrade deeptrade-quant
# 或
uv tool upgrade deeptrade-quant

# 插件升级（短名）
deeptrade plugin upgrade limit-up-board
# 输出：✔ upgraded: limit-up-board → v0.5.0
# 或：已是最新版本 v0.4.0

# 插件升级（本地开发版）
deeptrade plugin upgrade ./limit-up-board-dev
# 等版本：已是最新版本 v0.4.0  (exit 0)
# 高版本：✔ upgraded: limit-up-board → v0.5.0
# 低版本：✘ 待装版本 0.3.0 低于已装 0.4.0；如需降级，请先 uninstall --purge  (exit 2)
```

### 12.3 第三方插件安装

```bash
# 仓库根即插件目录的情况
deeptrade plugin install https://github.com/some-user/my-deeptrade-plugin

# 第三方 monorepo：必须本地 clone + 指定子目录
git clone https://github.com/some-user/their-mono
deeptrade plugin install ./their-mono/plugins/foo
```

---

## 13. 已确认事项（2026-05-09 锁定）

| 项 | 确认值 |
|----|--------|
| GitHub 用户名 | `ty19880929` |
| 框架仓库 | `github.com/ty19880929/deeptrade` |
| 官方插件仓库 | `github.com/ty19880929/DeepTradePluginOfficial`（已创建空仓库） |
| 本地插件仓库工作树 | `E:\personal\DeepTradePluginOfficial`（已创建空目录） |
| PyPI 账号 | `brainty` |
| PyPI 项目名（distribution name） | `deeptrade-quant`（PyPI 上 `deeptrade` 已被占用） |
| Python 包名（import name） | `deeptrade`（保持不变） |
| CLI 命令名 | `deeptrade`（保持不变） |
| Trusted Publisher | 已配置（绑定 `github.com/ty19880929/deeptrade` 仓库 + `release.yml`） |
| 注册表 `min_framework_version` 起始值 | `0.1.0`（首次插件 release 与框架 v0.1.0 同步发布） |
| PR-1 发布渠道 | **直接 PyPI**（不走 TestPyPI 中转） |
| 框架版本起点 | 当前 `pyproject.toml` `version` 为 `0.0.1`；PR-1 测发用 `0.0.2`，PR-7 正式新功能版用 `0.1.0` |
| 阶段 B 复制方式 | 简单 `cp -r`（不保留 git history） |
| 不做：不依赖 Python 的分发 | 已锁定 pipx / uv tool install 路线 |

## 14. 实施开工前的最后确认清单

- [x] 用户审阅本文档，对 §8 的"迁移而非删除"流程、§8.5 cutover 检查清单、§10 PR 顺序无异议
- [x] 在 PyPI 注册账号 `brainty`、配置 Trusted Publisher（绑定 `github.com/ty19880929/deeptrade` 仓库 + `release.yml` workflow 名）
- [x] 创建空仓库 `github.com/ty19880929/DeepTradePluginOfficial`
- [x] 创建本地空工作树 `E:\personal\DeepTradePluginOfficial`
- [x] 阶段 B 复制方式选定：简单 `cp -r`，不保留 history

**所有前置条件齐备，可从 PR-1 开工。**
