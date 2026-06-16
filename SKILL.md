---
name: portfolio-tracker
description: >
  Fills in a weekly EOD portfolio snapshot table (columns A–K) in the exact format
  of Ama's Google Sheets asset tracker. Use this skill whenever the user shares
  banking/app screenshots and asks to update their portfolio tracker, fill in this
  week's values, or generate a ready-to-paste table of asset values. Triggers on
  phrases like "fill in this week's tracker", "update my portfolio", "here are my
  screenshots", or any time one or more banking/brokerage app screenshots are
  shared alongside a request to produce portfolio data.
---

# Portfolio Tracker Skill

Produces a ready-to-paste tab-separated table of the user's weekly EOD asset snapshot,
covering all asset categories: Cash, Deposit, MF Bonds, Stocks, and ETFs.

---

## Output Format

Tab-separated rows, one asset per row, matching the Google Sheets columns exactly:

```
A (Date EOD) | B (Category) | C (Account) | D (Value IDR) | E (Key) | F (Qty) | G (Share price) | H (Avg price) | I (Pct change) | J (Abs change) | K (USD/IDR formula)
```

**Column rules:**
- **Date (A)**: Use the date stated by the user (or today's date if not specified), formatted `YYYY-MM-DD`
- **Category (B)**: Cash, Deposit, MF Bonds, Stock, or ETF
- **Account (C)**: Account name as per the roster below
- **Value (D)**:
  - Cash/Deposit/MF Bonds: hardcoded IDR integer from screenshot, no symbol, no thousand separators
  - Stock: formula `=F{row}*G{row}`
  - ETF: formula `=F{row}*G{row}*$K$1563` (always references the FX anchor row)
  - Ajaib (Cash): formula `={buying_power_usd}*$K$1563`
- **Key (E)**: Leave blank — user fills separately
- **Qty (F)**: For stocks/ETFs only. Store the **share count** (not lots) — i.e. lots × 100 for IDX stocks.
  - IDX stocks (BBCA, ICBP, BBRI): read from the broker screenshot.
  - **ETFs (automated/API use): leave as `0`.** A downstream script injects the share count from a static `holdings.json` file. Do NOT carry forward or guess ETF quantities.
- **Share price (G)**: For stocks/ETFs only. Hardcoded real-time price. IDR for IDX stocks, USD for US ETFs. 2 decimal places
- **Avg price (H)**: Cost basis.
  - IDX stocks: carry forward from prior data unless user states a change.
  - **ETFs (automated/API use): leave as `0`.** Injected from `holdings.json` by the script alongside qty.
- **Pct change (I)**: Formula `=(G{row}-H{row})/H{row}` — Google Sheets will format as % if column is formatted that way
- **Abs change (J)**: Formula `=(G{row}-H{row})*F{row}`
- **USD/IDR (K)**: Only filled on the FIRST row (Mandiri, row 1563). Formula: `=GOOGLEFINANCE("CURRENCY:USDIDR")`. All other K cells are blank. All ETF/Ajaib value formulas reference `$K$1563`

**No header row in output.** Never include a header row — paste starts directly with data.

**Column alignment for tab-separated output:**
- Cash/Deposit/MF Bonds rows: A, B, C, D, then 7 blank tabs (E through K), except row 1563 which has the GOOGLEFINANCE formula in K
- Stock/ETF rows: A, B, C, D(formula), E(blank), F(qty), G(price), H(avg), I(formula), J(formula), K(blank)
- This means every row has exactly 10 tabs (11 columns)

---

## Step-by-Step Workflow

### Step 1 — Collect inputs

Inputs needed:
1. Screenshots from each banking/brokerage app (one per account), including the broker screenshot showing IDX stock lots and last prices, and small crops for BNI (RDN) and Ajaib
2. The EOD date for this snapshot (or confirm it's today)
3. Any changes to stock/ETF quantities since last week (otherwise carry forward)

**In interactive (chat) use:** if Cash/Deposit/Bond screenshots are missing, ask before filling.

**In automated (API) use:** do NOT stall asking for more screenshots. Produce the complete table from whatever is provided; for any genuinely missing value, use 0 and append a note line after the table prefixed with `# `.

### Step 2 — Extract Cash, Deposit, MF Bond values from screenshots

For each screenshot:
- Identify the account (match to roster below)
- Read the balance shown
- All balances are in IDR; use as-is (integer, no decimals)
- Special cases:
  - **Mandiri**: main savings balance (Tabungan NOW IDR)
  - **BCA**: balance from m-Info popup
  - **Seabank**: Savings balance (not Time Deposit)
  - **Others**: sum of OVO + ShopeePay + GoPay from Mandiri Livin' e-wallet screen
  - **Superbank**: Tabungan Utama balance (Cash); Deposito balance (Deposit) — two separate rows
  - **Bibit (RDN)**: skip — negligible, do not include
  - **BNI (RDN)**: Cash Settlement End Balance
  - **Ajaib**: Buying Power USD amount — use formula `={amount}*$K$1563` in Value column
  - **Bibit**: Nilai Portofolio total (MF Bonds)

### Step 3 — Look up real-time prices for Stocks and ETFs

**Indonesian Stocks (IDX)** — price in IDR:
- Tickers: BBCA, ICBP, BBRI
- **Read the last price directly from the attached broker screenshot.** Do NOT web-search these — the screenshot is the source of truth and avoids search failures.
- The broker screenshot typically shows qty in LOTS (1 lot = 100 shares) — multiply by 100 for column F
- Share price (G): IDR, no decimals needed

**US ETFs** — price in USD:
- Tickers: VOO, VT, VTI, SPYM, GDX, VEA, SMH, GLD, IGV, XLP, XLE
- Search: `{TICKER} stock price google finance` — use **one source only: Google Finance** (cleanest to parse, consistent with the sheet's GOOGLEFINANCE FX anchor)
- Share price (G): USD, 2 decimal places
- **Quantity (F) and avg cost (H): output `0`.** These are supplied by the script from `holdings.json` (a static file the user maintains and updates when they buy/sell). The script overwrites col F and H after parsing, so your only job for ETFs is the price (G).
- FX conversion handled by `$K$1563` in the Value formula — do NOT hardcode the FX rate
- If US markets are closed (likely when run in WIB morning), Google Finance shows the prior session's closing price — that is correct for an EOD snapshot
- Single-source means no cross-check safety net. After producing prices, the downstream validator flags any price that deviates >30% from its carried-forward avg so stale/wrong prices surface for human review

### Step 4 — Determine row numbers

The block always starts at the row after the last existing data row. Confirm with user if unsure. Row numbers flow sequentially with no gaps:

Default roster order and row mapping (starting row = first data row, e.g. 1563):
```
+0  Mandiri         Cash
+1  BCA             Cash
+2  Seabank         Cash
+3  Others          Cash
+4  Superbank       Cash
+5  Superbank Dep.  Deposit
+6  Bibit           MF Bonds
+7  BNI (RDN)       Cash
+8  BBCA            Stock
+9  ICBP            Stock
+10 BBRI            Stock
+11 Ajaib           Cash
+12 VOO             ETF
+13 VT              ETF
+14 VTI             ETF
+15 SPYM            ETF
+16 GDX             ETF
+17 VEA             ETF
+18 SMH             ETF
+19 GLD             ETF
+20 IGV             ETF
+21 XLP             ETF
+22 XLE             ETF
```

### Step 5 — Assemble and output the table

- No header row
- Tab-separated, 11 columns (A–K) per row
- Cash/Deposit/MF Bonds: hardcoded value in D, blanks in E–K (except K on row 1563)
- Stock/ETF: formulas in D, I, J; hardcoded qty in F, price in G, avg price in H; blank in E and K
- After the table, output a summary: total by category and grand total (use the hardcoded/computed values for reference, noting ETF/stock values are formula-driven)

---

## Known Account Roster

| Account | Category | Notes |
|---|---|---|
| Mandiri | Cash | Tabungan NOW IDR |
| BCA | Cash | m-Info balance |
| Seabank | Cash | Savings balance only |
| Others | Cash | OVO + ShopeePay + GoPay combined |
| Superbank | Cash | Tabungan Utama |
| Superbank Deposit | Deposit | Detail Deposito balance |
| Bibit | MF Bonds | Nilai Portofolio total |
| BNI (RDN) | Cash | Cash Settlement End Balance |
| Ajaib | Cash | Buying Power USD × $K$1563 |
| BBCA | Stock | IDX, price + qty (lots × 100) from broker screenshot |
| ICBP | Stock | IDX, price + qty (lots × 100) from broker screenshot |
| BBRI | Stock | IDX, price + qty (lots × 100) from broker screenshot |
| VOO | ETF | USD |
| VT | ETF | USD |
| VTI | ETF | USD |
| SPYM | ETF | USD |
| GDX | ETF | USD |
| VEA | ETF | USD |
| SMH | ETF | USD |
| GLD | ETF | USD |
| IGV | ETF | USD |
| XLP | ETF | USD |
| XLE | ETF | USD |

---

## Edge Cases

- **Missing screenshot**: Flag which accounts are missing before outputting. Do not silently leave values blank.
- **Ambiguous balance**: If multiple figures shown in screenshot, ask user to confirm.
- **Market closed / price unavailable**: Use most recent closing price; note the date.
- **Qty change**: Update F, recompute D formula accordingly.
- **New account**: Ask for Category before proceeding.
- **GDX and volatile ETFs**: With single-source pricing there is no cross-check. The downstream validator flags any ETF whose price deviates >30% from its carried-forward avg — review those manually. Avoid Google Finance *history* pages (can show stale after-hours prices); use the live quote panel.
- **Row offset**: If user says the block starts at a different row than 1563, shift all row references and the $K$ anchor accordingly.
