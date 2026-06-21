#!/usr/bin/env python3
"""
bot.py
──────
Telegram front-end for the weekly asset tracker.

Flow (decided, see HANDOVER):
  1. User sends one or more screenshots — buffered in memory.
  2. User sends `/run YYYY-MM-DD` — Claude is called ONCE in dry-run mode,
     the parsed 23-row result is cached, and a human-readable preview is
     returned. No Sheets write yet.
  3. User sends `/confirm` — the ALREADY-PARSED result is written to Sheets
     (no second Claude call).
  4. `/clear` discards buffered photos and any cached result at any time.

Security: only the whitelisted Telegram user ID (Ama) may interact.

Run:
  python bot.py
Env vars (Railway / local .env):
  BOT_TOKEN, ALLOWED_USER_ID, ANTHROPIC_API_KEY, GAS_ENDPOINT, GAS_SECRET_TOKEN
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import portfolio_tracker as pt
import render

load_dotenv(override=True)  # .env wins over empty/stale inherited env vars

logging.basicConfig(
    format="%(asctime)s  %(levelname)s  %(name)s  %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # silence per-poll request lines
log = logging.getLogger("tracker-bot")

# ── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
# Whitelist. Only this Telegram user ID may use the bot.
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])

# Telegram messages cap at 4096 chars; keep a margin for the code-fence wrapper.
MAX_MSG = 3900
# ──────────────────────────────────────────────────────────────────────────────


# ── Per-session state ─────────────────────────────────────────────────────────
# Single-user bot, so one module-level state dict is enough. Reset by
# /confirm and /clear.
state: dict = {
    "photos": [],            # list of temp file paths, accumulated until /run or /clear
    "parsed_result": None,   # dict from pt.parse_screenshots(), held until /confirm
    "pending_confirm": False,
}


def reset_state() -> None:
    """Discard buffered photos (deleting temp files) and cached result."""
    for p in state["photos"]:
        try:
            os.unlink(p)
        except OSError:
            pass
    state["photos"] = []
    state["parsed_result"] = None
    state["pending_confirm"] = False


def authorized(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id == ALLOWED_USER_ID


# ── Handlers ──────────────────────────────────────────────────────────────────
async def guard(update: Update) -> bool:
    """Reject non-whitelisted users. Returns True if allowed to proceed."""
    if authorized(update):
        return True
    uid = update.effective_user.id if update.effective_user else "?"
    log.warning("Rejected message from unauthorized user id=%s", uid)
    if update.message:
        await update.message.reply_text("Not authorized.")
    return False


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await update.message.reply_text(
        "Asset tracker bot ready.\n\n"
        "1. Send your screenshots (any number).\n"
        "2. /run [YYYY-MM-DD] [model]  — parse + preview.\n"
        "      date optional (defaults to today); model optional (sonnet|opus|haiku).\n"
        "      e.g. /run  ·  /run sonnet  ·  /run 2026-06-19 opus\n"
        "3. /confirm         — write the preview to Sheets.\n"
        "/summary            — fetch the latest summary tables as images.\n"
        "/clear              — discard buffered photos and reset."
    )


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return

    # Accept both compressed photos and image documents.
    if update.message.photo:
        tg_file = await update.message.photo[-1].get_file()  # highest resolution
        suffix = ".jpg"
    else:
        doc = update.message.document
        suffix = Path(doc.file_name or "image.jpg").suffix or ".jpg"
        tg_file = await doc.get_file()

    fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="screenshot_")
    os.close(fd)
    await tg_file.download_to_drive(tmp_path)
    state["photos"].append(tmp_path)

    # A new screenshot invalidates any pending preview.
    state["parsed_result"] = None
    state["pending_confirm"] = False

    log.info("Buffered screenshot %s (total %d)", tmp_path, len(state["photos"]))
    await update.message.reply_text(
        f"📸 Got it — {len(state['photos'])} screenshot(s) buffered. "
        f"Send more, or /run YYYY-MM-DD."
    )


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return

    if not state["photos"]:
        await update.message.reply_text("No screenshots buffered yet. Send some first.")
        return

    # Args are optional and order-independent: a YYYY-MM-DD token is the date,
    # anything else is the model alias/id. Examples:
    #   /run                 → today, default model
    #   /run sonnet          → today, sonnet
    #   /run 2026-06-19      → that date, default model
    #   /run 2026-06-19 opus → that date, opus
    date, model = None, None
    for arg in context.args:
        tok = arg.strip().lstrip("-")            # tolerate a leading --
        if tok.lower().startswith("model="):
            tok = tok.split("=", 1)[1]
        elif tok.lower() == "model":
            continue
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", tok):
            date = tok
        else:
            model = tok
    date = date or pt.today_wib()
    model_name = pt.resolve_model(model)["name"]

    await update.message.reply_text(
        f"⏳ Parsing {len(state['photos'])} screenshot(s) for {date} "
        f"using model={model_name} — this calls Claude and may take a minute..."
    )
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    photos = list(state["photos"])
    try:
        # pt.parse_screenshots is blocking (network + image work); run it off
        # the event loop so the bot stays responsive. Checks are non-blocking now
        # — it returns issues for review instead of raising on a bad table.
        result = await asyncio.to_thread(pt.parse_screenshots, photos, date, None, model)
    except Exception as e:  # noqa: BLE001 — real failures (API/network); surface them
        log.exception("parse_screenshots failed")
        await update.message.reply_text(f"❌ Parse failed: {e}")
        return

    # Always cache + offer confirm; the user is the gate now.
    state["parsed_result"] = result
    state["pending_confirm"] = True

    errors = result.get("errors") or []
    warnings = result.get("warnings") or []
    notes = result.get("notes") or []
    log.info("Parsed %d rows, %d error(s), %d warning(s); archived to %s",
             len(result["rows"]), len(errors), len(warnings), result.get("log_dir"))

    body = result["preview"]
    if notes:
        body += "\n\nNotes from Claude:\n" + "\n".join(notes)
    await send_long(update, body)

    if errors:
        await send_long(update, "⚠️ Issues flagged (review carefully):\n"
                        + "\n".join(f"- {e}" for e in errors))
    if warnings:
        await send_long(update, "ℹ️ Price-sanity warnings:\n"
                        + "\n".join(f"- {w}" for w in warnings))

    n = len(result["rows"])
    status = ("⚠️ Parsed %d rows for row %d — issues above, REVIEW before writing." % (n, result["start_row"])
              if errors else
              "✅ Parsed %d rows for row %d — all checks passed." % (n, result["start_row"]))
    await update.message.reply_text(
        status + "\n\nReply /confirm to write to Sheets or /clear to abort."
    )


async def cmd_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return

    if not state["pending_confirm"] or not state["parsed_result"]:
        await update.message.reply_text("Nothing to confirm. Run /run YYYY-MM-DD first.")
        return

    result = state["parsed_result"]
    await update.message.reply_text("⏳ Writing to Sheets...")

    try:
        gas_result = await asyncio.to_thread(
            pt.write_rows, result["rows"], result["start_row"]
        )
    except Exception as e:  # noqa: BLE001
        log.exception("write_rows failed")
        await update.message.reply_text(f"❌ Write failed: {e}\n\nResult kept — you can /confirm again.")
        return

    if gas_result.get("status") == 200:
        n = len(result["rows"])
        reset_state()
        await update.message.reply_text(f"✅ Done — {n} rows written to Sheets.")
        # Summary tables now reflect the new rows — fetch, render, send.
        await send_summary(update, context)
    else:
        # Keep state so the user can retry without re-parsing.
        await update.message.reply_text(
            f"❌ Sheets rejected the write: {gas_result}\n\nResult kept — you can /confirm again."
        )


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await send_summary(update, context)


async def send_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch the two summary tables from GAS, render each to a PNG, and send
    both to the chat. Used after a successful /confirm and by /summary."""
    await update.message.reply_text("📊 Fetching summary tables...")
    try:
        summary = await asyncio.to_thread(pt.fetch_summary)
    except Exception as e:  # noqa: BLE001
        log.exception("fetch_summary failed")
        await update.message.reply_text(f"⚠️ Couldn't fetch summary: {e}")
        return

    panels = [
        ("trend",     "📈 Week-to-Week Trend"),
        ("breakdown", "🥧 Asset Breakdown"),
    ]
    for key, caption in panels:
        try:
            # title=None → image is just the table, preserving the sheet's format.
            img = await asyncio.to_thread(render.render_table, summary.get(key, {}), None, f"{key}.png")
        except Exception as e:  # noqa: BLE001
            log.exception("render failed for %s", key)
            await update.message.reply_text(f"⚠️ Couldn't render '{title}': {e}")
            continue
        await update.message.reply_photo(img, caption=caption)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    reset_state()
    await update.message.reply_text("🗑️ Cleared. Buffered photos and cached result discarded.")


async def send_long(update: Update, text: str) -> None:
    """Send text in <=MAX_MSG plain-text chunks (no parse_mode, so '=' / '$'
    in formulas don't need escaping)."""
    for i in range(0, len(text), MAX_MSG):
        await update.message.reply_text(text[i:i + MAX_MSG])


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("confirm", cmd_confirm))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))

    log.info("Bot starting (allowed user id=%s)...", ALLOWED_USER_ID)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
