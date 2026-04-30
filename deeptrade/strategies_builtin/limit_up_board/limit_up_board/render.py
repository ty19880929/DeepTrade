"""Render & export the limit-up-board final report.

DESIGN §12.8.3 + the v0.3.1 banner / S5 rules:
    * partial_failed / failed / cancelled  → red banner at top of summary.md
    * is_intraday=True                     → yellow `INTRADAY MODE` banner
    * Both stack
    * round2_predictions.json contains ALL R2 predictions (with batch_local_rank)
    * round2_final_ranking.json only emitted when R2 was multi-batch
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from deeptrade.core import paths
from deeptrade.core.run_status import RunStatus

from .data import Round1Bundle
from .schemas import (
    ContinuationCandidate,
    FinalRankingResponse,
    StrongCandidate,
)

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
    """Top-of-report banner stack — markdown blockquote style.

    F-L3: when ``status == PARTIAL_FAILED`` and ``failed_batch_ids`` is
    non-empty, the banner enumerates which batches failed so users don't have
    to grep ``llm_calls.jsonl`` to find them.
    """
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
# Markdown body
# ---------------------------------------------------------------------------


def render_summary_md(
    *,
    status: RunStatus,
    is_intraday: bool,
    bundle: Round1Bundle,
    selected: list[StrongCandidate],
    predictions: list[ContinuationCandidate],
    final_ranking: FinalRankingResponse | None,
    failed_batch_ids: list[str] | None = None,
) -> str:
    """Build the full summary.md content."""
    out = [
        render_banners(status=status, is_intraday=is_intraday, failed_batch_ids=failed_batch_ids)
    ]
    out.append("# 打板策略报告\n")
    out.append(
        f"- trade_date: **{bundle.trade_date}**\n"
        f"- next_trade_date: **{bundle.next_trade_date}**\n"
        f"- status: `{status.value}`\n"
        f"- intraday: `{is_intraday}`\n"
    )

    # Sector strength source label is meaningful — surface it.
    out.append(
        f"\n*sector_strength_source*: `{bundle.sector_strength.source}`  "
        f"_(可信度：limit_cpt_list > lu_desc_aggregation > industry_fallback)_\n"
    )

    if bundle.data_unavailable:
        out.append(f"\n*data_unavailable*: `{bundle.data_unavailable}`\n")

    # ----- R1 -----
    out.append(f"\n## R1 强势标的（{len(selected)}/{len(bundle.candidates)} selected）\n")
    if selected:
        out.append("| Rank | Code | Name | Score | Level | Theme/Industry | Rationale |\n")
        out.append("|------|------|------|-------|-------|----------------|-----------|\n")
        for i, c in enumerate(selected, 1):
            theme = _industry_for(c.candidate_id, bundle.candidates)
            out.append(
                f"| {i} | `{c.ts_code}` | {c.name} | {c.score:.1f} | "
                f"{c.strength_level} | {theme} | {c.rationale} |\n"
            )
    else:
        out.append("_(本轮无强势标的)_\n")

    # ----- R2 / Final -----
    if predictions:
        if final_ranking is not None:
            out.append("\n## 次日连板预测（按 final_rank 排序）\n")
            out.append("| # | Code | Name | Final Pred | Conf. | Δ vs batch | Reason |\n")
            out.append("|---|------|------|-----------|-------|-----------|--------|\n")
            for fi in sorted(final_ranking.finalists, key=lambda f: f.final_rank):
                out.append(
                    f"| {fi.final_rank} | `{fi.ts_code}` | "
                    f"{_name_for(fi.candidate_id, predictions)} | "
                    f"{fi.final_prediction} | {fi.final_confidence} | "
                    f"{fi.delta_vs_batch} | {fi.reason_vs_peers} |\n"
                )
        else:
            out.append("\n## 次日连板预测（单批）\n")
            out.append("| Rank | Code | Name | Score | Conf. | Pred | Rationale |\n")
            out.append("|------|------|------|-------|-------|------|-----------|\n")
            for p in sorted(predictions, key=lambda x: x.rank):
                out.append(
                    f"| {p.rank} | `{p.ts_code}` | {p.name} | "
                    f"{p.continuation_score:.1f} | {p.confidence} | "
                    f"{p.prediction} | {p.rationale} |\n"
                )
    else:
        out.append("\n## 次日连板预测\n_(本轮无候选标的)_\n")

    out.append("\n---\n*免责声明：本报告仅用于策略研究，不构成投资建议。*\n")
    return "".join(out)


# ---------------------------------------------------------------------------
# Report directory writer
# ---------------------------------------------------------------------------


def write_report(
    run_id: str,
    *,
    status: RunStatus,
    is_intraday: bool,
    bundle: Round1Bundle,
    selected: list[StrongCandidate],
    predictions: list[ContinuationCandidate],
    final_ranking: FinalRankingResponse | None,
    extra_files: dict[str, str] | None = None,
    reports_root: Path | None = None,
    failed_batch_ids: list[str] | None = None,
) -> Path:
    """Write the 5-file report directory and return its path."""
    root = (reports_root or paths.reports_dir()) / str(run_id)
    root.mkdir(parents=True, exist_ok=True)

    # 1. summary.md
    md = render_summary_md(
        status=status,
        is_intraday=is_intraday,
        bundle=bundle,
        selected=selected,
        predictions=predictions,
        final_ranking=final_ranking,
        failed_batch_ids=failed_batch_ids,
    )
    (root / "summary.md").write_text(md, encoding="utf-8")

    # 2. round1_strong_targets.json
    (root / "round1_strong_targets.json").write_text(
        json.dumps([s.model_dump(mode="json") for s in selected], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 3. round2_predictions.json (ALL predictions, with batch_local_rank)
    r2_out = []
    for p in predictions:
        rec = p.model_dump(mode="json")
        rec["batch_local_rank"] = p.rank  # explicit alias for downstream tools
        if final_ranking is not None:
            match = next(
                (f for f in final_ranking.finalists if f.candidate_id == p.candidate_id),
                None,
            )
            if match is not None:
                rec["final_rank"] = match.final_rank
                rec["delta_vs_batch"] = match.delta_vs_batch
        r2_out.append(rec)
    (root / "round2_predictions.json").write_text(
        json.dumps(r2_out, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 4. round2_final_ranking.json (only when multi-batch / final_ranking ran)
    if final_ranking is not None:
        (root / "round2_final_ranking.json").write_text(
            json.dumps(final_ranking.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # 5. data_snapshot.json
    snapshot: dict[str, Any] = {
        "trade_date": bundle.trade_date,
        "next_trade_date": bundle.next_trade_date,
        "status": status.value,
        "is_intraday": is_intraday,
        "candidates": bundle.candidates,
        "market_summary": bundle.market_summary,
        "sector_strength": asdict(bundle.sector_strength),
        "data_unavailable": bundle.data_unavailable,
    }
    (root / "data_snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 6. llm_calls.jsonl is written incrementally by the runner; we touch it
    # here so the file always exists in the report dir.
    (root / "llm_calls.jsonl").touch(exist_ok=True)

    # Caller-supplied extras (e.g. R1/R2 raw responses captured during run)
    if extra_files:
        for name, content in extra_files.items():
            (root / name).write_text(content, encoding="utf-8")

    return root


def render_terminal_summary(
    run_id: str,
    *,
    reports_root: Path | None = None,
    console: Any = None,
) -> None:
    """Print a concise, friendly summary of a finished run to the terminal.

    Reads from ``reports/<run_id>/`` so it works for both:
      - just-finished runs (called from ``cmd_run`` after the dashboard exits)
      - historical runs (called from ``cmd_report <run_id>``)

    Output sections (only the ones with data are shown):
      - Header line: trade_date / next_trade_date / status / counts
      - "次日重点关注" — R2 top_candidate picks (full table with rationale)
      - "观察仓"        — R2 watchlist (compact, no rationale)
      - "回避"          — R2 avoid (compact)
      - Footer: report directory + how to re-display
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

    snap = _safe_load_json(root / "data_snapshot.json", default={})
    r1 = _safe_load_json(root / "round1_strong_targets.json", default=[])
    r2 = _safe_load_json(root / "round2_predictions.json", default=[])
    has_final = (root / "round2_final_ranking.json").is_file()

    trade_date = snap.get("trade_date", "?")
    next_trade_date = snap.get("next_trade_date", "?")
    status = snap.get("status", "unknown")
    is_intraday = bool(snap.get("is_intraday", False))
    n_total = len(snap.get("candidates", []))
    n_selected = sum(1 for c in r1 if c.get("selected"))

    # ----- Banner / status -------------------------------------------------
    if status in ("partial_failed", "failed", "cancelled"):
        banner_style = "headline.alert" if status == "partial_failed" else "headline.fatal"
        banner_label = {
            "partial_failed": "PARTIAL — 结果不完整",
            "failed": "FAILED — 运行失败",
            "cancelled": "CANCELLED — 用户中断",
        }.get(status, status)
        console.print(Panel(banner_label, style=banner_style, border_style="panel.border.error"))
    if is_intraday:
        console.print(
            Panel(
                "INTRADAY MODE — 数据可能不完整，仅供盘中观察",
                style="headline.alert",
                border_style="panel.border.warn",
            )
        )

    # ----- Header line -----------------------------------------------------
    console.print(
        f"[title]打板策略[/title]  "
        f"[k.label]T=[/k.label][k.value]{trade_date}[/k.value]  "
        f"[k.label]T+1=[/k.label][k.value]{next_trade_date}[/k.value]  "
        f"[k.label]入选/候选=[/k.label][k.value]{n_selected}/{n_total}[/k.value]  "
        f"[k.label]状态=[/k.label][status.{'success' if status == 'success' else 'error'}]{status}[/]"
    )

    # ----- R2 grouped tables ----------------------------------------------
    if not r2:
        console.print("[subtitle](本轮无连板预测候选)[/subtitle]")
    else:
        # When final_ranking ran, sort by final_rank; else by rank
        sort_key = "final_rank" if has_final else "rank"
        # Group by prediction
        groups: dict[str, list[dict]] = {"top_candidate": [], "watchlist": [], "avoid": []}
        for p in r2:
            groups.setdefault(p.get("prediction", "watchlist"), []).append(p)
        for g in groups.values():
            g.sort(key=lambda x: x.get(sort_key, x.get("rank", 0)))

        # Top candidates (most actionable — show with rationale)
        if groups["top_candidate"]:
            t = Table(
                title=f"次日重点关注 · {len(groups['top_candidate'])} 只",
                title_style="title",
                border_style="panel.border.ok",
                header_style="k.label",
                expand=True,
            )
            t.add_column("#", justify="right", width=3)
            t.add_column("代码", style="k.value", no_wrap=True, width=11)
            t.add_column("名称", no_wrap=True, max_width=10)
            t.add_column("分", justify="right", width=4)
            t.add_column("信", width=4)
            t.add_column("理由", overflow="fold")
            for p in groups["top_candidate"]:
                t.add_row(
                    str(p.get(sort_key, p.get("rank", "?"))),
                    p.get("ts_code", "?"),
                    p.get("name", "?"),
                    f"{p.get('continuation_score', 0):.0f}",
                    _conf_short(p.get("confidence", "")),
                    p.get("rationale", ""),
                )
            console.print(t)

        # Watchlist (compact — code/name/score/conf only)
        if groups["watchlist"]:
            t = Table(
                title=f"观察仓 · {len(groups['watchlist'])} 只",
                title_style="subtitle",
                border_style="panel.border.primary",
                header_style="k.label",
                show_lines=False,
            )
            t.add_column("#", justify="right", width=3)
            t.add_column("代码", style="k.value", no_wrap=True, width=11)
            t.add_column("名称", no_wrap=True, max_width=10)
            t.add_column("分", justify="right", width=4)
            t.add_column("信", width=4)
            for p in groups["watchlist"]:
                t.add_row(
                    str(p.get(sort_key, p.get("rank", "?"))),
                    p.get("ts_code", "?"),
                    p.get("name", "?"),
                    f"{p.get('continuation_score', 0):.0f}",
                    _conf_short(p.get("confidence", "")),
                )
            console.print(t)

        # Avoid (just code+name, comma-separated to save space)
        if groups["avoid"]:
            avoid_text = "、".join(
                f"[k.value]{p.get('ts_code')}[/k.value] {p.get('name', '')}"
                for p in groups["avoid"]
            )
            console.print(f"[subtitle]回避 · {len(groups['avoid'])} 只:[/subtitle] {avoid_text}")

    # ----- Footer ---------------------------------------------------------
    console.print(f"\n[k.label]报告目录:[/k.label] [k.value]{root}[/k.value]")
    console.print(
        f"[k.label]完整报告:[/k.label] [k.value]deeptrade strategy report {run_id}[/k.value]  "
        "[subtitle](查看 markdown 全文 + R1 全表 + 数据快照)[/subtitle]"
    )
    console.print("[subtitle]免责声明: 本报告仅用于策略研究，不构成投资建议。[/subtitle]")


def _safe_load_json(path: Path, *, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _conf_short(c: str) -> str:
    """Map LLM 'high'/'medium'/'low' to a 1-char display."""
    return {"high": "高", "medium": "中", "low": "低"}.get(c, c[:1].upper() if c else "?")


def export_llm_calls(run_id: str, db, *, reports_root: Path | None = None) -> int:  # noqa: ANN001
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _industry_for(cid: str, candidates: list[dict[str, Any]]) -> str:
    for c in candidates:
        if c["candidate_id"] == cid:
            return str(c.get("industry") or c.get("lu_desc") or "—")
    return "—"


def _name_for(cid: str, predictions: list[ContinuationCandidate]) -> str:
    for p in predictions:
        if p.candidate_id == cid:
            return p.name
    return cid
