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

EK GUVENLIK KATMANLARI (bu surumde eklendi):
  - Sinyaller yalnizca KAPANMIS mumlarla uretilir (son/olusmakta olan
    mum atlanir) ve ayni mumda birden fazla emir verilmez.
  - Binance LOT_SIZE kurallarina gore miktar asagi yuvarlanir.
  - Gunluk gerceklesmis zarar MAX_DAILY_LOSS_PERCENT'i asarsa yeni
    ALIM yapilmaz.
  - EMERGENCY_STOP_FILE (varsayilan STOP_BOT) adinda bir dosya
    olusturursaniz bot bir sonraki kontrolde durur.
  - TRADE_PERCENT kod seviyesinde en fazla %33 ile sinirlandirilmistir.
"""

import hashlib
import hmac
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_DOWN

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass  # bazi ortamlarda (or. yeniden yonlendirilmis stdout) reconfigure olmayabilir


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
EMA_SLOPE_LOOKBACK = int(os.environ.get("EMA_SLOPE_LOOKBACK", "5"))
ATR_PERIOD = int(os.environ.get("ATR_PERIOD", "14"))
ATR_STOP_MULTIPLIER = float(os.environ.get("ATR_STOP_MULTIPLIER", "2.0"))
ATR_TAKE_PROFIT_MULTIPLIER = float(os.environ.get("ATR_TAKE_PROFIT_MULTIPLIER", "3.0"))
VOLUME_PERIOD = int(os.environ.get("VOLUME_PERIOD", "20"))
VOLUME_MULTIPLIER = float(os.environ.get("VOLUME_MULTIPLIER", "1.20"))
REGIME_LOOKBACK = int(os.environ.get("REGIME_LOOKBACK", "20"))
REGIME_TREND_THRESHOLD = float(os.environ.get("REGIME_TREND_THRESHOLD", "0.3"))
STATE_FILE = os.environ.get("STATE_FILE", "trade_bot_state.json")
STOP_LOSS_PERCENT = float(os.environ.get("STOP_LOSS_PERCENT", "3"))
TAKE_PROFIT_PERCENT = float(os.environ.get("TAKE_PROFIT_PERCENT", "6"))
MAX_DAILY_LOSS_PERCENT = float(os.environ.get("MAX_DAILY_LOSS_PERCENT", "2"))
EMERGENCY_STOP_FILE = os.environ.get("EMERGENCY_STOP_FILE", "STOP_BOT")

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
    # Son mum henuz kapanmamistir. Sinyal uretiminde repaint/tekrar emir riskini
    # azaltmak icin yalnizca kapanmis mumlari kullaniriz.
    return [float(k[4]) for k in data[:-1]]


def get_candles(interval=None, limit=300):
    data = _http(
        "GET", "/api/v3/klines",
        {"symbol": SYMBOL, "interval": interval or KLINE_INTERVAL, "limit": limit},
        base=MAINNET_BASE,
    )[:-1]
    return [
        {"open_time": int(k[0]), "open": float(k[1]), "high": float(k[2]),
         "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])}
        for k in data
    ]


def atr_hesapla(candles, period=ATR_PERIOD):
    if len(candles) < period + 1:
        return None
    true_ranges = []
    for i in range(1, len(candles)):
        c, prev = candles[i], candles[i - 1]
        true_ranges.append(max(c["high"] - c["low"], abs(c["high"] - prev["close"]),
                               abs(c["low"] - prev["close"])))
    return sum(true_ranges[-period:]) / period


def hacim_onayi(candles):
    if len(candles) < VOLUME_PERIOD + 1:
        return False
    ortalama = sum(c["volume"] for c in candles[-VOLUME_PERIOD - 1:-1]) / VOLUME_PERIOD
    return candles[-1]["volume"] >= ortalama * VOLUME_MULTIPLIER


def ema_egimi_yukari(fiyatlar):
    if len(fiyatlar) < EMA_TREND_SHORT + EMA_SLOPE_LOOKBACK:
        return False
    simdi = ema_hesapla(fiyatlar, EMA_TREND_SHORT)
    once = ema_hesapla(fiyatlar[:-EMA_SLOPE_LOOKBACK], EMA_TREND_SHORT)
    return simdi is not None and once is not None and simdi > once


def ema_hesapla(fiyat_listesi, periyot):
    if len(fiyat_listesi) < periyot:
        return None
    k = 2 / (periyot + 1)
    ema = sum(fiyat_listesi[:periyot]) / periyot
    for fiyat in fiyat_listesi[periyot:]:
        ema = fiyat * k + ema * (1 - k)
    return ema


def verimlilik_orani(fiyatlar, periyot=20):
    """Kaufman Efficiency Ratio: son `periyot` mumda fiyatin NET ne kadar
    hareket ettigi / TOPLAM ne kadar zigzag yaptigi orani (0-1 arasi).
    1'e yakin = guclu/duz trend (fiyat cogunlukla tek yonde ilerlemis).
    0'a yakin = yatay/dalgali piyasa (ileri geri hareket, net ilerleme az).
    Rejim (trend vs grid stratejisi) secimi icin kullanilir."""
    if len(fiyatlar) < periyot + 1:
        return None
    pencere = fiyatlar[-(periyot + 1):]
    net_degisim = abs(pencere[-1] - pencere[0])
    toplam_hareket = sum(abs(pencere[i] - pencere[i - 1]) for i in range(1, len(pencere)))
    if toplam_hareket == 0:
        return 0.0
    return net_degisim / toplam_hareket


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


def get_symbol_rules():
    info = _http("GET", "/api/v3/exchangeInfo", {"symbol": SYMBOL}, base=MAINNET_BASE)
    symbols = info.get("symbols", [])
    if not symbols:
        raise RuntimeError(f"Binance sembolu bulunamadi: {SYMBOL}")
    filters = {f["filterType"]: f for f in symbols[0].get("filters", [])}
    lot = filters.get("LOT_SIZE", {})
    notional = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {}))
    return {
        "step_size": lot.get("stepSize", "1"),
        "min_qty": float(lot.get("minQty", 0)),
        "min_notional": float(notional.get("minNotional", 0)),
    }


def floor_to_step(value, step):
    value_d, step_d = Decimal(str(value)), Decimal(str(step))
    return float((value_d / step_d).to_integral_value(rounding=ROUND_DOWN) * step_d)


def place_order(side, quote_or_base_qty, use_quote_qty):
    params = {"symbol": SYMBOL, "side": side, "type": "MARKET"}
    if use_quote_qty:
        params["quoteOrderQty"] = f"{quote_or_base_qty:.8f}"
    else:
        rules = get_symbol_rules()
        qty = floor_to_step(quote_or_base_qty, rules["step_size"])
        if qty < rules["min_qty"]:
            raise RuntimeError(f"Miktar LOT_SIZE altinda: {qty} < {rules['min_qty']}")
        params["quantity"] = format(qty, ".16f").rstrip("0").rstrip(".")
    return _http("POST", "/api/v3/order", params, signed=True)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"in_position": False, "entry_price": None, "position_qty": 0.0,
            "entry_quote": 0.0, "day": time.strftime("%Y-%m-%d"),
            "daily_realized_pnl": 0.0, "day_start_quote": None,
            "last_candle_close": None, "stop_price": None, "take_profit_price": None}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def normalize_state(state):
    defaults = load_state() if not os.path.exists(STATE_FILE) else {
        "in_position": False, "entry_price": None, "position_qty": 0.0,
        "entry_quote": 0.0, "day": time.strftime("%Y-%m-%d"),
        "daily_realized_pnl": 0.0, "day_start_quote": None,
        "last_candle_close": None, "stop_price": None, "take_profit_price": None,
    }
    for key, value in defaults.items():
        state.setdefault(key, value)
    today = time.strftime("%Y-%m-%d")
    if state["day"] != today:
        state.update(day=today, daily_realized_pnl=0.0, day_start_quote=None)
    return state


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
    if not 0 < TRADE_PERCENT <= 33:
        raise SystemExit("TRADE_PERCENT guvenlik nedeniyle 0-33 araliginda olmali.")
    if STOP_LOSS_PERCENT <= 0 or TAKE_PROFIT_PERCENT <= 0 or MAX_DAILY_LOSS_PERCENT <= 0:
        raise SystemExit("Risk yuzdeleri sifirdan buyuk olmali.")


def run_once(state):
    normalize_state(state)
    if os.path.exists(EMERGENCY_STOP_FILE):
        raise SystemExit(f"Acil durdurma dosyasi bulundu: {EMERGENCY_STOP_FILE}")
    candles = get_candles()
    fiyatlar = [c["close"] for c in candles]
    fiyat = fiyatlar[-1]
    candle_key = f"{KLINE_INTERVAL}:{candles[-1]['open_time']}"
    if state.get("last_candle_close") == candle_key:
        print("Yeni kapanmis mum yok; bekleniyor.")
        return
    state["last_candle_close"] = candle_key
    rsi = rsi_hesapla(fiyatlar)
    yukselis_trendi = trend_yonu(fiyatlar)
    ema_egimi = ema_egimi_yukari(fiyatlar)
    hacim_yuksek = hacim_onayi(candles)
    atr = atr_hesapla(candles)
    zaman = time.strftime("%d/%m/%Y %H:%M:%S")
    trend_etiket = "?" if yukselis_trendi is None else ("YUKARI" if yukselis_trendi else "ASAGI")

    al_sinyali = (
        rsi < RSI_BUY_THRESHOLD
        and yukselis_trendi is True  # sadece yukselis trendinde al (dususte "dusen bicak" alimi yok)
        and ema_egimi
        and hacim_yuksek
        and not state["in_position"]
    )
    entry_price = state.get("entry_price")
    stop_level = state.get("stop_price") or (entry_price * (1 - STOP_LOSS_PERCENT / 100) if entry_price else None)
    take_level = state.get("take_profit_price") or (entry_price * (1 + TAKE_PROFIT_PERCENT / 100) if entry_price else None)
    stop_loss = bool(stop_level and fiyat <= stop_level)
    take_profit = bool(take_level and fiyat >= take_level)
    gunluk_limit = bool(
        state.get("day_start_quote")
        and state.get("daily_realized_pnl", 0) <= -state["day_start_quote"] * MAX_DAILY_LOSS_PERCENT / 100
    )
    al_sinyali = al_sinyali and not gunluk_limit
    sat_sinyali = state["in_position"] and (
        rsi > RSI_SELL_THRESHOLD  # asiri alim -> kar realizasyonu
        or yukselis_trendi is False  # trend bozuldu -> koruyucu cikis
        or stop_loss
        or take_profit
    )

    if al_sinyali:
        if MODE == "dry_run":
            notify(
                f"🟢 [SIMULE] {zaman} - {SYMBOL} RSI={rsi:.1f} trend={trend_etiket} fiyat={fiyat} "
                f"-> bakiyenin %{TRADE_PERCENT} ile ALIM yapilirdi."
            )
            state["in_position"] = True
            state["entry_price"] = fiyat
            if atr:
                state["stop_price"] = fiyat - atr * ATR_STOP_MULTIPLIER
                state["take_profit_price"] = fiyat + atr * ATR_TAKE_PROFIT_MULTIPLIER
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
                state["position_qty"] = float(order.get("executedQty", 0))
                state["entry_quote"] = float(order.get("cummulativeQuoteQty", harcanacak))
                state["entry_price"] = (
                    state["entry_quote"] / state["position_qty"] if state["position_qty"] else fiyat
                )
                state["day_start_quote"] = state.get("day_start_quote") or bakiye
                if atr:
                    state["stop_price"] = state["entry_price"] - atr * ATR_STOP_MULTIPLIER
                    state["take_profit_price"] = state["entry_price"] + atr * ATR_TAKE_PROFIT_MULTIPLIER

    elif sat_sinyali:
        if stop_loss:
            sebep = "stop-loss"
        elif take_profit:
            sebep = "kar-al"
        elif rsi > RSI_SELL_THRESHOLD:
            sebep = "asiri alim"
        else:
            sebep = "trend bozuldu"
        if MODE == "dry_run":
            notify(
                f"🔴 [SIMULE] {zaman} - {SYMBOL} RSI={rsi:.1f} trend={trend_etiket} fiyat={fiyat} "
                f"-> {sebep} nedeniyle pozisyonun %{TRADE_PERCENT} ile SATIS yapilirdi."
            )
            state["in_position"] = False
            state["entry_price"] = None
            state["stop_price"] = None
            state["take_profit_price"] = None
        else:
            bakiye = get_balance(BASE_ASSET)
            # Giriste acilan pozisyonu tamamen kapat; hesapta onceden bulunan
            # coinleri satma. Eski surumun kalan pozisyon/yanlis bayrak hatasini onler.
            satilacak = min(bakiye, float(state.get("position_qty") or bakiye))
            if satilacak <= 0:
                notify(f"⚠️ {BASE_ASSET} bakiyesi yetersiz, satis atlandi.")
            else:
                order = place_order("SELL", satilacak, use_quote_qty=False)
                notify(
                    f"🔴 [{MODE.upper()}] SATIS emri gonderildi ({sebep}, RSI={rsi:.1f} trend={trend_etiket}): {order}"
                )
                state["in_position"] = False
                received = float(order.get("cummulativeQuoteQty", satilacak * fiyat))
                state["daily_realized_pnl"] += received - float(state.get("entry_quote") or 0)
                state["entry_price"] = None
                state["position_qty"] = 0.0
                state["entry_quote"] = 0.0
                state["stop_price"] = None
                state["take_profit_price"] = None
    else:
        pozisyon = "POZISYONDA" if state["in_position"] else "BEKLEMEDE"
        print(f"{zaman} - {SYMBOL} RSI={rsi:.1f} trend={trend_etiket} "
              f"ema_egim={'YUKARI' if ema_egimi else 'YATAY/ASAGI'} "
              f"hacim={'ONAY' if hacim_yuksek else 'DUSUK'} fiyat={fiyat} [{pozisyon}] -> islem yok.")

    save_state(state)


def main():
    check_mode_guard()
    notify(
        f"🤖 Trade botu basladi. MODE={MODE} SYMBOL={SYMBOL} "
        f"TRADE_PERCENT=%{TRADE_PERCENT} POLL={POLL_INTERVAL_SECONDS}s"
    )
    state = normalize_state(load_state())
    while True:
        try:
            run_once(state)
        except Exception as e:
            notify(f"⚠️ Hata: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
