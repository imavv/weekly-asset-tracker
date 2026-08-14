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
    """Return {ticker: {"qty": float, "avg": float | None}}.

    Returns {} if the file is missing so the server still starts; callers then
    surface a validation error rather than writing zeroed quantities.
    """
    if not HOLDINGS_PATH.exists():
        log.warning("No holdings file at %s", HOLDINGS_PATH)
        return {}

    raw = json.loads(HOLDINGS_PATH.read_text(encoding="utf-8"))
    # Skip "_comment" and any other non-dict bookkeeping keys.
    return {k: v for k, v in raw.items() if isinstance(v, dict)}
