"""ASGI application — the secret-path gate wrapped around the MCP server.

Two host-shaped problems are solved here.

**Authentication.** The URL *is* the credential: a bearer secret, like a house
key. Whoever holds it can call the tools, so it must never be committed — it
lives in the MCP_SECRET environment variable and is pasted into Claude's
connector settings once. Unauthorised requests get 404 rather than 403, so a
scanner cannot tell "wrong secret" from "nothing here".

**Lifespan.** The MCP transport starts a task group in its ASGI lifespan, and
serverless hosts (Vercel among them) do not reliably emit lifespan events — the
server would raise "Task group is not initialized" on the first call. Because we
run stateless, nothing needs to survive between requests, so we build a fresh
transport app per request and run its lifespan around that one request. Startup
and shutdown then happen in the same task that handles the call, which also
avoids anyio's cross-task cancel-scope trap.
"""

from __future__ import annotations

import hmac
import logging
import os

from mcp.server.transport_security import TransportSecuritySettings

from .config import mcp_secret
from .server import mcp

log = logging.getLogger(__name__)

# Where the SDK's Streamable HTTP endpoint lives inside the transport app.
MCP_PATH = "/mcp"


def _transport_security() -> TransportSecuritySettings:
    """Host/Origin policy for the Streamable HTTP transport.

    The SDK enables DNS-rebinding protection by default with an empty allow
    list, which rejects every request once deployed. That default protects MCP
    servers bound to localhost, where a malicious web page could otherwise reach
    them through the user's browser. This server is public over HTTPS and
    guarded by a secret path a browser cannot discover, so the check adds
    nothing here — but you can still pin it by setting MCP_ALLOWED_HOSTS to a
    comma-separated list of hostnames.
    """
    allowed = [
        h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()
    ]
    if allowed:
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed,
            allowed_origins=[f"https://{h}" for h in allowed],
        )
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


def build_transport_app():
    """A fresh Streamable HTTP app. Must not be shared between requests —
    its session manager may only run once."""
    return mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        # Plain JSON replies rather than SSE streams: nothing here streams, and
        # a single response body suits a serverless function far better.
        json_response=True,
        transport_security=_transport_security(),
    )


async def _respond(send, status: int, body: bytes) -> None:
    """Minimal ASGI response, used for 404s and the health check."""
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


def _secret_in_path(path: str) -> bool:
    """True if any path segment matches MCP_SECRET.

    Matching on *any* segment rather than a fixed prefix keeps this working
    regardless of how the host rewrites the incoming URL — Vercel's rewrite
    rules, a trailing slash, or a stripped prefix all still resolve.

    compare_digest avoids leaking the secret's length through timing.
    """
    secret = mcp_secret()
    return any(
        hmac.compare_digest(segment, secret) for segment in path.split("/") if segment
    )


async def app(scope, receive, send):
    """ASGI entrypoint: authorise, then dispatch to a fresh transport app."""
    if scope["type"] != "http":
        return

    path: str = scope.get("path", "")

    # Unauthenticated liveness probe. Reveals nothing about the secret.
    if path.rstrip("/") in ("/healthz", "/api/healthz"):
        return await _respond(send, 200, b"ok")

    if not _secret_in_path(path):
        log.warning("Rejected request with bad or missing secret")
        return await _respond(send, 404, b"Not Found")

    # Authorised. Rewrite to the path the transport actually serves, so the
    # secret segment never reaches the inner router.
    inner = dict(scope)
    inner["path"] = MCP_PATH
    inner["raw_path"] = MCP_PATH.encode()

    transport = build_transport_app()
    async with transport.router.lifespan_context(transport):
        await transport(inner, receive, send)
