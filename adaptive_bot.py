"""
Hibrit (Rejim-Uyarlamali) Trade Botu

trade_bot.py (trend takip) ve grid_bot.py (salinim) botlarinin ikisini de
icerir, hangisinin kullanilacagina PIYASA REJIMINE gore kendisi karar verir:

  - Verimlilik Orani (Kaufman Efficiency Ratio) son REGIME_LOOKBACK mumda
    fiyatin NET hareketi / TOPLAM zigzag hareketi oranidir (0-1).
  - ER >= REGIME_TREND_THRESHOLD (varsayilan 0.3) -> "TREND rejimi":
    trade_bot.py'nin RSI+EMA+ATR mantigiyla giris yapar (tek pozisyon,
    ATR tabanli stop/kar hedefi).
  - ER < REGIME_TREND_THRESHOLD -> "RANGE rejimi": grid_bot.py'nin %-adim
    mantigiyla giris yapar (coklu lot, sabit %kar hedefi).

Acik pozisyonlar HANGI mantikla acildiysa O mantikla yonetilir/kapatilir -
rejim degisse bile bir trend pozisyonu aniden grid kurallarina, ya da tam
tersi, gecmez. Sadece YENI giris karari rejime gore degisir.

GUVENLIK: trade_bot.py ile ayni MODE sistemi, ayni EMERGENCY_STOP_FILE,
ayni gunluk zarar limiti, ayni Binance siparis/LOT_SIZE altyapisi (import
edilerek, tekrar yazilmadan) kullanilir.
"""

import json
import os
import time

from trade_bot import (
    MODE,
    SYMBOL,
    BASE_ASSET,
    QUOTE_ASSET,
    BINANCE_API_KEY,
    BINANCE_API_SECRET,
    EMERGENCY_STOP_FILE,
    KLINE_INTERVAL,
    POLL_INTERVAL_SECONDS,
    RSI_BUY_THRESHOLD,
    RSI_SELL_THRESHOLD,
    ATR_STOP_MULTIPLIER,
    ATR_TAKE_PROFIT_MULTIPLIER,
    REGIME_LOOKBACK,
    REGIME_TREND_THRESHOLD,
    _load_dotenv,
    get_candles,
    rsi_hesapla,
    trend_yonu,
    ema_egimi_yukari,
    hacim_onayi,
    atr_hesapla,
    verimlilik_orani,
    get_balance,
    place_order,
    notify,
)

_load_dotenv()

GRID_STEP_DOWN_PERCENT = float(os.environ.get("GRID_STEP_DOWN_PERCENT", "1"))
GRID_STEP_UP_PERCENT = float(os.environ.get("GRID_STEP_UP_PERCENT", "1"))
GRID_ORDER_PERCENT = float(os.environ.get("GRID_ORDER_PERCENT", "10"))
GRID_MAX_OPEN_LOTS = int(os.environ.get("GRID_MAX_OPEN_LOTS", "8"))
GRID_RESERVE_PERCENT = float(os.environ.get("GRID_RESERVE_PERCENT", "20"))
GRID_LOT_STOP_PERCENT = float(os.environ.get("GRID_LOT_STOP_PERCENT", "10"))
TREND_ORDER_PERCENT = float(os.environ.get("TRADE_PERCENT", "33"))
MAX_DAILY_LOSS_PERCENT = float(os.environ.get("MAX_DAILY_LOSS_PERCENT", "2"))
ADAPTIVE_STATE_FILE = os.environ.get("ADAPTIVE_STATE_FILE", "adaptive_bot_state.json")
CONFIRM_REAL_MONEY = os.environ.get("CONFIRM_REAL_MONEY", "")


def load_state():
    if os.path.exists(ADAPTIVE_STATE_FILE):
        with open(ADAPTIVE_STATE_FILE) as f:
            return json.load(f)
    return {
        "open_lots": [],  # her biri: strategy, qty, entry_price, quote_spent, stop_price?, take_profit_price?
        "grid_reference_price": None,
        "last_candle_close": None,
        "day": time.strftime("%Y-%m-%d"),
        "daily_realized_pnl": 0.0,
        "day_start_equity": None,
        "initial_equity": None,
    }


def save_state(state):
    with open(ADAPTIVE_STATE_FILE, "w") as f:
        json.dump(state, f)


def normalize_state(state):
    defaults = {
        "open_lots": [], "grid_reference_price": None, "last_candle_close": None,
        "day": time.strftime("%Y-%m-%d"), "daily_realized_pnl": 0.0,
        "day_start_equity": None, "initial_equity": None,
    }
    for key, value in defaults.items():
        state.setdefault(key, value)
    today = time.strftime("%Y-%m-%d")
    if state["day"] != today:
        state.update(day=today, daily_realized_pnl=0.0, day_start_equity=None)
    return state


def check_mode_guard():
    if MODE not in ("dry_run", "testnet", "live"):
        raise SystemExit(f"Gecersiz MODE: {MODE} (dry_run | testnet | live)")
    if MODE == "live":
        if CONFIRM_REAL_MONEY != "EVET":
            raise SystemExit(
                "MODE=live secildi ama CONFIRM_REAL_MONEY=EVET verilmedi. "
                "Gercek para riskini bilerek onaylamadan bot baslamaz."
            )
        if not (BINANCE_API_KEY and BINANCE_API_SECRET):
            raise SystemExit("MODE=live icin BINANCE_API_KEY / BINANCE_API_SECRET gerekli.")
    if MODE == "testnet" and not (BINANCE_API_KEY and BINANCE_API_SECRET):
        raise SystemExit("MODE=testnet icin testnet API anahtarlari gerekli.")
    if not 0 < TREND_ORDER_PERCENT <= 33:
        raise SystemExit("TRADE_PERCENT guvenlik nedeniyle 0-33 araliginda olmali.")
    if not 0 < GRID_ORDER_PERCENT <= 33:
        raise SystemExit("GRID_ORDER_PERCENT guvenlik nedeniyle 0-33 araliginda olmali.")


def _gunluk_limit_asildi(state):
    return bool(
        state.get("day_start_equity")
        and state.get("daily_realized_pnl", 0) <= -state["day_start_equity"] * MAX_DAILY_LOSS_PERCENT / 100
    )


def _lot_sat(state, lot, fiyat, sebep, zaman):
    if MODE == "dry_run":
        notify(
            f"🔴 [SIMULE] {zaman} - {SYMBOL} [{lot['strategy'].upper()}] fiyat={fiyat} "
            f"giris={lot['entry_price']:.10f} -> SATIS ({sebep})"
        )
    else:
        bakiye = get_balance(BASE_ASSET)
        satilacak = min(bakiye, lot.get("qty") or bakiye)
        if satilacak <= 0:
            notify(f"⚠️ {BASE_ASSET} bakiyesi yetersiz, satis atlandi.")
            return
        order = place_order("SELL", satilacak, use_quote_qty=False)
        alinan = float(order.get("cummulativeQuoteQty", satilacak * fiyat))
        kar = alinan - (lot.get("quote_spent") or 0)
        state["daily_realized_pnl"] += kar
        notify(
            f"🔴 [{MODE.upper()}] [{lot['strategy'].upper()}] SATIS ({sebep}, giris={lot['entry_price']:.10f} "
            f"fiyat={fiyat} kar={kar:+.4f} {QUOTE_ASSET}): {order}"
        )
    state["open_lots"].remove(lot)
    if lot["strategy"] == "grid":
        state["grid_reference_price"] = fiyat


def run_once(state):
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
    er = verimlilik_orani(fiyatlar, REGIME_LOOKBACK)
    rejim = "TREND" if (er is not None and er >= REGIME_TREND_THRESHOLD) else "RANGE"
    zaman = time.strftime("%d/%m/%Y %H:%M:%S")

    if state["grid_reference_price"] is None:
        state["grid_reference_price"] = fiyat

    # --- 1) Once acik lotlarin cikis kosulunu kontrol et (en dusuk giris fiyatli once) ---
    for lot in sorted(state["open_lots"], key=lambda l: l["entry_price"]):
        if lot["strategy"] == "trend":
            stop_ok = lot.get("stop_price") is not None and fiyat <= lot["stop_price"]
            tp_ok = lot.get("take_profit_price") is not None and fiyat >= lot["take_profit_price"]
            if stop_ok or tp_ok or rsi > RSI_SELL_THRESHOLD or yukselis_trendi is False:
                sebep = "stop-loss" if stop_ok else "kar-al" if tp_ok else (
                    "asiri alim" if rsi > RSI_SELL_THRESHOLD else "trend bozuldu"
                )
                _lot_sat(state, lot, fiyat, sebep, zaman)
                save_state(state)
                return
        else:  # grid
            hedef = lot["entry_price"] * (1 + GRID_STEP_UP_PERCENT / 100)
            stop_seviyesi = lot["entry_price"] * (1 - GRID_LOT_STOP_PERCENT / 100)
            if fiyat >= hedef or fiyat <= stop_seviyesi:
                sebep = "grid hedefi" if fiyat >= hedef else "grid stop-loss"
                _lot_sat(state, lot, fiyat, sebep, zaman)
                save_state(state)
                return

    # --- 2) Cikis yoksa, rejime gore YENI giris degerlendir ---
    gunluk_limit = _gunluk_limit_asildi(state)
    trend_lot_var = any(l["strategy"] == "trend" for l in state["open_lots"])
    grid_lot_sayisi = sum(1 for l in state["open_lots"] if l["strategy"] == "grid")

    if rejim == "TREND" and not trend_lot_var and not gunluk_limit:
        al_sinyali = (
            rsi < RSI_BUY_THRESHOLD and yukselis_trendi is True
            and ema_egimi and hacim_yuksek and atr is not None
        )
        if al_sinyali:
            stop_fiyati = fiyat - ATR_STOP_MULTIPLIER * atr
            kar_al_fiyati = fiyat + ATR_TAKE_PROFIT_MULTIPLIER * atr
            if MODE == "dry_run":
                notify(
                    f"🟢 [SIMULE] {zaman} - {SYMBOL} [TREND, ER={er:.2f}] fiyat={fiyat} "
                    f"-> ALIM. Stop={stop_fiyati:.10f} Hedef={kar_al_fiyati:.10f}"
                )
                state["open_lots"].append({
                    "strategy": "trend", "qty": None, "entry_price": fiyat, "quote_spent": None,
                    "stop_price": stop_fiyati, "take_profit_price": kar_al_fiyati,
                })
            else:
                bakiye = get_balance(QUOTE_ASSET)
                harcanacak = bakiye * (TREND_ORDER_PERCENT / 100)
                if harcanacak <= 0:
                    notify(f"⚠️ {QUOTE_ASSET} bakiyesi yetersiz, trend alimi atlandi.")
                else:
                    order = place_order("BUY", harcanacak, use_quote_qty=True)
                    qty = float(order.get("executedQty", 0))
                    quote_spent = float(order.get("cummulativeQuoteQty", harcanacak))
                    giris = (quote_spent / qty) if qty else fiyat
                    notify(
                        f"🟢 [{MODE.upper()}] [TREND, ER={er:.2f}] ALIM: {order}"
                    )
                    state["open_lots"].append({
                        "strategy": "trend", "qty": qty, "entry_price": giris, "quote_spent": quote_spent,
                        "stop_price": giris - ATR_STOP_MULTIPLIER * atr,
                        "take_profit_price": giris + ATR_TAKE_PROFIT_MULTIPLIER * atr,
                    })
                    if state.get("day_start_equity") is None:
                        state["day_start_equity"] = bakiye
            save_state(state)
            return

    elif rejim == "RANGE" and grid_lot_sayisi < GRID_MAX_OPEN_LOTS and not gunluk_limit:
        al_tetik = fiyat <= state["grid_reference_price"] * (1 - GRID_STEP_DOWN_PERCENT / 100)
        if al_tetik:
            if MODE == "dry_run":
                notify(
                    f"🟢 [SIMULE] {zaman} - {SYMBOL} [RANGE, ER={er:.2f}] fiyat={fiyat} "
                    f"referans={state['grid_reference_price']} -> GRID ALIM ({grid_lot_sayisi + 1}/{GRID_MAX_OPEN_LOTS})"
                )
                state["open_lots"].append({
                    "strategy": "grid", "qty": None, "entry_price": fiyat, "quote_spent": None,
                })
                state["grid_reference_price"] = fiyat
            else:
                bakiye = get_balance(QUOTE_ASSET)
                if state.get("initial_equity") is None:
                    coin_bakiye = get_balance(BASE_ASSET)
                    state["initial_equity"] = bakiye + coin_bakiye * fiyat
                rezerv_taban = state["initial_equity"] * (GRID_RESERVE_PERCENT / 100)
                harcanacak = bakiye * (GRID_ORDER_PERCENT / 100)
                if harcanacak <= 0:
                    notify(f"⚠️ {QUOTE_ASSET} bakiyesi yetersiz, grid alimi atlandi.")
                elif bakiye - harcanacak < rezerv_taban:
                    notify(f"⚠️ Rezerv sinirina yaklasildi, grid alimi atlandi.")
                else:
                    order = place_order("BUY", harcanacak, use_quote_qty=True)
                    qty = float(order.get("executedQty", 0))
                    quote_spent = float(order.get("cummulativeQuoteQty", harcanacak))
                    notify(
                        f"🟢 [{MODE.upper()}] [RANGE, ER={er:.2f}] GRID ALIM "
                        f"({grid_lot_sayisi + 1}/{GRID_MAX_OPEN_LOTS}): {order}"
                    )
                    state["open_lots"].append({
                        "strategy": "grid", "qty": qty,
                        "entry_price": (quote_spent / qty) if qty else fiyat, "quote_spent": quote_spent,
                    })
                    state["grid_reference_price"] = fiyat
                    if state.get("day_start_equity") is None:
                        state["day_start_equity"] = bakiye
            save_state(state)
            return

    trend_lot = sum(1 for l in state["open_lots"] if l["strategy"] == "trend")
    grid_lot = sum(1 for l in state["open_lots"] if l["strategy"] == "grid")
    er_etiket = f"{er:.2f}" if er is not None else "?"
    print(
        f"{zaman} - {SYMBOL} rejim={rejim} (ER={er_etiket}) RSI={rsi:.1f} "
        f"fiyat={fiyat} trend_lot={trend_lot} grid_lot={grid_lot}/{GRID_MAX_OPEN_LOTS} -> islem yok."
    )
    save_state(state)


def main():
    check_mode_guard()
    notify(
        f"🤖 Hibrit (rejim-uyarlamali) bot basladi. MODE={MODE} SYMBOL={SYMBOL} "
        f"REGIME_TREND_THRESHOLD={REGIME_TREND_THRESHOLD} POLL={POLL_INTERVAL_SECONDS}s"
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
