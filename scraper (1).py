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
"""

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

# Public Solana RPC endpoint. If this one gets rate-limited, swap in another
# public RPC (e.g. a free Helius/QuickNode endpoint) here.
SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"

DEXSCREENER_URL = f"https://api.dexscreener.com/latest/dex/tokens/{STONK5_MINT}"


def _rpc(method: str, params: list) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    resp = requests.post(SOLANA_RPC_URL, json=payload, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Solana RPC error ({method}): {data['error']}")
    return data["result"]


def get_market_stats() -> dict:
    """Live price/market cap/liquidity from DexScreener (same style of data
    source the site's own live ticker uses)."""
    resp = requests.get(DEXSCREENER_URL, timeout=TIMEOUT)
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


if __name__ == "__main__":
    import json

    print("Market:", json.dumps(get_market_stats(), indent=2, ensure_ascii=False))
    print("Wallet/fees:", json.dumps(get_wallet_and_fees(), indent=2, ensure_ascii=False))
    print("Burned:", json.dumps(get_burned_total(), indent=2, ensure_ascii=False))
