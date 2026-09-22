# $STONK5 Telegram Bot

Shows price/fee stats and the total burned amount for [$STONK5](https://stonk5.com/) on request.

## Commands

- `/stat` — price, market cap, liquidity, fees toward next round, wallet holds
- `/burned` — total $STONK5 burned so far, read directly from the Solana blockchain
- `/start` — help

## How it works

- `/stat` scrapes the server-rendered homepage HTML of stonk5.com — the same
  technique as the 4STONK bot.
- `/burned` does **not** scrape the `/burn-lock` page, because that page's
  figures are loaded client-side by JavaScript reading the blockchain (the
  raw HTML just shows a "reading the chain…" placeholder). Instead,
  `scraper.py` makes one Solana JSON-RPC call (`getTokenSupply`) for the
  $STONK5 mint and computes `burned = 1,000,000,000 - current_supply` — the
  exact same calculation stonk5.com's own frontend does.

**Not included (by design, to keep this simple):** the exact number of
tokens already locked in Jupiter Lock's 5-year escrow. That figure isn't
exposed through the token's own supply and would require decoding Jupiter
Lock's on-chain program accounts — a good deal more work than the rest of
this bot. Let me know if you want that added later.

**Note:** if stonk5.com changes its page layout, `_find_after` in
`scraper.py` may need updating — run `python3 scraper.py` to check what it
finds. If the mint address or total-issued supply ever changes, update
`STONK5_MINT` / `TOTAL_ISSUED` at the top of `scraper.py`.

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
