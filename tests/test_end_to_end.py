"""End-to-end test: MCP protocol in, Apps Script request out.

Runs the real ASGI app (secret gate, transport, tool dispatch) against a fake
Apps Script endpoint, so the wiring between layers is exercised rather than
mocked. This is the test that would catch a broken deploy shape.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

os.environ.setdefault("MCP_SECRET", "test-secret")
os.environ.setdefault("GAS_SECRET_TOKEN", "test-gas-token")

from tracker.config import ETF_TICKERS  # noqa: E402

from tests.test_tracker import make_observations  # noqa: E402

START_ROW = 1600
POSTED: list[dict] = []
# Symbols the fake sheet should fail to resolve, for the unhappy-path tests.
UNRESOLVED: set[str] = set()

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


class FakeGas(BaseHTTPRequestHandler):
    """Stands in for the Apps Script web app."""

    def log_message(self, *args):  # silence the test output
        pass

    def _json(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        action = params.get("action", [None])[0]

        if action == "prices":
            tickers = params["tickers"][0].split(",")
            prices, unresolved = {}, {}
            for t in tickers:
                if t in UNRESOLVED:
                    unresolved[t] = "#N/A"
                else:
                    prices[t] = 100.0
            self._json({"status": 200, "prices": prices, "unresolved": unresolved})
        elif action == "summary":
            # Mirrors the real sheet's L4:O11 block, TOTAL row included.
            self._json({
                "status": 200,
                "trend": {"values": [
                    ["Category", "W-0", "W-1", "W-2"],
                    ["Cash", "112.7mn", "114.4mn", "116.2mn"],
                    ["Deposit", "107.8mn", "107.8mn", "107.8mn"],
                    ["MF Bonds", "2.0mn", "2.0mn", "2.0mn"],
                    ["Stock", "119.6mn", "120.8mn", "114.2mn"],
                    ["ETF", "202.7mn", "203.9mn", "194.2mn"],
                    ["FX", "178.7mn", "178.4mn", "180.7mn"],
                    ["TOTAL", "723.4mn", "727.2mn", "715.0mn"],
                ]},
                "breakdown": {"values": [["Asset", "Value"], ["Cash", "500"]]},
            })
        else:
            self._json({"status": 200, "start_row": START_ROW})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        payload = json.loads(raw)
        POSTED.append(payload)
        self._json({
            "status": 200,
            "message": f"OK — wrote {len(payload['rows'])} rows starting at row {payload['start_row']}",
        })


@pytest.fixture(scope="module")
def gas_server():
    server = HTTPServer(("127.0.0.1", 0), FakeGas)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    os.environ["GAS_ENDPOINT"] = f"http://{host}:{port}/exec"
    yield
    server.shutdown()


@pytest.fixture
def client(gas_server, monkeypatch):
    """An MCP client speaking to the real ASGI app, with FX pinned."""
    from tracker import app as app_module
    from tracker import gas as gas_module

    async def fake_fx():
        return 16250.0

    monkeypatch.setattr(gas_module, "fetch_usdidr", fake_fx)
    POSTED.clear()
    UNRESOLVED.clear()

    transport = httpx.ASGITransport(app=app_module.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def unwrap(resp: httpx.Response) -> dict:
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return resp.json()


async def call_tool(client: httpx.AsyncClient, name: str, arguments: dict) -> str:
    """Full MCP handshake, then one tools/call. Returns the text content."""
    url = f"/api/mcp/{os.environ['MCP_SECRET']}"
    init = await client.post(url, headers=HEADERS, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    })
    assert init.status_code == 200

    headers = dict(HEADERS)
    if sid := init.headers.get("mcp-session-id"):
        headers["mcp-session-id"] = sid

    resp = await client.post(url, headers=headers, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert resp.status_code == 200
    body = unwrap(resp)
    assert "error" not in body, body
    return body["result"]["content"][0]["text"]


@pytest.mark.anyio
async def test_preview_does_not_write(client):
    async with client:
        text = await call_tool(
            client, "prepare_snapshot",
            {"observations": make_observations().model_dump()},
        )

    assert "PREVIEW" in text
    assert "All checks passed" in text
    assert f"{START_ROW}" in text
    assert POSTED == []  # the whole point


@pytest.mark.anyio
async def test_submit_writes_a_correct_block(client):
    async with client:
        text = await call_tool(
            client, "submit_snapshot",
            {"observations": make_observations().model_dump()},
        )

    assert "WRITTEN" in text
    assert len(POSTED) == 1

    payload = POSTED[0]
    assert payload["token"] == "test-gas-token"   # secret injected server-side
    assert payload["start_row"] == START_ROW
    assert payload["force"] is False

    rows = payload["rows"]
    assert len(rows) == 29
    assert all(len(r) == 11 for r in rows)
    assert rows[0][2] == "Mandiri"
    assert rows[0][10] == 16250.0                  # FX locked on first row only
    assert rows[-1][2] == "JPY"

    bbca = next(r for r in rows if r[2] == "BBCA")
    assert bbca[5] == 4400                          # 44 lots -> shares
    assert bbca[3] == f"=F{START_ROW + 9}*G{START_ROW + 9}"


@pytest.mark.anyio
async def test_validation_failure_blocks_the_write(client):
    bad = make_observations().model_dump()
    bad["banks"] = bad["banks"][:-1]  # drop BNI (RDN)

    async with client:
        text = await call_tool(client, "submit_snapshot", {"observations": bad})

    assert "REFUSED" in text
    assert "Missing bank account" in text
    assert POSTED == []


@pytest.mark.anyio
async def test_secret_is_never_exposed_to_the_model(client):
    """The GAS token must not appear in any tool output."""
    async with client:
        preview = await call_tool(
            client, "prepare_snapshot",
            {"observations": make_observations().model_dump()},
        )
        prices = await call_tool(client, "get_market_data", {})

    assert "test-gas-token" not in preview
    assert "test-gas-token" not in prices
    assert "test-secret" not in preview


@pytest.mark.anyio
async def test_summary_renders_markdown(client):
    async with client:
        text = await call_tool(client, "get_summary", {})

    assert "## Week-to-Week Trend" in text
    assert "| Category | W-0 | W-1 | W-2 |" in text
    assert "| Cash | 112.7mn | 114.4mn | 116.2mn |" in text
    # FX is a category now, and TOTAL is the row a hardcoded range used to cut off.
    assert "| FX | 178.7mn | 178.4mn | 180.7mn |" in text
    assert "| TOTAL | 723.4mn | 727.2mn | 715.0mn |" in text
    assert "## Asset Breakdown" in text


@pytest.mark.anyio
async def test_secret_accepted_in_query_string(client):
    """Hosts that rewrite the path strip the secret segment; ?k= survives."""
    async with client:
        resp = await client.post(
            "/api/index", params={"k": os.environ["MCP_SECRET"]}, headers=HEADERS,
            json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
    assert resp.status_code == 200
    assert unwrap(resp)["result"]["serverInfo"]["name"] == "weekly-asset-tracker"


@pytest.mark.anyio
async def test_wrong_query_secret_is_rejected(client):
    async with client:
        resp = await client.post("/api/index", params={"k": "nope"}, headers=HEADERS, json={})
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_health_survives_a_rewritten_path(client):
    async with client:
        assert (await client.get("/healthz")).text == "ok"
        assert (await client.get("/api/index/healthz")).text == "ok"
        assert (await client.get("/api/index", params={"health": "1"})).text == "ok"


@pytest.mark.anyio
async def test_diagnostics_report_routing_without_leaking_the_secret(client):
    async with client:
        resp = await client.get(
            "/api/index", params={"diag": "1", "k": os.environ["MCP_SECRET"]}
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["observed_path"] == "/api/index"
    assert body["path_segments"] == ["api", "index"]
    assert body["secret_supplied_in_query"] is True
    # The value itself must never appear anywhere in the response.
    assert os.environ["MCP_SECRET"] not in resp.text


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_market_data_reports_why_a_symbol_failed(client):
    """An unresolved symbol must say what the sheet held, not vanish silently."""
    UNRESOLVED.add("CURRENCY:JPYIDR")
    async with client:
        text = await call_tool(client, "get_market_data", {})

    assert "ETF prices (USD per share):" in text
    assert "VOO" in text
    assert "FX rates (IDR per 1 unit):" in text
    assert "NOT RESOLVED: JPY" in text
    assert "CURRENCY:JPYIDR -> #N/A" in text
    # The pairs that did resolve are still reported.
    assert "USD" in text


@pytest.mark.anyio
async def test_prices_are_fetched_not_supplied_by_the_model(client):
    """Observations carry no prices, yet the written rows have them.

    This is what the consolidation bought: the model cannot mistype a price
    between the preview and the write, because it never handles one.
    """
    payload = make_observations().model_dump()
    assert "etfs" not in payload
    # FX now carries a balance, but still no rate — that stays server-side.
    assert all(set(f) == {"currency", "amount"} for f in payload["fx"])

    async with client:
        await call_tool(client, "submit_snapshot", {"observations": payload})

    rows = POSTED[0]["rows"]
    voo = next(r for r in rows if r[2] == "VOO")
    usd = next(r for r in rows if r[2] == "USD")

    assert voo[6] == 100.0   # the fake sheet's price, resolved server-side
    assert usd[6] == 100.0   # likewise the rate


@pytest.mark.anyio
async def test_date_defaults_to_wib_without_the_model_supplying_it(client):
    from tracker.config import today_wib

    async with client:
        await call_tool(
            client, "submit_snapshot",
            {"observations": make_observations().model_dump()},
        )

    assert all(r[0] == today_wib() for r in POSTED[0]["rows"])


@pytest.mark.anyio
async def test_explicit_date_overrides_the_default(client):
    async with client:
        await call_tool(
            client, "submit_snapshot",
            {"observations": make_observations(date="2026-08-10").model_dump()},
        )

    assert all(r[0] == "2026-08-10" for r in POSTED[0]["rows"])


@pytest.mark.anyio
async def test_unresolved_market_data_blocks_the_write(client):
    """A price we could not fetch must stop the write, not silently zero it."""
    UNRESOLVED.add("CURRENCY:JPYIDR")

    async with client:
        text = await call_tool(
            client, "submit_snapshot",
            {"observations": make_observations().model_dump()},
        )

    assert "REFUSED" in text
    assert "Market data unresolved for: JPY" in text
    assert "CURRENCY:JPYIDR -> #N/A" in text
    assert POSTED == []


@pytest.mark.anyio
async def test_an_override_unblocks_an_unresolvable_symbol(client):
    """The escape hatch: a hand-supplied rate lets the week still be written."""
    UNRESOLVED.add("CURRENCY:JPYIDR")
    obs = make_observations().model_dump()
    obs["fx_rate_overrides"] = [{"currency": "JPY", "rate_idr": 112.7763}]

    async with client:
        text = await call_tool(client, "submit_snapshot", {"observations": obs})

    assert "WRITTEN" in text
    jpy = next(r for r in POSTED[0]["rows"] if r[2] == "JPY")
    assert jpy[6] == 112.7763


@pytest.mark.anyio
async def test_a_new_currency_reaches_the_sheet(client):
    """The whole point, end to end: a currency nobody configured gets a row."""
    obs = make_observations().model_dump()
    obs["fx"].append({"currency": "KRW", "amount": 1_250_000})

    async with client:
        text = await call_tool(client, "submit_snapshot", {"observations": obs})

    assert "WRITTEN" in text
    rows = POSTED[0]["rows"]
    assert len(rows) == 30                       # one more than the old fixed 29
    krw = next(r for r in rows if r[2] == "KRW")
    assert krw[1] == "FX"
    assert krw[5] == 1_250_000
    assert krw[6] == 100.0                       # rate resolved server-side
    # The block's shape is the only thing that changed; ETFs are untouched.
    voo = next(r for r in rows if r[2] == "VOO")
    assert voo[5] == 3.068


@pytest.mark.anyio
async def test_a_changed_balance_overrides_holdings_json(client):
    obs = make_observations().model_dump()
    for entry in obs["fx"]:
        if entry["currency"] == "USD":
            entry["amount"] = 4800

    async with client:
        text = await call_tool(client, "prepare_snapshot", {"observations": obs})

    assert "4,800.00" in text and "5,142.44" in text  # both figures shown
    assert POSTED == []


@pytest.mark.anyio
async def test_a_dropped_currency_is_named_in_the_preview(client):
    obs = make_observations().model_dump()
    obs["fx"] = [f for f in obs["fx"] if f["currency"] != "JPY"]

    async with client:
        text = await call_tool(client, "submit_snapshot", {"observations": obs})

    assert "WRITTEN" in text
    assert "DROPPED" in text and "JPY" in text
    rows = POSTED[0]["rows"]
    assert len(rows) == 28
    assert not any(r[2] == "JPY" for r in rows)


@pytest.mark.anyio
async def test_no_fx_observed_falls_back_to_holdings_json(client):
    """A missing multi-currency screenshot must not wipe five rows."""
    obs = make_observations().model_dump()
    obs["fx"] = []

    async with client:
        text = await call_tool(client, "submit_snapshot", {"observations": obs})

    assert "WRITTEN" in text
    assert "fell back to holdings.json" in text
    rows = POSTED[0]["rows"]
    assert len(rows) == 29
    assert next(r for r in rows if r[2] == "USD")[5] == 5142.44


@pytest.mark.anyio
async def test_etf_rows_are_unaffected_by_the_fx_rework(client):
    """ETF behaviour is explicitly out of scope — pin it so it stays that way."""
    obs = make_observations().model_dump()
    obs["fx"] = [{"currency": "KRW", "amount": 5.0}]  # roster churn around it

    async with client:
        await call_tool(client, "submit_snapshot", {"observations": obs})

    rows = POSTED[0]["rows"]
    etfs = [r for r in rows if r[1] == "ETF"]
    assert [r[2] for r in etfs] == list(ETF_TICKERS)   # still the fixed roster
    voo_index = next(i for i, r in enumerate(rows) if r[2] == "VOO")
    r = START_ROW + voo_index
    assert rows[voo_index][5] == 3.068                 # qty still from the file
    assert rows[voo_index][3] == f"=F{r}*G{r}*$K${START_ROW}"  # still anchored


@pytest.mark.anyio
async def test_an_unpriced_currency_is_not_reported_as_dropped(client):
    """A rate GOOGLEFINANCE cannot resolve is a different failure from a sale.

    The currency is excluded from the block either way, but calling it DROPPED
    would tell the user their holding disappeared when in fact the write was
    refused and nothing happened at all.
    """
    UNRESOLVED.add("CURRENCY:AUDIDR")

    async with client:
        text = await call_tool(
            client, "prepare_snapshot",
            {"observations": make_observations().model_dump()},
        )

    assert "Market data unresolved for: AUD" in text
    assert "DROPPED" not in text
    assert POSTED == []
