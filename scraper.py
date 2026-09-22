"""
Live data lookup for $STONK5 (https://stonk5.com/).

Earlier version scraped stonk5.com's homepage HTML directly, but that page's
numbers are refreshed client-side via JavaScript polling (visible on the site
as "18s ago" / "2s ago" freshness labels) — the raw HTML the server sends is
a stale/initial snapshot, not the live value. Scraping it gave numbers that
lagged badly behind the real site.

Instead, this module goes straight to the same live sources the site itself
ultimately reads from:
  - Price / market cap / liquidity -> DexScreener's public API (same data
    source most Solana token sites use, including likely stonk5.com itself).
  - Wallet holds / fees toward next round -> a direct Solana RPC balance
    check of the public "engine wallet" (fees toward next round is simply
    that wallet's current SOL balance, capped against the 5 SOL round
    trigger — confirmed by the two figures being identical on the live
    site).
  - Burned total -> total issued (1,000,000,000, fixed) minus the mint's
    current on-chain supply, via one Solana JSON-RPC call. This is the exact
    calculation stonk5.com's own frontend does for "Burned so far".

RPC calls go through Helius (needs a free HELIUS_API_KEY, see
https://www.helius.dev) instead of the public api.mainnet-beta.solana.com
endpoint, which is heavily shared/rate-limited and unreliable from a
hosting provider's shared IPs. Helius doesn't offer liquidity data or
prices for unverified tokens, so price/market cap/liquidity still come from
DexScreener — made more resilient with a browser-like User-Agent, a short
cache, and one retry on a 429.
"""

import os
import time

import requests

TIMEOUT = 15

# From stonk5.com's own page copy ("Contract address" on the homepage).
STONK5_MINT = "F7CTvENFnkDJysMhaFFicDT2FwnbW2oasFZGG6WJnar7"
TOTAL_ISSUED = 1_000_000_000

# The public wallet that collects creator fees between rounds ("Engine
# wallet" on stonk5.com).
ENGINE_WALLET = "gPYVhFeYVrfbAruwNVZthfnVdeWjgBUiaSabdpn77B6"
ROUND_TARGET_SOL = 5.0

# Wrapped SOL mint — fees can sit in the wallet as native SOL or as a WSOL
# token balance, so "wallet holds" needs to add both together.
WSOL_MINT = "So11111111111111111111111111111111111111112"

HELIUS_API_KEY = os.environ["HELIUS_API_KEY"]
SOLANA_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

DEXSCREENER_URL = f"https://api.dexscreener.com/latest/dex/tokens/{STONK5_MINT}"

# StonkFun's public token-listing API (no key required) — used for the
# current "Top 5 by market cap" basket that fee-swaps buy into.
STONKFUN_TOKENS_URL = "https://www.stonkfun.xyz/api/public/v1/tokens"
MIN_DAILY_VOLUME_USD = 10_000  # stonk5.com's own basket rule
DEXSCREENER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Short in-memory cache so a burst of /stat calls doesn't hammer DexScreener.
_market_cache: dict = {"data": None, "fetched_at": 0.0}
_MARKET_CACHE_TTL = 20  # seconds

# Primary price source. DexScreener persistently 429s from Render's shared
# IPs (not just a burst limit — retries don't help), so Jupiter's Price V3
# API is tried first; it also conveniently includes liquidity and 24h price
# change. DexScreener is kept as a fallback in case Jupiter is ever down.
JUPITER_PRICE_URL = "https://api.jup.ag/price/v3"


def _rpc(method: str, params: list) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    resp = requests.post(SOLANA_RPC_URL, json=payload, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Solana RPC error ({method}): {data['error']}")
    return data["result"]


def _market_stats_from_dexscreener() -> dict:
    resp = requests.get(DEXSCREENER_URL, headers=DEXSCREENER_HEADERS, timeout=TIMEOUT)
    if resp.status_code == 429:
        time.sleep(1.5)
        resp = requests.get(DEXSCREENER_URL, headers=DEXSCREENER_HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    pairs = data.get("pairs") or []
    if not pairs:
        raise RuntimeError("No trading pairs found on DexScreener for this token")

    # Pick the pair with the highest liquidity (most representative price).
    pair = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)

    return {
        "price_usd": float(pair["priceUsd"]) if pair.get("priceUsd") else None,
        "market_cap": pair.get("marketCap"),
        "liquidity_usd": (pair.get("liquidity") or {}).get("usd"),
    }


def _market_stats_from_jupiter() -> dict:
    """Price + liquidity from Jupiter's Price V3 API. Market cap isn't
    returned directly, so it's computed from price times the current
    on-chain supply (via Helius, one extra RPC call)."""
    resp = requests.get(JUPITER_PRICE_URL, params={"ids": STONK5_MINT}, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    entry = data.get(STONK5_MINT)
    if not entry or entry.get("usdPrice") is None:
        raise RuntimeError("No price found on Jupiter for this token")

    price = float(entry["usdPrice"])
    liquidity = entry.get("liquidity")

    supply_result = _rpc("getTokenSupply", [STONK5_MINT])
    current_supply = float(supply_result["value"]["uiAmountString"])

    return {
        "price_usd": price,
        "market_cap": price * current_supply,
        "liquidity_usd": float(liquidity) if liquidity is not None else None,
        "price_change_24h": entry.get("priceChange24h"),
    }


def get_market_stats() -> dict:
    """Live price/market cap/liquidity, cached briefly. Tries Jupiter's
    Price V3 API first (reliable from Render's shared IPs, also includes
    liquidity and 24h price change); falls back to DexScreener if Jupiter
    is ever unavailable."""
    now = time.time()
    if _market_cache["data"] is not None and now - _market_cache["fetched_at"] < _MARKET_CACHE_TTL:
        return _market_cache["data"]

    try:
        result = _market_stats_from_jupiter()
    except Exception as jup_error:
        try:
            result = _market_stats_from_dexscreener()
        except Exception as dex_error:
            raise RuntimeError(
                f"Jupiter failed ({jup_error}); DexScreener fallback also failed ({dex_error})"
            )

    _market_cache["data"] = result
    _market_cache["fetched_at"] = now
    return result


def _get_wsol_balance(owner: str) -> float:
    """Sums the WSOL (wrapped SOL) token balance(s) held by an owner
    address. Returns 0.0 if the owner has no WSOL token account."""
    result = _rpc(
        "getTokenAccountsByOwner",
        [owner, {"mint": WSOL_MINT}, {"encoding": "jsonParsed"}],
    )
    total = 0.0
    for entry in result.get("value", []):
        parsed = entry["account"]["data"]["parsed"]["info"]
        total += float(parsed["tokenAmount"]["uiAmount"] or 0)
    return total


def get_wallet_and_fees() -> dict:
    """Wallet holds = native SOL balance of the engine wallet PLUS any
    wrapped SOL (WSOL) token balance it holds — fees can accumulate as
    either. Fees toward next round is the same combined total, shown as
    progress toward the 5 SOL round trigger."""
    native_result = _rpc("getBalance", [ENGINE_WALLET])
    native_sol = native_result["value"] / 1_000_000_000
    wsol = _get_wsol_balance(ENGINE_WALLET)
    total_sol = native_sol + wsol

    return {
        "wallet_sol": total_sol,
        "native_sol": native_sol,
        "wsol": wsol,
        "fees_progress_sol": total_sol,
        "fees_target_sol": ROUND_TARGET_SOL,
        "fees_pct": min(total_sol / ROUND_TARGET_SOL, 1.0) * 100,
    }


def get_burned_total() -> dict:
    """Computes the burned total the same way stonk5.com does: total issued
    minus the mint's current on-chain supply."""
    result = _rpc("getTokenSupply", [STONK5_MINT])
    current_supply = float(result["value"]["uiAmountString"])
    burned = TOTAL_ISSUED - current_supply
    pct = (burned / TOTAL_ISSUED) * 100

    return {
        "burned": burned,
        "current_supply": current_supply,
        "pct_of_supply": pct,
    }


def _pick(d: dict, *candidates, default=None):
    """Tries several possible key names (including dotted paths like
    'stats.marketCap') since the exact field names of StonkFun's API
    response haven't been confirmed against a live call yet."""
    for key in candidates:
        node = d
        ok = True
        for part in key.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                ok = False
                break
        if ok and node is not None:
            return node
    return default


def get_top5() -> list[dict]:
    """The current Top 5 StonkFun tokens by market cap that stonk5.com's
    fee-swaps buy into. Mirrors stonk5.com's own basket rule: tokens
    trading at least $10,000/day, ranked by market cap.

    No `status=graduated` filter is applied here on purpose — $STONK (the
    StonkFun platform's own token) is confirmed to appear in stonk5.com's
    live Top 5, and it's unclear whether StonkFun's API tags it as
    "graduated" the same way a regular launched token would be. Filtering
    by status risked silently excluding it, so this only sorts by market
    cap and applies the $10k/day volume floor.

    Field names are guessed defensively (_pick tries several candidates)
    since StonkFun's exact response schema wasn't confirmed via a live
    call while building this. If the output looks wrong (missing names,
    zero volume, STONK missing, etc.), send the /top5 output back and the
    field names in _pick() below can be corrected."""
    params = {"sort": "marketCap", "pageSize": 25}
    resp = requests.get(STONKFUN_TOKENS_URL, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    tokens = data if isinstance(data, list) else _pick(data, "tokens", "data", "items", default=[])

    candidates = []
    for t in tokens:
        market_cap = _pick(t, "marketCap", "market_cap", "stats.marketCap", "market.cap")
        volume = _pick(
            t, "volume24h", "volume24H", "volume_24h", "stats.volume24h", "volume.h24", default=0
        )
        name = _pick(t, "name", "tokenName", default="?")
        symbol = _pick(t, "symbol", "ticker", default="?")

        if market_cap is None:
            continue
        # Only drop the volume filter if the field truly isn't present
        # anywhere (volume == 0 default) — otherwise apply stonk5's rule.
        if volume and volume < MIN_DAILY_VOLUME_USD:
            continue

        candidates.append(
            {
                "name": name,
                "symbol": symbol,
                "market_cap": market_cap,
                "volume_24h": volume,
            }
        )

    candidates.sort(key=lambda t: t["market_cap"] or 0, reverse=True)
    return candidates[:5]


if __name__ == "__main__":
    import json

    print("Market:", json.dumps(get_market_stats(), indent=2, ensure_ascii=False))
    print("Wallet/fees:", json.dumps(get_wallet_and_fees(), indent=2, ensure_ascii=False))
    print("Burned:", json.dumps(get_burned_total(), indent=2, ensure_ascii=False))
    print("Top 5:", json.dumps(get_top5(), indent=2, ensure_ascii=False))
