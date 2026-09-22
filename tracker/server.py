"""MCP tool definitions — the server half of the conversation.

    prepare_snapshot   read-only   build + validate, show it, write nothing
    submit_snapshot    WRITES      the only tool that touches the sheet
    get_summary        read-only   dashboard tables after a write
    get_market_data    read-only   DIAGNOSTIC — not part of the normal flow

The weekly task needs exactly two decisions: one from the model (read these
screenshots) and one from the human (yes, write it). Everything in between is
a fixed sequence, so it lives inside `_build_block` rather than being split
across tools the model has to remember to call in order. A step that never
varies is not a decision, and exposing it as one only creates a way to get it
wrong.

Keep `submit_snapshot` on manual approval in Claude's connector settings — it
is the replacement for the old Telegram /confirm step, and the only judgement
call in the whole loop that is deliberately yours rather than the model's.
"""

from __future__ import annotations

import logging

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from . import gas
from .assemble import assemble_rows, format_preview
from .checks import check_snapshot, format_checks
from .config import (
    ETF_TICKERS,
    FX_CURRENCIES,
    fx_symbol,
    googlefinance_symbol,
    order_fx,
    today_wib,
)
from .holdings import load_fx_holdings
from .models import EtfHolding, FxPosition, Observations, Snapshot

log = logging.getLogger(__name__)

mcp = MCPServer(
    name="weekly-asset-tracker",
    version="1.0.0",
    instructions=(
        "Writes a weekly EOD portfolio snapshot to the user's Google Sheet.\n\n"
        "Workflow, in full: call prepare_snapshot with the numbers you read off "
        "the screenshots, SHOW THE RESULT TO THE USER, and call submit_snapshot "
        "with the same observations only once they confirm. Then get_summary.\n\n"
        "Report only what a screenshot shows. The server picks the date, fetches "
        "every ETF price and FX rate, reads ETF share counts from holdings.json, "
        "and computes all formulas and row numbers itself. Never supply prices, "
        "rates, formulas, row numbers or ETF share counts.\n\n"
        "The ONE quantity you do report is foreign-currency balances: list every "
        "currency on the multi-currency screen with its amount. That list is the "
        "week's FX roster — a currency you omit gets no row at all."
    ),
)

# Annotations tell the client which tools are safe to auto-approve. Only
# submit_snapshot changes anything.
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)


@mcp.tool(annotations=READ_ONLY)
async def get_market_data() -> str:
    """DIAGNOSTIC ONLY — inspect the prices and rates the sheet currently returns.

    You do NOT need this for the weekly snapshot; prepare_snapshot fetches the
    same figures itself. Use it only when the user asks to see current prices,
    or when prepare_snapshot reports market data it could not resolve and you
    are helping them work out why.

    ETF prices are USD per share. FX rates are IDR per ONE unit of the currency.
    """
    symbols = [googlefinance_symbol(n) for n in (*ETF_TICKERS, *FX_CURRENCIES)]
    resolved, unresolved = await gas.fetch_prices(symbols)

    lines = ["ETF prices (USD per share):"]
    missing: list[str] = []
    for ticker in ETF_TICKERS:
        value = resolved.get(googlefinance_symbol(ticker))
        if value is None:
            missing.append(ticker)
        else:
            lines.append(f"  {ticker:<6} {value:>12.2f}")

    lines.append("")
    lines.append("FX rates (IDR per 1 unit):")
    for currency in FX_CURRENCIES:
        value = resolved.get(googlefinance_symbol(currency))
        if value is None:
            missing.append(currency)
        else:
            lines.append(f"  {currency:<6} {value:>12.4f}")

    if missing:
        lines += ["", f"NOT RESOLVED: {', '.join(missing)}."]
        # Show what the sheet actually held, so the cause is visible rather
        # than the symbol just going missing.
        for symbol, cell in unresolved.items():
            lines.append(f"    {symbol} -> {cell}")
    return "\n".join(lines)


async def _build_block(
    obs: Observations,
) -> tuple[Snapshot, list[list], int, dict, tuple[str, ...]]:
    """Resolve everything the server owns, then assemble and validate.

    This is the whole workflow that used to be three separate tool calls:
    pick the date, fetch market data, build the snapshot, check it, find the
    start row, lock the FX rate, lay out the grid. The sequence never varies,
    so there is nothing here for a model to decide — and because prices are
    fetched rather than passed in, they cannot be mistyped in transit.

    Returns (snapshot, rows, start_row, checks, fx_roster). Writes nothing.
    """
    date = obs.date or today_wib()

    # The FX roster is this week's, not a constant. Whatever the screenshot
    # showed IS the roster; holdings.json only stands in when nothing was
    # observed at all. ETFs are untouched by any of this — their roster stays
    # ETF_TICKERS and their quantities stay in holdings.json, because an ETF
    # screenshot shows neither.
    observed_fx = {f.currency: f.amount for f in obs.fx}
    from_screenshot = bool(observed_fx)
    if not from_screenshot:
        stored = load_fx_holdings()
        observed_fx = {
            code: (entry.get("qty") or 0.0) for code, entry in stored.items()
        }
        log.info("No FX observed; falling back to holdings.json (%s)", list(observed_fx))

    fx_roster = order_fx(observed_fx)

    symbols = [googlefinance_symbol(t, ()) for t in ETF_TICKERS]
    symbols += [fx_symbol(c) for c in fx_roster]
    resolved, unresolved = await gas.fetch_prices(symbols)

    etf_overrides = {e.ticker: e.price_usd for e in obs.etf_price_overrides}
    fx_overrides = {f.currency: f.rate_idr for f in obs.fx_rate_overrides}

    etfs: list[EtfHolding] = []
    fx: list[FxPosition] = []
    missing: list[str] = []

    for ticker in ETF_TICKERS:
        price = etf_overrides.get(ticker)
        if price is None:
            price = resolved.get(googlefinance_symbol(ticker, ()))
        if price is None:
            missing.append(ticker)
        else:
            etfs.append(EtfHolding(ticker=ticker, price_usd=price))

    for currency in fx_roster:
        rate = fx_overrides.get(currency)
        if rate is None:
            rate = resolved.get(fx_symbol(currency))
        if rate is None:
            # Still an error, as before: a rate we could not fetch would value
            # the holding at nothing. A brand-new currency GOOGLEFINANCE does
            # not know lands here, and fx_rate_overrides is the way out.
            missing.append(currency)
        else:
            fx.append(FxPosition(
                currency=currency,
                amount=observed_fx[currency],
                rate_idr=rate,
                from_screenshot=from_screenshot,
            ))

    snapshot = Snapshot(
        date=date,
        banks=obs.banks,
        ajaib_usd=obs.ajaib_usd,
        stocks=obs.stocks,
        etfs=etfs,
        fx=fx,
        fx_total_idr=obs.fx_total_idr,
    )

    checks = check_snapshot(snapshot, fx_expected=fx_roster)
    if missing:
        detail = "; ".join(f"{sym} -> {cell}" for sym, cell in unresolved.items())
        checks["errors"].insert(0, (
            f"Market data unresolved for: {', '.join(missing)}. "
            f"The sheet returned [{detail}]. Check GOOGLEFINANCE, or ask the "
            "user for the figures and resend them as overrides."
        ))

    start_row = await gas.fetch_start_row()
    fx_rate = await gas.fetch_usdidr()
    rows = assemble_rows(snapshot, start_row, fx_rate)
    return snapshot, rows, start_row, checks, fx_roster


@mcp.tool(annotations=READ_ONLY)
async def prepare_snapshot(observations: Observations) -> str:
    """Build and validate this week's snapshot WITHOUT writing to the sheet.

    Report only what you can read off the screenshots. The server picks the
    date, fetches every ETF price and FX rate, looks up ETF share counts, and
    computes all formulas and row numbers itself.

    Foreign-currency balances are the exception: report every currency on the
    multi-currency screen with its amount, because that list decides which FX
    rows this week's block contains. Check the preview's FX source column
    before showing it — it names every currency that changed, appeared, or
    dropped out since last week.

    Always call this first and SHOW THE RESULT TO THE USER. It reports the exact
    rows that would be written, the locked USD/IDR rate, and any errors or
    warnings. Nothing is saved.
    """
    snapshot, rows, start_row, checks, fx_roster = await _build_block(observations)

    return "\n\n".join([
        f"PREVIEW — nothing written. Snapshot date {snapshot.date}.",
        format_preview(rows, start_row, snapshot, fx_roster),
        format_checks(checks),
        (
            "Fix the errors above before submitting."
            if checks["errors"]
            else "Ready to write. Show this to the user, and call submit_snapshot "
                 "only once they confirm."
        ),
    ])


@mcp.tool(annotations=WRITES)
async def submit_snapshot(observations: Observations, force: bool = False) -> str:
    """Write this week's snapshot to the Google Sheet. THIS MODIFIES THE SHEET.

    Pass the SAME observations you gave prepare_snapshot. Prices and rates are
    re-fetched server-side rather than carried over, so you never retype them.

    Only call this after prepare_snapshot has been shown to the user and they
    have explicitly confirmed. Refuses to write if validation finds errors, or
    if the sheet already contains a block for this date.

    Set force=True only when the user explicitly asks to write a duplicate date.
    """
    snapshot, rows, start_row, checks, fx_roster = await _build_block(observations)

    if checks["errors"]:
        return (
            "REFUSED — validation failed, nothing was written.\n\n"
            + format_checks(checks)
        )

    try:
        message = await gas.write_rows(rows, start_row, force=force)
    except gas.GasError as exc:
        return f"WRITE FAILED — the sheet was not modified.\n\n{exc}"

    log.info("Wrote %s rows at %s for %s", len(rows), start_row, snapshot.date)
    return "\n\n".join([
        f"WRITTEN — {message}",
        format_preview(rows, start_row, snapshot, fx_roster),
        format_checks(checks),
    ])


@mcp.tool(annotations=READ_ONLY)
async def get_summary() -> str:
    """Read back the two dashboard tables: week-to-week trend and asset breakdown.

    Call this immediately after a successful submit_snapshot and show both
    tables to the user — it is how they see the week land. The trend table
    covers every asset category plus the TOTAL row.
    """
    summary = await gas.fetch_summary()
    return "\n\n".join([
        "## Week-to-Week Trend",
        _markdown_table(summary["trend"]["values"]),
        "## Asset Breakdown",
        _markdown_table(summary["breakdown"]["values"]),
    ])


"""get_today_wib used to be a tool. It no longer is: prepare_snapshot resolves
the date itself, so exposing it only created a step for the model to forget."""


def _markdown_table(values: list[list[str]]) -> str:
    """Render a 2D array of display strings as a markdown table."""
    if not values:
        return "_(empty)_"
    header, *body = values
    width = len(header)
    lines = [
        "| " + " | ".join(str(c) for c in header) + " |",
        "|" + "---|" * width,
    ]
    for row in body:
        cells = [str(c) for c in row] + [""] * (width - len(row))
        lines.append("| " + " | ".join(cells[:width]) + " |")
    return "\n".join(lines)
