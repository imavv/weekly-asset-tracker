"""Turn a semantic Snapshot into the 11-column A–K grid.

This module is the whole point of the semantic-arguments design. It owns every
deterministic transform that used to be spread across
`portfolio_tracker.post_process_rows`:

  * roster order            (was: trusted to the model, checked afterwards)
  * lots -> shares  x100    (was: multiply_stock_lots)
  * ETF qty / avg injection (was: apply_holdings)
  * FX roster + amounts     (from the screenshot; file is fallback)
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
    ETF_TICKERS,
    FX_FORMULA,
    NUM_COLS,
    SHARES_PER_LOT,
    STOCK_TICKERS,
    build_roster,
    category_for,
)
from .holdings import load_fx_holdings, load_holdings
from .models import Snapshot

log = logging.getLogger(__name__)

Row = list


def _blank_row(date: str, account: str, fx_currencies=()) -> Row:
    """An A–K row with identity columns filled and the rest empty."""
    row: Row = [""] * NUM_COLS
    row[0] = date                                  # A
    row[1] = category_for(account, fx_currencies)  # B
    row[2] = account                               # C
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
    """Build the whole block, in roster order, ready to POST.

    `start_row` is the sheet row the block begins at, needed because every
    formula references absolute row numbers. `fx_rate` is the locked USD/IDR
    value; when None we fall back to the live GOOGLEFINANCE formula.

    The block's height is not fixed: everything up to the ETFs is constant, and
    the FX tail is whatever `snap.fx` holds this week.
    """
    banks = {b.account: b.value_idr for b in snap.banks}
    stocks = {s.ticker: s for s in snap.stocks}
    etfs = {e.ticker: e for e in snap.etfs}
    fx = {f.currency: f for f in snap.fx}
    holdings = load_holdings()
    fx_holdings = load_fx_holdings()

    roster = build_roster(fx)
    fx_currencies = tuple(c for c in roster if c in fx)

    anchor = start_row  # column K of the first row holds this block's FX rate
    rows: list[Row] = []

    for offset, account in enumerate(roster):
        r = start_row + offset
        row = _blank_row(snap.date, account, fx_currencies)

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

        elif account in fx_currencies:
            f = fx[account]
            # The amount is the screenshot's, not the file's — that is the
            # whole point of the FX rework. The file still owns the cost basis,
            # because no screenshot shows what you paid; a currency it has
            # never seen simply has none, and I/J stay blank.
            avg = (fx_holdings.get(account) or {}).get("avg")
            # Column G is IDR per unit, so no $K$ conversion is needed —
            # unlike ETFs, whose prices are quoted in USD.
            row[3] = f"=F{r}*G{r}"                        # D
            row[5] = f.amount                             # F — from screenshot
            row[6] = f.rate_idr                           # G — IDR per unit
            row[7] = avg if avg is not None else 0        # H — from holdings.json
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


def fx_provenance(snap: Snapshot, expected=None) -> dict[str, str]:
    """Per-currency note on where this week's amount came from.

    The FX amount is the one number that now overrides a stored value, so the
    preview has to say so plainly — a silent override is exactly the failure
    this design is meant to rule out. The user confirming the preview is the
    only safeguard between a misread screen and the sheet, so a misread has to
    be visible as a difference, not just as a number.

    `expected` is the roster the server set out to write, which differs from
    `snap.fx` when a rate failed to resolve. Without it such a currency would
    be reported as dropped, which is a different and much more alarming thing
    than "GOOGLEFINANCE did not answer" — and the write is blocked either way.
    """
    stored = load_fx_holdings()
    expected = set(expected) if expected is not None else {f.currency for f in snap.fx}
    notes: dict[str, str] = {}

    for position in snap.fx:
        previous = (stored.get(position.currency) or {}).get("qty")
        if not position.from_screenshot:
            notes[position.currency] = "holdings.json (no screenshot)"
        elif previous is None:
            notes[position.currency] = "NEW — not in holdings.json"
        elif abs(previous - position.amount) > 0.005:
            notes[position.currency] = f"screenshot (was {previous:,.2f})"
        else:
            notes[position.currency] = "screenshot (unchanged)"

    for currency in stored:
        if currency in notes or currency in expected:
            continue
        amount = (stored.get(currency) or {}).get("qty")
        shown = f"{amount:,.2f}" if isinstance(amount, (int, float)) else "?"
        notes[currency] = f"DROPPED — holdings.json had {shown}"
    return notes


def format_preview(
    rows: list[Row], start_row: int, snap: Snapshot | None = None, expected=None
) -> str:
    """A compact, human-scannable rendering of the block for chat."""
    notes = fx_provenance(snap, expected) if snap is not None else {}

    lines = [
        f"{len(rows)} rows, sheet rows {start_row}–{start_row + len(rows) - 1}",
        "",
        f"{'row':>5}  {'category':<9} {'account':<18} {'value / qty x price':<28}  source",
        f"{'-' * 5}  {'-' * 9} {'-' * 18} {'-' * 28}  {'-' * 6}",
    ]
    for i, row in enumerate(rows):
        r = start_row + i
        category, account = row[1], row[2]
        if category in ("Stock", "ETF", "FX"):
            detail = f"{row[5]} x {row[6]}"
        else:
            detail = f"{row[3]:,}" if isinstance(row[3], int) else str(row[3])
        note = notes.get(account, "") if category == "FX" else ""
        lines.append(f"{r:>5}  {category:<9} {account:<18} {detail:<28}  {note}".rstrip())

    dropped = [c for c, n in notes.items() if n.startswith("DROPPED")]
    if dropped:
        lines += ["", "NOT WRITTEN THIS WEEK — no row in this block:"]
        lines += [f"  {c:<6} {notes[c]}" for c in dropped]

    fx = rows[0][10]
    lines += ["", f"USD/IDR anchor (K{start_row}): {fx}"]
    return "\n".join(lines)
