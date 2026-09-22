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
value — the date, market prices, exchange rates, ETF share counts, lot
conversion, spreadsheet formulas, row numbers and row order.

The tool schema has no field for any of those, so if you find yourself wanting
to supply one, that is the signal you have misread the task.

**The one exception is foreign-currency balances.** They are the only quantity
you report, because they are the only one a screenshot actually shows. An ETF
screenshot shows a price but never a share count, so ETF quantities stay in
`holdings.json` where they always were. A multi-currency screen shows the
balance in plain sight, so it is the better source of truth and you report it.

---

## Workflow

### Step 1 — Read the screenshots
Extract one number per account, per the roster below. All balances are IDR
integers unless stated otherwise.

If a screenshot is missing or unreadable, use `0` and **tell the user which
accounts you zero-filled**. Never silently drop an account — every fixed roster
entry must be supplied.

**Foreign currencies are different, and the difference matters.** Read the
multi-currency screen and report `fx` as one entry per currency shown, with the
amount in that currency (`{"currency": "USD", "amount": 4800}` — not the IDR
equivalent). That list *is* this week's FX roster:

- A currency on the screen but never held before **gets a new row**.
- A currency held before but **not** on the screen **gets no row at all** — it
  is treated as closed, not carried over.

So report the whole screen, every time, not just what changed. If you only have
a partial view of it, say so and ask rather than guessing — an omission is
indistinguishable from a sale.

Leave `fx` empty **only** when you have no multi-currency screenshot at all.
The server then falls back to the last known amounts in `holdings.json` and
says so in the preview.

You do not need the date, ETF prices, or FX rates. The server resolves all
three itself — including the rate for a currency it has never seen.

### Step 2 — Prepare
Call `prepare_snapshot` with what you read, and **show the user the result**.
It reports the exact rows that would be written, the locked USD/IDR rate, and
any errors or warnings. Nothing is written.

Errors block the write — fix them and prepare again. Warnings are advisory:
surface them and let the user decide.

**Read the FX `source` column before you show it.** It marks every currency
that changed, is new, or dropped out since last week, and lists any dropped
ones under "NOT WRITTEN THIS WEEK". Those are the lines where a misread screen
looks identical to a real change, so call them out explicitly rather than
letting the user find them in the table. The user's confirmation is the only
safeguard between a misread balance and the sheet.

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
| `fx[].currency` | multi-currency screen | 3-letter code, e.g. `USD`, `SGD` |
| `fx[].amount` | multi-currency screen | Balance **in that currency**, never the IDR equivalent |

**Resolved by the server, never by you:** the snapshot date, all 11 ETF prices,
every FX rate, ETF share counts, FX cost basis, every spreadsheet formula, and
every row number.

---

## Account roster

Every fixed entry below must appear in the snapshot. The currency rows are the
exception — they come from the screen, not from this table.

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
| Foreign currencies | **the multi-currency screen** — report every currency and amount it shows |

Currencies are the only open-ended part of the roster. CNY, USD, SGD, AUD and
JPY are what has been held historically, but that list is neither a minimum nor
a maximum: report exactly what the screen shows.

**Bibit (RDN)** is negligible — skip it. It is not the same as **Bibit**, and it
is not **Stockbit (RDN)**, which *is* tracked.

The block is **23 fixed rows plus one row per currency** — 29 in total when the
usual five are held. The count changes when a currency appears or disappears,
and that is expected; the server computes it.

---

## Edge cases

- **Missing screenshot** — zero-fill, name the account to the user, keep going.
- **Ambiguous balance** — if a screenshot shows several plausible figures, ask.
- **US market closed** — expected when running in the WIB morning. The prior
  session's close is the correct value for an EOD snapshot.
- **ETF quantity changed** — ETF share counts live in `holdings.json` in the
  repo, not in this conversation. If the user says they bought or sold an ETF,
  tell them to update that file and redeploy; you cannot override it from chat.
- **FX amount changed** — just report what the screen says. The screenshot
  overrides `holdings.json`, so no redeploy is needed to write the right
  number. Do still tell the user to update the file afterwards: it is the
  fallback for a week with no screenshot, and the home of the cost basis.
- **A currency is missing from the screen** — do not carry it over from a
  previous week and do not invent a zero row. Report what you see; the preview
  will flag the currency as dropped, and the user decides at the confirm step
  whether that is real or a screenshot that cut off.
- **Inverted FX rate** — the rate is IDR per one unit of the foreign currency.
  A value below 1 is almost certainly upside down; the preview warns about it.
- **New account** — the roster is fixed in the server's schema for everything
  except currencies. A new bank, stock or ETF needs a code change, so tell the
  user rather than trying to squeeze it in. A new **currency** needs nothing:
  report it and it gets a row.
- **A new currency GOOGLEFINANCE cannot price** — the preview reports the rate
  as unresolved and blocks the write, exactly as it would for an ETF. Tell the
  user which code failed and ask for the rate in IDR per one unit, then pass it
  in `fx_rate_overrides`.
- **Large price move** — the preview warns when a price is more than 30% from
  its cost basis. That is often legitimate for a long-held position; check it
  is not a misread digit or a stale quote.
