"""
trend_engine.py'yi (rejim/guven-skoru/hedef-dagilim motoru) gecmis veride
test eden arac. grid_bot.py / grid_backtest.py'ye HIC dokunmaz - o ayri,
kanitlanmis grid stratejisi calismaya devam eder. Bu, "nakitte bos duran
payi coklu zaman dilimi + BTC/ETH/BNB trend filtresiyle degerlendirebilir
miyiz" sorusunu test eden BAGIMSIZ bir arac.

Kullanim:
    python3 trend_engine_backtest.py

Ne yapar:
  1) SHIB'i 3m/5m/15m/30m/1h/4h/1d, BTC/ETH/BNB'yi 1h/4h/1d'de ceker.
  2) 15 dakikalik "saat" uzerinde, her REBALANCE_INTERVAL_MINUTES'de bir
     trend_engine.classify() cagirip hedef SHIB yuzdesini gunceller
     (kademeli - MAX_STEP_PERCENT'ten fazla degisemez).
  3) Portfoyu bu hedefe dogru (esik-alti kucuk farklari atlayarak, ucret
     dahil) yeniden dengeler.
  4) 30/90/180 gun icin: USDT getirisi, token birikimi, max dusus, islem
     sayisi, "kacirilan buyuk yukselis" / "onlenemeyen buyuk dusus" sayisi.
  5) Ayni veride IKI govde (TAM: WaveTrend+Nadaraya-Watson dahil; BASIT:
     sadece coklu-zaman-dilimi EMA + ADX + majors filtresi) calistirilip
     karsilastirilir - "baska bir gosterge kombinasyonu daha mi iyi"
     sorusuna bu calistirmalar cevap verir.
"""

import os
import time

from backtest import fetch_history, INTERVAL_MS
from trade_bot import _load_dotenv
import trend_engine as te

_load_dotenv()

SYMBOL = os.environ.get("SYMBOL", "SHIBUSDT")
MAJOR_SYMBOLS = [s.strip() for s in os.environ.get("ENGINE_MAJOR_SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT").split(",") if s.strip()]
BACKTEST_DAYS = int(os.environ.get("BACKTEST_DAYS", "30"))
BACKTEST_START_CAPITAL = float(os.environ.get("BACKTEST_START_CAPITAL", "1000"))
TRADING_FEE_PERCENT = float(os.environ.get("TRADING_FEE_PERCENT", "0.1"))
START_IN_SHIB = os.environ.get("START_IN_SHIB", "false").lower() in ("1", "true", "evet")

BASE_INTERVAL = os.environ.get("ENGINE_BASE_INTERVAL", "15m")
SHIB_TIMEFRAMES = ["3m", "5m", "15m", "30m", "1h", "4h", "1d"]
MAJOR_TIMEFRAMES = ["1h", "4h", "1d"]

REBALANCE_INTERVAL_MINUTES = int(os.environ.get("ENGINE_REBALANCE_MINUTES", "60"))
MAX_STEP_PERCENT = float(os.environ.get("ENGINE_MAX_STEP_PERCENT", "10"))
MIN_REBALANCE_DELTA_PERCENT = float(os.environ.get("ENGINE_MIN_REBALANCE_DELTA", "5"))
BIG_MOVE_THRESHOLD_PERCENT = float(os.environ.get("ENGINE_BIG_MOVE_THRESHOLD", "5"))
BIG_MOVE_WINDOW_HOURS = float(os.environ.get("ENGINE_BIG_MOVE_WINDOW_HOURS", "24"))


# EMA(50)/ADX(14)'un "isinmasi" icin zaman dilimi basina gereken ekstra
# gecmis gun sayisi (orn. 1 gunluk EMA(50) icin 50 GUN gecmis gerekir - bu
# olmadan backtest'in tamami o zaman diliminde veri-yok/None doner). Bu
# ekstra gunler SADECE gosterge hesaplamak icin cekilir; simulasyon yine de
# sadece istenen BACKTEST_DAYS penceresinde calisir (asagida base seri
# kesiliyor).
WARMUP_DAYS = {"3m": 2, "5m": 2, "15m": 2, "30m": 3, "1h": 6, "4h": 16, "1d": 60}


def _fetch_series(symbol, interval, days, compute_wt=False, compute_nw=False):
    fetch_days = days + WARMUP_DAYS.get(interval, 2)
    candles = fetch_history(symbol, interval, fetch_days)
    open_times = [c[0] for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    closes = [float(c[4]) for c in candles]
    volumes = [float(c[5]) for c in candles]
    return te.TimeframeSeries(open_times, highs, lows, closes, volumes,
                               compute_wt=compute_wt, compute_nw=compute_nw)


def _fetch_all(days):
    print(f"Gecmis veri cekiliyor: {SYMBOL} (7 zaman dilimi, gosterge isinmasi icin fazladan gecmisle) "
          f"+ {', '.join(MAJOR_SYMBOLS)} (3 zaman dilimi), simulasyon penceresi son {days} gun...")
    shib_series = {}
    for tf in SHIB_TIMEFRAMES:
        shib_series[tf] = _fetch_series(SYMBOL, tf, days, compute_wt=(tf == "1h"), compute_nw=(tf == "1h"))
    majors_series = {}
    for sym in MAJOR_SYMBOLS:
        majors_series[sym] = {}
        for tf in MAJOR_TIMEFRAMES:
            majors_series[sym][tf] = _fetch_series(sym, tf, days)

    # Base (simulasyon) serisi SADECE istenen `days` penceresini kapsamali -
    # ekstra isinma gecmisi simulasyon suresine dahil edilmemeli.
    base_full = shib_series[BASE_INTERVAL]
    kesim_zamani = base_full.open_times[-1] - days * 86_400_000
    kesim_idx = 0
    while kesim_idx < len(base_full.open_times) and base_full.open_times[kesim_idx] < kesim_zamani:
        kesim_idx += 1
    zamanlar = base_full.open_times[kesim_idx:]
    kapanislar = base_full.closes[kesim_idx:]
    return shib_series, majors_series, zamanlar, kapanislar


def _big_move_events(zamanlar, kapanislar, threshold_pct, window_hours):
    """Fiyatin `window_hours` icinde >= threshold_pct hareket ettigi
    donemleri (baslangic_idx, bitis_idx, yon) olarak dondurur. Ustuste
    binen olaylari basitce ayri ayri sayar (yaklasik bir olcu, kesin
    degil)."""
    window_ms = window_hours * 3_600_000
    n = len(kapanislar)
    events = []
    j = 0
    i = 0
    while i < n:
        while j < n and zamanlar[j] - zamanlar[i] <= window_ms:
            j += 1
        son = min(j, n - 1)
        if son > i:
            degisim = (kapanislar[son] - kapanislar[i]) / kapanislar[i] * 100
            if abs(degisim) >= threshold_pct:
                yon = "YUKARI" if degisim > 0 else "ASAGI"
                events.append((i, son, yon, degisim))
                i = son  # ayni olayi tekrar tekrar saymamak icin ileri atla
                continue
        i += 1
    return events


def simulate(shib_series, majors_series, zamanlar, kapanislar, use_wt, use_nw):
    if START_IN_SHIB:
        usdt = 0.0
        coin = BACKTEST_START_CAPITAL / kapanislar[0]
        actual_pct = 100.0
    else:
        usdt = BACKTEST_START_CAPITAL
        coin = 0.0
        actual_pct = 0.0
    baslangic_coin_esdeger = coin + usdt / kapanislar[0]

    base_interval_minutes = INTERVAL_MS[BASE_INTERVAL] / 60_000
    steps_per_rebalance = max(1, round(REBALANCE_INTERVAL_MINUTES / base_interval_minutes))

    trades = []
    equity_egrisi = []
    coin_egrisi = []
    shib_pct_gecmisi = []
    regime_sayaci = {}
    target = actual_pct
    prev_score = 0.0

    for i, fiyat in enumerate(kapanislar):
        tarih = time.strftime("%Y-%m-%d %H:%M", time.localtime(zamanlar[i] / 1000))
        toplam_deger = usdt + coin * fiyat

        if i % steps_per_rebalance == 0:
            sonuc = te.classify(
                shib_series, majors_series, zamanlar[i], SHIB_TIMEFRAMES,
                use_wt=use_wt, use_nw=use_nw, prev_score=prev_score,
            )
            prev_score = sonuc["score"]
            regime_sayaci[sonuc["regime"]] = regime_sayaci.get(sonuc["regime"], 0) + 1
            target = te.smooth_target(target, sonuc["target_shib_pct_raw"], MAX_STEP_PERCENT)

            fark = target - actual_pct
            if abs(fark) >= MIN_REBALANCE_DELTA_PERCENT and toplam_deger > 0:
                hedef_shib_deger = toplam_deger * (target / 100)
                simdiki_shib_deger = coin * fiyat
                delta_deger = hedef_shib_deger - simdiki_shib_deger
                if delta_deger > 0:  # SHIB AL
                    harcanacak = min(delta_deger, usdt)
                    if harcanacak > 0:
                        alim_ucreti = harcanacak * TRADING_FEE_PERCENT / 100
                        qty = (harcanacak - alim_ucreti) / fiyat
                        usdt -= harcanacak
                        coin += qty
                        trades.append({"tip": "ENGINE_AL", "tarih": tarih, "fiyat": fiyat, "regime": sonuc["regime"]})
                else:  # SHIB SAT
                    satilacak_qty = min(coin, -delta_deger / fiyat)
                    if satilacak_qty > 0:
                        brut = satilacak_qty * fiyat
                        net = brut * (1 - TRADING_FEE_PERCENT / 100)
                        usdt += net
                        coin -= satilacak_qty
                        trades.append({"tip": "ENGINE_SAT", "tarih": tarih, "fiyat": fiyat, "regime": sonuc["regime"]})

        toplam_deger = usdt + coin * fiyat
        actual_pct = (coin * fiyat / toplam_deger * 100) if toplam_deger > 0 else actual_pct
        equity_egrisi.append(toplam_deger)
        coin_egrisi.append(coin + usdt / fiyat)
        shib_pct_gecmisi.append(actual_pct)

    return {
        "trades": trades, "equity_egrisi": equity_egrisi, "coin_egrisi": coin_egrisi,
        "baslangic_coin": baslangic_coin_esdeger, "shib_pct_gecmisi": shib_pct_gecmisi,
        "regime_sayaci": regime_sayaci,
    }


def _max_drawdown(equity_egrisi):
    tepe = float("-inf")
    max_dusus = 0.0
    for deger in equity_egrisi:
        tepe = max(tepe, deger)
        if tepe > 0:
            max_dusus = min(max_dusus, (deger - tepe) / tepe * 100)
    return max_dusus


def _missed_trend_report(zamanlar, kapanislar, shib_pct_gecmisi):
    events = _big_move_events(zamanlar, kapanislar, BIG_MOVE_THRESHOLD_PERCENT, BIG_MOVE_WINDOW_HOURS)
    kacirilan_yukselis = 0
    onlenemeyen_dusus = 0
    for start, end, yon, degisim in events:
        ort_pct = sum(shib_pct_gecmisi[start:end + 1]) / max(1, end - start + 1)
        if yon == "YUKARI" and ort_pct < 50:
            kacirilan_yukselis += 1
        elif yon == "ASAGI" and ort_pct > 50:
            onlenemeyen_dusus += 1
    return len(events), kacirilan_yukselis, onlenemeyen_dusus


def _ozet_yazdir(etiket, sonuc, zamanlar, kapanislar, mum_sayisi):
    equity_egrisi = sonuc["equity_egrisi"]
    coin_egrisi = sonuc["coin_egrisi"]
    baslangic_coin = sonuc["baslangic_coin"]
    trades = sonuc["trades"]

    toplam_deger = equity_egrisi[-1] if equity_egrisi else BACKTEST_START_CAPITAL
    getiri_yuzde = (toplam_deger - BACKTEST_START_CAPITAL) / BACKTEST_START_CAPITAL * 100
    bitis_coin = coin_egrisi[-1] if coin_egrisi else baslangic_coin
    token_degisim = (bitis_coin - baslangic_coin) / baslangic_coin * 100 if baslangic_coin else 0
    max_dusus = _max_drawdown(equity_egrisi)
    toplam_olay, kacirilan_yukselis, onlenemeyen_dusus = _missed_trend_report(
        zamanlar, kapanislar, sonuc["shib_pct_gecmisi"]
    )

    print(f"\n--- {etiket} ---")
    print(f"Bitis degeri            : {toplam_deger:,.2f} USDT  (getiri {getiri_yuzde:+.2f}%)")
    print(f"TOKEN ADEDI DEGISIMI    : {token_degisim:+.2f}%")
    print(f"En buyuk gerileme (DD)  : {max_dusus:.2f}%")
    print(f"Toplam islem            : {len(trades)}")
    print(f"Rejim dagilimi (kontrol sayisi): {sonuc['regime_sayaci']}")
    print(f">=%{BIG_MOVE_THRESHOLD_PERCENT} buyuk hareket sayisi ({BIG_MOVE_WINDOW_HOURS:.0f}sa pencere): {toplam_olay}")
    print(f"  -> Kacirilan buyuk YUKSELIS (SHIB payi <%50 iken)  : {kacirilan_yukselis}")
    print(f"  -> Onlenemeyen buyuk DUSUS (SHIB payi >%50 iken)  : {onlenemeyen_dusus}")
    return {
        "getiri": getiri_yuzde, "token_degisim": token_degisim, "max_dusus": max_dusus,
        "islem": len(trades), "kacirilan_yukselis": kacirilan_yukselis, "onlenemeyen_dusus": onlenemeyen_dusus,
    }


def run_for_days(days):
    shib_series, majors_series, zamanlar, kapanislar = _fetch_all(days)
    print(f"\n{'=' * 60}\nTREND INTELLIGENCE ENGINE BACKTEST: {SYMBOL} - son {days} gun ({len(kapanislar)} bar, {BASE_INTERVAL})")
    print(f"Rebalance: her {REBALANCE_INTERVAL_MINUTES} dk, max adim %{MAX_STEP_PERCENT}, min esik %{MIN_REBALANCE_DELTA_PERCENT}")
    print("=" * 60)

    tam = simulate(shib_series, majors_series, zamanlar, kapanislar, use_wt=True, use_nw=True)
    ozet_tam = _ozet_yazdir("TAM (EMA+ADX+WaveTrend+Nadaraya-Watson+majors)", tam, zamanlar, kapanislar, len(kapanislar))

    basit = simulate(shib_series, majors_series, zamanlar, kapanislar, use_wt=False, use_nw=False)
    ozet_basit = _ozet_yazdir("BASIT (sadece EMA coklu-zaman-dilimi + ADX + majors)", basit, zamanlar, kapanislar, len(kapanislar))

    print(f"\n--- {days} GUN KARSILASTIRMA ---")
    daha_iyi = "TAM" if ozet_tam["token_degisim"] > ozet_basit["token_degisim"] else "BASIT"
    print(f"Token birikiminde daha iyi kombinasyon: {daha_iyi} "
          f"(TAM {ozet_tam['token_degisim']:+.2f}% vs BASIT {ozet_basit['token_degisim']:+.2f}%)")
    print("(Not: gecmis performans gelecegi garanti etmez, tek donem yeterli kanit degildir.)")


def main():
    run_for_days(BACKTEST_DAYS)


if __name__ == "__main__":
    main()
