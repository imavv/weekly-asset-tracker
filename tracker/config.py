"""Configuration and the fixed asset roster.

Two kinds of thing live here:

1. **Secrets / endpoints** — read from environment variables only. Never
   defaulted to a real value, because this repo is public. A missing variable
   raises at import time so a misconfigured deploy fails loudly instead of
   silently writing nowhere.

2. **The roster** — the fixed order of the 23 rows in every weekly block. This
   is the single source of truth for row ordering; the model never chooses it.
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path

# ── Secrets / endpoints (env only) ───────────────────────────────────────────
# Set these in Vercel → Project → Settings → Environment Variables.


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name!r}. "
            "Set it in Vercel → Settings → Environment Variables."
        )
    return value


def gas_endpoint() -> str:
    """The Apps Script web app /exec URL."""
    return _require("GAS_ENDPOINT")


def gas_token() -> str:
    """Shared secret the Apps Script checks. Never expose this to the model."""
    return _require("GAS_SECRET_TOKEN")


def mcp_secret() -> str:
    """The secret path segment that authorises callers of this MCP server."""
    return _require("MCP_SECRET")


# ── Paths ────────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
HOLDINGS_PATH = REPO_ROOT / "holdings.json"

# ── Sheet shape ──────────────────────────────────────────────────────────────
NUM_COLS = 11  # A–K

# Accounts whose value is read straight off a screenshot as an IDR integer.
BANK_ACCOUNTS: tuple[str, ...] = (
    "Mandiri",
    "BCA",
    "Seabank",
    "Others",
    "Superbank",
    "Superbank Deposit",
    "Bibit",
    "Stockbit (RDN)",
    "BNI (RDN)",
)

# IDX stocks — price and lot count come from the broker screenshot.
STOCK_TICKERS: tuple[str, ...] = ("BBCA", "ICBP", "BBRI")

# US ETFs — price resolved via GOOGLEFINANCE, quantity from holdings.json.
ETF_TICKERS: tuple[str, ...] = (
    "VOO", "VT", "VTI", "SPYM", "GDX", "VEA",
    "SMH", "GLD", "IGV", "XLP", "XLE",
)

# Foreign currency balances. Rate resolved via GOOGLEFINANCE, amount held from
# holdings.json. Column G is IDR per unit, so value is simply F*G — no $K$
# anchor, unlike ETFs whose prices are quoted in USD.
FX_CURRENCIES: tuple[str, ...] = ("CNY", "USD", "SGD", "AUD", "JPY")

# Category per account. Ajaib is Cash even though its value is a USD formula.
CATEGORY: dict[str, str] = {
    "Mandiri": "Cash",
    "BCA": "Cash",
    "Seabank": "Cash",
    "Others": "Cash",
    "Superbank": "Cash",
    "Superbank Deposit": "Deposit",
    "Bibit": "MF Bonds",
    "Stockbit (RDN)": "Cash",
    "BNI (RDN)": "Cash",
    "Ajaib": "Cash",
    **{t: "Stock" for t in STOCK_TICKERS},
    **{t: "ETF" for t in ETF_TICKERS},
    **{c: "FX" for c in FX_CURRENCIES},
}

# THE ROSTER — the exact order of rows in every weekly block.
# Because the server builds rows from this list, "wrong row order" and
# "shifted columns" stop being possible failure modes.
ROSTER: tuple[str, ...] = (
    "Mandiri",
    "BCA",
    "Seabank",
    "Others",
    "Superbank",
    "Superbank Deposit",
    "Bibit",
    "Stockbit (RDN)",
    "BNI (RDN)",
    *STOCK_TICKERS,
    "Ajaib",
    *ETF_TICKERS,
    *FX_CURRENCIES,
)


def googlefinance_symbol(name: str) -> str:
    """The symbol to hand GOOGLEFINANCE for a roster entry.

    ETFs use the bare ticker; currencies need the CURRENCY:XXXIDR form.
    """
    if name in FX_CURRENCIES:
        return f"CURRENCY:{name}IDR"
    return name

SHARES_PER_LOT = 100  # IDX convention: 1 lot = 100 shares
FX_FORMULA = '=GOOGLEFINANCE("CURRENCY:USDIDR")'
PRICE_DEVIATION_THRESHOLD = 0.30  # advisory flag when price vs avg differs >30%


def today_wib() -> str:
    """Today's date (YYYY-MM-DD) in WIB / UTC+7, regardless of server timezone.

    Vercel runs in UTC, so this must never be derived from the local clock
    without the offset — a Friday-evening WIB snapshot is still Friday.
    """
    wib = datetime.timezone(datetime.timedelta(hours=7))
    return datetime.datetime.now(wib).date().isoformat()
