"""Render & export the volume-anomaly final reports.

Three report flavours, one per mode:
    screen   → screen_summary.md + screen_hits.json + screen_stats.json
    analyze  → analyze_summary.md + analyze_predictions.json + data_snapshot.json
    prune    → prune_summary.md + pruned_codes.json

All include the『已追踪时长』column when relevant.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from deeptrade.core import paths
from deeptrade.core.run_status import RunStatus

from .data import AnalyzeBundle, ScreenDiagnostics, ScreenResult, ScreenRules
from .schemas import VATrendCandidate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------


def render_banners(
    *,
    status: RunStatus,
    is_intraday: bool,
    failed_batch_ids: list[str] | None = None,
) -> str:
    parts: list[str] = []
    if status in {RunStatus.PARTIAL_FAILED, RunStatus.FAILED, RunStatus.CANCELLED}:
        marker = {
            RunStatus.PARTIAL_FAILED: "🚨 **PARTIAL — 本次结果不完整，不可作为有效筛选结果**",
            RunStatus.FAILED: "🚨 **FAILED — 运行失败**",
            RunStatus.CANCELLED: "⏹ **CANCELLED — 用户中断**",
        }[status]
        parts.append(f"> {marker}")
        if status == RunStatus.PARTIAL_FAILED and failed_batch_ids:
            parts.append(f"> 失败批次：`{', '.join(failed_batch_ids)}`（详见 `llm_calls.jsonl`）")
    if is_intraday:
        parts.append("> ⚠ **INTRADAY MODE** — 数据可能不完整，仅供盘中观察，不可与日终结果混用")
    return "\n".join(parts) + ("\n\n" if parts else "")


# ---------------------------------------------------------------------------
# SCREEN report
# ---------------------------------------------------------------------------


def write_screen_report(
    run_id: str,
    *,
    status: RunStatus,
    is_intraday: bool,
    result: ScreenResult,
    n_new: int,
    n_updated: int,
    watchlist_total: int,
    reports_root: Path | None = None,
) -> Path:
    root = (reports_root or paths.reports_dir()) / str(run_id)
    root.mkdir(parents=True, exist_ok=True)

    rules = result.rules
    md = [render_banners(status=status, is_intraday=is_intraday)]
    md.append("# 成交量异动策略 — 异动筛选\n")
    md.append(
        f"- mode: **screen**\n"
        f"- trade_date: **{result.trade_date}**\n"
        f"- status: `{status.value}`\n"
        f"- intraday: `{is_intraday}`\n"
    )
    md.append(_render_rules_md(rules))
    md.append(
        "\n## 筛选漏斗\n"
        f"- 主板池: **{result.n_main_board}**\n"
        f"- 排除 ST/停牌后: **{result.n_after_st_susp}**\n"
        f"- 满足『阳线 + 实体≥{rules.body_ratio_min} + 涨幅"
        f"{rules.pct_chg_min}-{rules.pct_chg_max}%』: **{result.n_after_t_day_rules}**\n"
        f"- 满足『换手率{rules.turnover_min}-{rules.turnover_max}%』: "
        f"**{result.n_after_turnover}**\n"
        f"- 满足『({rules.vol_max_short_window}日最大量 OR "
        f"{rules.lookback_trade_days}日top{rules.vol_top_n_long}) + "
        f"{int(rules.vol_ratio_5d_min)}日量比≥{rules.vol_ratio_5d_min}』"
        f"(最终命中): **{result.n_after_vol_rules}**\n"
    )
    md.append(
        "\n## 待追踪标的池写入\n"
        f"- 新增: **{n_new}**\n"
        f"- 已存在更新: **{n_updated}**\n"
        f"- 当前池总数: **{watchlist_total}**\n"
    )

    md.append(_render_diagnostics_md(result.diagnostics, rules))

    if result.data_unavailable:
        md.append("\n## 数据缺失/降级警示 (data_unavailable)\n")
        for entry in result.data_unavailable:
            md.append(f"- {entry}\n")

    md.append(f"\n## 本次命中明细 ({len(result.hits)} 只)\n")
    if result.hits:
        md.append(
            "| Code | Name | Industry | Pct% | Body | Turn% | VolRatio5d | "
            f"VolRank/{rules.lookback_trade_days}d | ShortMax | LongMax |\n"
        )
        md.append(
            "|------|------|----------|------|------|-------|-----------|"
            "------------|----------|---------|\n"
        )
        for h in result.hits:
            md.append(
                f"| `{h['ts_code']}` | {h.get('name') or '—'} | {h.get('industry') or '—'} | "
                f"{h.get('pct_chg')} | {h.get('body_ratio')} | {h.get('turnover_rate')} | "
                f"{h.get('vol_ratio_5d')} | {h.get('vol_rank_in_long_window', '—')} | "
                f"{h.get('max_vol_short_window', '—')} | {h.get('max_vol_long_window', '—')} |\n"
            )
    else:
        md.append("_(本次无命中)_\n")

    # P0 H2 — surface candidates that were excluded from vol rule due to insufficient history
    insuff = result.diagnostics.insufficient_history
    if insuff:
        md.append(
            f"\n## 因历史不足被排除的候选 ({len(insuff)} 只)\n"
            f"_这些标的通过了换手率筛选，但历史交易日数不足 "
            f"`min_history_coverage = {rules.min_history_coverage:.0%}`× "
            f"`lookback = {rules.lookback_trade_days}` = "
            f"{result.diagnostics.history_min_required_days} 天，无法可靠评估 vol 规则。_\n\n"
        )
        md.append("| Code | Name | Available | Required | Reason |\n")
        md.append("|------|------|-----------|----------|--------|\n")
        for r in insuff:
            md.append(
                f"| `{r['ts_code']}` | {r.get('name') or '—'} | "
                f"{r.get('available_days', '?')} | {r.get('required_days', '?')} | "
                f"{r.get('reason', '<lookback × min_coverage')} |\n"
            )

    md.append("\n---\n*免责声明：本报告仅用于策略研究，不构成投资建议。*\n")
    (root / "summary.md").write_text("".join(md), encoding="utf-8")

    (root / "screen_hits.json").write_text(
        json.dumps(result.hits, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (root / "screen_stats.json").write_text(
        json.dumps(
            {
                "trade_date": result.trade_date,
                "rules": result.rules.as_dict(),
                "diagnostics": _diagnostics_to_dict(result.diagnostics),
                "n_main_board": result.n_main_board,
                "n_after_st_susp": result.n_after_st_susp,
                "n_after_t_day_rules": result.n_after_t_day_rules,
                "n_after_turnover": result.n_after_turnover,
                "n_after_vol_rules": result.n_after_vol_rules,
                "n_new": n_new,
                "n_updated": n_updated,
                "watchlist_total": watchlist_total,
                "data_unavailable": result.data_unavailable,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (root / "llm_calls.jsonl").touch(exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# ANALYZE report
# ---------------------------------------------------------------------------


def write_analyze_report(
    run_id: str,
    *,
    status: RunStatus,
    is_intraday: bool,
    bundle: AnalyzeBundle,
    predictions: list[VATrendCandidate],
    market_context_summary: str | None,
    risk_disclaimer: str | None,
    failed_batch_ids: list[str] | None = None,
    reports_root: Path | None = None,
) -> Path:
    root = (reports_root or paths.reports_dir()) / str(run_id)
    root.mkdir(parents=True, exist_ok=True)

    # tracked_days lookup from candidates (keyed by candidate_id)
    tracked_days_lookup: dict[str, int] = {
        c["candidate_id"]: int(c.get("tracked_days") or 0)
        for c in bundle.candidates
        if isinstance(c, dict)
    }

    md = [render_banners(status=status, is_intraday=is_intraday, failed_batch_ids=failed_batch_ids)]
    md.append("# 成交量异动策略 — 走势分析\n")
    md.append(
        f"- mode: **analyze**\n"
        f"- trade_date: **{bundle.trade_date}**\n"
        f"- next_trade_date: **{bundle.next_trade_date}**\n"
        f"- status: `{status.value}`\n"
        f"- intraday: `{is_intraday}`\n"
        f"- 待追踪池规模: **{len(bundle.candidates)}**\n"
        f"- LLM 输出预测数: **{len(predictions)}**\n"
    )
    md.append(
        f"\n*sector_strength_source*: `{bundle.sector_strength_source}`  "
        f"_(可信度：limit_cpt_list > industry_fallback)_\n"
    )
    if bundle.data_unavailable:
        md.append(f"\n*data_unavailable*: `{bundle.data_unavailable}`\n")
    if market_context_summary:
        md.append(f"\n**市场背景**: {market_context_summary}\n")

    by_pred: dict[str, list[VATrendCandidate]] = {
        "imminent_launch": [],
        "watching": [],
        "not_yet": [],
    }
    for p in predictions:
        by_pred.setdefault(p.prediction, []).append(p)
    for k in by_pred:
        by_pred[k].sort(key=lambda c: c.rank)

    md.append(f"\n## 即将启动 · imminent_launch ({len(by_pred['imminent_launch'])} 只)\n")
    if by_pred["imminent_launch"]:
        md.append("| # | Code | Name | 已追踪 | Score | Pattern | 洗盘 | Conf | Rationale |\n")
        md.append("|---|------|------|-------|-------|---------|------|------|-----------|\n")
        for c in by_pred["imminent_launch"]:
            td = tracked_days_lookup.get(c.candidate_id, 0)
            md.append(
                f"| {c.rank} | `{c.ts_code}` | {c.name} | {td}日 | "
                f"{c.launch_score:.1f} | {c.pattern} | {c.washout_quality} | "
                f"{c.confidence} | {c.rationale} |\n"
            )
    else:
        md.append("_(本轮无即将启动标的)_\n")

    md.append(f"\n## 持续观察 · watching ({len(by_pred['watching'])} 只)\n")
    if by_pred["watching"]:
        md.append("| # | Code | Name | 已追踪 | Score | Pattern | 洗盘 | Conf |\n")
        md.append("|---|------|------|-------|-------|---------|------|------|\n")
        for c in by_pred["watching"]:
            td = tracked_days_lookup.get(c.candidate_id, 0)
            md.append(
                f"| {c.rank} | `{c.ts_code}` | {c.name} | {td}日 | "
                f"{c.launch_score:.1f} | {c.pattern} | {c.washout_quality} | "
                f"{c.confidence} |\n"
            )
    else:
        md.append("_(本轮无持续观察标的)_\n")

    md.append(f"\n## 时机未到 · not_yet ({len(by_pred['not_yet'])} 只)\n")
    if by_pred["not_yet"]:
        md.append("| Code | Name | 已追踪 | Reason |\n")
        md.append("|------|------|-------|--------|\n")
        for c in by_pred["not_yet"]:
            td = tracked_days_lookup.get(c.candidate_id, 0)
            md.append(
                f"| `{c.ts_code}` | {c.name} | {td}日 | {c.rationale[:80]} |\n"
            )
    else:
        md.append("_(无)_\n")

    if risk_disclaimer:
        md.append(f"\n**风险提示**: {risk_disclaimer}\n")
    md.append("\n---\n*免责声明：本报告仅用于策略研究，不构成投资建议。*\n")
    (root / "summary.md").write_text("".join(md), encoding="utf-8")

    # JSON outputs
    pred_json = []
    for p in predictions:
        rec = p.model_dump(mode="json")
        rec["tracked_days"] = tracked_days_lookup.get(p.candidate_id, 0)
        pred_json.append(rec)
    (root / "analyze_predictions.json").write_text(
        json.dumps(pred_json, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    snapshot: dict[str, Any] = {
        "trade_date": bundle.trade_date,
        "next_trade_date": bundle.next_trade_date,
        "status": status.value,
        "is_intraday": is_intraday,
        "candidates": bundle.candidates,
        "market_summary": bundle.market_summary,
        "sector_strength": {
            "source": bundle.sector_strength_source,
            "data": bundle.sector_strength_data,
        },
        "data_unavailable": bundle.data_unavailable,
    }
    (root / "data_snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    (root / "llm_calls.jsonl").touch(exist_ok=True)
    return root


def _json_default(o: Any) -> Any:
    if is_dataclass(o) and not isinstance(o, type):
        return asdict(o)
    return str(o)


# ---------------------------------------------------------------------------
# PRUNE report
# ---------------------------------------------------------------------------


def write_prune_report(
    run_id: str,
    *,
    status: RunStatus,
    today: str,
    min_tracked_days: int,
    pruned: list[dict[str, Any]],
    watchlist_remaining: int,
    reports_root: Path | None = None,
) -> Path:
    root = (reports_root or paths.reports_dir()) / str(run_id)
    root.mkdir(parents=True, exist_ok=True)

    md = [render_banners(status=status, is_intraday=False)]
    md.append("# 成交量异动策略 — 剔除已追踪 N 日标的\n")
    md.append(
        f"- mode: **prune**\n"
        f"- today: **{today}**\n"
        f"- 阈值: 已追踪 ≥ **{min_tracked_days}** 日历日\n"
        f"- status: `{status.value}`\n"
        f"- 剔除数量: **{len(pruned)}**\n"
        f"- 剔除后池剩余: **{watchlist_remaining}**\n"
    )
    md.append(f"\n## 被剔除标的 ({len(pruned)} 只)\n")
    if pruned:
        md.append("| Code | Name | Industry | Tracked Since | 已追踪 | Last Screened |\n")
        md.append("|------|------|----------|---------------|-------|---------------|\n")
        for r in pruned:
            md.append(
                f"| `{r['ts_code']}` | {r.get('name') or '—'} | {r.get('industry') or '—'} | "
                f"{r['tracked_since']} | {r.get('tracked_days', '?')}日 | "
                f"{r.get('last_screened') or '—'} |\n"
            )
    else:
        md.append("_(无满足阈值的标的)_\n")
    md.append("\n---\n*免责声明：本报告仅用于策略研究，不构成投资建议。*\n")
    (root / "summary.md").write_text("".join(md), encoding="utf-8")

    (root / "pruned_codes.json").write_text(
        json.dumps(pruned, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (root / "llm_calls.jsonl").touch(exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# Terminal summary (concise post-run print)
# ---------------------------------------------------------------------------


def render_terminal_summary(
    run_id: str,
    *,
    reports_root: Path | None = None,
    console: Any = None,
) -> None:
    """Print a compact summary after the run finishes.

    Auto-detects the mode by which JSON files exist in the report dir.
    """
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    from deeptrade.theme import EVA_THEME

    root = (reports_root or paths.reports_dir()) / str(run_id)
    if not root.is_dir():
        return
    if console is None:
        console = Console(theme=EVA_THEME)

    if (root / "analyze_predictions.json").is_file():
        _render_analyze_terminal(root, console, Table, Panel)
    elif (root / "screen_hits.json").is_file():
        _render_screen_terminal(root, console, Table, Panel)
    elif (root / "pruned_codes.json").is_file():
        _render_prune_terminal(root, console, Table, Panel)

    console.print(f"\n[k.label]报告目录:[/k.label] [k.value]{root}[/k.value]")
    console.print(
        f"[k.label]完整报告:[/k.label] [k.value]deeptrade strategy report {run_id}[/k.value]"
    )


def _render_screen_terminal(root: Path, console: Any, Table: Any, _Panel: Any) -> None:
    stats = _safe_load_json(root / "screen_stats.json", default={})
    hits = _safe_load_json(root / "screen_hits.json", default=[])
    console.print(
        f"[title]异动筛选[/title]  "
        f"[k.label]T=[/k.label][k.value]{stats.get('trade_date', '?')}[/k.value]  "
        f"[k.label]命中=[/k.label][k.value]{len(hits)}[/k.value]  "
        f"[k.label]新增=[/k.label][k.value]{stats.get('n_new', 0)}[/k.value]  "
        f"[k.label]池规模=[/k.label][k.value]{stats.get('watchlist_total', 0)}[/k.value]"
    )
    if not hits:
        return
    t = Table(title="本次命中", title_style="title", border_style="panel.border.ok",
              header_style="k.label")
    t.add_column("代码", style="k.value", no_wrap=True, width=11)
    t.add_column("名称", no_wrap=True, max_width=10)
    t.add_column("行业", no_wrap=True, max_width=12)
    t.add_column("涨%", justify="right", width=5)
    t.add_column("换手%", justify="right", width=6)
    t.add_column("量比5d", justify="right", width=6)
    for h in hits:
        t.add_row(
            h.get("ts_code", "?"),
            h.get("name", "?"),
            h.get("industry", "—"),
            f"{h.get('pct_chg', 0):.2f}",
            f"{h.get('turnover_rate', 0):.2f}",
            f"{h.get('vol_ratio_5d', 0):.2f}",
        )
    console.print(t)


def _render_analyze_terminal(root: Path, console: Any, Table: Any, _Panel: Any) -> None:
    snap = _safe_load_json(root / "data_snapshot.json", default={})
    preds = _safe_load_json(root / "analyze_predictions.json", default=[])
    n_imminent = sum(1 for p in preds if p.get("prediction") == "imminent_launch")
    n_watch = sum(1 for p in preds if p.get("prediction") == "watching")
    console.print(
        f"[title]走势分析[/title]  "
        f"[k.label]T=[/k.label][k.value]{snap.get('trade_date', '?')}[/k.value]  "
        f"[k.label]T+1=[/k.label][k.value]{snap.get('next_trade_date', '?')}[/k.value]  "
        f"[k.label]即将启动=[/k.label][k.value]{n_imminent}[/k.value]  "
        f"[k.label]观察=[/k.label][k.value]{n_watch}[/k.value]"
    )

    imminent = sorted(
        (p for p in preds if p.get("prediction") == "imminent_launch"),
        key=lambda p: p.get("rank", 0),
    )
    if imminent:
        t = Table(
            title=f"即将启动 · {len(imminent)} 只",
            title_style="title",
            border_style="panel.border.ok",
            header_style="k.label",
            expand=True,
        )
        t.add_column("#", justify="right", width=3)
        t.add_column("代码", style="k.value", no_wrap=True, width=11)
        t.add_column("名称", no_wrap=True, max_width=10)
        t.add_column("追踪", justify="right", width=5)
        t.add_column("分", justify="right", width=4)
        t.add_column("形态", width=10)
        t.add_column("洗盘", width=8)
        t.add_column("信", width=4)
        t.add_column("理由", overflow="fold")
        for p in imminent:
            t.add_row(
                str(p.get("rank", "?")),
                p.get("ts_code", "?"),
                p.get("name", "?"),
                f"{p.get('tracked_days', 0)}日",
                f"{p.get('launch_score', 0):.0f}",
                p.get("pattern", "?"),
                p.get("washout_quality", "?"),
                _conf_short(p.get("confidence", "")),
                p.get("rationale", ""),
            )
        console.print(t)

    watching = sorted(
        (p for p in preds if p.get("prediction") == "watching"),
        key=lambda p: p.get("rank", 0),
    )
    if watching:
        t = Table(
            title=f"观察 · {len(watching)} 只",
            title_style="subtitle",
            border_style="panel.border.primary",
            header_style="k.label",
        )
        t.add_column("#", justify="right", width=3)
        t.add_column("代码", style="k.value", no_wrap=True, width=11)
        t.add_column("名称", no_wrap=True, max_width=10)
        t.add_column("追踪", justify="right", width=5)
        t.add_column("分", justify="right", width=4)
        t.add_column("形态", width=10)
        t.add_column("洗盘", width=8)
        t.add_column("信", width=4)
        for p in watching:
            t.add_row(
                str(p.get("rank", "?")),
                p.get("ts_code", "?"),
                p.get("name", "?"),
                f"{p.get('tracked_days', 0)}日",
                f"{p.get('launch_score', 0):.0f}",
                p.get("pattern", "?"),
                p.get("washout_quality", "?"),
                _conf_short(p.get("confidence", "")),
            )
        console.print(t)


def _render_prune_terminal(root: Path, console: Any, Table: Any, _Panel: Any) -> None:
    pruned = _safe_load_json(root / "pruned_codes.json", default=[])
    console.print(
        f"[title]剔除追踪标的[/title]  [k.label]剔除=[/k.label][k.value]{len(pruned)}[/k.value]"
    )
    if not pruned:
        return
    t = Table(border_style="panel.border.warn", header_style="k.label")
    t.add_column("代码", style="k.value", no_wrap=True, width=11)
    t.add_column("名称", no_wrap=True, max_width=10)
    t.add_column("入池日", no_wrap=True, width=10)
    t.add_column("已追踪", justify="right", width=7)
    for r in pruned:
        t.add_row(
            r.get("ts_code", "?"),
            r.get("name", "?"),
            r.get("tracked_since", "?"),
            f"{r.get('tracked_days', '?')}日",
        )
    console.print(t)


def _render_diagnostics_md(diag: ScreenDiagnostics, rules: ScreenRules) -> str:
    """P0 — observable data-completeness section.

    Lets the user 自证 that every screening step had complete data, or pinpoint
    where degradation occurred.
    """

    def _pct(num: int, denom: int) -> str:
        return f"{num / denom * 100:.1f}%" if denom > 0 else "n/a"

    daily_t_cov = _pct(diag.daily_t_main_board_rows, diag.main_board_rows)
    db_t_cov = _pct(diag.daily_basic_t_main_board_rows, diag.main_board_rows)
    history_status = (
        "完整"
        if diag.history_window_actual_days == diag.history_window_planned_days
        else (
            f"⚠ 缺失 {len(diag.history_window_missing_dates)} 天 "
            f"({diag.history_window_actual_days}/{diag.history_window_planned_days})"
        )
    )
    st_status_marker = "" if diag.stock_st_status == "ok" else " 🚨"
    susp_status_marker = "" if diag.suspend_d_status == "ok" else " ⚠"
    db_status_marker = "" if diag.daily_basic_status == "ok" else " 🚨"

    adj_marker = ""
    if diag.vol_adjust_enabled:
        if diag.vol_adjust_status == "ok":
            adj_marker = ""
        elif diag.vol_adjust_status.startswith("degraded"):
            adj_marker = " ⚠"
        else:
            adj_marker = " 🚨"

    out = [
        "\n## 数据完整性诊断\n",
        f"- stock_basic 行数: **{diag.stock_basic_rows}** "
        f"(主板可用: **{diag.main_board_rows}**)\n",
        f"- stock_st(T) ST 标的数: **{diag.stock_st_count}** "
        f"`{diag.stock_st_status}`{st_status_marker}\n",
        f"- suspend_d(T) 停牌标的数: **{diag.suspend_d_count}** "
        f"`{diag.suspend_d_status}`{susp_status_marker}\n",
        f"- daily(T) 全市场行数: **{diag.daily_t_total_rows}** "
        f"(主板覆盖 **{diag.daily_t_main_board_rows}/{diag.main_board_rows} = {daily_t_cov}**)\n",
        f"- daily_basic(T) 全市场行数: **{diag.daily_basic_t_total_rows}** "
        f"(主板覆盖 **{diag.daily_basic_t_main_board_rows}/{diag.main_board_rows} = "
        f"{db_t_cov}**) `{diag.daily_basic_status}`{db_status_marker}\n",
        f"  - 候选缺 turnover_rate 被静默剔除: **{diag.n_turnover_missing}** 只\n",
        f"- 历史窗口: 计划 **{diag.history_window_planned_days}** 天 / "
        f"实拉 **{diag.history_window_actual_days}** 天 → {history_status}\n",
        f"- 个股历史覆盖率门槛: ≥ **{diag.history_min_required_days}** 天 "
        f"({rules.min_history_coverage:.0%}× lookback)\n",
        f"  - 因历史不足被排除: **{len(diag.insufficient_history)}** 只\n",
        f"- vol 复权调整 (vol_adjust): "
        f"`{diag.vol_adjust_status}`{adj_marker}\n",
    ]
    if diag.vol_adjust_enabled:
        out.append(
            f"  - adj_factor 窗口: 计划 **{diag.adj_factor_planned_days}** 天 / "
            f"实拉 **{diag.adj_factor_actual_days}** 天 / "
            f"缺失 **{len(diag.adj_factor_missing_dates)}** 天\n"
        )
        out.append(
            f"  - 候选 T 日 adj_factor 缺失数: **{len(diag.adj_factor_missing_codes)}** 只 "
            f"_(这些标的退化为原始 vol)_\n"
        )
    if diag.history_window_missing_dates:
        sample = diag.history_window_missing_dates[:10]
        ellipsis = "..." if len(diag.history_window_missing_dates) > 10 else ""
        out.append(f"  - daily 缺失日期样本: `{sample}{ellipsis}`\n")
    if diag.turnover_missing_codes:
        sample = diag.turnover_missing_codes[:10]
        ellipsis = "..." if len(diag.turnover_missing_codes) > 10 else ""
        out.append(f"  - turnover 缺失样本: `{sample}{ellipsis}`\n")
    if diag.adj_factor_missing_dates:
        sample = diag.adj_factor_missing_dates[:10]
        ellipsis = "..." if len(diag.adj_factor_missing_dates) > 10 else ""
        out.append(f"  - adj_factor 缺失日期样本: `{sample}{ellipsis}`\n")
    if diag.adj_factor_missing_codes:
        sample = diag.adj_factor_missing_codes[:10]
        ellipsis = "..." if len(diag.adj_factor_missing_codes) > 10 else ""
        out.append(f"  - adj_factor(T) 缺失代码样本: `{sample}{ellipsis}`\n")
    return "".join(out)


def _diagnostics_to_dict(diag: ScreenDiagnostics) -> dict[str, Any]:
    """Serialize ScreenDiagnostics for screen_stats.json."""
    return {
        "stock_basic_rows": diag.stock_basic_rows,
        "main_board_rows": diag.main_board_rows,
        "stock_st_count": diag.stock_st_count,
        "stock_st_status": diag.stock_st_status,
        "suspend_d_count": diag.suspend_d_count,
        "suspend_d_status": diag.suspend_d_status,
        "daily_t_total_rows": diag.daily_t_total_rows,
        "daily_t_main_board_rows": diag.daily_t_main_board_rows,
        "daily_basic_t_total_rows": diag.daily_basic_t_total_rows,
        "daily_basic_t_main_board_rows": diag.daily_basic_t_main_board_rows,
        "daily_basic_status": diag.daily_basic_status,
        "n_turnover_missing": diag.n_turnover_missing,
        "turnover_missing_codes": diag.turnover_missing_codes,
        "history_window_planned_days": diag.history_window_planned_days,
        "history_window_actual_days": diag.history_window_actual_days,
        "history_window_missing_dates": diag.history_window_missing_dates,
        "history_min_required_days": diag.history_min_required_days,
        "insufficient_history": diag.insufficient_history,
        "vol_adjust_enabled": diag.vol_adjust_enabled,
        "vol_adjust_status": diag.vol_adjust_status,
        "adj_factor_planned_days": diag.adj_factor_planned_days,
        "adj_factor_actual_days": diag.adj_factor_actual_days,
        "adj_factor_missing_dates": diag.adj_factor_missing_dates,
        "adj_factor_missing_codes": diag.adj_factor_missing_codes,
    }


def _render_rules_md(rules: ScreenRules) -> str:
    """Render the本次使用的筛选阈值 section (transparent + auditable)."""
    return (
        "\n## 筛选阈值（本次使用）\n"
        f"- 涨幅区间: **[{rules.pct_chg_min}%, {rules.pct_chg_max}%]**\n"
        f"- K线实体占比 ≥: **{rules.body_ratio_min}**\n"
        f"- 换手率区间: **[{rules.turnover_min}%, {rules.turnover_max}%]**\n"
        f"- 5日量比 ≥: **{rules.vol_ratio_5d_min}** "
        f"_(严格使用 T 之前最近 5 个连续交易日)_\n"
        f"- 量价规则: **{rules.vol_max_short_window}日最大量** OR "
        f"**{rules.lookback_trade_days}日 vol 排名前 {rules.vol_top_n_long}**\n"
        f"- 长窗口（vol 历史比较）: **{rules.lookback_trade_days}** 交易日\n"
        f"- 历史覆盖率门槛: ≥ **{rules.min_history_coverage:.0%}** × lookback\n"
        f"- vol 复权调整 (vol_adjust): **{'启用' if rules.vol_adjust else '关闭'}**\n"
    )


def _safe_load_json(path: Path, *, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _conf_short(c: str) -> str:
    return {"high": "高", "medium": "中", "low": "低"}.get(c, c[:1].upper() if c else "?")


def export_llm_calls(run_id: str, db: Any, *, reports_root: Path | None = None) -> int:
    """Pull this run's llm_calls rows into reports/<run_id>/llm_calls.jsonl."""
    root = (reports_root or paths.reports_dir()) / str(run_id)
    root.mkdir(parents=True, exist_ok=True)
    rows = db.fetchall(
        "SELECT call_id, stage, model, prompt_hash, input_tokens, output_tokens, "
        "latency_ms, validation_status, error, created_at "
        "FROM llm_calls WHERE run_id = ? ORDER BY created_at",
        (run_id,),
    )
    out_path = root / "llm_calls.jsonl"
    with out_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(
                json.dumps(
                    {
                        "call_id": str(row[0]),
                        "stage": row[1],
                        "model": row[2],
                        "prompt_hash": row[3],
                        "input_tokens": row[4],
                        "output_tokens": row[5],
                        "latency_ms": row[6],
                        "validation_status": row[7],
                        "error": row[8],
                        "created_at": str(row[9]),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return len(rows)
