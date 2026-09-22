"""
Live data lookup for $STONK5 (https://stonk5.com/).

Earlier version scraped stonk5.com's homepage HTML directly, but that page's
numbers are refreshed client-side via JavaScript polling (visible on the site
as "18s ago" / "2s ago" freshness labels) — the raw HTML the server sends is
a stale/initial snapshot, not the live value. Scraping it gave numbers that
lagged badly behind the real site.

Instead, this module goes straight to the same live sources the site itself
ultimately reads from:
  - Price / market cap / liquidity -> Jupiter's Price V3 API (falls back to
    DexScreener, which persistently 429s from some hosting providers'
    shared IPs).
  - Wallet holds -> a direct Solana RPC balance check (native SOL + WSOL) of
    the public "engine wallet". NOTE: this total is not the same as the
    site's "Rewards" figure that actually drives round progress — the
    wallet balance also includes a Rent reserve and any manually topped-up
    Spare SOL, and there's no RPC call that exposes that split.
  - Round timer -> the timestamp of the last "round" transaction (detected
    as the most recent transaction where the engine wallet's SOL balance
    dropped sharply) via Helius's transaction history, used to compute time
    remaining until the 5-hour trigger.
  - Burned total -> total issued (1,000,000,000, fixed) minus the mint's
    current on-chain supply, via one Solana JSON-RPC call. This is the exact
    calculation stonk5.com's own frontend does for "Burned so far".
  - Lock stats -> "In the vault" is a direct token-balance check of the
    public vault address. "Locked" (already committed to Jupiter Lock's
    5-year escrow) isn't exposed by a single call, so it's reconstructed by
    scanning the vault's transaction history for its periodic outgoing
    transfers (the weekly sweep into Jupiter Lock) and summing them —
    same technique as the round timer below.

RPC calls go through Helius (needs a free HELIUS_API_KEY, see
https://www.helius.dev) instead of the public api.mainnet-beta.solana.com
endpoint, which is heavily shared/rate-limited and unreliable from a
hosting provider's shared IPs.
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

# Wrapped SOL mint — fees can sit in the wallet as native SOL or as a WSOL
# token balance, so "wallet holds" needs to add both together.
WSOL_MINT = "So11111111111111111111111111111111111111112"

# The vault that accumulates $STONK5 bought for locking, before its weekly
# sweep into Jupiter Lock's 5-year escrow ("Burn and lock" page on
# stonk5.com — "the vault address is 9ezeAq8ozEuUru4vFaEXQBn56fdUF7SQoQ4xSTs3Pko").
VAULT_ADDRESS = "9ezeAq8ozEuUru4vFaEXQBn56fdUF7SQoQ4xSTs3Pko"

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


def _get_token_balance(owner: str, mint: str) -> float:
    """Sums the token balance(s) of a given mint held by an owner address.
    Returns 0.0 if the owner has no token account for that mint."""
    result = _rpc(
        "getTokenAccountsByOwner",
        [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
    )
    total = 0.0
    for entry in result.get("value", []):
        parsed = entry["account"]["data"]["parsed"]["info"]
        total += float(parsed["tokenAmount"]["uiAmount"] or 0)
    return total


def _get_wsol_balance(owner: str) -> float:
    return _get_token_balance(owner, WSOL_MINT)


def get_wallet_holds() -> dict:
    """Wallet holds = native SOL balance of the engine wallet PLUS any
    wrapped SOL (WSOL) token balance it holds.

    Note: this total is NOT the same as "fees toward next round" on
    stonk5.com. The site's own tooltip breaks the wallet balance into three
    parts — Rewards (counts toward the 5 SOL round trigger), Rent (reserved
    for opening payout accounts), and Spare (manually topped up, unused) —
    and only Rewards drives the round progress bar. There's no RPC call that
    exposes that split; it's internal accounting on stonk5's side based on
    its own transaction history. So this only reports the honest total
    wallet balance, without claiming it equals round progress."""
    native_result = _rpc("getBalance", [ENGINE_WALLET])
    native_sol = native_result["value"] / 1_000_000_000
    wsol = _get_wsol_balance(ENGINE_WALLET)
    total_sol = native_sol + wsol

    return {
        "wallet_sol": total_sol,
        "native_sol": native_sol,
        "wsol": wsol,
    }


ROUND_INTERVAL_HOURS = 5.0
# A round spends from the wallet: burn + lock + basket buy + payouts. Any
# single transaction that drops the wallet's COMBINED SOL+WSOL balance by at
# least this much is treated as a round having happened (small drops are
# just normal fee-forwarding/rent, not a round settling).
ROUND_DROP_THRESHOLD_SOL = 0.05

# The scan behind this can mean dozens of sequential RPC calls in the worst
# case, so the *timestamp* of the last round is cached — it only changes
# once per round (~every 5h), there's no need to re-scan on every /stat
# call. The remaining-time countdown is still recomputed fresh from "now"
# each call, using the cached timestamp.
_round_cache: dict = {"last_round_time": None, "fetched_at": 0.0}
_ROUND_CACHE_TTL = 300  # seconds


def _wsol_balance_from_tx(balances: list, owner: str) -> float:
    """Reads the WSOL uiAmount owned by `owner` from a pre/postTokenBalances
    array (0.0 if that owner had no WSOL entry in this transaction). Token
    balance entries are indexed by token-account position, not by owner
    account index, so this matches on the "owner" and "mint" fields rather
    than an account index."""
    for entry in balances or []:
        if entry.get("owner") == owner and entry.get("mint") == WSOL_MINT:
            return float(entry["uiTokenAmount"]["uiAmount"] or 0)
    return 0.0


def get_round_timer() -> dict:
    """Finds the most recent 'round' transaction via Helius transaction
    history, and computes time remaining until the 5-hour trigger.

    A round is detected as a transaction where the engine wallet's COMBINED
    native SOL + WSOL balance drops sharply — checking the combined total
    (not just native SOL) avoids a false positive when the wallet simply
    wraps SOL into WSOL ahead of a round: that shows up as a big native-SOL
    decrease but isn't an actual outflow, since the WSOL balance rises by
    the same amount in the same transaction.

    This is still a heuristic, not a check that the funds specifically went
    to buying the current Top 5 (that would need matching swap instructions
    against the live Top 5 mint list, which changes hour to hour — more
    involved than detecting *that* a round-sized outflow happened). Every
    trade forwards its own small fee to this wallet as a separate
    transaction, so a single page of recent signatures may not reach back
    far enough to find the last round — this pages back through up to
    MAX_SIGNATURES_TO_SCAN signatures if needed. If the output still looks
    wrong, ROUND_DROP_THRESHOLD_SOL may need tuning.

    The scan result (the timestamp itself) is cached for _ROUND_CACHE_TTL
    seconds — the scan can be dozens of sequential RPC calls in the worst
    case, and the last-round timestamp only changes once per round anyway."""
    now = time.time()
    cached = _round_cache["last_round_time"]
    if cached is not None and now - _round_cache["fetched_at"] < _ROUND_CACHE_TTL:
        elapsed_hours = (now - cached) / 3600
        remaining_hours = max(0.0, ROUND_INTERVAL_HOURS - elapsed_hours)
        return {
            "last_round_timestamp": cached,
            "elapsed_hours": elapsed_hours,
            "remaining_hours": remaining_hours,
            "due": remaining_hours <= 0,
        }

    PAGE_SIZE = 100
    MAX_SIGNATURES_TO_SCAN = 500

    last_round_time = None
    before = None
    scanned = 0

    while scanned < MAX_SIGNATURES_TO_SCAN and last_round_time is None:
        params = [ENGINE_WALLET, {"limit": PAGE_SIZE}]
        if before:
            params[1]["before"] = before
        signatures = _rpc("getSignaturesForAddress", params)
        if not signatures:
            break

        for sig_info in signatures:
            scanned += 1
            before = sig_info["signature"]
            if sig_info.get("err") is not None:
                continue
            tx = _rpc(
                "getTransaction",
                [before, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
            )
            if not tx:
                continue

            try:
                account_keys = tx["transaction"]["message"]["accountKeys"]
                idx = next(
                    i
                    for i, k in enumerate(account_keys)
                    if (k.get("pubkey") if isinstance(k, dict) else k) == ENGINE_WALLET
                )
                pre_sol = tx["meta"]["preBalances"][idx]
                post_sol = tx["meta"]["postBalances"][idx]
            except (KeyError, IndexError, StopIteration, TypeError):
                continue

            pre_wsol = _wsol_balance_from_tx(tx["meta"].get("preTokenBalances"), ENGINE_WALLET)
            post_wsol = _wsol_balance_from_tx(tx["meta"].get("postTokenBalances"), ENGINE_WALLET)

            pre_total = pre_sol / 1_000_000_000 + pre_wsol
            post_total = post_sol / 1_000_000_000 + post_wsol
            drop_sol = pre_total - post_total

            if drop_sol >= ROUND_DROP_THRESHOLD_SOL:
                last_round_time = sig_info.get("blockTime")
                break

    if last_round_time is None:
        raise RuntimeError(
            f"Could not find a round transaction in the last {scanned} "
            "signatures — the detection threshold may need adjusting"
        )

    _round_cache["last_round_time"] = last_round_time
    _round_cache["fetched_at"] = now

    elapsed_hours = (now - last_round_time) / 3600
    remaining_hours = max(0.0, ROUND_INTERVAL_HOURS - elapsed_hours)

    return {
        "last_round_timestamp": last_round_time,
        "elapsed_hours": elapsed_hours,
        "remaining_hours": remaining_hours,
        "due": remaining_hours <= 0,
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


# Any single transaction where the vault's STONK5 balance drops by at least
# this much is treated as a sweep into Jupiter Lock (not routine dust).
LOCK_SWEEP_THRESHOLD_TOKENS = 1.0

_lock_cache: dict = {"data": None, "fetched_at": 0.0}
_LOCK_CACHE_TTL = 900  # seconds — locks only happen weekly, no need to rescan often


def get_lock_stats() -> dict:
    """"In the vault" = current $STONK5 balance of the public vault address
    (bought, waiting for the weekly sweep). "Locked" = total $STONK5 ever
    swept out of the vault into Jupiter Lock's 5-year escrow, reconstructed
    by scanning the vault's transaction history for its periodic outgoing
    transfers and summing them (same technique as get_round_timer).

    This is a heuristic: it assumes any transaction where the vault's
    STONK5 balance drops by at least LOCK_SWEEP_THRESHOLD_TOKENS was a
    sweep to Jupiter Lock, not some other kind of outgoing transfer. If the
    "locked" figure looks wrong compared to stonk5.com's own /burn-lock
    page, that threshold — or MAX_SIGNATURES_TO_SCAN below — may need
    adjusting. The scan can be a lot of sequential RPC calls (every round
    deposits into the vault, so its full history can be long), so the
    result is cached for _LOCK_CACHE_TTL seconds."""
    now = time.time()
    if _lock_cache["data"] is not None and now - _lock_cache["fetched_at"] < _LOCK_CACHE_TTL:
        return _lock_cache["data"]

    in_vault = _get_token_balance(VAULT_ADDRESS, STONK5_MINT)

    PAGE_SIZE = 100
    MAX_SIGNATURES_TO_SCAN = 1000

    total_locked = 0.0
    before = None
    scanned = 0

    while scanned < MAX_SIGNATURES_TO_SCAN:
        params = [VAULT_ADDRESS, {"limit": PAGE_SIZE}]
        if before:
            params[1]["before"] = before
        signatures = _rpc("getSignaturesForAddress", params)
        if not signatures:
            break

        for sig_info in signatures:
            scanned += 1
            before = sig_info["signature"]
            if sig_info.get("err") is not None:
                continue
            tx = _rpc(
                "getTransaction",
                [before, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
            )
            if not tx:
                continue

            pre_bal = None
            post_bal = None
            for entry in tx["meta"].get("preTokenBalances") or []:
                if entry.get("owner") == VAULT_ADDRESS and entry.get("mint") == STONK5_MINT:
                    pre_bal = float(entry["uiTokenAmount"]["uiAmount"] or 0)
            for entry in tx["meta"].get("postTokenBalances") or []:
                if entry.get("owner") == VAULT_ADDRESS and entry.get("mint") == STONK5_MINT:
                    post_bal = float(entry["uiTokenAmount"]["uiAmount"] or 0)

            if pre_bal is None or post_bal is None:
                continue

            drop = pre_bal - post_bal
            if drop >= LOCK_SWEEP_THRESHOLD_TOKENS:
                total_locked += drop

    result = {
        "in_vault": in_vault,
        "locked": total_locked,
        "together": in_vault + total_locked,
        "pct_of_supply": ((in_vault + total_locked) / TOTAL_ISSUED) * 100,
        "scanned_signatures": scanned,
    }
    _lock_cache["data"] = result
    _lock_cache["fetched_at"] = now
    return result


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


_top5_cache: dict = {"data": None, "fetched_at": 0.0}
_TOP5_CACHE_TTL = 60  # seconds


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

    Confirmed live response shape (fetched 2026-09-22):
        {"data": {"tokens": [ { "name", "symbol", "market": {
            "marketCapUsd", "volume24hUsd", ... }, "status", ... } ],
            "pagination": {...} }, "meta": {...}}
    i.e. the token list is at data.tokens, and market-cap/volume live
    under each token's nested "market" object as marketCapUsd/volume24hUsd
    — NOT top-level "marketCap"/"volume24h" as originally guessed (that
    mismatch was the bug behind "/top5" returning "No qualifying tokens
    found"). _pick still tries a couple of fallback spellings in case the
    API changes shape again.

    StonkFun's API has been observed to be slow/unresponsive at times, so
    this uses a longer timeout, retries once before giving up, and caches
    the result briefly so repeated /top5 calls don't hammer a slow API."""
    now = time.time()
    if _top5_cache["data"] is not None and now - _top5_cache["fetched_at"] < _TOP5_CACHE_TTL:
        return _top5_cache["data"]

    params = {"sort": "marketCap", "pageSize": 25}
    STONKFUN_TIMEOUT = 25
    try:
        resp = requests.get(STONKFUN_TOKENS_URL, params=params, timeout=STONKFUN_TIMEOUT)
    except requests.exceptions.RequestException:
        resp = requests.get(STONKFUN_TOKENS_URL, params=params, timeout=STONKFUN_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    tokens = (
        data
        if isinstance(data, list)
        else _pick(data, "data.tokens", "tokens", "data", "items", default=[])
    )

    candidates = []
    for t in tokens:
        market_cap = _pick(
            t, "market.marketCapUsd", "marketCapUsd", "marketCap", "market_cap", "stats.marketCap"
        )
        volume = _pick(
            t,
            "market.volume24hUsd",
            "volume24hUsd",
            "volume24h",
            "volume24H",
            "volume_24h",
            "stats.volume24h",
            default=0,
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
    result = candidates[:5]
    _top5_cache["data"] = result
    _top5_cache["fetched_at"] = now
    return result


if __name__ == "__main__":
    import json

    print("Market:", json.dumps(get_market_stats(), indent=2, ensure_ascii=False))
    print("Wallet:", json.dumps(get_wallet_holds(), indent=2, ensure_ascii=False))
    print("Burned:", json.dumps(get_burned_total(), indent=2, ensure_ascii=False))
    print("Locked:", json.dumps(get_lock_stats(), indent=2, ensure_ascii=False))
    print("Top 5:", json.dumps(get_top5(), indent=2, ensure_ascii=False))
