"""Weekly asset tracker — MCP server engine.

Pure, testable logic for turning a *semantic* portfolio snapshot (what Claude
read off the screenshots) into the 11-column A–K grid the Google Sheet expects,
then writing it via the Apps Script web app.

Nothing in this package knows about MCP; `tracker.server` wires it up.
"""
