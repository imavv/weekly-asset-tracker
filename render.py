#!/usr/bin/env python3
"""
render.py
─────────
Render a sheet range into a PNG that preserves the sheet's own formatting
(cell fill, font colour, bold, italic) — so the image matches Google Sheets
exactly. Returns an in-memory BytesIO for Telegram's reply_photo.

Input `table` is the dict GAS returns for a range:
    {
      "values":      [[str, ...], ...],   # display text, as shown in the sheet
      "backgrounds": [["#rrggbb", ...]],   # cell fill
      "fontColors":  [["#rrggbb", ...]],
      "fontWeights": [["bold"|"normal"]],
      "fontStyles":  [["italic"|"normal"]],
    }
A plain 2D list is also accepted (rendered with default white/black styling).

Uses a headless matplotlib backend (Agg) so it works with no display (Railway).
"""

from __future__ import annotations

import io

import matplotlib
matplotlib.use("Agg")  # headless — must be set before pyplot import
import matplotlib.pyplot as plt


def _norm_color(c: str | None, default: str) -> str:
    """Normalise a GAS colour string to something matplotlib accepts."""
    if not c or not isinstance(c, str):
        return default
    c = c.strip()
    return c if c.startswith("#") else default


def render_table(table, title: str | None = None,
                 filename: str = "table.png") -> io.BytesIO:
    """Render `table` to a PNG BytesIO, preserving the sheet's formatting."""
    # Accept either the rich dict or a plain 2D list.
    if isinstance(table, dict):
        values = table.get("values") or []
        backgrounds = table.get("backgrounds")
        font_colors = table.get("fontColors")
        font_weights = table.get("fontWeights")
        font_styles = table.get("fontStyles")
    else:
        values = table or []
        backgrounds = font_colors = font_weights = font_styles = None

    if not values:
        values = [["(empty)"]]

    n_rows = len(values)
    n_cols = max(len(r) for r in values)

    def cell(grid, i, j, default):
        if not grid or i >= len(grid) or j >= len(grid[i]):
            return default
        return grid[i][j]

    fig, ax = plt.subplots(figsize=(max(4.0, n_cols * 1.9), max(1.4, n_rows * 0.5 + 0.6)))
    ax.axis("off")
    if title:
        ax.set_title(title, fontweight="bold", fontsize=14, pad=12)

    text = [[("" if c is None else str(c)) for c in row] + [""] * (n_cols - len(row))
            for row in values]

    tbl = ax.table(cellText=text, cellLoc="center", loc="center")
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(11)
    tbl.scale(1, 1.6)
    tbl.auto_set_column_width(col=list(range(n_cols)))

    for i in range(n_rows):
        for j in range(n_cols):
            c = tbl[i, j]
            c.set_facecolor(_norm_color(cell(backgrounds, i, j, "#ffffff"), "#ffffff"))
            c.set_edgecolor("#d0d0d0")
            weight = "bold" if cell(font_weights, i, j, "normal") == "bold" else "normal"
            style = "italic" if cell(font_styles, i, j, "normal") == "italic" else "normal"
            c.set_text_props(
                color=_norm_color(cell(font_colors, i, j, "#000000"), "#000000"),
                fontweight=weight,
                fontstyle=style,
            )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    buf.name = filename
    return buf
