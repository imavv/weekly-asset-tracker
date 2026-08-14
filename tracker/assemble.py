"""Turn a semantic Snapshot into the 11-column A–K grid.

This module is the whole point of the semantic-arguments design. It owns every
deterministic transform that used to be spread across
`portfolio_tracker.post_process_rows`:

  * roster order            (was: trusted to the model, checked afterwards)
  * lots -> shares  x100    (was: multiply_stock_lots)
  * ETF qty / avg injection (was: apply_holdings)
  * key formula, column E   (was: apply_key_formula)
  * FX anchor, column K     (was: apply_fx_anchor)
  * value / change formulas (was: emitted by the model, per SKILL.md)

Column map:
    A date | B category | C account | D value | E key | F qty | G price
    H avg  | I pct chg  | J abs chg | K USD/IDR anchor (first row only)
"""

from __future__ import annotations

import logging

from .config import (
    CATEGORY,
    ETF_TICKERS,
    FX_FORMULA,
    NUM_COLS,
    ROSTER,
    SHARES_PER_LOT,
    STOCK_TICKERS,
)
from .holdings import load_holdings
from .models import Snapshot

log = logging.getLogger(__name__)

Row = list


def _blank_row(date: str, account: str) -> Row:
    """An A–K row with identity columns filled and the rest empty."""
    row: Row = [""] * NUM_COLS
    row[0] = date                    # A
    row[1] = CATEGORY[account]       # B
    row[2] = account                 # C
    return row


def _change_formulas(row: Row, r: int, avg: float) -> None:
    """Columns I and J — percent and absolute change vs cost basis.

    When avg is 0 or unknown the formulas would render #DIV/0! and #VALUE!, so
    leave them blank instead. (Fixes the cosmetic divide-by-zero noted in
    CONTEXT.md for ETFs whose holdings.json avg is null.)
    """
    if avg and avg > 0:
        row[8] = f"=(G{r}-H{r})/H{r}"   # I
        row[9] = f"=(G{r}-H{r})*F{r}"   # J
    else:
        row[8] = ""
        row[9] = ""


def assemble_rows(snap: Snapshot, start_row: int, fx_rate: float | None) -> list[Row]:
    """Build the full 23-row block, in roster order, ready to POST.

    `start_row` is the sheet row the block begins at, needed because every
    formula references absolute row numbers. `fx_rate` is the locked USD/IDR
    value; when None we fall back to the live GOOGLEFINANCE formula.
    """
    banks = {b.account: b.value_idr for b in snap.banks}
    stocks = {s.ticker: s for s in snap.stocks}
    etfs = {e.ticker: e for e in snap.etfs}
    holdings = load_holdings()

    anchor = start_row  # column K of the first row holds this block's FX rate
    rows: list[Row] = []

    for offset, account in enumerate(ROSTER):
        r = start_row + offset
        row = _blank_row(snap.date, account)

        if account in STOCK_TICKERS:
            s = stocks.get(account)
            if s is not None:
                row[3] = f"=F{r}*G{r}"                    # D
                row[5] = s.lots * SHARES_PER_LOT          # F — lots to shares
                row[6] = s.price_idr                      # G
                row[7] = s.avg_idr                        # H
                _change_formulas(row, r, s.avg_idr)

        elif account in ETF_TICKERS:
            e = etfs.get(account)
            h = holdings.get(account, {})
            qty = h.get("qty")
            avg = h.get("avg")
            if e is not None:
                row[3] = f"=F{r}*G{r}*$K${anchor}"        # D — USD to IDR
                row[5] = qty if qty is not None else 0    # F — from holdings.json
                row[6] = e.price_usd                      # G
                row[7] = avg if avg is not None else 0    # H — from holdings.json
                _change_formulas(row, r, avg or 0)

        elif account == "Ajaib":
            row[3] = f"={snap.ajaib_usd}*$K${anchor}"     # D — USD buying power

        else:  # plain IDR balance from a screenshot
            row[3] = banks.get(account, 0)                # D

        row[4] = f'=CONCATENATE(A{r},"-",B{r})'           # E — key
        rows.append(row)

    # Column K, first row only: lock the FX rate as a static number so this
    # snapshot does not drift when the sheet recalculates in future weeks.
    rows[0][10] = round(fx_rate, 2) if fx_rate else FX_FORMULA
    if not fx_rate:
        log.warning("No FX rate available — falling back to live GOOGLEFINANCE")

    return rows


def format_preview(rows: list[Row], start_row: int) -> str:
    """A compact, human-scannable rendering of the block for chat."""
    lines = [
        f"{len(rows)} rows, sheet rows {start_row}–{start_row + len(rows) - 1}",
        "",
        f"{'row':>5}  {'category':<9} {'account':<18} {'value / qty x price':<28}",
        f"{'-' * 5}  {'-' * 9} {'-' * 18} {'-' * 28}",
    ]
    for i, row in enumerate(rows):
        r = start_row + i
        category, account = row[1], row[2]
        if category in ("Stock", "ETF"):
            detail = f"{row[5]} x {row[6]}"
        else:
            detail = f"{row[3]:,}" if isinstance(row[3], int) else str(row[3])
        lines.append(f"{r:>5}  {category:<9} {account:<18} {detail:<28}")

    fx = rows[0][10]
    lines += ["", f"USD/IDR anchor (K{start_row}): {fx}"]
    return "\n".join(lines)
