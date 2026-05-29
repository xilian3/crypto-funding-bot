import os
import json
import time
import requests
import asyncio
from telegram import Bot

# =========================================================
# CONFIG
# =========================================================

MIN_24H_VOLUME = 50_000_000           # Binance 24h quote volume must be >= $50M

COOLDOWN_HOURS = 8
COOLDOWN_SECONDS = COOLDOWN_HOURS * 60 * 60
COOLDOWN_FILE = "cooldown_state.json"

MARKET_CAP_CACHE_FILE = "market_caps_cache.json"
MARKET_CAP_CACHE_TTL = 24 * 60 * 60   # 24 hours

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}

# =========================================================
# TELEGRAM
# =========================================================

async def send_alert(message):
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(chat_id=CHAT_ID, text=message)

# =========================================================
# COOLDOWN STORAGE
# =========================================================

def load_cooldowns():
    if not os.path.exists(COOLDOWN_FILE):
        return {}

    try:
        with open(COOLDOWN_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"Cooldown file error: {e}")
        return {}

def save_cooldowns(cooldowns):
    with open(COOLDOWN_FILE, "w") as f:
        json.dump(cooldowns, f, indent=2)

def is_on_cooldown(symbol, cooldowns):
    last_alert_time = cooldowns.get(symbol)

    if not last_alert_time:
        return False

    elapsed = time.time() - float(last_alert_time)

    return elapsed < COOLDOWN_SECONDS

def update_cooldown(symbol, cooldowns):
    cooldowns[symbol] = time.time()
    save_cooldowns(cooldowns)

def clean_old_cooldowns(cooldowns):
    now = time.time()

    cleaned = {
        symbol: timestamp
        for symbol, timestamp in cooldowns.items()
        if now - float(timestamp) < COOLDOWN_SECONDS
    }

    save_cooldowns(cleaned)

    return cleaned

# =========================================================
# HTTP HELPERS
# =========================================================

def get_json(url, params=None):
    response = requests.get(
        url,
        params=params,
        headers=HEADERS,
        timeout=15
    )

    if response.status_code in (403, 451):
        raise RuntimeError(
            f"Exchange blocked this server/IP. "
            f"HTTP {response.status_code} for {response.url}"
        )

    response.raise_for_status()
    return response.json()

# =========================================================
# MARKET CAP CACHE
# =========================================================

def load_market_cap_cache(allow_stale=False):
    if not os.path.exists(MARKET_CAP_CACHE_FILE):
        return None

    try:
        with open(MARKET_CAP_CACHE_FILE, "r") as f:
            cached = json.load(f)

        timestamp = cached.get("timestamp", 0)
        market_caps = cached.get("market_caps", {})

        cache_age = time.time() - timestamp

        if cache_age < MARKET_CAP_CACHE_TTL:
            print("Using cached market cap data.")
            return market_caps

        if allow_stale:
            print("Using stale market cap cache because fresh fetch failed.")
            return market_caps

        print("Market cap cache expired. Refreshing...")

    except Exception as e:
        print(f"Market cap cache error: {e}")

    return None

def save_market_cap_cache(market_caps):
    with open(MARKET_CAP_CACHE_FILE, "w") as f:
        json.dump(
            {
                "timestamp": time.time(),
                "market_caps": market_caps,
            },
            f
        )

def get_market_caps():
    cached_market_caps = load_market_cap_cache()

    if cached_market_caps:
        return cached_market_caps

    print("Fetching fresh market cap data from CoinGecko...")

    market_caps = {}

    for page in range(1, 5):
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 250,
            "page": page,
        }

        try:
            response = requests.get(
                url,
                params=params,
                headers=HEADERS,
                timeout=15
            )

            if response.status_code == 429:
                print("CoinGecko rate limit hit.")
                break

            response.raise_for_status()
            data = response.json()

            for coin in data:
                symbol = coin["symbol"].upper()
                market_cap = coin.get("market_cap", 0) or 0

                if symbol not in market_caps:
                    market_caps[symbol] = market_cap
                else:
                    market_caps[symbol] = max(market_caps[symbol], market_cap)

            time.sleep(1.5)

        except Exception as e:
            print(f"CoinGecko error on page {page}: {e}")
            break

    if market_caps:
        save_market_cap_cache(market_caps)
        return market_caps

    stale_market_caps = load_market_cap_cache(allow_stale=True)

    if stale_market_caps:
        return stale_market_caps

    print("No market cap data available.")
    return {}

# =========================================================
# BINANCE API DATA
# =========================================================

def get_binance_funding_data():
    url = "https://fapi.binance.com/fapi/v1/premiumIndex"
    data = get_json(url)

    funding_map = {}

    for item in data:
        symbol = item.get("symbol", "")

        if not symbol.endswith("USDT"):
            continue

        try:
            funding_map[symbol] = float(item.get("lastFundingRate", 0) or 0)
        except Exception:
            funding_map[symbol] = 0.0

    return funding_map

def get_binance_ticker_data():
    url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    data = get_json(url)

    ticker_map = {}

    for item in data:
        symbol = item.get("symbol", "")

        if not symbol.endswith("USDT"):
            continue

        ticker_map[symbol] = item

    return ticker_map

# =========================================================
# MARKET CAP / SYMBOL HELPERS
# =========================================================

def get_market_cap_for_symbol(symbol, market_caps):
    base_symbol = symbol.replace("USDT", "").upper()

    candidates = [base_symbol]

    # Handles contracts like 1000PEPEUSDT, 1000BONKUSDT, 1000SHIBUSDT, etc.
    for prefix in ["1000000", "10000", "1000"]:
        if base_symbol.startswith(prefix):
            candidates.append(base_symbol.replace(prefix, "", 1))

    for candidate in candidates:
        if candidate in market_caps:
            return market_caps[candidate], candidate

    return 0, base_symbol

def get_funding_threshold(market_cap):
    if market_cap < 1_000_000_000:
        return -0.0015, "Small Cap < $1B"

    elif market_cap <= 10_000_000_000:
        return -0.0010, "Mid Cap $1B-$10B"

    else:
        return -0.0003, "Large Cap > $10B"

# =========================================================
# MAIN SCANNER
# =========================================================

async def scan_market():
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise ValueError("Missing TELEGRAM_TOKEN or CHAT_ID environment variable.")

    print("Fetching market data from Binance...")

    cooldowns = load_cooldowns()
    cooldowns = clean_old_cooldowns(cooldowns)

    market_caps = get_market_caps()

    if not market_caps:
        print("No market cap data available. Scan stopped.")
        return

    funding_data = get_binance_funding_data()
    ticker_data = get_binance_ticker_data()

    checked_symbols = 0
    passed_volume = 0
    passed_funding = 0
    candidates_found = 0
    skipped_cooldown = 0

    for symbol, ticker in ticker_data.items():
        try:
            checked_symbols += 1

            # =================================================
            # COOLDOWN FILTER
            # =================================================

            if is_on_cooldown(symbol, cooldowns):
                skipped_cooldown += 1
                continue

            # =================================================
            # MARKET CAP FILTER / TIER
            # =================================================

            market_cap, mapped_symbol = get_market_cap_for_symbol(symbol, market_caps)

            if market_cap <= 0:
                continue

            funding_threshold, cap_tier = get_funding_threshold(market_cap)

            # =================================================
            # 24H VOLUME FILTER
            # =================================================

            volume_24h = float(ticker.get("quoteVolume", 0) or 0)

            if volume_24h < MIN_24H_VOLUME:
                continue

            passed_volume += 1

            # =================================================
            # FUNDING FILTER
            # =================================================

            funding = funding_data.get(symbol)

            if funding is None:
                continue

            if funding > funding_threshold:
                continue

            passed_funding += 1
            candidates_found += 1

            message = f"""
🚨 EXTREME BINANCE FUNDING SIGNAL

Coin: {symbol}
Mapped Symbol: {mapped_symbol}
Tier: {cap_tier}

Funding Rate: {funding * 100:.4f}%
Required Funding: {funding_threshold * 100:.4f}%

24h Binance Quote Volume: ${volume_24h:,.0f}
Market Cap: ${market_cap:,.0f}

Cooldown: {COOLDOWN_HOURS} hours

Possible extreme negative funding / positioning imbalance detected.
"""

            print(message)
            await send_alert(message)

            # Start cooldown only after successful Telegram alert
            update_cooldown(symbol, cooldowns)

        except Exception as e:
            print(f"Error processing {symbol}: {e}")

    print(
        f"Scan completed. Checked symbols: {checked_symbols}. "
        f"Passed volume: {passed_volume}. "
        f"Passed funding: {passed_funding}. "
        f"Candidates found: {candidates_found}. "
        f"Skipped by cooldown: {skipped_cooldown}."
    )

# =========================================================
# START
# =========================================================

asyncio.run(scan_market())
