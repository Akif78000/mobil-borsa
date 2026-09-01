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
EMA_SLOPE_LOOKBACK = int(os.environ.get("EMA_SLOPE_LOOKBACK", "3"))
ATR_PERIOD = int(os.environ.get("ATR_PERIOD", "14"))
ATR_STOP_MULTIPLIER = float(os.environ.get("ATR_STOP_MULTIPLIER", "1.5"))
ATR_TP_MULTIPLIER = float(os.environ.get("ATR_TP_MULTIPLIER", "3.0"))
VOLUME_PERIOD = int(os.environ.get("VOLUME_PERIOD", "20"))
VOLUME_MULTIPLIER = float(os.environ.get("VOLUME_MULTIPLIER", "1.2"))
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
    """Kapanis, yuksek, dusuk ve hacim listelerini dondurur (son eleman =
    olusmakta olan mum). ATR ve hacim filtresi icin yuksek/dusuk/hacim de
    gerekli, sadece kapanis yeterli degil."""
    data = _http(
        "GET",
        "/api/v3/klines",
        {"symbol": SYMBOL, "interval": interval or KLINE_INTERVAL, "limit": limit},
        base=MAINNET_BASE,  # fiyat verisi icin her zaman mainnet (gercek piyasa)
    )
    kapanislar = [float(k[4]) for k in data]
    yuksekler = [float(k[2]) for k in data]
    dusukler = [float(k[3]) for k in data]
    hacimler = [float(k[5]) for k in data]
    return kapanislar, yuksekler, dusukler, hacimler


def atr_hesapla(yuksekler, dusukler, kapanislar, periyot=14):
    """Ortalama Gercek Aralik (Average True Range) - oynakliga gore stop/kar
    hedefi belirlemek icin. Basit hareketli ortalama kullanir (rsi_hesapla ile
    tutarli olsun diye Wilder yumusatmasi yerine)."""
    if len(kapanislar) < periyot + 1:
        return None
    gercek_araliklar = []
    for i in range(1, len(kapanislar)):
        tr = max(
            yuksekler[i] - dusukler[i],
            abs(yuksekler[i] - kapanislar[i - 1]),
            abs(dusukler[i] - kapanislar[i - 1]),
        )
        gercek_araliklar.append(tr)
    if len(gercek_araliklar) < periyot:
        return None
    return sum(gercek_araliklar[-periyot:]) / periyot


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
    varsayilan = {"in_position": False, "stop_price": None, "take_profit_price": None}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            kayitli = json.load(f)
        varsayilan.update(kayitli)
        return varsayilan
    return varsayilan


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
    kapanislar, yuksekler, dusukler, hacimler = get_klines()
    fiyat = kapanislar[-1]
    rsi = rsi_hesapla(kapanislar)
    yukselis_trendi = trend_yonu(kapanislar)

    # Egim icin kisa EMA degil, UZUN EMA (200) kullanilir: kisa EMA zaten RSI
    # dususu sirasinda asagi doner (bu tam da alinmak istenen dip), o yuzden
    # kisa EMA'nin egimini sart kosmak RSI mantigiyla celisir. Uzun EMA cok
    # daha yavas tepki verdigi icin kisa vadeli bir geri cekilmede bile ana
    # trendin gercekten guclenip guclenmedigini gosterir.
    ema_uzun_simdi = ema_hesapla(kapanislar, EMA_TREND_LONG)
    ema_uzun_once = (
        ema_hesapla(kapanislar[:-EMA_SLOPE_LOOKBACK], EMA_TREND_LONG)
        if len(kapanislar) > EMA_TREND_LONG + EMA_SLOPE_LOOKBACK
        else None
    )
    trend_yukseliyor = (
        None if ema_uzun_simdi is None or ema_uzun_once is None else ema_uzun_simdi > ema_uzun_once
    )

    atr = atr_hesapla(yuksekler, dusukler, kapanislar, ATR_PERIOD)

    ortalama_hacim = (
        sum(hacimler[-VOLUME_PERIOD:]) / VOLUME_PERIOD if len(hacimler) >= VOLUME_PERIOD else None
    )
    guncel_hacim = hacimler[-1]
    hacim_teyidi = ortalama_hacim is not None and guncel_hacim > ortalama_hacim * VOLUME_MULTIPLIER

    zaman = time.strftime("%d/%m/%Y %H:%M:%S")
    trend_etiket = "?" if yukselis_trendi is None else ("YUKARI" if yukselis_trendi else "ASAGI")

    al_sinyali = (
        not state["in_position"]
        and rsi < RSI_BUY_THRESHOLD
        and yukselis_trendi is True  # sadece yukselis trendinde al (dususte "dusen bicak" alimi yok)
        and trend_yukseliyor is True  # EMA egimi de yukari olsun (yataylasan/donen trende girme)
        and hacim_teyidi  # dusuk hacimli, guvenilmez hareketleri eleme
        and atr is not None  # ATR yoksa stop/kar hedefi konulamaz, islem yapma
    )

    stop_tetiklendi = state["in_position"] and state.get("stop_price") and fiyat <= state["stop_price"]
    tp_tetiklendi = (
        state["in_position"]
        and not stop_tetiklendi
        and state.get("take_profit_price")
        and fiyat >= state["take_profit_price"]
    )
    sat_sinyali = state["in_position"] and (
        stop_tetiklendi
        or tp_tetiklendi
        or rsi > RSI_SELL_THRESHOLD  # asiri alim -> kar realizasyonu
        or yukselis_trendi is False  # trend bozuldu -> koruyucu cikis
    )

    if al_sinyali:
        stop_fiyati = fiyat - ATR_STOP_MULTIPLIER * atr
        kar_hedefi = fiyat + ATR_TP_MULTIPLIER * atr
        if MODE == "dry_run":
            notify(
                f"🟢 [SIMULE] {zaman} - {SYMBOL} RSI={rsi:.1f} trend={trend_etiket} fiyat={fiyat} "
                f"-> bakiyenin %{TRADE_PERCENT} ile ALIM yapilirdi. "
                f"Stop={stop_fiyati:.8f} Hedef={kar_hedefi:.8f}"
            )
            state["in_position"] = True
            state["stop_price"] = stop_fiyati
            state["take_profit_price"] = kar_hedefi
        else:
            bakiye = get_balance(QUOTE_ASSET)
            harcanacak = bakiye * (TRADE_PERCENT / 100)
            if harcanacak <= 0:
                notify(f"⚠️ {QUOTE_ASSET} bakiyesi yetersiz, alim atlandi.")
            else:
                order = place_order("BUY", harcanacak, use_quote_qty=True)
                notify(
                    f"🟢 [{MODE.upper()}] ALIM emri gonderildi (RSI={rsi:.1f} trend={trend_etiket} "
                    f"Stop={stop_fiyati:.8f} Hedef={kar_hedefi:.8f}): {order}"
                )
                state["in_position"] = True
                state["stop_price"] = stop_fiyati
                state["take_profit_price"] = kar_hedefi

    elif sat_sinyali:
        if stop_tetiklendi:
            sebep = "stop-loss"
        elif tp_tetiklendi:
            sebep = "kar hedefi"
        elif rsi > RSI_SELL_THRESHOLD:
            sebep = "asiri alim"
        else:
            sebep = "trend bozuldu"
        if MODE == "dry_run":
            notify(
                f"🔴 [SIMULE] {zaman} - {SYMBOL} RSI={rsi:.1f} trend={trend_etiket} fiyat={fiyat} "
                f"-> {sebep} nedeniyle pozisyonun %{TRADE_PERCENT} ile SATIS yapilirdi."
            )
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
        state["stop_price"] = None
        state["take_profit_price"] = None
    else:
        pozisyon = "POZISYONDA" if state["in_position"] else "BEKLEMEDE"
        hacim_etiket = "yeterli" if hacim_teyidi else "yetersiz"
        print(
            f"{zaman} - {SYMBOL} RSI={rsi:.1f} trend={trend_etiket} egim={'yukari' if trend_yukseliyor else 'asagi/?'} "
            f"hacim={hacim_etiket} fiyat={fiyat} [{pozisyon}] -> islem yok."
        )

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
