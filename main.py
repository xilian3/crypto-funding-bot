import requests
import time
import asyncio
from telegram import Bot

# =========================================================
# CONFIG
# =========================================================
import os

TELEGRAM_TOKEN = os.getenv("8789507186:AAHI4J5ofmgd4iZfp8z_cme1qkeiHAvjJss
")
CHAT_ID = os.getenv("1100102176")

FUNDING_THRESHOLD = -0.0008      # -0.08%
PRICE_CHANGE_THRESHOLD = 3       # 3%
OI_THRESHOLD = 2                 # +2%
MIN_MARKET_CAP = 100000000       # $100M
MIN_24H_VOLUME = 30000000        # $30M

SCAN_INTERVAL = 300              # 5 minutes
ALERT_COOLDOWN = 1800            # 30 minutes

# =========================================================
# TELEGRAM
# =========================================================

bot = Bot(token=TELEGRAM_TOKEN)

async def send_alert(message):
    await bot.send_message(
        chat_id=CHAT_ID,
        text=message
    )

# =========================================================
# ALERT TRACKER
# =========================================================

last_alert_time = {}

# =========================================================
# MARKET CAPS
# =========================================================

def get_market_caps():

    print("Fetching market caps...")

    url = (
        "https://api.coingecko.com/api/v3/coins/markets"
        "?vs_currency=usd"
        "&order=market_cap_desc"
        "&per_page=250"
        "&page=1"
    )

    response = requests.get(url, timeout=10)
    data = response.json()

    market_caps = {}

    for coin in data:
        symbol = coin['symbol'].upper()
        market_caps[symbol] = coin.get('market_cap', 0)

    return market_caps

# =========================================================
# FUNDING DATA
# =========================================================

def get_funding_data():

    url = "https://fapi.binance.com/fapi/v1/premiumIndex"

    response = requests.get(url)

    return response.json()

# =========================================================
# TICKER DATA
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
# OPEN INTEREST CHANGE
# =========================================================

def get_open_interest_change(symbol):

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

        if old_oi == 0:
            return 0

        oi_change = ((new_oi - old_oi) / old_oi) * 100

        return oi_change

    except Exception as e:

        print(f"OI Error for {symbol}: {e}")

        return 0

# =========================================================
# MAIN SCANNER
# =========================================================

async def scan_market():

    print("\n==============================")
    print("SCANNING MARKET...")
    print("==============================\n")

    market_caps = get_market_caps()

    funding_data = get_funding_data()

    ticker_data = get_ticker_data()

    candidates_found = 0

    for coin in funding_data:

        try:

            symbol = coin['symbol']

            # Only USDT perpetuals
            if not symbol.endswith("USDT"):
                continue

            funding = float(coin['lastFundingRate'])

            # =================================================
            # FUNDING FILTER
            # =================================================

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
            # VOLUME FILTER
            # =================================================

            volume_24h = float(ticker['quoteVolume'])

            if volume_24h < MIN_24H_VOLUME:
                continue

            # =================================================
            # MARKET CAP FILTER
            # =================================================

            base_symbol = symbol.replace("USDT", "").upper()

            market_cap = market_caps.get(base_symbol, 0)

            if market_cap < MIN_MARKET_CAP:
                continue

            # =================================================
            # OPEN INTEREST FILTER
            # =================================================

            oi_change = get_open_interest_change(symbol)

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

            candidates_found += 1

            # =================================================
            # DEBUG LOG
            # =================================================

            print(f"\nMATCH FOUND: {symbol}")
            print(f"Funding: {funding * 100:.4f}%")
            print(f"Price Change: {price_change:.2f}%")
            print(f"24h Volume: ${volume_24h:,.0f}")
            print(f"OI Change: {oi_change:.2f}%")
            print(f"Market Cap: ${market_cap:,.0f}")

            # =================================================
            # TELEGRAM ALERT
            # =================================================

            message = f"""
🚨 EXTREME FUNDING EVENT

Coin: {symbol}

Funding Rate: {funding * 100:.4f}%
Price Move: {price_change:.2f}%
Open Interest Change: {oi_change:.2f}%

24h Volume: ${volume_24h:,.0f}
Market Cap: ${market_cap:,.0f}

Potential positioning imbalance detected.
"""

            await send_alert(message)

        except Exception as e:

            print(f"Error processing {coin}: {e}")

    print(f"\nScan completed.")
    print(f"Candidates found: {candidates_found}")

# =========================================================
# MAIN LOOP
# =========================================================

async def main():

    while True:

        try:

            await scan_market()

        except Exception as e:

            print(f"\nMAIN LOOP ERROR: {e}")

        print(f"\nSleeping {SCAN_INTERVAL} seconds...\n")

        await asyncio.sleep(SCAN_INTERVAL)

# =========================================================
# START
# =========================================================

asyncio.run(main())