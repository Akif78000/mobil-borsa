"""
SHIB/USDT Binance otomatik alim-satim botu.

YASAL UYARI: Bu bot gercek parayla, Binance hesabinizda otomatik islem acar/kapatir.
Kullanimi tamamen kendi sorumlulugunuzdadir; kar/zarardan gelistirici sorumlu tutulamaz.
Once Binance Testnet'te denemeniz siddetle onerilir.

Strateji: RSI(14) + EMA(50) trend filtresi (mean-reversion)
  - AL: RSI < 30  VE  fiyat EMA50'nin ustunde (genel trend yukari)
  - SAT: RSI > 70 VE elde SHIB pozisyonu var
  - Pozisyon buyuklugu: alimda kullanilabilir USDT bakiyesinin %33'u

Gerekli ortam degiskenleri (API anahtarinizi asla kod icine yazmayin):
  BINANCE_API_KEY
  BINANCE_API_SECRET

Calistirma:
  pip install -r requirements.txt
  export BINANCE_API_KEY=...
  export BINANCE_API_SECRET=...
  python binance_shib_bot.py
"""
import hashlib
import hmac
import logging
import os
import time
from urllib.parse import urlencode

import requests

SYMBOL = "SHIBUSDT"
BASE_ASSET = "SHIB"
QUOTE_ASSET = "USDT"
KLINE_INTERVAL = "1h"
RSI_PERIOD = 14
EMA_TREND_PERIOD = 50
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
POSITION_FRACTION = 0.33
CHECK_INTERVAL_SECONDS = 300

BASE_URL = "https://api.binance.com"
API_KEY = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_API_SECRET")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("binance_shib_bot.log")],
)
log = logging.getLogger("shib_bot")


def _signed_request(method, path, params=None):
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 10000
    query = urlencode(params)
    signature = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{BASE_URL}{path}?{query}&signature={signature}"
    resp = requests.request(method, url, headers={"X-MBX-APIKEY": API_KEY}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_klines(symbol, interval, limit=200):
    resp = requests.get(
        f"{BASE_URL}/api/v3/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=15,
    )
    resp.raise_for_status()
    return [float(k[4]) for k in resp.json()]


def get_lot_step_size(symbol):
    resp = requests.get(f"{BASE_URL}/api/v3/exchangeInfo", params={"symbol": symbol}, timeout=15)
    resp.raise_for_status()
    filters = resp.json()["symbols"][0]["filters"]
    lot = next(f for f in filters if f["filterType"] == "LOT_SIZE")
    return float(lot["stepSize"])


def round_down_to_step(quantity, step_size):
    precision = max(0, len(str(step_size).split(".")[-1].rstrip("0")))
    steps = int(quantity / step_size)
    return round(steps * step_size, precision)


def get_free_balance(asset):
    account = _signed_request("GET", "/api/v3/account")
    for bal in account["balances"]:
        if bal["asset"] == asset:
            return float(bal["free"])
    return 0.0


def place_market_buy_quote(symbol, quote_amount):
    return _signed_request(
        "POST",
        "/api/v3/order",
        {"symbol": symbol, "side": "BUY", "type": "MARKET", "quoteOrderQty": round(quote_amount, 2)},
    )


def place_market_sell(symbol, quantity):
    return _signed_request(
        "POST",
        "/api/v3/order",
        {"symbol": symbol, "side": "SELL", "type": "MARKET", "quantity": quantity},
    )


def compute_rsi(closes, period=RSI_PERIOD):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_ema(closes, period):
    if len(closes) < period:
        return closes[-1]
    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


def run_cycle(step_size):
    closes = get_klines(SYMBOL, KLINE_INTERVAL)
    price = closes[-1]
    rsi = compute_rsi(closes)
    ema_trend = compute_ema(closes, EMA_TREND_PERIOD)
    trend_up = price > ema_trend

    shib_balance = get_free_balance(BASE_ASSET)
    usdt_balance = get_free_balance(QUOTE_ASSET)
    has_position = (shib_balance * price) > 5.0

    log.info(
        "fiyat=%.8f rsi=%.2f ema50=%.8f trend_up=%s shib=%.2f usdt=%.2f pozisyon=%s",
        price, rsi, ema_trend, trend_up, shib_balance, usdt_balance, has_position,
    )

    if not has_position and rsi < RSI_OVERSOLD and trend_up:
        buy_amount = usdt_balance * POSITION_FRACTION
        if buy_amount < 5.0:
            log.warning("Alim icin yetersiz USDT bakiyesi (%.2f), islem atlandi.", buy_amount)
            return
        order = place_market_buy_quote(SYMBOL, buy_amount)
        log.info("ALIM emri gonderildi: %s", order)

    elif has_position and rsi > RSI_OVERBOUGHT:
        quantity = round_down_to_step(shib_balance, step_size)
        if quantity <= 0:
            log.warning("Satis icin yetersiz SHIB miktari, islem atlandi.")
            return
        order = place_market_sell(SYMBOL, quantity)
        log.info("SATIS emri gonderildi: %s", order)

    else:
        log.info("Sinyal yok, bekleniyor.")


def main():
    if not API_KEY or not API_SECRET:
        raise SystemExit("BINANCE_API_KEY ve BINANCE_API_SECRET ortam degiskenlerini ayarlayin.")

    step_size = get_lot_step_size(SYMBOL)
    log.info("Bot baslatildi. Sembol=%s, pozisyon orani=%%%d", SYMBOL, POSITION_FRACTION * 100)

    while True:
        try:
            run_cycle(step_size)
        except Exception:
            log.exception("Dongude hata olustu, bir sonraki denemede devam edilecek.")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
