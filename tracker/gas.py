"""Client for the Google Apps Script web app.

This module is where our MCP server stops being a server and becomes a *client*
of something else — it holds GAS_SECRET_TOKEN the way a confidential OAuth
client holds a client secret: server-side, never in a tool argument, never in
anything the model can read back.

Uses httpx (already a dependency of the MCP SDK) so the calls are async and do
not block the event loop.

Apps Script web apps cold-start, occasionally past 20 seconds, so every call
uses a generous timeout and retries once.
"""

from __future__ import annotations

import logging

import httpx

from .config import gas_endpoint, gas_token

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(60.0)
ATTEMPTS = 2


class GasError(RuntimeError):
    """The Apps Script endpoint failed or returned something unusable."""


async def _get(params: dict) -> dict:
    """GET the web app with the shared token, retrying once on cold start."""
    params = {"token": gas_token(), **params}
    last: Exception | None = None

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        for attempt in range(1, ATTEMPTS + 1):
            try:
                resp = await client.get(gas_endpoint(), params=params)
                try:
                    body = resp.json()
                except ValueError:
                    raise GasError(
                        f"Apps Script returned non-JSON (HTTP {resp.status_code}): "
                        f"{resp.text[:300]}"
                    )
                if body.get("status") != 200:
                    raise GasError(f"Apps Script error: {body}")
                return body
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last = exc
                log.warning("GAS GET attempt %s failed: %s", attempt, exc)

    raise GasError(f"Apps Script unreachable after {ATTEMPTS} attempts: {last}")


async def fetch_start_row() -> int:
    """The first empty row in column A — where this week's block goes."""
    body = await _get({})
    return int(body["start_row"])


async def fetch_summary() -> dict:
    """The two dashboard tables, as display values with formatting."""
    body = await _get({"action": "summary"})
    return {"trend": body["trend"], "breakdown": body["breakdown"]}


async def fetch_prices(tickers: list[str]) -> tuple[dict[str, float], dict[str, str]]:
    """Resolve symbols to static prices via GOOGLEFINANCE inside the sheet.

    The Apps Script writes formulas to a hidden scratch sheet, waits for them to
    settle, reads the computed numbers, then clears it. We get Google's price
    data as plain numbers — so column G holds a fixed value that will not
    rewrite itself when the sheet recalculates next month.

    Returns (prices, unresolved). `unresolved` maps a symbol to whatever the
    cell actually held ("#N/A", "Loading...", "(blank)"), so a failure can be
    diagnosed from the tool output rather than just going missing.
    """
    body = await _get({"action": "prices", "tickers": ",".join(tickers)})
    prices = {
        k: float(v) for k, v in body.get("prices", {}).items() if v not in ("", None)
    }
    unresolved = {k: str(v) for k, v in body.get("unresolved", {}).items()}
    return prices, unresolved


async def fetch_usdidr() -> float | None:
    """Current USD -> IDR rate. None on failure, so the caller can fall back.

    Ported from `portfolio_tracker.fetch_usdidr` — a free FX API rather than the
    sheet, because this rate anchors every ETF value formula in the block.
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            resp = await client.get("https://open.er-api.com/v6/latest/USD")
            rate = resp.json().get("rates", {}).get("IDR")
        if rate and float(rate) > 0:
            return float(rate)
        log.warning("USD/IDR fetch returned no usable rate")
    except Exception as exc:  # noqa: BLE001 — any failure falls back to the formula
        log.warning("USD/IDR fetch failed: %s", exc)
    return None


async def write_rows(rows: list[list], start_row: int, force: bool = False) -> str:
    """POST the block to the sheet. Returns the Apps Script status message.

    `force` overrides the duplicate-date guard, which otherwise refuses to write
    a second block for a date the sheet already contains.
    """
    payload = {
        "token": gas_token(),
        "start_row": start_row,
        "rows": rows,
        "force": force,
    }

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        resp = await client.post(gas_endpoint(), json=payload)

    try:
        body = resp.json()
    except ValueError:
        raise GasError(
            f"Apps Script returned non-JSON (HTTP {resp.status_code}): {resp.text[:300]}"
        )

    if body.get("status") != 200:
        raise GasError(body.get("message") or str(body))
    return body.get("message", "OK")
