"""Tests for the deterministic layer: assembly and validation.

These cover the transforms that used to live in `portfolio_tracker.py` — the
arithmetic that must never regress, because a silent error here writes wrong
numbers into a financial record.

Run with:  python -m pytest tests/ -q
"""

from __future__ import annotations

import pytest

from tracker.assemble import assemble_rows
from tracker.checks import check_snapshot
from tracker.config import (
    BANK_ACCOUNTS, ETF_TICKERS, FIXED_ROSTER, FX_CURRENCIES, NUM_COLS, ROSTER,
    STOCK_TICKERS, build_roster, order_fx,
)
from tracker.holdings import load_fx_holdings
from tracker.models import (
    BankBalance, EtfHolding, FxBalance, FxHolding, FxPosition, Observations,
    Snapshot, StockHolding,
)

START_ROW = 1600
FX = 16250.0

# The amounts holdings.json currently carries — the fallback, and what a
# screenshot has to differ from for a drift warning to mean anything.
STORED_FX = {c: e["qty"] for c, e in load_fx_holdings().items()}


def fx_positions(amounts=None, rate=1000.0, from_screenshot=True):
    """FX rows for a snapshot. Defaults to holdings.json's own amounts."""
    amounts = STORED_FX if amounts is None else amounts
    return [
        FxPosition(
            currency=c, amount=a, rate_idr=rate, from_screenshot=from_screenshot
        )
        for c, a in amounts.items()
    ]


def make_snapshot(**overrides) -> Snapshot:
    """A complete, valid snapshot; override any field to test a variation."""
    data = {
        "date": "2026-08-14",
        "banks": [BankBalance(account=a, value_idr=1_000_000) for a in BANK_ACCOUNTS],
        "ajaib_usd": 412.5,
        "stocks": [
            StockHolding(ticker="BBCA", lots=44, price_idr=8300, avg_idr=7900),
            StockHolding(ticker="ICBP", lots=10, price_idr=11000, avg_idr=10500),
            StockHolding(ticker="BBRI", lots=25, price_idr=4200, avg_idr=4000),
        ],
        "etfs": [EtfHolding(ticker=t, price_usd=100.0) for t in ETF_TICKERS],
        "fx": fx_positions(),
    }
    data.update(overrides)
    return Snapshot(**data)


def make_observations(**overrides) -> Observations:
    """Only what a screenshot shows — no prices, rates or date."""
    data = {
        "banks": [BankBalance(account=a, value_idr=1_000_000) for a in BANK_ACCOUNTS],
        "ajaib_usd": 412.5,
        "stocks": [
            StockHolding(ticker="BBCA", lots=44, price_idr=8300, avg_idr=7900),
            StockHolding(ticker="ICBP", lots=10, price_idr=11000, avg_idr=10500),
            StockHolding(ticker="BBRI", lots=25, price_idr=4200, avg_idr=4000),
        ],
        # The FX screen, read off the screenshot. Matching holdings.json here
        # keeps the baseline quiet, so a test that changes an amount is the
        # only thing producing a drift warning.
        "fx": [FxBalance(currency=c, amount=a) for c, a in STORED_FX.items()],
    }
    data.update(overrides)
    return Observations(**data)


# ── Assembly ─────────────────────────────────────────────────────────────────

def test_block_shape_and_order():
    rows = assemble_rows(make_snapshot(), START_ROW, FX)

    assert len(rows) == len(ROSTER) == 29
    assert all(len(r) == NUM_COLS for r in rows)
    # Order comes from ROSTER, so it cannot drift.
    assert [r[2] for r in rows] == list(ROSTER)
    assert all(r[0] == "2026-08-14" for r in rows)


def test_stock_lots_are_converted_to_shares():
    rows = assemble_rows(make_snapshot(), START_ROW, FX)
    bbca = next(r for r in rows if r[2] == "BBCA")

    assert bbca[5] == 44 * 100  # 44 lots -> 4400 shares
    assert bbca[6] == 8300


def test_etf_quantity_comes_from_holdings_not_the_model():
    rows = assemble_rows(make_snapshot(), START_ROW, FX)
    voo = next(r for r in rows if r[2] == "VOO")

    # holdings.json says 3.068; the model supplied no quantity at all.
    assert voo[5] == 3.068
    assert voo[6] == 100.0


def test_formulas_reference_their_own_row_and_the_fx_anchor():
    rows = assemble_rows(make_snapshot(), START_ROW, FX)

    voo_index = ROSTER.index("VOO")
    r = START_ROW + voo_index
    voo = rows[voo_index]

    assert voo[3] == f"=F{r}*G{r}*$K${START_ROW}"
    assert voo[4] == f'=CONCATENATE(A{r},"-",B{r})'

    bbca_index = ROSTER.index("BBCA")
    rb = START_ROW + bbca_index
    assert rows[bbca_index][3] == f"=F{rb}*G{rb}"


def test_ajaib_converts_usd_via_the_anchor():
    rows = assemble_rows(make_snapshot(), START_ROW, FX)
    ajaib = next(r for r in rows if r[2] == "Ajaib")

    assert ajaib[3] == f"=412.5*$K${START_ROW}"
    assert ajaib[1] == "Cash"


def test_fx_rate_is_locked_on_the_first_row_only():
    rows = assemble_rows(make_snapshot(), START_ROW, FX)

    assert rows[0][10] == 16250.0
    assert all(r[10] == "" for r in rows[1:])


def test_fx_falls_back_to_formula_when_unavailable():
    rows = assemble_rows(make_snapshot(), START_ROW, None)
    assert rows[0][10] == '=GOOGLEFINANCE("CURRENCY:USDIDR")'


def test_change_formulas_omitted_when_cost_basis_is_zero():
    """ETFs with avg=null in holdings.json must not emit #DIV/0!."""
    rows = assemble_rows(make_snapshot(), START_ROW, FX)
    voo = next(r for r in rows if r[2] == "VOO")

    assert voo[7] == 0        # avg is null in holdings.json
    assert voo[8] == ""       # I — no divide-by-zero
    assert voo[9] == ""       # J


def test_change_formulas_present_when_cost_basis_is_known():
    rows = assemble_rows(make_snapshot(), START_ROW, FX)
    idx = ROSTER.index("BBCA")
    r = START_ROW + idx

    assert rows[idx][8] == f"=(G{r}-H{r})/H{r}"
    assert rows[idx][9] == f"=(G{r}-H{r})*F{r}"


def test_bank_balances_are_written_as_plain_integers():
    snap = make_snapshot(
        banks=[BankBalance(account=a, value_idr=5_000) for a in BANK_ACCOUNTS]
    )
    rows = assemble_rows(snap, START_ROW, FX)
    mandiri = next(r for r in rows if r[2] == "Mandiri")

    assert mandiri[3] == 5_000
    assert mandiri[1] == "Cash"
    assert next(r for r in rows if r[2] == "Superbank Deposit")[1] == "Deposit"
    assert next(r for r in rows if r[2] == "Bibit")[1] == "MF Bonds"


def test_fx_rows_use_a_plain_product_with_no_anchor():
    """Column G is IDR per unit, so FX needs no $K$ conversion."""
    rows = assemble_rows(make_snapshot(), START_ROW, FX)
    idx = ROSTER.index("USD")
    r = START_ROW + idx

    assert rows[idx][1] == "FX"
    assert rows[idx][3] == f"=F{r}*G{r}"
    assert "$K$" not in rows[idx][3]
    assert rows[idx][6] == 1000.0


def test_fx_amount_comes_from_the_snapshot_not_the_file():
    """The screenshot's figure wins, even when holdings.json disagrees.

    This is the rework in one assertion: the file says 5142.44 USD, the screen
    says 4800, and 4800 is what reaches the sheet.
    """
    amounts = dict(STORED_FX, USD=4800.0)
    rows = assemble_rows(make_snapshot(fx=fx_positions(amounts)), START_ROW, FX)

    assert next(r for r in rows if r[2] == "USD")[5] == 4800.0
    assert STORED_FX["USD"] == 5142.44  # the file is unchanged and overridden
    # Currencies the screen agreed with are untouched.
    assert next(r for r in rows if r[2] == "JPY")[5] == 28749.11


def test_a_new_currency_gets_its_own_row():
    """A currency holdings.json has never seen still lands in the block."""
    amounts = dict(STORED_FX, KRW=1_250_000.0)
    snap = make_snapshot(fx=fx_positions(amounts))
    rows = assemble_rows(snap, START_ROW, FX)

    krw_index = next(i for i, r in enumerate(rows) if r[2] == "KRW")
    r = START_ROW + krw_index
    krw = rows[krw_index]

    assert len(rows) == len(ROSTER) + 1     # the block grew by exactly one row
    assert krw[1] == "FX"                   # categorised without a config entry
    assert krw[5] == 1_250_000.0
    assert krw[3] == f"=F{r}*G{r}"
    assert krw[4] == f'=CONCATENATE(A{r},"-",B{r})'
    # No cost basis exists for it, so the change columns stay blank rather
    # than dividing by zero.
    assert krw[7] == 0
    assert krw[8] == "" and krw[9] == ""
    # New currencies go after the known ones, so no existing row shifts.
    assert [r[2] for r in rows][:len(ROSTER)] == list(ROSTER)


def test_new_currencies_are_appended_alphabetically():
    """Row order must not depend on the order the model read the screen."""
    assert order_fx(["THB", "USD", "KRW"]) == ("USD", "KRW", "THB")
    assert order_fx(["KRW", "USD", "THB"]) == ("USD", "KRW", "THB")
    # De-duplicated, and known currencies keep their configured order.
    assert order_fx(["JPY", "CNY", "JPY"]) == ("CNY", "JPY")


def test_an_unreported_currency_gets_no_row():
    """Absent from the screen means absent from the block — not carried over."""
    amounts = {c: a for c, a in STORED_FX.items() if c != "JPY"}
    rows = assemble_rows(make_snapshot(fx=fx_positions(amounts)), START_ROW, FX)

    accounts = [r[2] for r in rows]
    assert "JPY" not in accounts
    assert len(rows) == len(ROSTER) - 1
    # Everything above the FX tail is untouched, so no formula anchor moves.
    assert accounts[:len(FIXED_ROSTER)] == list(FIXED_ROSTER)


def test_fx_cost_basis_still_comes_from_the_file():
    """The screenshot shows a balance, never what was paid for it."""
    import json

    from tracker import holdings as holdings_module

    amounts = dict(STORED_FX, USD=4800.0)
    snap = make_snapshot(fx=fx_positions(amounts))
    raw = json.loads(holdings_module.HOLDINGS_PATH.read_text())

    assert raw["fx"]["USD"]["avg"] is None  # nothing to track yet
    usd = next(r for r in assemble_rows(snap, START_ROW, FX) if r[2] == "USD")
    assert usd[7] == 0      # H — blank cost basis, not the screenshot's amount
    assert usd[8] == ""     # I/J stay blank rather than #DIV/0!


def test_fx_values_reproduce_the_real_sheet():
    """Regression against the block written on 2026-08-10 (rows 1843-1847)."""
    observed = {
        "CNY": (17375.69, 2638.123, 45_839_207),
        "USD": (5142.44, 17801.0, 91_540_574),
        "SGD": (2181.52, 13924.27, 30_376_073),
        "AUD": (586.71, 12579.08, 7_380_272),
        "JPY": (28749.11, 112.7763, 3_242_218),
    }
    snap = make_snapshot(fx=[
        FxPosition(currency=c, amount=qty, rate_idr=rate)
        for c, (qty, rate, _) in observed.items()
    ])
    rows = assemble_rows(snap, START_ROW, FX)

    for currency, (qty, rate, value) in observed.items():
        row = next(r for r in rows if r[2] == currency)
        assert row[5] == qty, currency          # F as observed
        assert row[6] == rate, currency         # G as supplied
        assert abs(row[5] * row[6] - value) < 2, currency  # what Sheets computes


def test_stockbit_rdn_is_in_the_roster_before_bni():
    rows = assemble_rows(make_snapshot(), START_ROW, FX)
    accounts = [r[2] for r in rows]

    assert accounts.index("Stockbit (RDN)") == accounts.index("Bibit") + 1
    assert accounts.index("BNI (RDN)") == accounts.index("Stockbit (RDN)") + 1
    assert next(r for r in rows if r[2] == "Stockbit (RDN)")[1] == "Cash"


# ── Validation ───────────────────────────────────────────────────────────────

def test_complete_snapshot_passes():
    assert check_snapshot(make_snapshot())["errors"] == []


def test_missing_account_is_an_error():
    partial = [BankBalance(account=a, value_idr=1) for a in BANK_ACCOUNTS[:-1]]
    result = check_snapshot(make_snapshot(banks=partial))

    assert any("Missing bank account" in e for e in result["errors"])
    assert BANK_ACCOUNTS[-1] in result["errors"][0]


def test_duplicate_ticker_is_an_error():
    dupes = [
        StockHolding(ticker="BBCA", lots=1, price_idr=1, avg_idr=1),
        StockHolding(ticker="BBCA", lots=2, price_idr=2, avg_idr=2),
        StockHolding(ticker="ICBP", lots=1, price_idr=1, avg_idr=1),
        StockHolding(ticker="BBRI", lots=1, price_idr=1, avg_idr=1),
    ]
    result = check_snapshot(make_snapshot(stocks=dupes))
    assert any("Duplicate stock" in e for e in result["errors"])


def test_price_far_from_cost_basis_warns_without_blocking():
    stocks = [
        StockHolding(ticker="BBCA", lots=44, price_idr=20000, avg_idr=7900),
        StockHolding(ticker="ICBP", lots=10, price_idr=11000, avg_idr=10500),
        StockHolding(ticker="BBRI", lots=25, price_idr=4200, avg_idr=4000),
    ]
    result = check_snapshot(make_snapshot(stocks=stocks))

    assert result["errors"] == []
    assert any("BBCA" in w and "+153%" in w for w in result["warnings"])


def test_a_dropped_currency_warns_but_does_not_block():
    """The old rule was the opposite: a missing currency used to be an error.

    It cannot be one any more. The screenshot is the roster, so a currency
    that is not on it is the user saying it is gone — a fact, not a defect.
    What it must never be is silent, because a currency scrolled off-screen
    looks exactly the same from here.
    """
    amounts = {c: a for c, a in STORED_FX.items() if c != "JPY"}
    result = check_snapshot(make_snapshot(fx=fx_positions(amounts)))

    assert result["errors"] == []
    dropped = [w for w in result["warnings"] if "DROPPED" in w]
    assert len(dropped) == 1
    assert "JPY" in dropped[0]
    assert "28749.11" in dropped[0]  # says what is being given up


def test_a_changed_amount_warns_with_both_figures():
    result = check_snapshot(make_snapshot(fx=fx_positions(dict(STORED_FX, USD=4800.0))))

    assert result["errors"] == []
    drift = next(w for w in result["warnings"] if w.startswith("USD:"))
    assert "4,800.00" in drift and "5,142.44" in drift and "-342.44" in drift


def test_a_new_currency_warns_that_it_has_no_cost_basis():
    result = check_snapshot(make_snapshot(fx=fx_positions(dict(STORED_FX, KRW=1000.0))))

    assert result["errors"] == []
    assert any("KRW" in w and "new currency" in w for w in result["warnings"])


def test_amounts_matching_the_file_produce_no_fx_noise():
    """The common case — nothing changed — must stay quiet, or the warnings
    that do matter get lost in a wall of routine ones."""
    result = check_snapshot(make_snapshot())

    assert result["errors"] == []
    assert result["warnings"] == []


def test_the_holdings_fallback_says_it_is_a_fallback():
    snap = make_snapshot(fx=fx_positions(from_screenshot=False))
    result = check_snapshot(snap)

    assert result["errors"] == []
    assert any("fell back to holdings.json" in w for w in result["warnings"])


def test_a_duplicate_currency_is_an_error():
    """Reading one currency off two screens would double its row."""
    doubled = fx_positions() + [FxPosition(currency="USD", amount=1.0, rate_idr=1.0)]
    result = check_snapshot(make_snapshot(fx=doubled))

    assert any("Duplicate currency" in e for e in result["errors"])


def test_inverted_fx_rate_warns():
    """USD/IDR reported as 0.000056 instead of ~17800 would zero the holding."""
    inverted = fx_positions()
    inverted[0] = FxPosition(
        currency=inverted[0].currency, amount=inverted[0].amount, rate_idr=0.000056
    )
    result = check_snapshot(make_snapshot(fx=inverted))

    assert result["errors"] == []
    assert any("looks inverted" in w for w in result["warnings"])


def test_zero_balance_warns():
    banks = [BankBalance(account=a, value_idr=0) for a in BANK_ACCOUNTS]
    result = check_snapshot(make_snapshot(banks=banks))
    assert any("Zero balance" in w for w in result["warnings"])


# ── Schema ───────────────────────────────────────────────────────────────────

def test_unknown_ticker_is_rejected_by_the_schema():
    with pytest.raises(Exception):
        StockHolding(ticker="TSLA", lots=1, price_idr=1, avg_idr=1)


def test_malformed_date_is_rejected():
    with pytest.raises(Exception):
        make_snapshot(date="14/08/2026")


# ── Observations: the tool-facing input ──────────────────────────────────────

def test_observations_carry_no_market_data():
    """Prices, rates and the date are the server's job, not the model's."""
    fields = set(Observations.model_fields)

    assert {"banks", "ajaib_usd", "stocks", "fx"} <= fields
    # There is still no way to hand us an ETF price or a share count.
    assert "etfs" not in fields
    # FX is a balance, never a rate — the rate stays server-resolved.
    assert set(FxBalance.model_fields) == {"currency", "amount"}
    for optional in ("date", "fx", "etf_price_overrides", "fx_rate_overrides"):
        assert Observations.model_fields[optional].is_required() is False


def test_observations_default_to_no_overrides_and_no_date():
    obs = make_observations()
    assert obs.date is None
    assert obs.etf_price_overrides == []
    assert obs.fx_rate_overrides == []


def test_an_unknown_currency_is_accepted_unlike_an_unknown_ticker():
    """The asymmetry is the point: FX is open, ETFs and stocks are closed."""
    assert FxBalance(currency="KRW", amount=1.0).currency == "KRW"
    assert FxBalance(currency="krw", amount=1.0).currency == "KRW"  # normalised

    with pytest.raises(Exception):
        EtfHolding(ticker="ARKK", price_usd=1.0)


def test_idr_is_rejected_as_a_currency():
    """An IDR row would double-count a cash balance at a rate of 1."""
    with pytest.raises(Exception):
        FxBalance(currency="IDR", amount=1.0)


def test_a_malformed_currency_code_is_rejected():
    for bad in ("US", "USDD", "US1", "", "CURRENCY:USDIDR"):
        with pytest.raises(Exception):
            FxBalance(currency=bad, amount=1.0)


def test_observations_reject_a_malformed_date():
    with pytest.raises(Exception):
        make_observations(date="14/08/2026")
