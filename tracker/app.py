"""ASGI application — the secret-path gate wrapped around the MCP server.

Two host-shaped problems are solved here.

**Authentication.** The URL *is* the credential: a bearer secret, like a house
key. Whoever holds it can call the tools, so it must never be committed — it
lives in the MCP_SECRET environment variable and is pasted into Claude's
connector settings once. Unauthorised requests get 404 rather than 403, so a
scanner cannot tell "wrong secret" from "nothing here".

The secret is accepted either as a path segment or as the `k` query parameter.
Vercel's catch-all rewrite replaces the request path before the function sees
it, so a path-borne secret can vanish in transit; the query string survives.
`?diag=1` reports how the URL actually arrived, without echoing the secret.

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
import json
import logging
import os
from urllib.parse import parse_qs

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


async def _respond(
    send, status: int, body: bytes, content_type: bytes = b"text/plain; charset=utf-8"
) -> None:
    """Minimal ASGI response, used for 404s, health and diagnostics."""
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", content_type),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


def _authorised(path: str, query: dict[str, list[str]]) -> bool:
    """True if MCP_SECRET appears as a path segment or as the `k` query param.

    Both forms are accepted because hosts rewrite paths. Vercel's catch-all
    rewrite replaces the request path before the function sees it, so a secret
    carried in the path can disappear — the query string survives. Path form
    stays supported for hosts that preserve it, and for local development.

    compare_digest avoids leaking the secret's length through timing.
    """
    secret = mcp_secret()
    candidates = [seg for seg in path.split("/") if seg] + query.get("k", [])
    return any(hmac.compare_digest(c, secret) for c in candidates)


def _diagnostics(scope, path: str, query: dict[str, list[str]]) -> bytes:
    """What the app actually received, for debugging host routing.

    Deliberately excludes headers, environment and the secret itself — it
    reports only how the URL arrived, so it is safe to leave unauthenticated.
    """
    return json.dumps({
        "observed_path": path,
        "path_segments": [seg for seg in path.split("/") if seg],
        "query_keys": sorted(query),
        "secret_supplied_in_query": bool(query.get("k")),
        "method": scope.get("method"),
        "root_path": scope.get("root_path", ""),
    }, indent=2).encode()


async def app(scope, receive, send):
    """ASGI entrypoint: authorise, then dispatch to a fresh transport app."""
    if scope["type"] != "http":
        return

    path: str = scope.get("path", "")
    query = parse_qs(scope.get("query_string", b"").decode(), keep_blank_values=True)

    # Routing diagnostic: ?diag=1 on any path. Reveals no secrets.
    if query.get("diag"):
        return await _respond(send, 200, _diagnostics(scope, path, query), b"application/json")

    # Unauthenticated liveness probe. Matches on the final segment, or ?health=1,
    # so it survives a host rewriting the path prefix.
    if path.rstrip("/").rsplit("/", 1)[-1] == "healthz" or query.get("health"):
        return await _respond(send, 200, b"ok")

    if not _authorised(path, query):
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
