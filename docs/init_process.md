● 在干净环境下第一次运行 DeepTrade — 数据库自动建表过程

  简短答案：不会"隐式"自动建表。框架要求用户显式跑一次 deeptrade init，这个命令是唯一负责执行核心
  schema migrations 的入口；而插件业务表（如 lub_*）只有在 deeptrade plugin install 时才会被建。

  下面按调用顺序拆解。

  1. 入口：deeptrade init

  deeptrade/cli.py:173-196 定义 init 命令：

  1. paths.ensure_layout() —— 创建
  ~/.deeptrade/{logs,reports,plugins/installed,plugins/cache}（paths.py:46-49）。DEEPTRADE_HOME
  环境变量可以改根目录。
  2. 计算 db_file = paths.db_path()（默认 ~/.deeptrade/deeptrade.duckdb，DEEPTRADE_DB_PATH
  可覆盖）。记录 fresh = not db_file.exists()，用于决定是否打印 "Database created"。
  3. db = Database(db_file)：
    - Database.__init__ (db.py:32-40) 只做 db_file.parent.mkdir(parents=True, exist_ok=True) +
  duckdb.connect(str(self._path))。duckdb.connect 自身会在路径不存在时创建一个空的 DuckDB
  文件，但它绝不会执行任何 DDL。
  4. applied = apply_core_migrations(db) —— 真正建表的地方。
  5. 关闭连接，可选地走 questionary 引导用户配置 tushare / LLM。

  2. apply_core_migrations 的逻辑（db.py:144-180）

  1. 发现迁移文件：_list_core_migrations() 用
  importlib.resources.files("deeptrade.core.migrations.core") 遍历包资源目录。这层抽象保证 wheel
  安装与源码运行一视同仁。
  2. 筛选 + 排序：文件名必须匹配 ^(\d{8}_\d{3,})_.+\.sql$（如 20260427_001_init.sql），按 version
  字符串字典序排序。
  3. 判断已应用版本：_applied_versions(db) 先查 information_schema.tables 看 schema_migrations
  表是否存在；不存在（首次运行的情况）就返回空集，短路避免对不存在的表执行 SELECT。
  4. 逐个应用未应用的迁移：每个迁移在 db.transaction()（即 BEGIN … COMMIT）里：
    - db.execute(sql_text) 执行整段 SQL（DuckDB 支持多语句字符串）。
    - INSERT INTO schema_migrations(version) VALUES (?) 记录版本号。
    - 失败则 ROLLBACK，部分应用不会污染状态。
  5. 数据迁移（非 DDL）：核心 SQL 跑完后再调用 migrate_legacy_deepseek_keys /
  migrate_legacy_deepseek_profile_key / migrate_llm_default_provider 三个幂等函数，把 v0.5/v0.6/v0.7
  的旧 config 键名搬到新位置。这些数据迁移不写
  schema_migrations，依靠"读当前状态判断是否要做"自身保证幂等。

  3. 第一次跑会创建的核心表

  干净环境下两个 SQL 都会被应用：

  20260427_001_init.sql —— 全是 CREATE TABLE IF NOT EXISTS：
  - app_config、secret_store —— 框架配置 / 加密机密
  - schema_migrations —— 框架自身的 migration 簿记表（注意：先建表，再 INSERT 自己这条记录，靠 IF NOT
  EXISTS 让"启动那一刻还没这张表"也能 work）
  - plugins、plugin_tables、plugin_schema_migrations —— 插件注册表
  - llm_calls、tushare_sync_state、tushare_calls —— 框架级审计 / 同步状态

  20260501_002_drop_llm_calls_stage.sql —— ALTER TABLE llm_calls DROP COLUMN IF EXISTS
  stage。在干净环境上是 no-op（v0.7 起 llm_calls 已经不带 stage），但仍会写入
  schema_migrations，保证后面再来一次不会重跑。

  init.sql 第 1-7 行的注释强调了核心边界：框架只拥有这些表；任何业务数据表（stock_basic、daily
  等）一律由插件用自己的 migrations/*.sql 声明。

  4. 插件表如何创建（独立流程）

  业务表只在 deeptrade plugin install <path> 时进入数据库。流程位于 core/plugin_manager.py：

  1. _load_metadata_yaml(source/deeptrade_plugin.yaml) —— Pydantic 校验。
  2. 校验 plugin_id 不在 RESERVED_PLUGIN_IDS = {"init","config","plugin","data"}，api_version ==
  "1"，permissions.llm_tools is False，plugin_id 全局唯一。
  3. 对每个 migrations[*] 跑 _verify_migration_checksum：读文件 → 计算 sha256 → 跟 yaml 里的 checksum
  比对，不一致直接报错，避免插件作者改 SQL 但忘了更新清单。
  4. shutil.copytree(source_path, ~/.deeptrade/plugins/installed/<plugin_id>/<version>/)。
  5. 打开单个事务，按顺序：
    - _apply_migrations —— 用 _iter_statements 把每个 SQL 文件按顶层 ; 切（识别引号和 --
  行注释），逐条执行。
    - _record_plugin → INSERT INTO plugins
    - _record_tables → plugin_tables（按 yaml 的 tables: 列表）
    - _record_migrations → plugin_schema_migrations（带 checksum）
    - _missing_declared_tables —— 查 information_schema.tables 验证 yaml tables:
  中每个声明的表都真的被 SQL 创建出来了；否则抛 PluginInstallError 触发整个事务回滚。
  6. 事务外再调用 validate_static(ctx)（无网络的静态自检）。失败则 _rollback_install 倒序删表 +
  删注册表行 + 删 install 目录。

  例如 deeptrade/strategies_builtin/limit_up_board/deeptrade_plugin.yaml 列了 7 个迁移和 14 张 lub_*
  表 —— 这些在 deeptrade init 阶段完全不会出现，必须用户主动 deeptrade plugin install
  deeptrade/strategies_builtin/limit_up_board 后才落地。

  5. 一些容易踩坑的点

  - 跳过 init 直接跑 deeptrade config show 之类，不会被框架自动救回：cli_config.py:_open 等只是
  Database(paths.db_path())，不会调 apply_core_migrations。DuckDB 文件被建出来但里面是空的，后续访问
  app_config 会以"表不存在"报错。init 是规约里唯一的自举入口。
  - schema_migrations 自身由 init.sql 创建——之所以能在第一次 migration 之内 INSERT 自己，是因为
  _applied_versions 在表不存在时短路返回空集，先 CREATE TABLE IF NOT EXISTS schema_migrations 再
  INSERT，单事务原子化。
  - 数据迁移（v06/v07/v08）不进 schema_migrations，靠"读现状判等"幂等。所以如果你删掉 DuckDB 重跑
  init，它们只是 no-op。
  - 迁移文件版本号的有效形态由 _MIGRATION_FILENAME_RE = ^(\d{8}_\d{3,})_.+\.sql$ 决定；想新增就按
  YYYYMMDD_NNN_xxx.sql 命名。