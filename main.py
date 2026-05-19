import requests
import time
from telegram import Bot
import asyncio

# =========================================================
# CONFIG
# =========================================================

TELEGRAM_TOKEN = "8789507186:AAHI4J5ofmgd4iZfp8z_cme1qkeiHAvjJss"
CHAT_ID = "1100102176"

FUNDING_THRESHOLD = -0.0015      # -0.1500%
PRICE_CHANGE_THRESHOLD = 5       # 5%
VOLUME_SPIKE_THRESHOLD = 2       # 2x average
OI_THRESHOLD = 5                 # +5%
MIN_MARKET_CAP = 100000000   # $1B

SCAN_INTERVAL = 300              # 5 minutes
ALERT_COOLDOWN = 1800            # 30 minutes

# =========================================================
# TELEGRAM BOT
# =========================================================

bot = Bot(token=TELEGRAM_TOKEN)

async def send_telegram_alert(message):
    await bot.send_message(
        chat_id=CHAT_ID,
        text=message
    )

# =========================================================
# TRACK LAST ALERT TIME
# =========================================================

last_alert_time = {}

# =========================================================
# GET MARKET CAPS FROM COINGECKO
# =========================================================

def get_market_caps():

    url = (
        "https://api.coingecko.com/api/v3/coins/markets"
        "?vs_currency=usd"
        "&order=market_cap_desc"
        "&per_page=250"
        "&page=1"
    )

    response = requests.get(url)
    data = response.json()

    market_caps = {}

    for coin in data:
        symbol = coin['symbol'].upper()
        market_caps[symbol] = coin.get('market_cap', 0)

    return market_caps

# =========================================================
# GET BINANCE FUNDING DATA
# =========================================================

def get_funding_data():

    url = "https://fapi.binance.com/fapi/v1/premiumIndex"

    response = requests.get(url)
    return response.json()

# =========================================================
# GET 24H TICKER DATA
# =========================================================

def get_ticker_data():

    url = "https://fapi.binance.com/fapi/v1/ticker/24hr"

    response = requests.get(url)

    data = response.json()

    ticker_map = {}

    for item in data:
        ticker_map[item['symbol']] = item

    return ticker_map

# =========================================================
# GET OPEN INTEREST
# =========================================================

def get_open_interest(symbol):

    try:

        url = (
            "https://fapi.binance.com/futures/data/openInterestHist"
            f"?symbol={symbol}"
            "&period=5m"
            "&limit=2"
        )

        response = requests.get(url)
        data = response.json()

        if len(data) < 2:
            return 0

        old_oi = float(data[0]['sumOpenInterest'])
        new_oi = float(data[1]['sumOpenInterest'])

        oi_change = ((new_oi - old_oi) / old_oi) * 100

        return oi_change

    except:
        return 0

# =========================================================
# MAIN SCANNER
# =========================================================

async def scan_market():

    print("\nScanning market...\n")

    market_caps = get_market_caps()

    funding_data = get_funding_data()

    ticker_data = get_ticker_data()

    for coin in funding_data:

        try:

            symbol = coin['symbol']

            # Only USDT perpetuals
            if not symbol.endswith("USDT"):
                continue

            funding = float(coin['lastFundingRate'])

            # Funding filter
            if funding > FUNDING_THRESHOLD:
                continue

            if symbol not in ticker_data:
                continue

            ticker = ticker_data[symbol]

            # =================================================
            # PRICE CHANGE
            # =================================================

            price_change = abs(float(ticker['priceChangePercent']))

            if price_change < PRICE_CHANGE_THRESHOLD:
                continue

            # =================================================
            # VOLUME SPIKE
            # =================================================

            current_volume = float(ticker['quoteVolume'])

            avg_volume = current_volume / 24

            volume_ratio = current_volume / avg_volume

            if volume_ratio < VOLUME_SPIKE_THRESHOLD:
                continue

            # =================================================
            # MARKET CAP FILTER
            # =================================================

            base_symbol = symbol.replace("USDT", "").lower()

            market_cap = market_caps.get(base_symbol.upper(), 0)

            if market_cap < MIN_MARKET_CAP:
                continue

            # =================================================
            # OPEN INTEREST FILTER
            # =================================================

            oi_change = get_open_interest(symbol)

            if oi_change < OI_THRESHOLD:
                continue

            # =================================================
            # COOLDOWN FILTER
            # =================================================

            now = time.time()

            if symbol in last_alert_time:

                elapsed = now - last_alert_time[symbol]

                if elapsed < ALERT_COOLDOWN:
                    continue

            last_alert_time[symbol] = now

            # =================================================
            # ALERT MESSAGE
            # =================================================

            message = f"""
🚨 EXTREME FUNDING ALERT

Coin: {symbol}

Funding Rate: {funding * 100:.4f}%
24h Price Move: {price_change:.2f}%
Volume Spike: {volume_ratio:.2f}x
Open Interest Change: {oi_change:.2f}%

Market Cap: ${market_cap:,.0f}

Potential positioning imbalance detected.
"""

            print(message)

            await send_telegram_alert(message)

        except Exception as e:
            print(f"Error processing {coin}: {e}")

# =========================================================
# RUN LOOP
# =========================================================

async def main():

    while True:

        try:
            await scan_market()

        except Exception as e:
            print("Scanner Error:", e)

        print(f"\nSleeping for {SCAN_INTERVAL} seconds...\n")

        time.sleep(SCAN_INTERVAL)

# =========================================================
# START BOT
# =========================================================

asyncio.run(main())