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

**You do not:** compute or look up anything. The server owns every derived
value — the date, market prices, exchange rates, share counts, lot conversion,
spreadsheet formulas, row numbers and row order.

The tool schema has no field for any of those, so if you find yourself wanting
to supply one, that is the signal you have misread the task.

---

## Workflow

### Step 1 — Read the screenshots
Extract one number per account, per the roster below. All balances are IDR
integers unless stated otherwise.

If a screenshot is missing or unreadable, use `0` and **tell the user which
accounts you zero-filled**. Never silently drop an account — every roster entry
must be supplied.

You do not need the date, ETF prices, or FX rates. The server resolves all
three itself.

### Step 2 — Prepare
Call `prepare_snapshot` with what you read, and **show the user the result**.
It reports the exact rows that would be written, the locked USD/IDR rate, and
any errors or warnings. Nothing is written.

Errors block the write — fix them and prepare again. Warnings are advisory:
surface them and let the user decide.

If it reports market data it could not resolve, tell the user which symbols
failed and what the sheet returned. Only if they supply a figure by hand should
you use `etf_price_overrides` / `fx_rate_overrides`.

### Step 3 — Confirm, then submit
Ask the user to confirm. Only after they say yes, call `submit_snapshot` with
**the same observations** you passed to `prepare_snapshot`. Prices are
re-fetched server-side, so you never retype them.

If it reports that a block for this date already exists, **stop and ask** —
that usually means the week was already recorded. Only pass `force=true` if the
user explicitly asks for a duplicate.

### Step 4 — Show the result
Call `get_summary` and show the updated trend and breakdown tables.

---

## What to report

Everything in this table comes off a screenshot. Nothing else belongs in the
tool call.

| Field | Source | Notes |
|---|---|---|
| Bank balances | screenshots | IDR integer, no separators |
| `ajaib_usd` | Ajaib screenshot | Buying Power in **USD** — the server converts it |
| Stock `lots` | broker screenshot | **RAW lot count** as displayed. Do NOT multiply by 100 |
| Stock `price_idr` | broker screenshot | Last price, IDR |
| Stock `avg_idr` | prior week / user | Cost basis; carry forward unless the user says it changed |

**Resolved by the server, never by you:** the snapshot date, all 11 ETF prices,
all 5 FX rates, ETF share counts, currency amounts, every spreadsheet formula,
and every row number.

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
| Stockbit (RDN) | Cash / RDN balance in the Stockbit app |
| BNI (RDN) | Cash Settlement End Balance |
| Ajaib | Buying Power, in USD |
| BBCA / ICBP / BBRI | Broker screenshot: lots + last price |
| VOO, VT, VTI, SPYM, GDX, VEA, SMH, GLD, IGV, XLP, XLE | server-resolved — you supply nothing |
| CNY, USD, SGD, AUD, JPY | server-resolved — you supply nothing |

**Bibit (RDN)** is negligible — skip it. It is not the same as **Bibit**, and it
is not **Stockbit (RDN)**, which *is* tracked.

The block is **29 rows** in the order above.

---

## Edge cases

- **Missing screenshot** — zero-fill, name the account to the user, keep going.
- **Ambiguous balance** — if a screenshot shows several plausible figures, ask.
- **US market closed** — expected when running in the WIB morning. The prior
  session's close is the correct value for an EOD snapshot.
- **Quantity changed** — ETF share counts and foreign-currency amounts live in
  `holdings.json` in the repo, not in this conversation. If the user says they
  bought or sold, or moved money between currencies, tell them to update that
  file and redeploy; you cannot override it from chat.
- **Inverted FX rate** — the rate is IDR per one unit of the foreign currency.
  A value below 1 is almost certainly upside down; the preview warns about it.
- **New account** — the roster is fixed in the server's schema. A new account
  needs a code change, so tell the user rather than trying to squeeze it in.
- **Large price move** — the preview warns when a price is more than 30% from
  its cost basis. That is often legitimate for a long-held position; check it
  is not a misread digit or a stale quote.
