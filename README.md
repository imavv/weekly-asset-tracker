# weekly-asset-tracker

Weekly automation that turns my banking/brokerage screenshots into a portfolio
snapshot row in my Google Sheets tracker (cash, stocks, ETFs, deposits, bonds).

**Frontend:** Claude Desktop / mobile — I drop in screenshots and confirm.
**Backend:** a remote [MCP](https://modelcontextprotocol.io) server on Vercel that
does all the arithmetic and writes to the sheet through a Google Apps Script
endpoint.

```
screenshots → Claude reads them → MCP server assembles + validates → Apps Script → Sheet
```

Claude reports only what it can see (`{"ticker": "VOO", "price_usd": 512.34}`).
Every derived value — share counts, lot conversion, spreadsheet formulas, row
numbers, FX conversion — is computed server-side, so the model never touches the
arithmetic.

See [`CONTEXT.md`](CONTEXT.md) for architecture, deployment and the security
model.

---

Previously a Telegram bot calling the Claude API directly (`bot.py`,
`portfolio_tracker.py`, `render.py`) — retired, kept for reference and rollback.
