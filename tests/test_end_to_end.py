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

from tests.test_tracker import make_snapshot  # noqa: E402

START_ROW = 1600
POSTED: list[dict] = []

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
            self._json({"status": 200, "prices": {t: 100.0 for t in tickers}})
        elif action == "summary":
            self._json({
                "status": 200,
                "trend": {"values": [["Week", "Total"], ["W-0", "1,000"]]},
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
            client, "preview_snapshot",
            {"snapshot": make_snapshot().model_dump()},
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
            {"snapshot": make_snapshot().model_dump()},
        )

    assert "WRITTEN" in text
    assert len(POSTED) == 1

    payload = POSTED[0]
    assert payload["token"] == "test-gas-token"   # secret injected server-side
    assert payload["start_row"] == START_ROW
    assert payload["force"] is False

    rows = payload["rows"]
    assert len(rows) == 23
    assert all(len(r) == 11 for r in rows)
    assert rows[0][2] == "Mandiri"
    assert rows[0][10] == 16250.0                  # FX locked on first row only
    assert rows[-1][2] == "XLE"

    bbca = next(r for r in rows if r[2] == "BBCA")
    assert bbca[5] == 4400                          # 44 lots -> shares
    assert bbca[3] == f"=F{START_ROW + 8}*G{START_ROW + 8}"


@pytest.mark.anyio
async def test_validation_failure_blocks_the_write(client):
    bad = make_snapshot().model_dump()
    bad["banks"] = bad["banks"][:-1]  # drop BNI (RDN)

    async with client:
        text = await call_tool(client, "submit_snapshot", {"snapshot": bad})

    assert "REFUSED" in text
    assert "Missing bank account" in text
    assert POSTED == []


@pytest.mark.anyio
async def test_secret_is_never_exposed_to_the_model(client):
    """The GAS token must not appear in any tool output."""
    async with client:
        preview = await call_tool(
            client, "preview_snapshot",
            {"snapshot": make_snapshot().model_dump()},
        )
        prices = await call_tool(client, "get_etf_prices", {})

    assert "test-gas-token" not in preview
    assert "test-gas-token" not in prices
    assert "test-secret" not in preview


@pytest.mark.anyio
async def test_summary_renders_markdown(client):
    async with client:
        text = await call_tool(client, "get_summary", {})

    assert "## Week-to-Week Trend" in text
    assert "| Week | Total |" in text
    assert "| Cash | 500 |" in text


@pytest.fixture
def anyio_backend():
    return "asyncio"
