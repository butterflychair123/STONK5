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

from scraper import get_market_stats, get_wallet_holds, get_round_timer, get_burned_total, get_top5

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
    lines = ["*$STONK5 — Stats*", ""]

    try:
        market = get_market_stats()
        if market.get("price_usd") is not None:
            change = market.get("price_change_24h")
            change_str = f" ({change:+.1f}% 24h)" if change is not None else ""
            lines.append(f"💲 Price: `${market['price_usd']:.6f}`{change_str}")
        if market.get("market_cap") is not None:
            lines.append(f"📊 Market cap: `${market['market_cap']:,.0f}`")
        if market.get("liquidity_usd") is not None:
            lines.append(f"💧 Liquidity: `${market['liquidity_usd']:,.0f}`")
    except Exception as e:
        logger.exception("Failed to fetch market stats")
        lines.append(f"⚠️ Could not fetch price/market data: {e}")

    try:
        wallet = get_wallet_holds()
        lines.append(
            f"👛 Wallet holds: `{wallet['wallet_sol']:.3f} SOL` "
            f"({wallet['native_sol']:.3f} SOL + {wallet['wsol']:.3f} WSOL)"
        )
        lines.append(
            "_Note: not all of this pays out next round — some is reserved "
            "as rent, and some may be spare SOL topped up by hand._"
        )
    except Exception as e:
        logger.exception("Failed to fetch wallet balance")
        lines.append(f"⚠️ Could not fetch wallet data: {e}")

    try:
        timer = get_round_timer()
        if timer["due"]:
            lines.append("⏰ Next round: `due now`")
        else:
            h = int(timer["remaining_hours"])
            m = int((timer["remaining_hours"] - h) * 60)
            lines.append(f"⏰ Next round in: `~{h}h {m:02d}m` (or sooner if 5 SOL is reached)")
    except Exception as e:
        logger.exception("Failed to fetch round timer")
        lines.append(f"⚠️ Could not determine round timer: {e}")

    if len(lines) == 2:
        lines.append("No figures found.")

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


def _fmt_top5() -> str:
    try:
        top5 = get_top5()
    except Exception as e:
        logger.exception("Failed to fetch top 5")
        return f"⚠️ Could not fetch the StonkFun leaderboard: {e}"

    if not top5:
        return "No qualifying tokens found — the API response format may have changed."

    lines = ["*$STONK5 — Current Top 5*", "", "The 5 tokens fee-swaps are currently buying:", ""]
    for i, t in enumerate(top5, start=1):
        lines.append(
            f"{i}. *${t['symbol']}* — {t['name']}\n"
            f"   Market cap: `${t['market_cap']:,.0f}` · 24h volume: `${t['volume_24h']:,.0f}`"
        )

    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hi! I show price/fee stats and the burned total for $STONK5.\n\n"
        "Commands:\n"
        "/stat or /fees – price, market cap, liquidity, wallet holds\n"
        "/burn – total $STONK5 burned so far (read directly from the chain)\n"
        "/top5 – the current top 5 StonkFun tokens being bought"
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_markdown(_fmt_stats())


async def burned(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_markdown(_fmt_burned())


async def top5(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_markdown(_fmt_top5())


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler(["stat", "fees"], stats))
    app.add_handler(CommandHandler("burn", burned))
    app.add_handler(CommandHandler("top5", top5))

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
