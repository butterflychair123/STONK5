"""
Scraper + on-chain lookup for https://stonk5.com/

The homepage (price, market cap, liquidity, fees toward next round, wallet
holds) is server-rendered, so a plain HTTP GET + text parsing is enough.

The "Burned so far" figure on the /burn-lock page, however, loads client-side
via JavaScript reading the Solana blockchain directly (the raw HTML shows a
"reading the chain…" placeholder). Instead of scraping that page, we compute
the same number the site does: total issued (1,000,000,000, fixed) minus the
mint's current on-chain supply, fetched with one Solana JSON-RPC call.
"""

import re
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://stonk5.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
TIMEOUT = 15

# From stonk5.com's own page copy ("Contract address" on the homepage).
STONK5_MINT = "F7CTvENFnkDJysMhaFFicDT2FwnbW2oasFZGG6WJnar7"
TOTAL_ISSUED = 1_000_000_000

# Public Solana RPC endpoint. If this one gets rate-limited, swap in another
# public RPC (e.g. a free Helius/QuickNode endpoint) here.
SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"


def _get_text(path: str) -> str:
    resp = requests.get(f"{BASE_URL}{path}", headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    return soup.get_text(separator="\n")


def _find_after(
    label: str, text: str, pattern: str = r"\$[\d][\d,\.]*", same_line: bool = False
) -> str | None:
    """Finds the label in the text and returns the next line matching the
    pattern. By default the label's own line is skipped (the value sits on
    the line after it on this site); set same_line=True for labels that
    share a line with their value."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for i, line in enumerate(lines):
        if label.lower() in line.lower():
            start = i if same_line else i + 1
            for j in range(start, min(start + 4, len(lines))):
                m = re.search(pattern, lines[j])
                if m:
                    return m.group().strip()
    return None


def get_homepage_stats() -> dict:
    text = _get_text("/")
    return {
        "price": _find_after("Price", text, r"\$[\d\.]+"),
        "market_cap": _find_after("Market cap", text, r"\$[\d,\.]+"),
        "liquidity": _find_after("Liquidity", text, r"\$[\d,\.]+"),
        "fees_progress": _find_after(
            "Fees toward next round", text, r"[\d\.]+\s*/\s*[\d\.]+\s*SOL"
        ),
        "fees_pct": _find_after(
            "Fees toward next round", text, r"[\d\.]+%"
        ),
        "wallet_holds": _find_after("Wallet holds", text, r"[\d\.]+\s*SOL"),
    }


def get_burned_total() -> dict:
    """Computes the burned total the same way stonk5.com does: total issued
    minus the mint's current on-chain supply."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenSupply",
        "params": [STONK5_MINT],
    }
    resp = requests.post(SOLANA_RPC_URL, json=payload, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise RuntimeError(f"Solana RPC error: {data['error']}")

    current_supply = float(data["result"]["value"]["uiAmountString"])
    burned = TOTAL_ISSUED - current_supply
    pct = (burned / TOTAL_ISSUED) * 100

    return {
        "burned": burned,
        "current_supply": current_supply,
        "pct_of_supply": pct,
    }


if __name__ == "__main__":
    import json

    print("Homepage:", json.dumps(get_homepage_stats(), indent=2, ensure_ascii=False))
    print("Burned:", json.dumps(get_burned_total(), indent=2, ensure_ascii=False))
