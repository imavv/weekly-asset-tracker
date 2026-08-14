"""Vercel serverless entrypoint.

Vercel looks for a module-level `app` and serves it as an ASGI application.
All the real work lives in the `tracker` package so it stays testable and is
not coupled to any particular host.
"""

from tracker.app import app  # noqa: F401
