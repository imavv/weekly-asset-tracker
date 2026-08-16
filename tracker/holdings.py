"""Static ETF share counts and cost basis, bundled with the deployment.

Ported from `portfolio_tracker.apply_holdings`. This file is the reason the
model is told to leave ETF quantity alone: guessing a share count is exactly
the kind of deterministic fact that belongs in code, not in a prompt.

Editing holdings.json and pushing triggers a Vercel redeploy — that is the
update path after a buy or sell.
"""

from __future__ import annotations

import json
import logging

from .config import HOLDINGS_PATH

log = logging.getLogger(__name__)


def load_holdings() -> dict[str, dict]:
    """Return {symbol: {"qty": float, "avg": float | None}}.

    The file is grouped into "etfs" and "fx" sections so it stays readable when
    hand-edited; this flattens them into one lookup. ETF tickers and currency
    codes never collide, so a flat namespace is safe.

    Returns {} if the file is missing so the server still starts; callers then
    surface a validation error rather than writing zeroed quantities.
    """
    if not HOLDINGS_PATH.exists():
        log.warning("No holdings file at %s", HOLDINGS_PATH)
        return {}

    raw = json.loads(HOLDINGS_PATH.read_text(encoding="utf-8"))

    flat: dict[str, dict] = {}
    for section, entries in raw.items():
        if section.startswith("_") or not isinstance(entries, dict):
            continue  # skips "_comment"
        for symbol, holding in entries.items():
            if isinstance(holding, dict):
                flat[symbol] = holding
    return flat
