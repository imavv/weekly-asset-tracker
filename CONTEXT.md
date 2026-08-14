# Context Handover — Weekly Asset Tracker

Turns banking/brokerage screenshots into a weekly EOD portfolio snapshot in a
Google Sheet. Claude Desktop/mobile reads the screenshots; a remote **MCP
server** on Vercel does all the arithmetic and writes to the sheet.

**Status:** migrating from the Telegram bot (Railway, retired) to the MCP
server. Repo: `github.com/imavv/weekly-asset-tracker`.

---

## Architecture

```
 You, in Claude Desktop or mobile
      │  screenshots + "update my tracker"
      ▼
 ┌──────────────────┐   Claude reads the screenshots and reports
 │  Claude + SKILL  │   OBSERVATIONS — no formulas, no row numbers
 └────────┬─────────┘
          │  MCP over Streamable HTTP (secret path in the URL)
          ▼
 ┌──────────────────────────────────────────────────────┐
 │  MCP server  (Vercel, Python)                        │
 │                                                      │
 │  get_etf_prices   preview_snapshot                   │
 │  get_today_wib    submit_snapshot  ← only writer     │
 │                   get_summary                        │
 │                                                      │
 │  assemble.py  roster order, lots x100, holdings,     │
 │               formulas, FX lock                      │
 │  checks.py    completeness + price sanity            │
 └────────┬─────────────────────────────────────────────┘
          │  HTTPS + GAS_SECRET_TOKEN
          ▼
 ┌──────────────────┐
 │  Apps Script     │  start_row · prices · summary · write
 │  web app (/exec) │
 └────────┬─────────┘
          ▼
     Google Sheet
```

### Why it is shaped this way

**Semantic tool arguments.** Claude sends `{"ticker": "VOO", "price_usd": 512.34}`,
not an 11-column spreadsheet row. The server turns observations into the A–K
grid. This makes "wrong row order" and "shifted columns" structurally impossible
rather than something a validator has to catch afterwards, and lets JSON Schema
reject malformed calls before any of our code runs.

**Judgement to the model, arithmetic to the code.** Reading a blurry Superbank
screenshot needs judgement. Multiplying lots by 100 does not — and a model doing
deterministic work fails silently, where code fails loudly.

**Prices resolved to static numbers.** `get_etf_prices` has Apps Script evaluate
`GOOGLEFINANCE` in a hidden scratch tab and hand back plain numbers. Writing the
live formula into column G would rewrite every past week's snapshot on each
recalculation.

---

## File map

| File | Role |
|---|---|
| `api/index.py` | Vercel entrypoint — exports the ASGI app |
| `tracker/app.py` | Secret-path gate, transport wiring, per-request lifespan |
| `tracker/server.py` | The five MCP tool definitions |
| `tracker/models.py` | Semantic input schema (Pydantic → JSON Schema) |
| `tracker/assemble.py` | Snapshot → 11-column A–K rows |
| `tracker/checks.py` | Completeness and price-sanity validation |
| `tracker/gas.py` | Apps Script client (async httpx) |
| `tracker/config.py` | Env vars + the fixed 23-entry roster |
| `holdings.json` | Static ETF share counts. **Edit + push to change** |
| `portfolio_gas.js` | Apps Script source (deployed separately in Google) |
| `SKILL.md` | Instructions for Claude |
| `tests/` | 22 tests: assembly, validation, end-to-end over MCP |
| `bot.py`, `render.py`, `portfolio_tracker.py` | **Retired** Telegram pipeline |

---

## Deployment

### 1. Apps Script
Paste `portfolio_gas.js` into the sheet's Apps Script editor, filling in the real
`SPREADSHEET_ID` and `SECRET_TOKEN`. Deploy → **New deployment** → Web App,
execute as Me, access Anyone. Copy the `/exec` URL.

> Edits need a **New version** deploy or `/exec` keeps serving stale code.

Run `testResolvePrices` in the editor first to confirm GOOGLEFINANCE resolves.

### 2. Vercel
Import the **whole repo** — Vercel deploys a Git repo plus branch, there is no
file picker. What actually ships is narrowed by two files:

- `.vercelignore` keeps `screenshots/`, the retired bot and the tests out of the
  deployment entirely.
- `vercel.json` sets `outputDirectory` to an empty `public/`, so Vercel has
  nothing to serve statically. Left at the default it serves the repo root, and
  every file in it becomes fetchable from the deployment URL.

`includeFiles` still pulls `tracker/**` and `holdings.json` into the function
bundle — those are runtime dependencies, not static assets.

After the first deploy, confirm nothing leaked:

```bash
curl -so /dev/null -w '%{http_code}\n' https://<project>.vercel.app/holdings.json   # expect 404
curl -s https://<project>.vercel.app/healthz                                        # expect ok
```

Then set environment variables:

| Variable | Value |
|---|---|
| `GAS_ENDPOINT` | the `/exec` URL |
| `GAS_SECRET_TOKEN` | must match `SECRET_TOKEN` in the Apps Script |
| `MCP_SECRET` | a long random string — `openssl rand -hex 32` |
| `MCP_ALLOWED_HOSTS` | *(optional)* your Vercel hostname, to pin Host/Origin |

### 3. Claude
Settings → Connectors → Add custom connector:

```
https://<your-project>.vercel.app/api/mcp/<MCP_SECRET>
```

Added on claude.ai, it works on desktop and mobile both.

**Keep `submit_snapshot` on manual approval.** It is the replacement for the old
`/confirm` step — the tool is annotated `destructive_hint`, so clients should
prompt by default. Do not tick "always allow" for it.

---

## Security model

- **The URL is the credential.** A bearer secret: whoever holds it can write to
  the sheet. Unscoped, non-expiring, revocable only by rotating `MCP_SECRET` and
  re-pasting the connector URL. Accepted deliberately — the blast radius is
  appending rows to one spreadsheet. OAuth would fix the scoping but means
  running an authorization server.
- **`GAS_SECRET_TOKEN` never reaches the model.** It lives in Vercel env vars and
  is injected by `tracker/gas.py`. It is never a tool argument, because tool
  arguments land in the chat transcript. A test asserts it never appears in tool
  output.
- Bad secret returns **404**, not 403, so a scanner cannot tell the path exists.
- Tickers are sanitised in both Python and Apps Script before being interpolated
  into a `GOOGLEFINANCE` formula.

---

## Gotchas

- **Vercel does not run ASGI lifespan events.** The MCP transport starts a task
  group in its lifespan, so `tracker/app.py` builds a fresh transport app per
  request and runs the lifespan around it. Safe because we run stateless. If you
  ever switch to stateful sessions, this must be rethought.
- **Cold starts.** Apps Script can take >20s after idle. `maxDuration` is 60s in
  `vercel.json`; lower it if your plan rejects that.
- **`holdings.json` is bundled into the deployment.** A buy or sell means edit +
  push + redeploy. Claude cannot change it from chat.
- **Duplicate writes** are blocked by a date guard in `doPost` — a second block
  for a date already in column A returns 409 unless `force:true`.

---

## Open issues

- **`SECRET_TOKEN` is still `REPLACE_WITH_YOUR_SECRET`** in the Apps Script, and
  a real `/exec` URL was committed to this public repo in an earlier version of
  `portfolio_tracker.py`. Rotate both the token and the deployment URL.
- **This repo is public** and contains `holdings.json` (exact share counts) plus
  10 real banking screenshots under `screenshots/`. Make it private, or at
  minimum purge the screenshots.
- **Summary images are gone** with `render.py`. `get_summary` returns markdown
  tables instead of the formatted PNGs.
- **No run log.** The Telegram pipeline wrote `logs/`; Vercel's filesystem is
  ephemeral, so runs are only in Vercel's log stream.

---

## Quick commands

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest anyio
.venv/bin/python -m pytest tests/ -q          # 22 tests, no network needed

# Inspect the server interactively
MCP_SECRET=dev GAS_ENDPOINT=... GAS_SECRET_TOKEN=... \
  npx @modelcontextprotocol/inspector
# then connect to http://127.0.0.1:8000/api/mcp/dev via Streamable HTTP
```

To roll back to the Telegram bot: `pip install -r requirements-bot.txt` and
restore the Railway worker. The Apps Script contract is unchanged, so both
clients still work.
