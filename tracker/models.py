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

from .config import BANK_ACCOUNTS, ETF_TICKERS, FX_CURRENCIES, STOCK_TICKERS

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

BankAccountName = Literal[BANK_ACCOUNTS]  # type: ignore[valid-type]
StockTicker = Literal[STOCK_TICKERS]  # type: ignore[valid-type]
EtfTicker = Literal[ETF_TICKERS]  # type: ignore[valid-type]
FxCurrency = Literal[FX_CURRENCIES]  # type: ignore[valid-type]


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


class FxHolding(BaseModel):
    """A foreign-currency balance. Only the rate is supplied.

    The amount held comes from holdings.json — the model must not guess it.
    """

    currency: FxCurrency = Field(description="Currency code.")
    rate_idr: float = Field(
        ge=0,
        description=(
            "Exchange rate in IDR per ONE unit of this currency (e.g. ~17800 "
            "for USD, ~113 for JPY). Take it verbatim from get_market_data."
        ),
    )


class Snapshot(BaseModel):
    """One complete weekly EOD portfolio snapshot."""

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
    fx: list[FxHolding] = Field(
        description=f"All {len(FX_CURRENCIES)} foreign-currency exchange rates."
    )

    @field_validator("date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError(f"date must be YYYY-MM-DD, got {v!r}")
        return v
