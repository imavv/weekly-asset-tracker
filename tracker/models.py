"""Semantic input schema — what Claude reports, not what the sheet looks like.

Every field here names an *observation* ("BBCA traded at 8300, I hold 44 lots").
Nothing names a spreadsheet position. The server turns observations into the
A–K grid in `assemble.py`, which is why the model can't shift a column.

These are Pydantic models, so the MCP SDK publishes them as JSON Schema and the
transport rejects malformed calls before any of our code runs.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .config import BANK_ACCOUNTS, BASE_CURRENCY, ETF_TICKERS, STOCK_TICKERS

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CURRENCY_RE = re.compile(r"^[A-Za-z]{3}$")

BankAccountName = Literal[BANK_ACCOUNTS]  # type: ignore[valid-type]
StockTicker = Literal[STOCK_TICKERS]  # type: ignore[valid-type]
EtfTicker = Literal[ETF_TICKERS]  # type: ignore[valid-type]


def _clean_currency(v: str) -> str:
    """Normalise and sanity-check an ISO 4217 code.

    Currencies are deliberately NOT a Literal, unlike every other identifier in
    this module: the roster is whatever the screenshot shows, so a code the
    server has never seen must be accepted. The shape check is what replaces
    the closed set — it keeps the value ticker-shaped before it reaches a
    GOOGLEFINANCE formula, and it rejects the one code that cannot be an FX
    row: IDR is the base currency, so an IDR row would double-count a cash
    balance at a rate of 1.
    """
    code = str(v).strip().upper()
    if not CURRENCY_RE.match(code):
        raise ValueError(
            f"currency must be a 3-letter code such as USD or SGD, got {v!r}"
        )
    if code == BASE_CURRENCY:
        raise ValueError(
            f"{BASE_CURRENCY} is the base currency, not an FX holding — an "
            f"{BASE_CURRENCY} row would double-count a cash balance."
        )
    return code


class BankBalance(BaseModel):
    """A cash / deposit / bond balance read directly off a screenshot."""

    account: BankAccountName = Field(
        description="Account name, exactly as listed in the roster."
    )
    value_idr: int = Field(
        ge=0,
        description=(
            "Balance in IDR as a whole number — no separators, no currency "
            "symbol. Use 0 if the screenshot is missing or unreadable."
        ),
    )


class StockHolding(BaseModel):
    """An IDX stock position, all three numbers from the broker screenshot."""

    ticker: StockTicker = Field(description="IDX ticker.")
    lots: int = Field(
        ge=0,
        description=(
            "RAW lot count exactly as shown by the broker. Do NOT multiply by "
            "100 — the server converts lots to shares."
        ),
    )
    price_idr: float = Field(
        ge=0, description="Last traded price in IDR, from the broker screenshot."
    )
    avg_idr: float = Field(
        ge=0,
        description=(
            "Average cost basis in IDR. Carry forward from last week unless the "
            "user says it changed."
        ),
    )


class EtfHolding(BaseModel):
    """A US ETF position. Only the price is supplied.

    Share count and cost basis come from holdings.json — the model must not
    guess them.
    """

    ticker: EtfTicker = Field(description="US ETF ticker.")
    price_usd: float = Field(ge=0, description="Share price in USD, 2 decimals.")


class FxBalance(BaseModel):
    """A foreign-currency balance as it appears on the screenshot.

    This is the one quantity the model DOES report. ETF share counts stay in
    holdings.json because an ETF screenshot never shows them; a multi-currency
    screen shows the balance right there, so the screenshot is the better
    source of truth and holdings.json is only the fallback.
    """

    currency: str = Field(
        description="3-letter currency code, e.g. USD, SGD, JPY, CNY, AUD.",
    )
    amount: float = Field(
        ge=0,
        description=(
            "Balance in units of THAT currency, exactly as the screenshot "
            "shows it — 4800 for USD 4,800.00. Never the IDR equivalent, and "
            "never converted: the server applies the exchange rate itself."
        ),
    )

    @field_validator("currency")
    @classmethod
    def _normalise(cls, v: str) -> str:
        return _clean_currency(v)


class FxHolding(BaseModel):
    """An exchange rate for one currency — the escape-hatch override shape."""

    currency: str = Field(description="3-letter currency code.")
    rate_idr: float = Field(
        ge=0,
        description=(
            "Exchange rate in IDR per ONE unit of this currency (e.g. ~17800 "
            "for USD, ~113 for JPY). Take it verbatim from get_market_data."
        ),
    )

    @field_validator("currency")
    @classmethod
    def _normalise(cls, v: str) -> str:
        return _clean_currency(v)


class FxPosition(BaseModel):
    """A resolved FX row: how much is held, at what rate, and on whose word.

    Internal only. `from_screenshot` is False when the amount was taken from
    holdings.json because no FX was observed at all — the preview says so, so
    a fallback week never passes for an observed one.
    """

    currency: str = Field(description="3-letter currency code.")
    amount: float = Field(ge=0, description="Balance in units of that currency.")
    rate_idr: float = Field(ge=0, description="IDR per one unit.")
    from_screenshot: bool = Field(
        default=True,
        description="False when the amount fell back to holdings.json.",
    )

    @field_validator("currency")
    @classmethod
    def _normalise(cls, v: str) -> str:
        return _clean_currency(v)


class Observations(BaseModel):
    """What the model reads off the screenshots — and nothing else.

    This is the tool-facing input. Prices, exchange rates, the date, row
    numbers and formulas are all resolved server-side, so none of them appear
    here. Quantities are split: ETF share counts stay server-side because an
    ETF screenshot never shows one, while FX balances are reported here because
    the multi-currency screen shows them plainly. If a value cannot be observed
    from a screenshot, it does not belong in this model.
    """

    banks: list[BankBalance] = Field(
        description=f"All {len(BANK_ACCOUNTS)} cash/deposit/bond balances."
    )
    ajaib_usd: float = Field(
        ge=0,
        description=(
            "Ajaib buying power in USD, from the screenshot. The server converts "
            "it to IDR using this block's locked FX rate."
        ),
    )
    stocks: list[StockHolding] = Field(
        description=f"All {len(STOCK_TICKERS)} IDX stock positions."
    )
    fx: list[FxBalance] = Field(
        default_factory=list,
        description=(
            "EVERY foreign-currency balance on the multi-currency screen, with "
            "the amount in that currency. This list IS the week's FX roster: a "
            "currency here gets a row even if the tracker has never held it, "
            "and a currency the tracker held before but that is NOT here is "
            "dropped from this week's block. So list the whole screen, not "
            "just what changed. Leave it empty ONLY when you have no "
            "multi-currency screenshot at all — the server then falls back to "
            "the last known amounts in holdings.json and says so."
        ),
    )
    fx_total_idr: float | None = Field(
        default=None,
        ge=0,
        description=(
            "The 'Total Forex Pocket Value' in IDR shown at the top of the "
            "multi-currency screen. This is NOT a balance and never becomes a "
            "row — the server only adds up the currencies you listed and warns "
            "if the two disagree, which is how a currency scrolled off the "
            "bottom of the list gets caught. Report it whenever it is visible."
        ),
    )
    date: str | None = Field(
        default=None,
        description=(
            "EOD date as YYYY-MM-DD. Leave unset unless the user names a "
            "specific date — the server defaults to today in WIB."
        ),
    )
    etf_price_overrides: list[EtfHolding] = Field(
        default_factory=list,
        description=(
            "ESCAPE HATCH — leave empty. Only use it when the server reports an "
            "ETF price it could not resolve and the user supplies one by hand."
        ),
    )
    fx_rate_overrides: list[FxHolding] = Field(
        default_factory=list,
        description=(
            "ESCAPE HATCH — leave empty. Only use it when the server reports an "
            "FX rate it could not resolve and the user supplies one by hand."
        ),
    )

    @field_validator("date")
    @classmethod
    def _check_optional_date(cls, v: str | None) -> str | None:
        if v is not None and not DATE_RE.match(v):
            raise ValueError(f"date must be YYYY-MM-DD, got {v!r}")
        return v


class Snapshot(BaseModel):
    """One complete weekly EOD portfolio snapshot.

    Internal only — built by the server from Observations plus resolved market
    data. It is not a tool argument, so the model never constructs one.
    """

    date: str = Field(
        description="EOD date for this snapshot, YYYY-MM-DD (WIB)."
    )
    banks: list[BankBalance] = Field(
        description=f"All {len(BANK_ACCOUNTS)} cash/deposit/bond balances."
    )
    ajaib_usd: float = Field(
        ge=0,
        description=(
            "Ajaib buying power in USD. The server converts it to IDR using "
            "this block's locked FX rate."
        ),
    )
    stocks: list[StockHolding] = Field(
        description=f"All {len(STOCK_TICKERS)} IDX stock positions."
    )
    etfs: list[EtfHolding] = Field(
        description=f"All {len(ETF_TICKERS)} US ETF prices."
    )
    fx: list[FxPosition] = Field(
        default_factory=list,
        description=(
            "This week's FX rows, already resolved to amount + rate. The list "
            "may be empty, and its length varies week to week — it is the "
            "screenshot's roster, not a fixed one."
        ),
    )
    fx_total_idr: float | None = Field(
        default=None,
        ge=0,
        description="The screen's own IDR total, for the completeness check.",
    )

    @field_validator("date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError(f"date must be YYYY-MM-DD, got {v!r}")
        return v
