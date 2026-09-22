"""Configuration and the fixed asset roster.

Two kinds of thing live here:

1. **Secrets / endpoints** — read from environment variables only. Never
   defaulted to a real value, because this repo is public. A missing variable
   raises at import time so a misconfigured deploy fails loudly instead of
   silently writing nowhere.

2. **The roster** — the order of rows in every weekly block. Everything up to
   the ETFs is fixed; the FX tail comes from the week's screenshot. This is
   the single source of truth for row ordering; the model never chooses it.
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

# Foreign currencies. UNLIKE every other roster entry, this list is NOT the
# roster — it is a display order plus the fallback set.
#
# The FX roster is whatever the week's screenshot shows: a currency the model
# reports is written even if it is absent here, and one it does not report is
# dropped even if it is here. This list only decides (a) the order known
# currencies appear in, and (b) what to fall back to when no FX was observed at
# all. ETFs are the opposite and deliberately so — an ETF screenshot shows a
# price but never a share count, so ETF_TICKERS above really is the roster.
#
# Rate is resolved via GOOGLEFINANCE. Column G is IDR per unit, so value is
# simply F*G — no $K$ anchor, unlike ETFs whose prices are quoted in USD.
FX_CURRENCIES: tuple[str, ...] = ("CNY", "USD", "SGD", "AUD", "JPY")

# The base currency. An "IDR" FX row would double-count a cash balance and its
# rate would be 1, so it is rejected at the schema boundary.
BASE_CURRENCY = "IDR"

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

# The fixed head of the roster — everything whose membership never varies.
# Because the server builds rows from this list, "wrong row order" and
# "shifted columns" stop being possible failure modes.
FIXED_ROSTER: tuple[str, ...] = (
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
)


def order_fx(currencies) -> tuple[str, ...]:
    """Put a week's currencies in a stable order: known ones first, then new.

    Known currencies keep their FX_CURRENCIES position so a row does not move
    between weeks. Anything new is appended alphabetically rather than in the
    order the model happened to read it off the screen — the sheet's row order
    should not depend on which way the model's eye travelled.
    """
    seen = dict.fromkeys(currencies)  # de-duplicate, preserve first appearance
    known = [c for c in FX_CURRENCIES if c in seen]
    new = sorted(c for c in seen if c not in FX_CURRENCIES)
    return (*known, *new)


def build_roster(fx_currencies) -> tuple[str, ...]:
    """The exact order of rows in one weekly block.

    The block is FIXED_ROSTER plus this week's currencies, so its length varies
    with how many currencies the screenshot showed. Nothing downstream assumes
    a row count: the Apps Script writes `rows.length` rows, and the dashboard
    ranges scan to the first blank row rather than using a fixed height.
    """
    return (*FIXED_ROSTER, *order_fx(fx_currencies))


# The roster as it stands with no observations — the fallback shape.
ROSTER: tuple[str, ...] = build_roster(FX_CURRENCIES)


def category_for(name: str, fx_currencies=()) -> str:
    """Column B for a roster entry.

    CATEGORY covers everything fixed. A currency this week's screenshot
    introduced is not in it, so `fx_currencies` names the week's FX roster and
    anything in it falls into the FX category.
    """
    if name in CATEGORY:
        return CATEGORY[name]
    if name in fx_currencies:
        return "FX"
    raise KeyError(f"No category for roster entry {name!r}")


def fx_symbol(currency: str) -> str:
    """The GOOGLEFINANCE symbol for a currency: IDR per one unit."""
    return f"CURRENCY:{currency}IDR"


def googlefinance_symbol(name: str, fx_currencies=FX_CURRENCIES) -> str:
    """The symbol to hand GOOGLEFINANCE for a roster entry.

    ETFs use the bare ticker; currencies need the CURRENCY:XXXIDR form. Pass
    `fx_currencies` when the week's FX roster differs from the default.
    """
    if name in fx_currencies:
        return fx_symbol(name)
    return name

SHARES_PER_LOT = 100  # IDX convention: 1 lot = 100 shares
FX_FORMULA = '=GOOGLEFINANCE("CURRENCY:USDIDR")'
PRICE_DEVIATION_THRESHOLD = 0.30  # advisory flag when price vs avg differs >30%

# How far the multi-currency screen's own IDR total may sit from the sum of the
# currencies reported before it is worth mentioning. It is never zero: the bank
# converts at its own rates and we convert at GOOGLEFINANCE's, which alone runs
# to about a percent. Wide enough not to cry wolf every week, narrow enough that
# a missing currency of any real size trips it.
FX_TOTAL_TOLERANCE = 0.03


def today_wib() -> str:
    """Today's date (YYYY-MM-DD) in WIB / UTC+7, regardless of server timezone.

    Vercel runs in UTC, so this must never be derived from the local clock
    without the offset — a Friday-evening WIB snapshot is still Friday.
    """
    wib = datetime.timezone(datetime.timedelta(hours=7))
    return datetime.datetime.now(wib).date().isoformat()
