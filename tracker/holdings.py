"""Static ETF share counts and cost basis, bundled with the deployment.

Ported from `portfolio_tracker.apply_holdings`. For ETFs this file is still
authoritative, and it is the reason the model is told to leave ETF quantity
alone: an ETF screenshot shows a price but never a share count, so guessing one
is exactly the kind of deterministic fact that belongs in code, not a prompt.

For FX it is now a **fallback**. The multi-currency screen shows the balance
directly, so the screenshot wins; the `fx` section here supplies the cost basis
(which no screenshot shows) and stands in for the amounts on a week where no
multi-currency screenshot was taken at all.

Editing holdings.json and pushing triggers a Vercel redeploy — that is the
update path after a buy or sell, and the way to keep the FX fallback honest.
"""

from __future__ import annotations

import json
import logging

from .config import HOLDINGS_PATH

log = logging.getLogger(__name__)


def load_sections() -> dict[str, dict[str, dict]]:
    """Return the file's sections as {"etfs": {...}, "fx": {...}}.

    Key order is preserved, which matters for FX: the `fx` section's order is
    the fallback row order on a week with no multi-currency screenshot.

    Returns empty sections if the file is missing so the server still starts;
    callers then surface a validation error rather than writing zeroed
    quantities.
    """
    if not HOLDINGS_PATH.exists():
        log.warning("No holdings file at %s", HOLDINGS_PATH)
        return {"etfs": {}, "fx": {}}

    raw = json.loads(HOLDINGS_PATH.read_text(encoding="utf-8"))

    sections: dict[str, dict[str, dict]] = {"etfs": {}, "fx": {}}
    for section, entries in raw.items():
        if section.startswith("_") or not isinstance(entries, dict):
            continue  # skips "_comment"
        sections[section] = {
            symbol: holding
            for symbol, holding in entries.items()
            if isinstance(holding, dict)
        }
    return sections


def load_holdings() -> dict[str, dict]:
    """Every entry flattened into one {symbol: {"qty", "avg"}} lookup.

    ETF tickers and currency codes never collide, so a flat namespace is safe.
    """
    flat: dict[str, dict] = {}
    for entries in load_sections().values():
        flat.update(entries)
    return flat


def load_fx_holdings() -> dict[str, dict]:
    """Just the `fx` section, in file order — the FX fallback.

    Used for two things: the amounts to fall back to when a week has no
    multi-currency screenshot, and the cost basis (column H) for a currency
    whose amount DID come off a screenshot, since no screenshot shows what you
    paid. A currency the file has never seen simply has no cost basis, and its
    change columns are left blank rather than divided by zero.
    """
    return load_sections().get("fx", {})
