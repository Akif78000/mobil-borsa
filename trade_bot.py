"""
SHIB/USDT RSI Trade Botu (Binance)

GUVENLIK / MOD SISTEMI
-----------------------
MODE ortam degiskeni calisma bicimini belirler:

  dry_run  (VARSAYILAN) - Hicbir Binance emri gonderilmez. Sadece RSI
           sinyaline gore ne yapilacagini hesaplar, Telegram'a ve
           konsola "SIMULE" mesaji yazar. API anahtari GEREKMEZ.

  testnet  - Binance Spot Testnet'e (testnet.binance.vision) gercek emir
             gonderir ama bu ortamdaki para SAHTEDIR. Testnet API
             anahtari gerekir (https://testnet.binance.vision).

  live     - GERCEK PARAYLA gercek Binance hesabinizda emir acar/kapatir.
             Bunu acmak icin MODE=live YETMEZ, ayrica
             CONFIRM_REAL_MONEY=EVET de ortam degiskeni olarak
             verilmelidir; aksi halde bot baslamayi reddeder.

API anahtarlariniz asla bu dosyada / repoda saklanmaz. Ortam degiskeni
olarak (.env dosyasi ya da shell export ile) kendiniz saglarsiniz.

Botu siz kendi bilgisayarinizda / sunucunuzda / telefonunuzda (orn.
Termux) kendi anahtarlarinizla calistirirsiniz. Bu betik surekli
calisan bir servis olarak baskasi tarafindan sizin adiniza
isletilmiyor.
"""

import hashlib
import hmac
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

def _load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

MODE = os.environ.get("MODE", "dry_run").lower()  # dry_run | testnet | live
SYMBOL = os.environ.get("SYMBOL", "SHIBUSDT")
BASE_ASSET = os.environ.get("BASE_ASSET", "SHIB")
QUOTE_ASSET = os.environ.get("QUOTE_ASSET", "USDT")
TRADE_PERCENT = float(os.environ.get("TRADE_PERCENT", "33"))
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
KLINE_INTERVAL = os.environ.get("KLINE_INTERVAL", "15m")
RSI_BUY_THRESHOLD = float(os.environ.get("RSI_BUY_THRESHOLD", "30"))
RSI_SELL_THRESHOLD = float(os.environ.get("RSI_SELL_THRESHOLD", "70"))
EMA_TREND_SHORT = int(os.environ.get("EMA_TREND_SHORT", "50"))
EMA_TREND_LONG = int(os.environ.get("EMA_TREND_LONG", "200"))
STATE_FILE = os.environ.get("STATE_FILE", "trade_bot_state.json")

BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MAINNET_BASE = "https://api.binance.com"
TESTNET_BASE = "https://testnet.binance.vision"

_CTX = ssl.create_default_context()


def _api_base():
    return TESTNET_BASE if MODE == "testnet" else MAINNET_BASE


def _http(method, path, params=None, signed=False, base=None):
    base = base or _api_base()
    params = dict(params or {})
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        query = urllib.parse.urlencode(params)
        signature = hmac.new(
            BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        query += f"&signature={signature}"
    else:
        query = urllib.parse.urlencode(params)

    url = f"{base}{path}"
    headers = {"User-Agent": "Mozilla/5.0"}
    if signed:
        headers["X-MBX-APIKEY"] = BINANCE_API_KEY

    if method == "GET":
        full_url = f"{url}?{query}" if query else url
        req = urllib.request.Request(full_url, headers=headers)
    else:
        req = urllib.request.Request(
            f"{url}?{query}" if query else url, method=method, headers=headers
        )

    with urllib.request.urlopen(req, context=_CTX, timeout=15) as resp:
        return json.loads(resp.read().decode())


def rsi_hesapla(fiyat_listesi):
    if len(fiyat_listesi) < 15:
        return 50
    kayiplar, kazanclar = [], []
    for i in range(1, len(fiyat_listesi)):
        fark = fiyat_listesi[i] - fiyat_listesi[i - 1]
        if fark > 0:
            kazanclar.append(fark)
            kayiplar.append(0)
        else:
            kazanclar.append(0)
            kayiplar.append(abs(fark))
    son_14_kazanc = sum(kazanclar[-14:]) / 14
    son_14_kayip = sum(kayiplar[-14:]) / 14
    if son_14_kayip == 0:
        return 100
    return 100 - (100 / (1 + (son_14_kazanc / son_14_kayip)))


def get_klines(interval=None, limit=300):
    data = _http(
        "GET",
        "/api/v3/klines",
        {"symbol": SYMBOL, "interval": interval or KLINE_INTERVAL, "limit": limit},
        base=MAINNET_BASE,  # fiyat verisi icin her zaman mainnet (gercek piyasa)
    )
    return [float(k[4]) for k in data]  # kapanis fiyatlari (son eleman = olusmakta olan mum)


def ema_hesapla(fiyat_listesi, periyot):
    if len(fiyat_listesi) < periyot:
        return None
    k = 2 / (periyot + 1)
    ema = sum(fiyat_listesi[:periyot]) / periyot
    for fiyat in fiyat_listesi[periyot:]:
        ema = fiyat * k + ema * (1 - k)
    return ema


def trend_yonu(fiyatlar):
    """True = yukselis trendi (kisa EMA > uzun EMA), False = dususte, None = yetersiz veri."""
    ema_kisa = ema_hesapla(fiyatlar, EMA_TREND_SHORT)
    ema_uzun = ema_hesapla(fiyatlar, EMA_TREND_LONG)
    if ema_kisa is None or ema_uzun is None:
        return None
    return ema_kisa > ema_uzun


def get_balance(asset):
    account = _http("GET", "/api/v3/account", signed=True)
    for b in account.get("balances", []):
        if b["asset"] == asset:
            return float(b["free"])
    return 0.0


def place_order(side, quote_or_base_qty, use_quote_qty):
    params = {"symbol": SYMBOL, "side": side, "type": "MARKET"}
    if use_quote_qty:
        params["quoteOrderQty"] = f"{quote_or_base_qty:.8f}"
    else:
        params["quantity"] = f"{quote_or_base_qty:.0f}"
    return _http("POST", "/api/v3/order", params, signed=True)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"in_position": False}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def notify(text):
    print(text)
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = urllib.parse.urlencode(
            {"chat_id": TELEGRAM_CHAT_ID, "text": text}
        ).encode()
        req = urllib.request.Request(url, data=payload, method="POST")
        urllib.request.urlopen(req, context=_CTX, timeout=10)
    except urllib.error.URLError as e:
        print(f"Telegram bildirimi gonderilemedi: {e}")


def check_mode_guard():
    if MODE not in ("dry_run", "testnet", "live"):
        raise SystemExit(f"Gecersiz MODE: {MODE} (dry_run | testnet | live)")
    if MODE == "live":
        if os.environ.get("CONFIRM_REAL_MONEY") != "EVET":
            raise SystemExit(
                "MODE=live secildi ama CONFIRM_REAL_MONEY=EVET verilmedi. "
                "Gercek para riskini bilerek onaylamadan bot baslamaz."
            )
        if not (BINANCE_API_KEY and BINANCE_API_SECRET):
            raise SystemExit("MODE=live icin BINANCE_API_KEY / BINANCE_API_SECRET gerekli.")
    if MODE == "testnet" and not (BINANCE_API_KEY and BINANCE_API_SECRET):
        raise SystemExit("MODE=testnet icin testnet API anahtarlari gerekli.")


def run_once(state):
    fiyatlar = get_klines()
    fiyat = fiyatlar[-1]
    rsi = rsi_hesapla(fiyatlar)
    yukselis_trendi = trend_yonu(fiyatlar)
    zaman = time.strftime("%d/%m/%Y %H:%M:%S")
    trend_etiket = "?" if yukselis_trendi is None else ("YUKARI" if yukselis_trendi else "ASAGI")

    al_sinyali = (
        rsi < RSI_BUY_THRESHOLD
        and yukselis_trendi is True  # sadece yukselis trendinde al (dususte "dusen bicak" alimi yok)
        and not state["in_position"]
    )
    sat_sinyali = state["in_position"] and (
        rsi > RSI_SELL_THRESHOLD  # asiri alim -> kar realizasyonu
        or yukselis_trendi is False  # trend bozuldu -> koruyucu cikis
    )

    if al_sinyali:
        if MODE == "dry_run":
            notify(
                f"🟢 [SIMULE] {zaman} - {SYMBOL} RSI={rsi:.1f} trend={trend_etiket} fiyat={fiyat} "
                f"-> bakiyenin %{TRADE_PERCENT} ile ALIM yapilirdi."
            )
            state["in_position"] = True
        else:
            bakiye = get_balance(QUOTE_ASSET)
            harcanacak = bakiye * (TRADE_PERCENT / 100)
            if harcanacak <= 0:
                notify(f"⚠️ {QUOTE_ASSET} bakiyesi yetersiz, alim atlandi.")
            else:
                order = place_order("BUY", harcanacak, use_quote_qty=True)
                notify(
                    f"🟢 [{MODE.upper()}] ALIM emri gonderildi (RSI={rsi:.1f} trend={trend_etiket}): {order}"
                )
                state["in_position"] = True

    elif sat_sinyali:
        sebep = "asiri alim" if rsi > RSI_SELL_THRESHOLD else "trend bozuldu"
        if MODE == "dry_run":
            notify(
                f"🔴 [SIMULE] {zaman} - {SYMBOL} RSI={rsi:.1f} trend={trend_etiket} fiyat={fiyat} "
                f"-> {sebep} nedeniyle pozisyonun %{TRADE_PERCENT} ile SATIS yapilirdi."
            )
            state["in_position"] = False
        else:
            bakiye = get_balance(BASE_ASSET)
            satilacak = bakiye * (TRADE_PERCENT / 100)
            if satilacak <= 0:
                notify(f"⚠️ {BASE_ASSET} bakiyesi yetersiz, satis atlandi.")
            else:
                order = place_order("SELL", satilacak, use_quote_qty=False)
                notify(
                    f"🔴 [{MODE.upper()}] SATIS emri gonderildi ({sebep}, RSI={rsi:.1f} trend={trend_etiket}): {order}"
                )
                state["in_position"] = False
    else:
        pozisyon = "POZISYONDA" if state["in_position"] else "BEKLEMEDE"
        print(f"{zaman} - {SYMBOL} RSI={rsi:.1f} trend={trend_etiket} fiyat={fiyat} [{pozisyon}] -> islem yok.")

    save_state(state)


def main():
    check_mode_guard()
    notify(
        f"🤖 Trade botu basladi. MODE={MODE} SYMBOL={SYMBOL} "
        f"TRADE_PERCENT=%{TRADE_PERCENT} POLL={POLL_INTERVAL_SECONDS}s"
    )
    state = load_state()
    while True:
        try:
            run_once(state)
        except Exception as e:
            notify(f"⚠️ Hata: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
