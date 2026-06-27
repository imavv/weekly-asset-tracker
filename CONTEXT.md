# Context Handover — Weekly Asset Tracker Bot

A Telegram bot that turns banking/brokerage screenshots into a weekly EOD
portfolio snapshot row in a Google Sheet, using Claude (vision + web search) to
parse the numbers.

**Status:** Deployed on Railway, running 24/7 as the sole Telegram poller
(since 2026-06-21). Repo: `github.com/imavv/weekly-asset-tracker`, branch `main`.

---

## Architecture

```
 Telegram user
      │  screenshots + /run [date] [model]
      ▼
 ┌──────────┐   buffers photos, whitelist, state      ┌────────────────────────┐
 │  bot.py  │ ───────────────────────────────────────▶│  portfolio_tracker.py  │
 │ (Telegram│   asyncio.to_thread(blocking work)       │       (engine)         │
 │ frontend)│◀─────────────────────────────────────── │                        │
 └──────────┘   preview + checks → /confirm            └───────────┬────────────┘
      │  ▲                                                          │
      │  │ summary images                          parse_screenshots│ write_rows
      │  │                                          fetch_summary    │ (+ checks)
      ▼  │                                                          ▼
 ┌──────────┐                                   ┌──────────┐   ┌──────────────────┐
 │ render.py│◀── sheet range ───────────────────│ Claude   │   │ Google Apps      │
 │ (PNG via │    (matplotlib, headless Agg)     │ API      │   │ Script web app   │
 │ mpl/Agg) │                                   │(vision + │   │ (/exec) → Sheet  │
 └──────────┘                                   │ web srch)│   └──────────────────┘
                                                └──────────┘
```

### Flow (one cycle)
1. User sends screenshots → `bot.py` buffers them (in-memory).
2. `/run [date] [model]` → `parse_screenshots()` calls Claude (vision + web
   search) to read a 23-row portfolio table → `post_process_rows()` applies
   holdings/lot math → `check_rows()` runs non-blocking sanity checks.
3. Bot replies with a **preview + warnings** (does not write yet).
4. `/confirm` → `write_rows()` POSTs to the GAS web app, which appends the block
   to the Google Sheet.
5. Bot auto-sends **two summary table images** (`fetch_summary()` → `render.py`).

---

## File map
| File | Role |
|------|------|
| `bot.py` | Telegram frontend — handlers, whitelist, in-memory state, `asyncio.to_thread`. |
| `portfolio_tracker.py` | Engine — `parse_screenshots()`, `write_rows()`, `fetch_summary()`, model registry, `post_process_rows()`, `check_rows()`. |
| `render.py` | Sheet range → PNG (matplotlib, headless Agg backend). |
| `portfolio_gas.js` | Google Apps Script (deployed separately in Google; repo copy has placeholders). |
| `SKILL.md` | Claude system prompt. |
| `holdings.json` | Static ETF qty / avg cost. |
| `requirements.txt` / `Procfile` | Deps (anthropic, requests, pandas, matplotlib) / `worker: python bot.py`. |

---

## Deployment (Railway)
- Auto-redeploys on push to `main`. Runs `Procfile` → `worker: python bot.py`.
- **Required env vars:** `BOT_TOKEN`, `ALLOWED_USER_ID` (worker crashes without
  these two), `ANTHROPIC_API_KEY`, `GAS_ENDPOINT`, `GAS_SECRET_TOKEN`,
  `MODEL=sonnet`, `PYTHONUNBUFFERED=1`. All mirrored in local `.env` (gitignored).
- **One poller per token:** never run a second instance (local + cloud) or
  Telegram returns `409 Conflict`. To debug locally, stop Railway first.

### Gotchas
- **Ephemeral state:** a redeploy/restart wipes `logs/`, buffered photos, and any
  pending `/confirm`. Re-send screenshots after a restart.
- **GAS edits need a NEW VERSION deploy** (Manage deployments → Edit → New
  version) or `/exec` serves stale code. Keep the real `SPREADSHEET_ID` /
  `SECRET_TOKEN` in the live script — repo copy is placeholders on purpose.
- **Cold starts:** GAS first hit after idle can take >20s; `fetch_summary` retries.

---

## Models
`/run [date] [model]` accepts `sonnet | opus | haiku`. **Use `sonnet`** —
Haiku is unreliable for the strict 11-column format. Railway default is `sonnet`.

## Open issues (non-blocking)
- **No double-write guard:** a second `/confirm` appends a duplicate 23-row block
  (inflates the W-0 summary). Possible fix: warn if the sheet's last row already
  has today's date.
- **Weak `GAS_SECRET_TOKEN`** (`REPLACE_WITH_YOUR_SECRET`) — rotate in both Apps
  Script and Railway.
- **`#DIV/0!`** in ETF column I when avg (H) = 0 (`holdings.json` avg=null). Cosmetic.
- Sheet may contain leftover duplicate / corrupt `2026-06-21` test blocks — clean manually.

## Quick commands
- CLI run (no Telegram): `python portfolio_tracker.py --model sonnet screenshots/*.jpg`
- Local bot (debug only; stop Railway first): `python -u bot.py > /tmp/bot.log 2>&1 &` / `pkill -f bot.py`
