"""Validation — ported and narrowed from `portfolio_tracker.check_rows`.

Three of the original checks are gone because the semantic schema makes their
failure modes unreachable:

  * "expected 23 rows, got N"        -> rows are built from ROSTER
  * "row N: expected account X"      -> ditto
  * "column layout looks shifted"    -> the server writes the columns

What remains is about *data quality*, which no schema can enforce: did we
actually receive every account, and do the numbers look sane?

`errors` block a write. `warnings` are advisory and surface in the preview.
"""

from __future__ import annotations

from .config import (
    BANK_ACCOUNTS,
    ETF_TICKERS,
    FX_CURRENCIES,
    PRICE_DEVIATION_THRESHOLD,
    STOCK_TICKERS,
)
from .holdings import load_holdings
from .models import Snapshot


def check_snapshot(snap: Snapshot) -> dict[str, list[str]]:
    """Return {"errors": [...], "warnings": [...]} for a semantic snapshot."""
    errors: list[str] = []
    warnings: list[str] = []

    # ── Completeness: every roster member reported exactly once ──────────
    for label, expected, got in (
        ("bank account", set(BANK_ACCOUNTS), [b.account for b in snap.banks]),
        ("stock", set(STOCK_TICKERS), [s.ticker for s in snap.stocks]),
        ("ETF", set(ETF_TICKERS), [e.ticker for e in snap.etfs]),
        ("currency", set(FX_CURRENCIES), [f.currency for f in snap.fx]),
    ):
        missing = expected - set(got)
        if missing:
            errors.append(
                f"Missing {label}(s): {', '.join(sorted(missing))}. "
                "Every roster entry must be supplied, using 0 if unreadable."
            )
        duplicates = {n for n in got if got.count(n) > 1}
        if duplicates:
            errors.append(f"Duplicate {label}(s): {', '.join(sorted(duplicates))}.")

    # ── Holdings file must cover every ETF, or quantities silently zero ──
    holdings = load_holdings()
    if not holdings:
        errors.append(
            "holdings.json is missing or empty — ETF quantities would all be 0."
        )
    else:
        unlisted = [t for t in (*ETF_TICKERS, *FX_CURRENCIES) if t not in holdings]
        if unlisted:
            errors.append(
                f"holdings.json has no entry for: {', '.join(unlisted)} — "
                "their quantities would be 0."
            )

    # ── Advisory: zero-filled balances ───────────────────────────────────
    zeroed = [b.account for b in snap.banks if b.value_idr == 0]
    if zeroed:
        warnings.append(
            f"Zero balance recorded for: {', '.join(zeroed)} — "
            "confirm the screenshot was present and readable."
        )

    # ── Advisory: price vs cost basis, catches stale or misread prices ───
    for s in snap.stocks:
        if s.price_idr == 0:
            warnings.append(f"{s.ticker}: price is 0 — value will be 0.")
        elif s.avg_idr and abs(s.price_idr - s.avg_idr) / s.avg_idr > PRICE_DEVIATION_THRESHOLD:
            pct = (s.price_idr - s.avg_idr) / s.avg_idr * 100
            warnings.append(
                f"{s.ticker}: price {s.price_idr:,.0f} vs avg {s.avg_idr:,.0f} "
                f"({pct:+.0f}%) — verify it is not stale or misread."
            )

    for e in snap.etfs:
        if e.price_usd == 0:
            warnings.append(f"{e.ticker}: price is 0 — value will be 0.")
            continue
        avg = (holdings.get(e.ticker) or {}).get("avg")
        if avg:
            deviation = abs(e.price_usd - avg) / avg
            if deviation > PRICE_DEVIATION_THRESHOLD:
                pct = (e.price_usd - avg) / avg * 100
                warnings.append(
                    f"{e.ticker}: price {e.price_usd} vs avg {avg} "
                    f"({pct:+.0f}%) — verify it is not stale or misread."
                )

    # ── Advisory: FX rates in a plausible IDR-per-unit range ─────────────
    # The classic mistake is an inverted rate (USD/IDR reported as 0.000056
    # instead of ~17800), which would silently value the holding at nothing.
    for f in snap.fx:
        if f.rate_idr == 0:
            warnings.append(f"{f.currency}: rate is 0 — value will be 0.")
        elif f.rate_idr < 1:
            warnings.append(
                f"{f.currency}: rate {f.rate_idr} looks inverted — column G must "
                "be IDR per one unit of the currency, not the other way round."
            )

    return {"errors": errors, "warnings": warnings}


def format_checks(result: dict[str, list[str]]) -> str:
    """Render check output for chat, or a clean bill of health."""
    parts: list[str] = []
    if result["errors"]:
        parts.append("ERRORS (block the write):")
        parts += [f"  - {e}" for e in result["errors"]]
    if result["warnings"]:
        parts.append("WARNINGS (review, do not block):")
        parts += [f"  - {w}" for w in result["warnings"]]
    return "\n".join(parts) if parts else "All checks passed."
