import os
import json
import time
import requests
import asyncio
from telegram import Bot

# =========================================================
# CONFIG
# =========================================================

PRICE_IMPACT_THRESHOLD = 5.0          # 5m price impact >= 5%
OI_SPIKE_THRESHOLD = 2.0              # 5m open interest increase >= 2%
MIN_24H_VOLUME = 10_000_000           # Bybit 24h turnover must be >= $10M

COOLDOWN_HOURS = 8
COOLDOWN_SECONDS = COOLDOWN_HOURS * 60 * 60
COOLDOWN_FILE = "cooldown_state.json"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

bot = Bot(token=TELEGRAM_TOKEN)

# =========================================================
# TELEGRAM
# =========================================================

async def send_alert(message):
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
    except Exception:
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
    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    data = response.json()

    if data.get("retCode") not in (0, None):
        raise ValueError(f"Bybit API error: {data}")

    return data

# =========================================================
# MARKET CAP DATA - COINGECKO
# =========================================================

def get_market_caps():
    market_caps = {}

    for page in range(1, 5):
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 250,
            "page": page,
        }

        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()

        for coin in data:
            symbol = coin["symbol"].upper()
            market_caps[symbol] = coin.get("market_cap", 0) or 0

    return market_caps

# =========================================================
# BYBIT API DATA
# =========================================================

def get_bybit_tickers():
    url = "https://api.bybit.com/v5/market/tickers"
    params = {"category": "linear"}
    data = get_json(url, params=params)

    ticker_map = {}

    for item in data["result"].get("list", []):
        symbol = item.get("symbol", "")

        if not symbol.endswith("USDT"):
            continue

        ticker_map[symbol] = item

    return ticker_map

def get_5m_klines(symbol, limit=3):
    url = "https://api.bybit.com/v5/market/kline"
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": "5",
        "limit": limit,
    }

    data = get_json(url, params=params)
    klines = data["result"].get("list", [])

    # Bybit usually returns newest first, so sort oldest -> newest
    klines.sort(key=lambda x: int(x[0]))

    return klines

def get_5m_price_impact(symbol):
    klines = get_5m_klines(symbol, limit=3)

    if len(klines) < 2:
        return 0

    # Use latest closed candle, not currently forming candle
    last_closed = klines[-2]

    open_price = float(last_closed[1])
    close_price = float(last_closed[4])

    if open_price == 0:
        return 0

    return abs((close_price - open_price) / open_price * 100)

def get_open_interest_spike(symbol):
    url = "https://api.bybit.com/v5/market/open-interest"
    params = {
        "category": "linear",
        "symbol": symbol,
        "intervalTime": "5min",
        "limit": 2,
    }

    data = get_json(url, params=params)
    oi_list = data["result"].get("list", [])

    if len(oi_list) < 2:
        return 0

    # Sort oldest -> newest
    oi_list.sort(key=lambda x: int(x["timestamp"]))

    old_oi = float(oi_list[0]["openInterest"])
    new_oi = float(oi_list[-1]["openInterest"])

    if old_oi == 0:
        return 0

    return ((new_oi - old_oi) / old_oi) * 100

# =========================================================
# SIGNAL THRESHOLDS
# =========================================================

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

    print("Fetching market data from Bybit...")

    cooldowns = load_cooldowns()
    cooldowns = clean_old_cooldowns(cooldowns)

    market_caps = get_market_caps()
    ticker_data = get_bybit_tickers()

    candidates_found = 0
    checked_symbols = 0
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

            base_symbol = symbol.replace("USDT", "").upper()
            market_cap = market_caps.get(base_symbol, 0)

            if market_cap <= 0:
                continue

            # =================================================
            # 24H VOLUME FILTER
            # =================================================

            volume_24h = float(ticker.get("turnover24h", 0) or 0)

            if volume_24h < MIN_24H_VOLUME:
                continue

            # =================================================
            # FUNDING FILTER
            # =================================================

            funding = float(ticker.get("fundingRate", 0) or 0)
            funding_threshold, cap_tier = get_funding_threshold(market_cap)

            if funding > funding_threshold:
                continue

            # =================================================
            # PRICE IMPACT FILTER
            # =================================================

            price_impact = get_5m_price_impact(symbol)

            if price_impact < PRICE_IMPACT_THRESHOLD:
                continue

            # =================================================
            # OPEN INTEREST FILTER
            # =================================================

            oi_spike = get_open_interest_spike(symbol)

            if oi_spike < OI_SPIKE_THRESHOLD:
                continue

            candidates_found += 1

            message = f"""
🚨 EXTREME BYBIT PERP SIGNAL

Coin: {symbol}
Tier: {cap_tier}

Funding Rate: {funding * 100:.4f}%
Required Funding: {funding_threshold * 100:.4f}%

5m Price Impact: {price_impact:.2f}%
5m OI Spike: +{oi_spike:.2f}%

24h Bybit Turnover: ${volume_24h:,.0f}
Market Cap: ${market_cap:,.0f}

Cooldown: {COOLDOWN_HOURS} hours

Possible aggressive positioning imbalance detected.
"""

            print(message)
            await send_alert(message)

            # Start 8-hour cooldown only after successful alert
            update_cooldown(symbol, cooldowns)

        except Exception as e:
            print(f"Error processing {symbol}: {e}")

    print(
        f"Scan completed. Checked symbols: {checked_symbols}. "
        f"Candidates found: {candidates_found}. "
        f"Skipped by cooldown: {skipped_cooldown}."
    )

# =========================================================
# START
# =========================================================

asyncio.run(scan_market())
