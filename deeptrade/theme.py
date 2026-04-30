"""EVA-themed Rich design tokens.

V0.0: tokens defined but not yet applied to a Console.
V0.6: full Theme integration into the Live dashboard (DESIGN §9.2).

Naming convention: tokens use semantic role names (status.running, panel.border.error)
so callers reference styles by meaning, not raw color literals.
"""

from __future__ import annotations

from rich.theme import Theme

# === EVA-01 design tokens (truecolor) ===
EVA_BG = "#0B0B10"  # 黑底
EVA_PANEL = "#15131D"
EVA_PURPLE = "#6B2FBF"  # EVA-01 紫，主框线
EVA_DEEP_PURPLE = "#3B0764"
EVA_LIME = "#78D64B"  # 终端绿，正常 / 成功
EVA_ORANGE = "#FF8A00"  # NERV 橙，进行中
EVA_RED = "#E53935"  # 错误
EVA_YELLOW = "#FFB000"  # 警告
EVA_TEXT = "#E8E6F0"
EVA_DIM = "#8C8799"
EVA_STOCK_UP = "#E53935"  # A 股约定红涨
EVA_STOCK_DOWN = "#78D64B"  # 绿跌

EVA_THEME = Theme(
    {
        "title": f"bold {EVA_LIME}",
        "subtitle": f"italic {EVA_DIM}",
        "panel.border.primary": EVA_PURPLE,
        "panel.border.warn": EVA_YELLOW,
        "panel.border.error": EVA_RED,
        "panel.border.ok": EVA_LIME,
        "status.pending": EVA_DIM,
        "status.running": EVA_ORANGE,
        "status.success": EVA_LIME,
        "status.error": EVA_RED,
        "k.value": EVA_LIME,
        "k.label": EVA_DIM,
        "stock.up": EVA_STOCK_UP,
        "stock.down": EVA_STOCK_DOWN,
        "spinner": EVA_ORANGE,
        "headline.alert": f"bold {EVA_YELLOW} on {EVA_DEEP_PURPLE}",
        "headline.fatal": f"bold {EVA_RED} on {EVA_DEEP_PURPLE}",
    }
)
