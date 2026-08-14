"""MCP tool definitions — the server half of the conversation.

Four tools, deliberately split so the write is never a surprise:

    get_etf_prices     read-only   fetch prices before building a snapshot
    preview_snapshot   read-only   assemble + validate, show it, write nothing
    submit_snapshot    WRITES      the only tool that touches the sheet
    get_summary        read-only   dashboard tables after a write

Keep `submit_snapshot` on manual approval in Claude's connector settings — it
is the replacement for the old Telegram /confirm step.
"""

from __future__ import annotations

import logging

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from . import gas
from .assemble import assemble_rows, format_preview
from .checks import check_snapshot, format_checks
from .config import ETF_TICKERS, today_wib
from .models import Snapshot

log = logging.getLogger(__name__)

mcp = MCPServer(
    name="weekly-asset-tracker",
    version="1.0.0",
    instructions=(
        "Writes a weekly EOD portfolio snapshot to the user's Google Sheet.\n\n"
        "Workflow: call get_today_wib for the date, get_etf_prices for ETF "
        "prices, then preview_snapshot and SHOW THE RESULT TO THE USER. Only "
        "call submit_snapshot after they confirm. Report every number you read "
        "off a screenshot; the server computes all quantities, formulas and "
        "row positions itself."
    ),
)

# Annotations tell the client which tools are safe to auto-approve. Only
# submit_snapshot changes anything.
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)


@mcp.tool(annotations=READ_ONLY)
async def get_etf_prices() -> str:
    """Fetch current prices for all tracked US ETFs.

    Resolves GOOGLEFINANCE inside the Google Sheet and returns plain numbers,
    so no web search is needed. Call this before preview_snapshot and use the
    returned prices verbatim.
    """
    prices = await gas.fetch_prices(list(ETF_TICKERS))

    lines = [f"{t:<6} {prices[t]:>10.2f}" for t in ETF_TICKERS if t in prices]
    missing = [t for t in ETF_TICKERS if t not in prices]
    if missing:
        lines.append("")
        lines.append(f"NO PRICE RETURNED for: {', '.join(missing)} — ask the user.")
    return "\n".join(lines)


@mcp.tool(annotations=READ_ONLY)
async def preview_snapshot(snapshot: Snapshot) -> str:
    """Assemble and validate a weekly snapshot WITHOUT writing to the sheet.

    Always call this first and show the result to the user. It reports the exact
    sheet rows that would be written, the locked USD/IDR rate, and any errors or
    warnings. Nothing is saved.
    """
    checks = check_snapshot(snapshot)
    start_row = await gas.fetch_start_row()
    fx_rate = await gas.fetch_usdidr()
    rows = assemble_rows(snapshot, start_row, fx_rate)

    return "\n\n".join([
        f"PREVIEW — nothing written. Snapshot date {snapshot.date}.",
        format_preview(rows, start_row),
        format_checks(checks),
        (
            "Fix the errors above before submitting."
            if checks["errors"]
            else "Ready to submit. Ask the user to confirm, then call submit_snapshot."
        ),
    ])


@mcp.tool(annotations=WRITES)
async def submit_snapshot(snapshot: Snapshot, force: bool = False) -> str:
    """Write a weekly snapshot to the Google Sheet. THIS MODIFIES THE SHEET.

    Only call this after preview_snapshot has been shown to the user and they
    have explicitly confirmed. Refuses to write if validation finds errors, or
    if the sheet already contains a block for this date.

    Set force=True only when the user explicitly asks to write a duplicate date.
    """
    checks = check_snapshot(snapshot)
    if checks["errors"]:
        return (
            "REFUSED — validation failed, nothing was written.\n\n"
            + format_checks(checks)
        )

    start_row = await gas.fetch_start_row()
    fx_rate = await gas.fetch_usdidr()
    rows = assemble_rows(snapshot, start_row, fx_rate)

    try:
        message = await gas.write_rows(rows, start_row, force=force)
    except gas.GasError as exc:
        return f"WRITE FAILED — the sheet was not modified.\n\n{exc}"

    log.info("Wrote %s rows at %s for %s", len(rows), start_row, snapshot.date)
    return "\n\n".join([
        f"WRITTEN — {message}",
        format_preview(rows, start_row),
        format_checks(checks),
    ])


@mcp.tool(annotations=READ_ONLY)
async def get_summary() -> str:
    """Read back the two dashboard tables: week-to-week trend and asset breakdown.

    Use this after submit_snapshot to show the user the updated position.
    """
    summary = await gas.fetch_summary()
    return "\n\n".join([
        "## Week-to-Week Trend",
        _markdown_table(summary["trend"]["values"]),
        "## Asset Breakdown",
        _markdown_table(summary["breakdown"]["values"]),
    ])


@mcp.tool(annotations=READ_ONLY)
def get_today_wib() -> str:
    """Today's date in WIB (UTC+7), as YYYY-MM-DD.

    Use this for the snapshot date unless the user names a different one — do
    not assume the server's timezone matches theirs.
    """
    return today_wib()


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
