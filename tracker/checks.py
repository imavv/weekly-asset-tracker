"""Validation — ported and narrowed from `portfolio_tracker.check_rows`.

Three of the original checks are gone because the semantic schema makes their
failure modes unreachable:

  * "expected N rows, got M"         -> rows are built from the roster
  * "row N: expected account X"      -> ditto
  * "column layout looks shifted"    -> the server writes the columns

What remains is about *data quality*, which no schema can enforce: did we
actually receive every account, and do the numbers look sane?

FX is the one roster that is not checked for completeness, because it no longer
has a fixed membership — the screenshot decides it. What FX gets instead is a
set of warnings describing how the week's roster differs from the last known
one, so that a misread screen and a real change do not look alike.

`errors` block a write. `warnings` are advisory and surface in the preview.
"""

from __future__ import annotations

from .config import (
    BANK_ACCOUNTS,
    ETF_TICKERS,
    PRICE_DEVIATION_THRESHOLD,
    STOCK_TICKERS,
)
from .holdings import load_fx_holdings, load_holdings
from .models import Snapshot


def check_snapshot(snap: Snapshot, fx_expected=None) -> dict[str, list[str]]:
    """Return {"errors": [...], "warnings": [...]} for a semantic snapshot.

    `fx_expected` is the FX roster the server intended to write. It differs
    from the snapshot's own when a rate could not be resolved, and passing it
    keeps such a currency from being reported as dropped when it is only
    unpriced — a separate error already covers that case.
    """
    errors: list[str] = []
    warnings: list[str] = []

    # ── Completeness: every FIXED roster member reported exactly once ────
    # FX is deliberately absent from this loop. Its roster is the screenshot's,
    # so "missing" is not a defect there — it is the user telling us a currency
    # is gone. The fixed rosters still have to arrive whole, because nothing
    # else reveals that a screenshot was skipped.
    for label, expected, got in (
        ("bank account", set(BANK_ACCOUNTS), [b.account for b in snap.banks]),
        ("stock", set(STOCK_TICKERS), [s.ticker for s in snap.stocks]),
        ("ETF", set(ETF_TICKERS), [e.ticker for e in snap.etfs]),
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

    fx_codes = [f.currency for f in snap.fx]
    fx_duplicates = {c for c in fx_codes if fx_codes.count(c) > 1}
    if fx_duplicates:
        errors.append(
            f"Duplicate currency(s): {', '.join(sorted(fx_duplicates))}. "
            "Report each currency once, with its total balance."
        )

    # ── Holdings file must cover every ETF, or quantities silently zero ──
    # FX is not checked here any more: a currency absent from the file is a
    # new one, which is now expected rather than an error.
    holdings = load_holdings()
    if not holdings:
        errors.append(
            "holdings.json is missing or empty — ETF quantities would all be 0."
        )
    else:
        unlisted = [t for t in ETF_TICKERS if t not in holdings]
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

    warnings.extend(_fx_roster_warnings(snap, fx_expected))
    return {"errors": errors, "warnings": warnings}


def _fx_roster_warnings(snap: Snapshot, expected=None) -> list[str]:
    """Advisory notes on how this week's FX roster differs from the last one.

    None of these block the write — the screenshot is the source of truth, so a
    difference is a fact, not a fault. They exist because the user's confirm is
    the only thing standing between a misread screen and the sheet, and a
    misread looks exactly like a legitimate change unless it is spelled out.
    """
    stored = load_fx_holdings()
    expected = set(expected) if expected is not None else {f.currency for f in snap.fx}
    notes: list[str] = []

    if not snap.fx and not expected:
        return ["No FX rows at all this week — every currency row is absent."]

    if all(not f.from_screenshot for f in snap.fx):
        notes.append(
            "No multi-currency screenshot was supplied, so every FX amount fell "
            "back to holdings.json. These are last-known balances, not this "
            "week's — confirm they are still right, or supply the screenshot."
        )
        return notes

    for f in snap.fx:
        previous = (stored.get(f.currency) or {}).get("qty")
        if f.amount == 0:
            notes.append(f"{f.currency}: amount is 0 — the row will value at 0.")
        if previous is None:
            notes.append(
                f"{f.currency}: new currency, no entry in holdings.json. Its "
                "row is written from the screenshot, but it has no cost basis, "
                "so the change columns stay blank. Add it to holdings.json to "
                "track one."
            )
        elif abs(previous - f.amount) > 0.005:
            delta = f.amount - previous
            notes.append(
                f"{f.currency}: screenshot says {f.amount:,.2f}, holdings.json "
                f"says {previous:,.2f} ({delta:+,.2f}). Writing the screenshot "
                "figure — check it is not a misread, then update holdings.json."
            )

    dropped = [c for c in stored if c not in expected]
    if dropped:
        listed = ", ".join(
            f"{c} ({(stored.get(c) or {}).get('qty', '?')})" for c in dropped
        )
        notes.append(
            f"DROPPED from this week's block: {listed}. These are in "
            "holdings.json but were not on the screenshot, so they get no row "
            "at all — their value disappears from this week's FX total. If that "
            "is wrong, the currency was probably just off-screen."
        )
    return notes


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
