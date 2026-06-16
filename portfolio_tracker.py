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

import argparse
import base64
import json
import re
import sys
from pathlib import Path

import anthropic
import requests

# ── CONFIG ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GAS_ENDPOINT      = "https://script.google.com/macros/s/AKfycbzopTrglyn2mxMoBHVLVE6l5x17vLRJcuAkzOdjHJwI5KmqoXQTVMlj1B3nddTOM1fy/exec"
GAS_SECRET_TOKEN  = "REPLACE_WITH_YOUR_SECRET"   # must match GAS script

SKILL_MD_PATH = Path(__file__).parent / "SKILL.md"      # put your SKILL.md next to this file
HOLDINGS_PATH = Path(__file__).parent / "holdings.json"  # static share counts (col F) + optional avg (col H)

NUM_COLS = 11   # A–K
# ──────────────────────────────────────────────────────────────────────────────


def load_skill_md() -> str:
    """Load the SKILL.md system prompt."""
    if not SKILL_MD_PATH.exists():
        sys.exit(f"[ERROR] SKILL.md not found at {SKILL_MD_PATH}")
    return SKILL_MD_PATH.read_text(encoding="utf-8")


def encode_images(paths: list[str]) -> list[dict]:
    """Return a list of Claude API image content blocks."""
    blocks = []
    for p in paths:
        path = Path(p)
        if not path.exists():
            sys.exit(f"[ERROR] Screenshot not found: {p}")

        suffix = path.suffix.lower()
        media_type_map = {
            ".jpg":  "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png":  "image/png",
            ".webp": "image/webp",
        }
        media_type = media_type_map.get(suffix)
        if not media_type:
            sys.exit(f"[ERROR] Unsupported image type: {suffix}")

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


def call_claude(image_blocks: list[dict], date: str, start_row: int) -> str:
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
        f"script injects them from a static holdings file — do NOT try to guess or carry them forward.\n\n"
        f"Produce the COMPLETE tab-separated table now. Rules:\n"
        f"- EXACTLY 11 tab-separated values per row, no more, no less\n"
        f"- No header row, no markdown, no code fences\n"
        f"- Empty cells are empty strings (just a tab), not extra blank tabs\n"
        f"- If a value is genuinely missing, use 0 and note it on a line AFTER the table prefixed with '# '\n"
        f"Output the raw table first, then any '# ' notes."
    )

    # Combine image blocks + text block
    content = image_blocks + [{"type": "text", "text": user_text}]

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    print("\n[→] Calling Claude API (web search enabled, single-source ETFs)...")
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
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

    # Rough cost estimate (Sonnet 4.6: $3/MTok in, $15/MTok out; search $0.01 each)
    est = (u.input_tokens / 1e6 * 3) + (u.output_tokens / 1e6 * 15) + (searches * 0.01)
    print(f"    Est. cost:     ${est:.4f}")

    return raw


def parse_table(raw: str, start_row: int) -> list[list]:
    """
    Parse Claude's tab-separated output into a 2D array suitable for Sheets.

    Rules from SKILL.md:
    - No header row
    - 11 columns (A–K), tab-separated
    - Formula strings start with '=' — keep as-is; Sheets will evaluate them
    - Blank cells are empty strings ""
    - Numeric cells (balances, prices, quantities) are cast to float/int
    """
    # Keep only genuine table lines: non-empty, not a '# ' note, and tab-containing
    notes = []
    table_lines = []
    for l in raw.strip().splitlines():
        if not l.strip():
            continue
        if l.lstrip().startswith("#"):
            notes.append(l.strip())
            continue
        if "\t" not in l:        # skip any stray prose Claude added
            notes.append(l.strip())
            continue
        table_lines.append(l)

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
    return rows


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


def validate_rows(rows: list[list], start_row: int):
    """
    Sanity-check the parsed rows before writing.
    Raises ValueError with a descriptive message if something looks wrong.
    """
    expected_accounts = [
        "Mandiri","BCA","Seabank","Others","Superbank","Superbank Deposit",
        "Bibit","BNI (RDN)","BBCA","ICBP","BBRI","Ajaib",
        "VOO","VT","VTI","SPYM","GDX","VEA","SMH","GLD","IGV","XLP","XLE",
    ]

    if len(rows) != len(expected_accounts):
        raise ValueError(
            f"Expected {len(expected_accounts)} rows, got {len(rows)}. "
            "Check Claude's output for missing or extra rows."
        )

    for i, (row, expected) in enumerate(zip(rows, expected_accounts)):
        account = row[2]  # column C
        if account != expected:
            raise ValueError(
                f"Row {start_row + i}: expected account '{expected}', got '{account}'. "
                "Row order mismatch — do not write to Sheets."
            )

    # Check that the first row (Mandiri) has the GOOGLEFINANCE formula in col K
    if not str(rows[0][10]).startswith("=GOOGLEFINANCE"):
        raise ValueError(
            "Row 0 (Mandiri) is missing the GOOGLEFINANCE formula in column K. "
            "FX anchor will be broken."
        )

    # ── Soft price sanity check (non-blocking) ──────────────────────────
    # We don't have last week's price in the script, so this compares this
    # week's price (G, col 6) against the carried-forward avg (H, col 7).
    # A huge gap MIGHT be a real gain/loss, OR a stale/wrong price. We only
    # WARN — we never block — because we can't tell the two apart here.
    # This is the unguarded-single-source risk made visible, for free.
    THRESHOLD = 0.30  # 30%
    warnings = []
    for i, row in enumerate(rows):
        category = row[1]
        if category not in ("Stock", "ETF"):
            continue
        try:
            g = float(row[6])   # this week's price
            h = float(row[7])   # avg price (cost basis)
            if h and abs(g - h) / h > THRESHOLD:
                pct = (g - h) / h * 100
                warnings.append(
                    f"      {row[2]:<6} price {g} vs avg {h}  ({pct:+.0f}% — verify this isn't a stale/wrong price)"
                )
        except (ValueError, TypeError):
            warnings.append(f"      {row[2]:<6} non-numeric price/avg — check Claude's output")

    if warnings:
        print("\n[!] Price sanity check — review these (NOT blocking):")
        print("\n".join(warnings))
        print("    (Large % vs avg can be legit gains, or a bad single-source price. Eyeball them.)")

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
        sys.exit(f"[ERROR] GAS GET returned non-JSON (HTTP {resp.status_code}):\n{resp.text[:500]}")
    if result.get("status") != 200:
        sys.exit(f"[ERROR] Could not fetch start_row: {result}")
    row = result["start_row"]
    print(f"[✓] start_row = {row}")
    return row


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


def preview_table(rows: list[list], start_row: int):
    """Print a human-readable preview before writing."""
    print(f"\n{'─'*60}")
    print(f"  PREVIEW — {len(rows)} rows starting at row {start_row}")
    print(f"{'─'*60}")
    headers = ["Date","Cat","Account","Value","Key","Qty","Price","Avg","Pct","Abs","FX"]
    print("  " + " | ".join(f"{h:<10}" for h in headers))
    print("  " + "─"*58)
    for i, row in enumerate(rows):
        display = []
        for cell in row:
            s = str(cell)
            display.append(s[:10].ljust(10))
        print(f"  {display[0]} | {' | '.join(display[1:])}")
    print(f"{'─'*60}\n")


def main():
    parser = argparse.ArgumentParser(description="Portfolio tracker automation")
    parser.add_argument("screenshots", nargs="+", help="Paths to screenshot images")
    parser.add_argument("--date",      required=True, help="EOD date, e.g. 2026-06-16")
    parser.add_argument("--start-row", type=int, default=None,
                        help="First row to write in Sheets (auto-detected from GAS if omitted)")
    parser.add_argument("--dry-run",   action="store_true", help="Parse and preview only, don't write to Sheets")
    args = parser.parse_args()

    start_row = args.start_row if args.start_row is not None else fetch_start_row()

    print(f"\n{'='*60}")
    print(f"  Portfolio Tracker  |  {args.date}  |  start_row={start_row}")
    print(f"{'='*60}")
    print(f"\n[1/5] Encoding {len(args.screenshots)} screenshot(s)...")
    image_blocks = encode_images(args.screenshots)

    print("\n[2/5] Calling Claude API...")
    raw_output = call_claude(image_blocks, args.date, start_row)

    print("\n[3/5] Parsing table...")
    rows = parse_table(raw_output, start_row)
    rows = apply_holdings(rows)

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
