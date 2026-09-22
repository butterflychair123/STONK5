# $STONK5 Telegram Bot

Shows price/fee stats, burned total, and locked-tokens total for
[$STONK5](https://stonk5.com/) on request.

## Commands

- `/stat` or `/fees` — price, market cap, liquidity, wallet holds, round timer
- `/burn`, `/burned`, `/lock` or `/locked` — burned total + locked/vault totals in one overview
- `/top5` — the current top 5 StonkFun tokens fee-swaps are buying
- `/start` — help

## How it works

All figures are read live from on-chain data or APIs — nothing is scraped
from stonk5.com's HTML anymore, since its numbers refresh client-side via
JavaScript polling and the raw server HTML is a stale snapshot.

- **Price / market cap / liquidity**: Jupiter's Price V3 API (falls back to
  DexScreener).
- **Wallet holds**: direct Solana RPC balance check (native SOL + WSOL) of
  the public "engine wallet". This is *not* the same as "fees toward next
  round" — the wallet balance also includes a rent reserve and any manually
  topped-up spare SOL, and there's no RPC call that exposes that split, so
  the bot only reports the honest total with a disclaimer.
- **Round timer**: detected as the most recent transaction where the engine
  wallet's combined SOL+WSOL balance dropped sharply (a heuristic, since
  there's no structured "round" event to read directly).
- **Burned total**: `1,000,000,000 - current on-chain supply`, via one
  `getTokenSupply` RPC call — the same calculation stonk5.com's own frontend
  does.
- **Locked total**: `/burn` (same as `/locked`) shows the burned total plus
  two more numbers — "in the vault" (a direct token-balance check of the
  public vault address that accumulates $STONK5 before its weekly sweep)
  and "locked" (tokens already swept into Jupiter Lock's 5-year escrow),
  plus a combined burned+locked+vault total. The escrow total isn't exposed
  by a single RPC call, so it's reconstructed by scanning the vault's
  transaction history for its periodic outgoing transfers and summing them
  — the same heuristic technique used for the round timer. Like the round
  timer, this may need threshold tuning (`LOCK_SWEEP_THRESHOLD_TOKENS` in
  `scraper.py`) if the number doesn't match stonk5.com's own `/burn-lock`
  page — send back the bot's output and the real numbers if so.

**Note:** if the mint address, engine wallet, vault address, or total-issued
supply ever change, update the constants at the top of `scraper.py`.

## Setup

Same as the 4STONK bot:

1. Create a bot via **@BotFather** in Telegram, get its token
2. `pip install -r requirements.txt`
3. Local test: `TELEGRAM_BOT_TOKEN=... python3 bot.py`
4. Deploy free on Render.com (Web Service, webhook mode):
   - Build command: `pip install -r requirements.txt`
   - Start command: `python3 bot.py`
   - Env vars: `TELEGRAM_BOT_TOKEN`, and `WEBHOOK_URL` (set after first deploy, no trailing slash)
   - `runtime.txt` pins Python to 3.11 (needed for `python-telegram-bot` to work correctly on Render)
