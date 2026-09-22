"""
Telegram bot that shows current price/fees stats and the total burned amount
for $STONK5 (https://stonk5.com/) on request.

Works both via polling (running locally) and via webhook (for hosting on
Render.com, see README.md).
"""

import logging
import os

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from scraper import get_homepage_stats, get_burned_total

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
# For webhook hosting (Render): the public URL of your service, e.g.
# https://your-app.onrender.com  (no trailing slash)
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", "8080"))


def _fmt_stats() -> str:
    try:
        home = get_homepage_stats()
    except Exception as e:
        logger.exception("Failed to fetch homepage")
        return f"⚠️ Could not reach stonk5.com: {e}"

    lines = ["*$STONK5 — Stats*", ""]

    if home.get("price"):
        lines.append(f"💲 Price: `{home['price']}`")
    if home.get("market_cap"):
        lines.append(f"📊 Market cap: `{home['market_cap']}`")
    if home.get("liquidity"):
        lines.append(f"💧 Liquidity: `{home['liquidity']}`")
    if home.get("fees_progress"):
        pct = f" ({home['fees_pct']})" if home.get("fees_pct") else ""
        lines.append(f"⏳ Fees toward next round: `{home['fees_progress']}`{pct}")
    if home.get("wallet_holds"):
        lines.append(f"👛 Wallet holds: `{home['wallet_holds']}`")

    if len(lines) == 2:
        lines.append("No figures found — the site layout may have changed.")

    return "\n".join(lines)


def _fmt_burned() -> str:
    try:
        burned = get_burned_total()
    except Exception as e:
        logger.exception("Failed to fetch burned total")
        return f"⚠️ Could not fetch on-chain burn data: {e}"

    lines = ["*$STONK5 — Burned*", ""]
    lines.append(f"🔥 Burned so far: `{burned['burned']:,.0f}` $STONK5")
    lines.append(f"📉 That's `{burned['pct_of_supply']:.3f}%` of the 1,000,000,000 issued")
    lines.append(f"🪙 Current supply: `{burned['current_supply']:,.0f}` $STONK5")

    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hi! I show price/fee stats and the burned total for $STONK5.\n\n"
        "Commands:\n"
        "/stat – price, market cap, liquidity, fees toward next round\n"
        "/burned – total $STONK5 burned so far (read directly from the chain)"
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_markdown(_fmt_stats())


async def burned(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_markdown(_fmt_burned())


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stat", stats))
    app.add_handler(CommandHandler("burned", burned))

    if WEBHOOK_URL:
        logger.info("Starting in webhook mode on %s", WEBHOOK_URL)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
        )
    else:
        logger.info("Starting in polling mode")
        app.run_polling()


if __name__ == "__main__":
    main()
