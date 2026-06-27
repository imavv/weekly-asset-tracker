#!/usr/bin/env python3
"""
portfolio_tracker.py
────────────────────
Full pipeline:
  1. Accept screenshot paths from CLI
  2. Base64-encode images
  3. Call Claude API (vision) with SKILL.md as system prompt
  4. Parse Claude's tab-separated output into a 2D array
  5. POST the array to your GAS Web App endpoint

Usage:
  python portfolio_tracker.py --date 2026-06-16 --start-row 1585 \
    screenshots/mandiri.jpg screenshots/bca.jpg screenshots/seabank.jpg ...

Requirements:
  pip install anthropic requests
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import re
import sys
from pathlib import Path

import anthropic
import requests
import os

from dotenv import load_dotenv
# override=True so values in .env win over empty/stale vars already present in
# the shell environment (e.g. an inherited empty ANTHROPIC_API_KEY).
load_dotenv(override=True)


# ── CONFIG ────────────────────────────────────────────────────────────────────
# All three are read from the environment (Railway variables / local .env).
# Defaults preserve the original hard-coded values for backwards compatibility.
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GAS_ENDPOINT      = os.environ.get(
    "GAS_ENDPOINT",
    "https://script.google.com/macros/s/AKfycbzopTrglyn2mxMoBHVLVE6l5x17vLRJcuAkzOdjHJwI5KmqoXQTVMlj1B3nddTOM1fy/exec",
)
GAS_SECRET_TOKEN  = os.environ.get("GAS_SECRET_TOKEN", "REPLACE_WITH_YOUR_SECRET")  # must match GAS script

SKILL_MD_PATH = Path(__file__).parent / "SKILL.md"      # put your SKILL.md next to this file
HOLDINGS_PATH = Path(__file__).parent / "holdings.json"  # static share counts (col F) + optional avg (col H)
LOG_DIR       = Path(__file__).parent / "logs"           # per-run audit logs, indexed by timestamp

NUM_COLS = 11   # A–K
MIN_TABLE_ROWS = 20   # a real table block must have at least this many contiguous
                      # rows. Set below the current 23-row roster on purpose, so
                      # adding a few new asset types later doesn't break selection.

# ── MODEL SELECTION ───────────────────────────────────────────────────────────
# Switch models to compare quality vs cost. Pick via:
#   - MODEL env var  (Railway / .env), or
#   - --model CLI flag,
# using a short alias ("sonnet" | "opus" | "haiku") or a full model id.
# Default is sonnet. Prices are USD per 1M tokens (input / output) for the
# cost estimate only — keep them roughly in sync with current pricing.
DEFAULT_MODEL = "haiku"
MODEL_REGISTRY = {
    "sonnet": {"id": "claude-sonnet-4-6", "in": 3.0,  "out": 15.0},
    "opus":   {"id": "claude-opus-4-1",   "in": 15.0, "out": 75.0},
    "haiku":  {"id": "claude-haiku-4-5",  "in": 1.0,  "out": 5.0},
}


def resolve_model(name: str | None = None) -> dict:
    """Resolve a model alias / id into {id, in, out}.

    Precedence: explicit `name` arg > MODEL env var > DEFAULT_MODEL.
    A short alias maps through MODEL_REGISTRY; anything else is treated as a
    literal model id (pricing falls back to sonnet rates for the estimate).
    """
    name = (name or os.environ.get("MODEL") or DEFAULT_MODEL).strip()
    if name in MODEL_REGISTRY:
        return {"name": name, **MODEL_REGISTRY[name]}
    # Unknown -> literal model id, estimate with sonnet pricing as a placeholder.
    return {"name": name, "id": name, "in": 3.0, "out": 15.0}
# ──────────────────────────────────────────────────────────────────────────────


def load_skill_md() -> str:
    """Load the SKILL.md system prompt."""
    if not SKILL_MD_PATH.exists():
        raise FileNotFoundError(f"SKILL.md not found at {SKILL_MD_PATH}")
    return SKILL_MD_PATH.read_text(encoding="utf-8")


def encode_images(paths: list[str]) -> list[dict]:
    """Return a list of Claude API image content blocks."""
    blocks = []
    for p in paths:
        path = Path(p)
        if not path.exists():
            raise FileNotFoundError(f"Screenshot not found: {p}")

        suffix = path.suffix.lower()
        media_type_map = {
            ".jpg":  "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png":  "image/png",
            ".webp": "image/webp",
        }
        media_type = media_type_map.get(suffix)
        if not media_type:
            raise ValueError(f"Unsupported image type: {suffix}")

        data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": data,
            }
        })
        print(f"  [✓] Encoded {path.name} ({len(data) // 1024} KB base64)")

    return blocks


def call_claude(image_blocks: list[dict], date: str, start_row: int,
                model: str | None = None, return_debug: bool = False):
    """Send images to Claude and return the raw text response.

    Web search is enabled as a SERVER-side tool. Anthropic executes the
    searches internally and loops on its own — this remains a single
    messages.create() call. We just have to extract the text block(s) from
    a response whose content may also contain search-related blocks.
    """
    system_prompt = load_skill_md()

    user_text = (
        f"Fill in this week's portfolio tracker.\n"
        f"Date: {date}\n"
        f"Start row: {start_row}\n\n"
        f"PRICE SOURCING — read carefully:\n"
        f"- IDX stock prices (BBCA, ICBP, BBRI): READ FROM THE ATTACHED BROKER SCREENSHOT. "
        f"Do NOT web-search these.\n"
        f"- US ETF prices (VOO, VT, VTI, SPYM, GDX, VEA, SMH, GLD, IGV, XLP, XLE): "
        f"look up via web search, ONE source only — prefer Google Finance. "
        f"Use the search query format `{{TICKER}} stock price google finance`.\n"
        f"- All cash/deposit/bond balances and the BNI/Ajaib values are in the attached screenshots.\n"
        f"- ETF quantity (col F) and average cost (col H): leave these as 0. A downstream "
        f"script injects them from a static holdings file — do NOT try to guess or carry them forward.\n"
        f"- IDX stock quantity (col F): output the RAW lot count exactly as shown in the broker "
        f"screenshot (e.g. 44). Do NOT multiply by 100 — a downstream script converts lots to shares.\n"
        f"- Column E (Key) and column K (USD/IDR): leave column E blank and you may put the "
        f"GOOGLEFINANCE formula in K — a downstream script overwrites both, so don't worry about them.\n\n"
        f"OUTPUT CONTRACT — follow exactly, this is consumed by a script:\n"
        f"- Output ALL 23 rows, in the exact roster order, ALWAYS. Never drop a row.\n"
        f"- If a screenshot for an account is missing or a value can't be read, "
        f"STILL output that row and put 0 in the value/price cell. Do NOT skip it, "
        f"do NOT stall, do NOT ask for more screenshots.\n"
        f"- EXACTLY 11 tab-separated values per row, no more, no less.\n"
        f"- Output ONLY the raw tab-separated table. No header row, no markdown, "
        f"no code fences (```), no summary table, no commentary or narration before "
        f"or after the table.\n"
        f"- Empty cells are empty strings (just a tab), not extra blank tabs.\n"
        f"- The ONLY thing allowed after the table is optional note lines, each "
        f"prefixed with '# ', listing which values were set to 0 because data was missing.\n"
        f"Begin the table on the very first line of your response now."
    )

    # Combine image blocks + text block
    content = image_blocks + [{"type": "text", "text": user_text}]

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    spec = resolve_model(model)
    print(f"\n[→] Calling Claude API — model={spec['name']} ({spec['id']}) "
          f"(web search enabled, single-source ETFs)...")
    message = client.messages.create(
        model=spec["id"],
        max_tokens=8192,
        system=system_prompt,
        messages=[{"role": "user", "content": content}],
        tools=[{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 15,   # hard ceiling: 11 ETFs single-source + margin
        }],
    )

    # ── Extract text blocks (response may contain search blocks too) ──────
    text_parts = [
        block.text for block in message.content
        if getattr(block, "type", None) == "text"
    ]
    raw = "\n".join(text_parts).strip()

    # ── Log usage so you can see exactly what you were billed for ─────────
    u = message.usage
    print(f"[✓] Claude responded ({len(raw)} chars)")
    print(f"    Input tokens:  {u.input_tokens:,}")
    print(f"    Output tokens: {u.output_tokens:,}")
    searches = 0
    if getattr(u, "server_tool_use", None):
        searches = getattr(u.server_tool_use, "web_search_requests", 0) or 0
        print(f"    Web searches:  {searches}")

    # Rough cost estimate using the selected model's pricing; search $0.01 each.
    est = (u.input_tokens / 1e6 * spec["in"]) + (u.output_tokens / 1e6 * spec["out"]) + (searches * 0.01)
    print(f"    Est. cost:     ${est:.4f}  (model={spec['name']})")

    if return_debug:
        # Full, serialized response: every content block (incl. web_search
        # queries + returned snippets) and usage — for run-log troubleshooting.
        def _dump(obj):
            for attr in ("model_dump", "dict"):
                fn = getattr(obj, attr, None)
                if fn:
                    try:
                        return fn()
                    except Exception:  # noqa: BLE001
                        pass
            return str(obj)

        debug = {
            "model_id": spec["id"],
            "usage": _dump(u),
            "stop_reason": getattr(message, "stop_reason", None),
            "content_blocks": [_dump(b) for b in message.content],
        }
        return raw, debug

    return raw


def parse_table(raw: str, start_row: int) -> tuple[list[list], list[str]]:
    """
    Parse Claude's tab-separated output into a 2D array suitable for Sheets.

    Rules from SKILL.md:
    - No header row
    - 11 columns (A–K), tab-separated
    - Formula strings start with '=' — keep as-is; Sheets will evaluate them
    - Blank cells are empty strings ""
    - Numeric cells (balances, prices, quantities) are cast to float/int
    """
    # A "table line" is a non-blank, non-'#' line that contains a tab.
    def _is_table_line(l: str) -> bool:
        return bool(l.strip()) and not l.lstrip().startswith("#") and "\t" in l

    raw_lines = raw.strip().splitlines()

    # Claude can emit several DRAFT tables in one response (it "thinks out loud"),
    # and the API may return multiple text blocks that get joined here — so the
    # raw text can contain many tab-separated lines, not just the final table.
    # Segment the text into contiguous runs of table lines, then take the LAST
    # run that has >= MIN_TABLE_ROWS rows: that's the final, complete table.
    runs: list[tuple[list[str], int]] = []   # (rows, index of first line AFTER run)
    cur: list[str] = []
    for idx, l in enumerate(raw_lines):
        if _is_table_line(l):
            cur.append(l)
        elif cur:
            runs.append((cur, idx))
            cur = []
    if cur:
        runs.append((cur, len(raw_lines)))

    qualifying = [(rows, end) for rows, end in runs if len(rows) >= MIN_TABLE_ROWS]
    if qualifying:
        table_lines, block_end = qualifying[-1]      # last complete block
    elif runs:
        table_lines, block_end = max(runs, key=lambda r: len(r[0]))  # best effort
    else:
        table_lines, block_end = [], len(raw_lines)

    if len(runs) > 1 or (runs and len(table_lines) != sum(len(r) for r, _ in runs)):
        dropped = sum(len(r) for r, _ in runs) - len(table_lines)
        print(f"[!] Found {len(runs)} table block(s) in Claude's output; kept the "
              f"last block of {len(table_lines)} rows, discarded {dropped} draft/"
              f"stray row(s).")

    # Notes = '# ' lines after the chosen block (per SKILL, the only legit
    # trailing content — e.g. "# VT set to 0"). Scratch-work earlier is ignored.
    notes = [l.strip() for l in raw_lines[block_end:] if l.lstrip().startswith("#")]
    if notes:
        print("\n[!] Claude attached notes:")
        for n in notes:
            print(f"      {n}")

    rows = []
    for i, line in enumerate(table_lines):
        cells = line.split("\t")

        # Collapse runs of empty cells beyond col 11 — Claude sometimes adds
        # extra blank tabs, pushing later values (e.g. GOOGLEFINANCE) too far right.
        # Strategy: keep the last non-empty cell if it belongs in col K (index 10).
        if len(cells) > NUM_COLS:
            overflow = cells[NUM_COLS:]
            non_empty = [c.strip() for c in overflow if c.strip()]
            cells = cells[:NUM_COLS]
            if non_empty and not cells[10].strip():
                cells[10] = non_empty[-1]

        # Pad if too short
        if len(cells) < NUM_COLS:
            cells += [""] * (NUM_COLS - len(cells))

        typed_cells = []
        for col_idx, cell in enumerate(cells):
            cell = cell.strip()

            if cell == "":
                typed_cells.append("")

            elif cell.startswith("="):
                # Formula — keep as string; GAS will use USER_ENTERED input option
                typed_cells.append(cell)

            elif col_idx == 3:
                # Column D (Value) — could be a formula or a plain integer
                # If we reach here it's a plain number (Cash/Deposit/MF Bonds)
                try:
                    typed_cells.append(int(float(cell.replace(",", ""))))
                except ValueError:
                    typed_cells.append(cell)  # fallback: keep as string

            elif col_idx in (5, 6, 7):
                # F (Qty), G (Share price), H (Avg price) — numeric
                try:
                    val = float(cell.replace(",", ""))
                    # G and H: keep as float (prices); F (qty): integer
                    typed_cells.append(int(val) if col_idx == 5 else val)
                except ValueError:
                    typed_cells.append(cell)

            else:
                typed_cells.append(cell)

        rows.append(typed_cells)

    print(f"[✓] Parsed {len(rows)} rows from Claude's output")
    return rows, notes


def load_holdings() -> dict:
    """Load the static holdings file (ticker -> {qty, avg}).

    Returns {} if the file is absent so the pipeline still runs (qty/avg just
    stay as whatever Claude produced).
    """
    if not HOLDINGS_PATH.exists():
        print(f"[!] No holdings file at {HOLDINGS_PATH} — skipping qty/avg injection")
        return {}
    return json.loads(HOLDINGS_PATH.read_text(encoding="utf-8"))


def apply_holdings(rows: list[list]) -> list[list]:
    """Overwrite column F (qty) and H (avg) from the static holdings file.

    Matches on column C (account/ticker). Only entries present in the file are
    touched, so cash/deposit rows and any unlisted ticker pass through unchanged.
    Sheets does the actual D = F*G*$K multiplication — we only supply F (and H).
    """
    holdings = load_holdings()
    if not holdings:
        return rows

    applied = []
    for row in rows:
        ticker = str(row[2]).strip()        # column C
        h = holdings.get(ticker)
        if isinstance(h, dict):             # skips "_comment" and missing tickers
            if h.get("qty") is not None:
                row[5] = h["qty"]           # column F
            if h.get("avg") is not None:
                row[7] = h["avg"]           # column H
            applied.append(ticker)

    if applied:
        print(f"[✓] Injected qty/avg from holdings.json for: {', '.join(applied)}")
    return rows


EXPECTED_ACCOUNTS = [
    "Mandiri","BCA","Seabank","Others","Superbank","Superbank Deposit",
    "Bibit","BNI (RDN)","BBCA","ICBP","BBRI","Ajaib",
    "VOO","VT","VTI","SPYM","GDX","VEA","SMH","GLD","IGV","XLP","XLE",
]


def check_rows(rows: list[list], start_row: int) -> dict:
    """Sanity-check parsed rows WITHOUT raising.

    Returns {"errors": [...], "warnings": [...]} as lists of human-readable
    strings. `errors` are structural problems (wrong row count / order / missing
    FX anchor); `warnings` are advisory price-sanity flags. Nothing here blocks —
    the bot surfaces these for the user to review before /confirm.
    """
    errors: list[str] = []
    warnings: list[str] = []

    # ── Row count ───────────────────────────────────────────────────────
    if len(rows) != len(EXPECTED_ACCOUNTS):
        errors.append(
            f"Expected {len(EXPECTED_ACCOUNTS)} rows, got {len(rows)} — "
            "missing/extra/duplicated rows in Claude's output."
        )

    # ── Account order (over the overlap, so it still helps on count drift) ─
    for i, (row, expected) in enumerate(zip(rows, EXPECTED_ACCOUNTS)):
        account = row[2] if len(row) > 2 else ""
        if account != expected:
            errors.append(f"Row {start_row + i}: expected account '{expected}', got '{account}'.")

    # ── FX anchor on first row (Mandiri, col K): locked value or formula ─
    k0 = str(rows[0][10]) if (rows and len(rows[0]) > 10) else ""
    fx_ok = k0.startswith("=GOOGLEFINANCE")
    if not fx_ok:
        try:
            fx_ok = float(k0) > 0  # locked static rate
        except ValueError:
            fx_ok = False
    if not fx_ok:
        errors.append("Row 0 (Mandiri) has no USD/IDR anchor in column K (locked value or GOOGLEFINANCE formula).")

    # ── Column-layout sanity for Stock/ETF rows ─────────────────────────
    # A correctly-shaped Stock/ETF row has a FORMULA in D (Value, =F*G...) and
    # in I (Pct change, =(G-H)/H). If those aren't formulas, Claude emitted the
    # columns in the wrong order/count and the row is shifted — catch it here.
    for i, row in enumerate(rows):
        category = row[1] if len(row) > 1 else ""
        if category not in ("Stock", "ETF"):
            continue
        name = row[2] if len(row) > 2 else "?"
        d_val = str(row[3]) if len(row) > 3 else ""
        i_val = str(row[8]) if len(row) > 8 else ""
        if not d_val.startswith("=") or not i_val.startswith("="):
            errors.append(
                f"Row {start_row + i} ({name}): column layout looks shifted "
                f"(Value/Pct aren't formulas) — Claude likely emitted the wrong columns. DO NOT write."
            )

    # ── Soft price sanity (advisory): this week's price (G) vs avg (H) ──
    THRESHOLD = 0.30  # 30%
    for row in rows:
        category = row[1] if len(row) > 1 else ""
        if category not in ("Stock", "ETF"):
            continue
        name = row[2] if len(row) > 2 else "?"
        try:
            g = float(row[6])
            h = float(row[7])
            if h and abs(g - h) / h > THRESHOLD:
                pct = (g - h) / h * 100
                warnings.append(f"{name} price {g} vs avg {h} ({pct:+.0f}% — verify not stale/wrong)")
        except (ValueError, TypeError, IndexError):
            warnings.append(f"{name} non-numeric price/avg — check Claude's output")

    return {"errors": errors, "warnings": warnings}


FX_FORMULA = '=GOOGLEFINANCE("CURRENCY:USDIDR")'


def today_wib() -> str:
    """Today's date (YYYY-MM-DD) in WIB / UTC+7, regardless of server timezone."""
    wib = datetime.timezone(datetime.timedelta(hours=7))
    return datetime.datetime.now(wib).date().isoformat()


def multiply_stock_lots(rows: list[list]) -> list[list]:
    """Column F for IDX Stock rows: convert raw lots → shares (×100).

    Claude reads the raw lot count from the broker screenshot; 1 lot = 100
    shares on IDX, so we scale here (single source of truth — the prompt tells
    Claude NOT to multiply).
    """
    scaled = []
    for row in rows:
        category = row[1] if len(row) > 1 else ""
        if category == "Stock":
            try:
                row[5] = int(float(str(row[5]).replace(",", ""))) * 100  # col F
                scaled.append(row[2] if len(row) > 2 else "?")
            except (ValueError, TypeError):
                pass  # blank/non-numeric — leave as-is
    if scaled:
        print(f"[✓] Stock lots ×100 → shares for: {', '.join(scaled)}")
    return rows


def apply_key_formula(rows: list[list], start_row: int) -> list[list]:
    """Column E (Key): write =CONCATENATE(A{r},"-",B{r}) for every row, using
    the absolute sheet row number. Overrides whatever Claude put there."""
    for i, row in enumerate(rows):
        r = start_row + i
        row[4] = f'=CONCATENATE(A{r},"-",B{r})'  # col E
    return rows


def fetch_usdidr() -> float | None:
    """Fetch the current USD→IDR rate from a free FX API. Returns None on any
    failure so the caller can fall back to the GOOGLEFINANCE formula."""
    try:
        resp = requests.get("https://open.er-api.com/v6/latest/USD", timeout=10)
        rate = resp.json().get("rates", {}).get("IDR")
        if rate and float(rate) > 0:
            return float(rate)
        print(f"[!] USD/IDR fetch returned no usable rate: {resp.text[:200]}")
    except Exception as e:  # noqa: BLE001
        print(f"[!] USD/IDR fetch failed: {e}")
    return None


def apply_fx_anchor(rows: list[list]) -> list[list]:
    """Column K on the first row (Mandiri): LOCK the USD/IDR rate as a static
    value (preserves the rate at this date). Falls back to the live
    GOOGLEFINANCE formula if the fetch fails."""
    if not rows:
        return rows
    rate = fetch_usdidr()
    if rate is not None:
        rows[0][10] = round(rate, 2)  # col K — static value
        print(f"[✓] Locked USD/IDR = {rows[0][10]} (static value)")
    else:
        rows[0][10] = FX_FORMULA
        print("[!] Fell back to GOOGLEFINANCE formula for USD/IDR (not locked).")
    return rows


def post_process_rows(rows: list[list], start_row: int) -> list[list]:
    """Apply all deterministic post-parse transforms, in order:
    holdings (ETF F/H) → stock lots ×100 → key formula (E) → FX anchor (K)."""
    rows = apply_holdings(rows)
    rows = multiply_stock_lots(rows)
    rows = apply_key_formula(rows, start_row)
    rows = apply_fx_anchor(rows)
    return rows


def validate_rows(rows: list[list], start_row: int):
    """Hard-validation wrapper (CLI / tests): prints warnings, raises on the
    first structural error. The bot uses check_rows() directly instead."""
    res = check_rows(rows, start_row)
    if res["warnings"]:
        print("\n[!] Price sanity check — review these (NOT blocking):")
        for w in res["warnings"]:
            print(f"      {w}")
    if res["errors"]:
        raise ValueError(res["errors"][0])
    print(f"\n[✓] Validation passed — {len(rows)} rows, correct order")


def fetch_start_row() -> int:
    """GET the next empty row from GAS (first blank cell in column A)."""
    print("[→] Fetching start_row from GAS...")
    resp = requests.get(
        GAS_ENDPOINT,
        params={"token": GAS_SECRET_TOKEN},
        timeout=15,
        allow_redirects=True,
    )
    try:
        result = resp.json()
    except Exception:
        raise RuntimeError(f"GAS GET returned non-JSON (HTTP {resp.status_code}): {resp.text[:300]}")
    if result.get("status") != 200:
        raise RuntimeError(f"Could not fetch start_row: {result}")
    row = result["start_row"]
    print(f"[✓] start_row = {row}")
    return row


def fetch_summary() -> dict:
    """GET the two summary tables from GAS (action=summary).

    Returns {"trend": [[...]], "breakdown": [[...]]} where each is a 2D array
    of DISPLAY values (formatting preserved exactly as shown in the sheet).
    Raises RuntimeError on failure.
    """
    print("[→] Fetching summary tables from GAS...")
    # Apps Script web apps can cold-start (multi-second, occasionally >20s right
    # after a deploy). Use a generous timeout and retry once before giving up.
    last_err = None
    for attempt in (1, 2):
        try:
            resp = requests.get(
                GAS_ENDPOINT,
                params={"token": GAS_SECRET_TOKEN, "action": "summary"},
                timeout=45,
                allow_redirects=True,
            )
            result = resp.json()
            if result.get("status") != 200:
                raise RuntimeError(f"GAS summary error: {result}")
            return {"trend": result["trend"], "breakdown": result["breakdown"]}
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = e
            print(f"[!] Summary fetch attempt {attempt} timed out/failed; retrying..." if attempt == 1
                  else f"[!] Summary fetch attempt {attempt} failed.")
        except ValueError:  # non-JSON body
            raise RuntimeError(f"GAS summary returned non-JSON (HTTP {resp.status_code}): {resp.text[:300]}")
    raise RuntimeError(f"GAS summary unreachable after 2 attempts: {last_err}")


def post_to_sheets(rows: list[list], start_row: int) -> dict:
    """POST the 2D array to the GAS Web App endpoint."""
    payload = {
        "token":     GAS_SECRET_TOKEN,
        "start_row": start_row,
        "rows":      rows,
    }

    print(f"\n[→] POSTing to GAS endpoint (start_row={start_row})...")
    response = requests.post(
        GAS_ENDPOINT,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=30,
    )

    result = response.json()
    if result.get("status") == 200:
        print(f"[✓] Sheets updated: {result['message']}")
    else:
        print(f"[✗] GAS returned error: {result}")

    return result


def format_preview(rows: list[list], start_row: int) -> str:
    """Return a human-readable preview string (compact, one line per row).

    Used both for the CLI print and the Telegram message, so it stays plain
    text with no terminal-only formatting.
    """
    lines = [f"PREVIEW — {len(rows)} rows starting at row {start_row}", ""]
    for i, row in enumerate(rows):
        date, cat, account, value = row[0], row[1], row[2], row[3]
        qty, price = row[5], row[6]
        # Show the most useful fields per row: account, category, value/price.
        detail = f"value={value}" if str(value) else ""
        if str(qty) and str(qty) != "0":
            detail = f"qty={qty} @ {price}"
        lines.append(f"  {start_row + i:>5}  {str(account):<16} {str(cat):<6} {detail}")
    return "\n".join(lines)


def preview_table(rows: list[list], start_row: int):
    """Print a human-readable preview before writing."""
    print(f"\n{'─'*60}")
    print(format_preview(rows, start_row))
    print(f"{'─'*60}\n")


def log_run(raw_output: str, rows: list[list], meta: dict,
            debug: dict | None = None) -> Path:
    """Persist one Claude run to logs/<timestamp>/ for later assessment.

    Writes:
      raw.txt       — Claude's raw text response (exactly what we parsed)
      rows.json     — the parsed + holdings-injected 2D array
      meta.json     — model, date, start_row, row count, issues, notes, usage
      response.json — FULL serialized API response: every content block,
                      including web_search queries + returned snippets (for
                      troubleshooting why a price came back wrong/missing)

    Returns the run directory path.
    """
    LOG_DIR.mkdir(exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = LOG_DIR / ts
    # Avoid clobbering if two runs land in the same second.
    n = 1
    while run_dir.exists():
        run_dir = LOG_DIR / f"{ts}_{n}"
        n += 1
    run_dir.mkdir(parents=True)

    (run_dir / "raw.txt").write_text(raw_output, encoding="utf-8")
    (run_dir / "rows.json").write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    if debug is not None:
        (run_dir / "response.json").write_text(json.dumps(debug, indent=2, default=str), encoding="utf-8")
    return run_dir


def parse_screenshots(photo_paths: list[str], date: str,
                      start_row: int | None = None,
                      model: str | None = None) -> dict:
    """Parse-only phase (the /run step in the bot).

    Encodes screenshots, calls Claude, parses the table, injects static holdings,
    and runs NON-BLOCKING checks. Persists every run to logs/<timestamp>/.
    Does NOT write to Sheets and does NOT raise on validation problems — instead
    it returns any issues for the user to review before /confirm.

    Returns a dict:
      {
        "rows":      [...],          # parsed 2D array (may be off-spec)
        "start_row": int,            # row the write would begin at
        "preview":   str,            # human-readable preview
        "notes":     [str, ...],     # '# ' notes Claude attached
        "errors":    [str, ...],     # structural issues (count/order/FX)
        "warnings":  [str, ...],     # advisory price-sanity flags
        "log_dir":   str,            # where this run was archived
      }
    """
    if start_row is None:
        start_row = fetch_start_row()

    spec = resolve_model(model)
    image_blocks = encode_images(photo_paths)
    raw_output, debug = call_claude(image_blocks, date, start_row, model=model, return_debug=True)
    rows, notes = parse_table(raw_output, start_row)
    rows = post_process_rows(rows, start_row)

    checks = check_rows(rows, start_row)  # non-raising

    meta = {
        "timestamp":     datetime.datetime.now().isoformat(timespec="seconds"),
        "model":         spec["name"],
        "model_id":      spec["id"],
        "date":          date,
        "start_row":     start_row,
        "num_rows":      len(rows),
        "expected_rows": len(EXPECTED_ACCOUNTS),
        "errors":        checks["errors"],
        "warnings":      checks["warnings"],
        "notes":         notes,
    }
    run_dir = log_run(raw_output, rows, meta, debug=debug)
    print(f"[✓] Archived run to {run_dir}")
    if checks["errors"]:
        print(f"[!] {len(checks['errors'])} issue(s) flagged (non-blocking):")
        for e in checks["errors"]:
            print(f"      {e}")

    return {
        "rows":      rows,
        "start_row": start_row,
        "preview":   format_preview(rows, start_row),
        "notes":     notes,
        "errors":    checks["errors"],
        "warnings":  checks["warnings"],
        "log_dir":   str(run_dir),
    }


def write_rows(rows: list[list], start_row: int) -> dict:
    """Write-only phase (the /confirm step in the bot).

    Writes an ALREADY-PARSED result to Sheets — no Claude call. Returns the
    raw GAS response dict (status 200 on success).
    """
    return post_to_sheets(rows, start_row)


def main():
    parser = argparse.ArgumentParser(description="Portfolio tracker automation")
    parser.add_argument("screenshots", nargs="+", help="Paths to screenshot images")
    parser.add_argument("--date",      default=None,
                        help="EOD date, e.g. 2026-06-16 (default: today in WIB)")
    parser.add_argument("--start-row", type=int, default=None,
                        help="First row to write in Sheets (auto-detected from GAS if omitted)")
    parser.add_argument("--dry-run",   action="store_true", help="Parse and preview only, don't write to Sheets")
    parser.add_argument("--model",     default=None,
                        help="Model alias (sonnet|opus|haiku) or full id. "
                             "Overrides MODEL env var; default sonnet.")
    args = parser.parse_args()

    date = args.date or today_wib()
    start_row = args.start_row if args.start_row is not None else fetch_start_row()

    print(f"\n{'='*60}")
    print(f"  Portfolio Tracker  |  {date}  |  start_row={start_row}")
    print(f"{'='*60}")
    print(f"\n[1/5] Encoding {len(args.screenshots)} screenshot(s)...")
    image_blocks = encode_images(args.screenshots)

    print("\n[2/5] Calling Claude API...")
    raw_output = call_claude(image_blocks, date, start_row, model=args.model)

    print("\n[3/5] Parsing table...")
    rows, _notes = parse_table(raw_output, start_row)
    rows = post_process_rows(rows, start_row)

    print("\n[4/5] Validating rows...")
    try:
        validate_rows(rows, start_row)
    except ValueError as e:
        print(f"\n[✗] Validation failed:\n  {e}")
        print("\n--- Claude's raw output ---")
        print(raw_output)
        sys.exit(1)

    preview_table(rows, start_row)

    if args.dry_run:
        print("[DRY RUN] Skipping Sheets write.")
        return

    print("[5/5] Writing to Google Sheets...")
    result = post_to_sheets(rows, start_row)

    if result.get("status") == 200:
        print("\n[✓] Done. Your tracker is updated.")
    else:
        print("\n[✗] Write failed. Check error above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
