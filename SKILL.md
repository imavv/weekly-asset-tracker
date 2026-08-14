---
name: portfolio-tracker
description: >
  Reads Ama's banking/brokerage screenshots and writes a weekly EOD portfolio
  snapshot to the Google Sheets asset tracker via the weekly-asset-tracker MCP
  server. Use this skill whenever the user shares banking/app screenshots and
  asks to update their portfolio tracker, fill in this week's values, or record
  this week's snapshot. Triggers on phrases like "fill in this week's tracker",
  "update my portfolio", "here are my screenshots", or any time one or more
  banking/brokerage app screenshots are shared alongside a request to produce
  portfolio data.
---

# Portfolio Tracker Skill

Turns weekly banking/brokerage screenshots into a row block in the user's Google
Sheet, through the `weekly-asset-tracker` MCP server.

## Your job, and what is NOT your job

**You do:** read numbers off screenshots and report them.

**You do not:** compute anything. The server owns every derived value — share
counts, lot conversion, spreadsheet formulas, row numbers, USD→IDR conversion,
and the order of rows. Report raw observations and let it do the rest.

Specifically, never try to supply: ETF share counts, spreadsheet formulas, row
numbers, the FX rate, or a `$K$` anchor. There is no place to put them.

---

## Workflow

### Step 1 — Establish the date
Call `get_today_wib` unless the user names a different EOD date. Do not infer
the date from your own sense of "today" — the server runs in UTC and the user is
in WIB.

### Step 2 — Read the screenshots
Extract one number per account, per the roster below. All balances are IDR
integers unless stated otherwise.

If a screenshot is missing or unreadable, use `0` and **tell the user which
accounts you zero-filled** before previewing. Never silently drop an account —
every roster entry must be supplied.

### Step 3 — Get ETF prices
Call `get_etf_prices`. It resolves prices inside the sheet itself and returns
plain numbers. Use them verbatim.

Do **not** web-search ETF prices. Do not carry prices forward from a previous
week. If the tool reports a ticker with no price, ask the user rather than
guessing.

IDX stock prices are different — read those off the broker screenshot, which is
the source of truth for BBCA, ICBP and BBRI.

### Step 4 — Preview
Call `preview_snapshot` and **show the user the result**. It reports the exact
sheet rows that would be written, the locked USD/IDR rate, and any errors or
warnings. Nothing is written.

Errors block the write — fix them and preview again. Warnings are advisory:
surface them and let the user decide.

### Step 5 — Confirm, then submit
Ask the user to confirm. Only after they say yes, call `submit_snapshot`.

If it reports that a block for this date already exists, **stop and ask** — that
usually means the week was already recorded. Only pass `force=true` if the user
explicitly asks for a duplicate.

### Step 6 — Show the result
Call `get_summary` and show the updated trend and breakdown tables.

---

## What to report

| Field | Source | Notes |
|---|---|---|
| Bank balances | screenshots | IDR integer, no separators |
| `ajaib_usd` | Ajaib screenshot | Buying Power in **USD** — the server converts it |
| Stock `lots` | broker screenshot | **RAW lot count** as displayed. Do NOT multiply by 100 |
| Stock `price_idr` | broker screenshot | Last price, IDR |
| Stock `avg_idr` | prior week / user | Cost basis; carry forward unless the user says it changed |
| ETF `price_usd` | `get_etf_prices` | USD, verbatim from the tool |

---

## Account roster

Every one of these must appear in the snapshot.

| Account | Where the number comes from |
|---|---|
| Mandiri | Tabungan NOW IDR — main savings balance |
| BCA | m-Info popup balance |
| Seabank | Savings balance (**not** Time Deposit) |
| Others | OVO + ShopeePay + GoPay summed, from Mandiri Livin' e-wallet screen |
| Superbank | Tabungan Utama balance |
| Superbank Deposit | Detail Deposito balance |
| Bibit | Nilai Portofolio total |
| BNI (RDN) | Cash Settlement End Balance |
| Ajaib | Buying Power, in USD |
| BBCA / ICBP / BBRI | Broker screenshot: lots + last price |
| VOO, VT, VTI, SPYM, GDX, VEA, SMH, GLD, IGV, XLP, XLE | `get_etf_prices` |

**Bibit (RDN)** is negligible — skip it. It is not the same as **Bibit**.

---

## Edge cases

- **Missing screenshot** — zero-fill, name the account to the user, keep going.
- **Ambiguous balance** — if a screenshot shows several plausible figures, ask.
- **US market closed** — expected when running in the WIB morning. The prior
  session's close is the correct value for an EOD snapshot.
- **Quantity changed** — ETF share counts live in `holdings.json` in the repo,
  not in this conversation. If the user says they bought or sold an ETF, tell
  them to update that file and redeploy; you cannot override it from chat.
- **New account** — the roster is fixed in the server's schema. A new account
  needs a code change, so tell the user rather than trying to squeeze it in.
- **Large price move** — the preview warns when a price is more than 30% from
  its cost basis. That is often legitimate for a long-held position; check it
  is not a misread digit or a stale quote.
