import os
import requests
import asyncio
from telegram import Bot

# =========================================================
# CONFIG
# =========================================================

PRICE_IMPACT_THRESHOLD = 5.0          # 5m price impact >= 5%
OI_SPIKE_THRESHOLD = 2.0              # 5m open interest increase >= 2%
VOLUME_SPIKE_MULTIPLIER = 2.0         # latest closed 5m volume >= 2x previous average
MIN_24H_VOLUME = 10_000_000           # Bybit 24h turnover must be >= $10M

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

bot = Bot(token=TELEGRAM_TOKEN)

# =========================================================
# TELEGRAM
# =========================================================

async def send_alert(message):
    await bot.send_message(chat_id=CHAT_ID, text=message)

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

def get_5m_klines(symbol, limit=13):
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

def get_5m_volume_spike(symbol):
    klines = get_5m_klines(symbol, limit=14)

    if len(klines) < 13:
        return 0

    # Remove current forming candle
    closed_klines = klines[:-1]

    # Bybit kline fields:
    # [startTime, open, high, low, close, volume, turnover]
    latest_turnover = float(closed_klines[-1][6])
    previous_turnovers = [float(k[6]) for k in closed_klines[:-1]]

    avg_turnover = sum(previous_turnovers) / len(previous_turnovers)

    if avg_turnover == 0:
        return 0

    return latest_turnover / avg_turnover

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
        raise ValueError("Missing TELEGRAM_TOKEN or CHAT_ID GitHub secret.")

    print("Fetching market data from Bybit...")

    market_caps = get_market_caps()
    ticker_data = get_bybit_tickers()

    candidates_found = 0
    checked_symbols = 0

    for symbol, ticker in ticker_data.items():
        try:
            checked_symbols += 1

            base_symbol = symbol.replace("USDT", "").upper()
            market_cap = market_caps.get(base_symbol, 0)

            if market_cap <= 0:
                continue

            volume_24h = float(ticker.get("turnover24h", 0) or 0)

            if volume_24h < MIN_24H_VOLUME:
                continue

            funding = float(ticker.get("fundingRate", 0) or 0)
            funding_threshold, cap_tier = get_funding_threshold(market_cap)

            if funding > funding_threshold:
                continue

            price_impact = get_5m_price_impact(symbol)

            if price_impact < PRICE_IMPACT_THRESHOLD:
                continue

            oi_spike = get_open_interest_spike(symbol)

            if oi_spike < OI_SPIKE_THRESHOLD:
                continue

            volume_spike = get_5m_volume_spike(symbol)

            if volume_spike < VOLUME_SPIKE_MULTIPLIER:
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
5m Volume Spike: {volume_spike:.2f}x

24h Bybit Turnover: ${volume_24h:,.0f}
Market Cap: ${market_cap:,.0f}

Possible aggressive positioning imbalance detected.
"""

            print(message)
            await send_alert(message)

        except Exception as e:
            print(f"Error processing {symbol}: {e}")

    print(f"Scan completed. Checked symbols: {checked_symbols}. Candidates found: {candidates_found}")

# =========================================================
# START
# =========================================================

asyncio.run(scan_market())
