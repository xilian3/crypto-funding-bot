import os
import requests
import asyncio
from telegram import Bot

PRICE_IMPACT_THRESHOLD = 5.0
OI_SPIKE_THRESHOLD = 2.0
VOLUME_SPIKE_MULTIPLIER = 2.0
MIN_24H_VOLUME = 30_000_000

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")    
CHAT_ID = os.getenv("CHAT_ID")

bot = Bot(token=TELEGRAM_TOKEN)

async def send_alert(message):
    await bot.send_message(chat_id=CHAT_ID, text=message)

def get_json(url):
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    return response.json()

def get_market_caps():
    market_caps = {}

    for page in range(1, 5):
        url = (
            "https://api.coingecko.com/api/v3/coins/markets"
            "?vs_currency=usd"
            "&order=market_cap_desc"
            "&per_page=250"
            f"&page={page}"
        )

        data = get_json(url)

        for coin in data:
            symbol = coin["symbol"].upper()
            market_caps[symbol] = coin.get("market_cap", 0) or 0

    return market_caps

def get_funding_data():
    return get_json("https://fapi.binance.com/fapi/v1/premiumIndex")

def get_ticker_data():
    data = get_json("https://fapi.binance.com/fapi/v1/ticker/24hr")
    return {item["symbol"]: item for item in data}

def get_5m_klines(symbol, limit=13):
    url = (
        "https://fapi.binance.com/fapi/v1/klines"
        f"?symbol={symbol}"
        "&interval=5m"
        f"&limit={limit}"
    )
    return get_json(url)

def get_5m_price_impact(symbol):
    klines = get_5m_klines(symbol, limit=2)

    if len(klines) < 2:
        return 0

    last_closed = klines[-2]

    open_price = float(last_closed[1])
    close_price = float(last_closed[4])

    if open_price == 0:
        return 0

    return abs((close_price - open_price) / open_price * 100)

def get_5m_volume_spike(symbol):
    klines = get_5m_klines(symbol, limit=13)

    if len(klines) < 13:
        return 0

    closed_klines = klines[:-1]

    latest_volume = float(closed_klines[-1][7])
    previous_volumes = [float(k[7]) for k in closed_klines[:-1]]

    avg_volume = sum(previous_volumes) / len(previous_volumes)

    if avg_volume == 0:
        return 0

    return latest_volume / avg_volume

def get_open_interest_spike(symbol):
    url = (
        "https://fapi.binance.com/futures/data/openInterestHist"
        f"?symbol={symbol}"
        "&period=5m"
        "&limit=2"
    )

    data = get_json(url)

    if len(data) < 2:
        return 0

    old_oi = float(data[0]["sumOpenInterest"])
    new_oi = float(data[1]["sumOpenInterest"])

    if old_oi == 0:
        return 0

    return ((new_oi - old_oi) / old_oi) * 100

def get_funding_threshold(market_cap):
    if market_cap < 1_000_000_000:
        return -0.0015, "Small Cap < $1B"

    elif market_cap <= 10_000_000_000:
        return -0.0010, "Mid Cap $1B-$10B"

    else:
        return -0.0003, "Large Cap > $10B"

async def scan_market():
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise ValueError("Missing TELEGRAM_TOKEN or CHAT_ID environment variable.")

    print("Fetching market data...")

    market_caps = get_market_caps()
    funding_data = get_funding_data()
    ticker_data = get_ticker_data()

    candidates_found = 0

    for coin in funding_data:
        try:
            symbol = coin["symbol"]

            if not symbol.endswith("USDT"):
                continue

            base_symbol = symbol.replace("USDT", "").upper()
            market_cap = market_caps.get(base_symbol, 0)

            if market_cap <= 0:
                continue

            if symbol not in ticker_data:
                continue

            ticker = ticker_data[symbol]
            volume_24h = float(ticker["quoteVolume"])

            if volume_24h < MIN_24H_VOLUME:
                continue

            funding = float(coin["lastFundingRate"])
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
🚨 EXTREME PERP SIGNAL

Coin: {symbol}
Tier: {cap_tier}

Funding Rate: {funding * 100:.4f}%
Required Funding: {funding_threshold * 100:.4f}%

5m Price Impact: {price_impact:.2f}%
5m OI Spike: +{oi_spike:.2f}%
5m Volume Spike: {volume_spike:.2f}x

24h Volume: ${volume_24h:,.0f}
Market Cap: ${market_cap:,.0f}

Possible aggressive positioning imbalance detected.
"""

            print(message)
            await send_alert(message)

        except Exception as e:
            print(f"Error processing {coin.get('symbol', 'UNKNOWN')}: {e}")

    print(f"Scan completed. Candidates found: {candidates_found}")

asyncio.run(scan_market())