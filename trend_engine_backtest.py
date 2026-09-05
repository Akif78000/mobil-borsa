"""
Uc katmanli mimariyi (trend_engine.py -> portfolio_manager.py -> executor)
gecmis veride test eden arac. grid_bot.py / grid_backtest.py'ye HIC
dokunmaz - o ayri, kanitlanmis grid stratejisi calismaya devam eder.

Kullanim:
    python3 trend_engine_backtest.py

Ne yapar:
  1) SHIB + BTC/ETH/BNB'yi 1sa/4sa/1gun'de ceker (gosterge isinmasi icin
     fazladan gecmisle).
  2) 1 saatlik "saat" uzerinde, her ENGINE_REBALANCE_MINUTES'de bir
     trend_engine.classify() + portfolio_manager.target_allocation()
     cagirip hedef SHIB yuzdesini gunceller (kademeli).
  3) Portfoyu bu hedefe dogru (esik-alti farklari atlayarak, ucret dahil)
     yeniden dengeler.
  4) UC dedektoru (EMA / SUPERTREND / KAMA) AYNI veride yaristirir - "SHIB
     icin hangi gosterge daha iyi calisiyor" sorusuna bu cevap verir.
  5) 30/90/180 gun icin sadece kar degil: USDT getirisi, token birikimi,
     max dusus, islem sayisi, TREND YAKALAMA ORANI, KACIRILAN GUCLU TREND
     SAYISI, TREND TESPIT GECIKMESI, YANLIS POZITIF ORANI raporlar.
"""

import os
import time

from backtest import fetch_history, INTERVAL_MS
from trade_bot import _load_dotenv
import trend_engine as te
import portfolio_manager as pm

_load_dotenv()

SYMBOL = os.environ.get("SYMBOL", "SHIBUSDT")
MAJOR_SYMBOLS = [s.strip() for s in os.environ.get("ENGINE_MAJOR_SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT").split(",") if s.strip()]
BACKTEST_DAYS = int(os.environ.get("BACKTEST_DAYS", "30"))
BACKTEST_START_CAPITAL = float(os.environ.get("BACKTEST_START_CAPITAL", "1000"))
TRADING_FEE_PERCENT = float(os.environ.get("TRADING_FEE_PERCENT", "0.1"))
# Binance MARKET emri gercek fiyattan biraz kayabilir (defter derinligine gore) -
# bunu her islemde ekstra bir maliyet yuzdesi olarak modelliyoruz (komisyonla
# ayni yonde etki eder, buyuk emirlerde/dusuk likiditede daha yuksek tutulmali).
SLIPPAGE_PERCENT = float(os.environ.get("SLIPPAGE_PERCENT", "0.05"))
START_IN_SHIB = os.environ.get("START_IN_SHIB", "false").lower() in ("1", "true", "evet")

BASE_INTERVAL = "1h"
TIMEFRAMES = te.TIMEFRAMES  # ["1h", "4h", "1d"]

REBALANCE_INTERVAL_MINUTES = int(os.environ.get("ENGINE_REBALANCE_MINUTES", "60"))
MAX_STEP_PERCENT = float(os.environ.get("ENGINE_MAX_STEP_PERCENT", "10"))
MIN_REBALANCE_DELTA_PERCENT = float(os.environ.get("ENGINE_MIN_REBALANCE_DELTA", "5"))
BIG_MOVE_THRESHOLD_PERCENT = float(os.environ.get("ENGINE_BIG_MOVE_THRESHOLD", "5"))
BIG_MOVE_WINDOW_HOURS = float(os.environ.get("ENGINE_BIG_MOVE_WINDOW_HOURS", "24"))

# EMA(50)/KAMA(30)/SuperTrend(10)/ADX(14) gibi gostergelerin "isinmasi" icin
# zaman dilimi basina gereken ekstra gecmis gun sayisi (bkz. onceki surumde
# bulunan 1-gunluk EMA(50) sorunu - 50 GUN gecmis olmadan hep veri-yok doner).
WARMUP_DAYS = {"1h": 6, "4h": 16, "1d": 60}


def _fetch_series(symbol, interval, days, detector):
    fetch_days = days + WARMUP_DAYS.get(interval, 6)
    candles = fetch_history(symbol, interval, fetch_days)
    open_times = [c[0] for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    closes = [float(c[4]) for c in candles]
    volumes = [float(c[5]) for c in candles]
    return te.TimeframeSeries(open_times, highs, lows, closes, volumes, INTERVAL_MS[interval], detector=detector)


def _fetch_all(days, detector):
    print(f"Gecmis veri cekiliyor: {SYMBOL} + {', '.join(MAJOR_SYMBOLS)} (1sa/4sa/1gun, "
          f"gosterge isinmasi icin fazladan gecmisle), simulasyon penceresi son {days} gun, dedektor={detector}...")
    shib_series = {tf: _fetch_series(SYMBOL, tf, days, detector) for tf in TIMEFRAMES}
    majors_series = {}
    for sym in MAJOR_SYMBOLS:
        majors_series[sym] = {tf: _fetch_series(sym, tf, days, "EMA") for tf in TIMEFRAMES}

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
    (baslangic_idx, bitis_idx, yon, degisim) olaylarini dondurur."""
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
                i = son
                continue
        i += 1
    return events


def simulate(shib_series, majors_series, zamanlar, kapanislar, cost_percent=None):
    """cost_percent: bir islemde uygulanacak TOPLAM maliyet yuzdesi (komisyon
    + kayma). None ise TRADING_FEE_PERCENT+SLIPPAGE_PERCENT kullanilir; 0
    verilirse maliyetsiz (BRUT) bir kosum yapilir - NET ile karsilastirmak
    icin ayni karar dizisini iki kez calistirmak amaciyla."""
    islem_maliyeti_yuzde = (TRADING_FEE_PERCENT + SLIPPAGE_PERCENT) if cost_percent is None else cost_percent
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
    regime_gecmisi = []  # (idx, timestamp_ms, regime, score, target)
    target = actual_pct

    for i, fiyat in enumerate(kapanislar):
        tarih = time.strftime("%Y-%m-%d %H:%M", time.localtime(zamanlar[i] / 1000))
        # `fiyat` = bu barin KAPANIS fiyati; bar gercekte zamanlar[i] +
        # BASE_INTERVAL kadar sonra kapanir. Karar anini o gercek kapanis
        # zamanina gore almazsak, henuz kapanmamis ust zaman dilimi
        # barlarini "biliniyor" sayma riski olur (lookahead).
        karar_zamani = zamanlar[i] + INTERVAL_MS[BASE_INTERVAL]

        if i % steps_per_rebalance == 0:
            sonuc = te.classify(shib_series, majors_series, karar_zamani)
            hedef_raw = pm.target_allocation(sonuc["regime"], sonuc["confidence"], sonuc["score"])
            target = pm.smooth_target(target, hedef_raw, MAX_STEP_PERCENT)
            regime_gecmisi.append((i, karar_zamani, sonuc["regime"], sonuc["score"], target))

            toplam_deger = usdt + coin * fiyat
            fark = target - actual_pct
            if abs(fark) >= MIN_REBALANCE_DELTA_PERCENT and toplam_deger > 0:
                hedef_shib_deger = toplam_deger * (target / 100)
                simdiki_shib_deger = coin * fiyat
                delta_deger = hedef_shib_deger - simdiki_shib_deger
                if delta_deger > 0:
                    harcanacak = min(delta_deger, usdt)
                    if harcanacak > 0:
                        maliyet = harcanacak * islem_maliyeti_yuzde / 100
                        qty = (harcanacak - maliyet) / fiyat
                        usdt -= harcanacak
                        coin += qty
                        trades.append({"tip": "ENGINE_AL", "tarih": tarih, "fiyat": fiyat, "regime": sonuc["regime"]})
                else:
                    satilacak_qty = min(coin, -delta_deger / fiyat)
                    if satilacak_qty > 0:
                        brut = satilacak_qty * fiyat
                        net = brut * (1 - islem_maliyeti_yuzde / 100)
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
        "regime_gecmisi": regime_gecmisi,
    }


def _max_drawdown(equity_egrisi):
    tepe = float("-inf")
    max_dusus = 0.0
    for deger in equity_egrisi:
        tepe = max(tepe, deger)
        if tepe > 0:
            max_dusus = min(max_dusus, (deger - tepe) / tepe * 100)
    return max_dusus


def _rejim_bazli_ort_pct(shib_pct_gecmisi, regime_gecmisi, hedef_rejim):
    """`hedef_rejim` etiketiyle isaretli kontrol noktalarinda GECERLI olan
    (bir sonraki kontrole kadar suren) baris boyunca ortalama SHIB payi."""
    n = len(shib_pct_gecmisi)
    degerler = []
    for k, (idx, _ts, regime, _score, _target) in enumerate(regime_gecmisi):
        if regime != hedef_rejim:
            continue
        bitis = regime_gecmisi[k + 1][0] if k + 1 < len(regime_gecmisi) else n
        pencere = shib_pct_gecmisi[idx:bitis]
        degerler.extend(pencere)
    return (sum(degerler) / len(degerler)) if degerler else None


def _trend_metrics(zamanlar, kapanislar, shib_pct_gecmisi, regime_gecmisi):
    """Kar-disi metrikler - TANIMLAR (birbirinden FARKLI olcumler, kasitli
    olarak matematiksel olarak birebir ortusmezler):

      - Trend Yakalama Orani (%): her buyuk-hareket OLAYININ SURESI boyunca
        dogru tarafta gecirilen zaman ORANI, sonra TUM olaylar uzerinden
        ORTALAMASI. SUREKLI bir olcu (0-100 arasi herhangi bir deger
        olabilir), "ne kadar iyi yakaladik" sorusuna DERECELI cevap verir.
      - Dogru Rejimle Yakalanan / Kacirilan Guclu Trend (sayi, sayi): AYNI
        capture_frac degerini bu kez ESIK'e (>=  %50 mi degil mi) gore
        IKIYE AYIRIR - her olay ya "yakalandi" ya "kacirildi" sayilir, bu
        yuzden TOPLAMLARI HER ZAMAN buyuk_hareket_sayisi'na esittir. Bu
        SAYI/ORAN, yukaridaki SUREKLI ortalamayla AYNI SAYI OLMAZ (orn.
        %48 ortalama capture ile olaylarin yarisi >=50% diger yarisi <50%
        olabilir) - biri "ortalama not", digeri "gecme orani" gibi
        dusunulmeli, ikisi de dogru ama farkli sorulara cevap verir.
      - Kacirilan Yukselis / Onlenemeyen Dusus: yukaridaki "kacirilan"
        sayisinin YON'e gore ikiye bolunmus hali (yukselis kacirildi mi,
        dususte fazla maruz mu kalindi).
      - Trend Tespit Gecikmesi (saat): hareket basladiktan kac saat sonra
        motor DOGRU yonlu rejime (YUKSELIS/GUCLU_YUKSELIS veya DUSUS/
        GUCLU_DUSUS) gecti (ortalama).
      - Yanlis Pozitif Orani (%): GUCLU_ sinyali verilen kontrol
        noktalarinin, o sinyal penceresinde gercek bir buyuk hareketle
        ORTUSMEYEN yuzdesi.
    NOT: kontrol-bazli yaklasik bir olcumdur (bir olay boyunca surekli ayni
    GUCLU_ etiketi kalirsa, yanlis-pozitif sayaci icin birden fazla kez
    sayilir) - tek donem icin egilim gostermesi amaclanir, kesin bir bilim
    degildir."""
    events = _big_move_events(zamanlar, kapanislar, BIG_MOVE_THRESHOLD_PERCENT, BIG_MOVE_WINDOW_HOURS)
    if not events:
        guclu_dusus_pct = _rejim_bazli_ort_pct(shib_pct_gecmisi, regime_gecmisi, "GUCLU_DUSUS")
        return {
            "trend_yakalama_orani": None, "kacirilan_guclu_trend": 0,
            "kacirilan_yukselis": 0, "onlenemeyen_dusus": 0,
            "tespit_gecikmesi_saat": None, "yanlis_pozitif_orani": None,
            "buyuk_hareket_sayisi": 0, "dogru_rejimle_yakalanan": 0,
            "guclu_sinyal_sayisi": len(
                [r for r in regime_gecmisi if r[2] in ("GUCLU_YUKSELIS", "GUCLU_DUSUS")]
            ),
            "ort_shib_pct_guclu_yukselis": _rejim_bazli_ort_pct(shib_pct_gecmisi, regime_gecmisi, "GUCLU_YUKSELIS"),
            "ort_usdt_pct_guclu_dusus": (100 - guclu_dusus_pct) if guclu_dusus_pct is not None else None,
        }

    capture_fracs = []
    lags = []
    kacirilan_guclu = 0
    kacirilan_yukselis = 0
    onlenemeyen_dusus = 0
    dogru_rejimle_yakalanan = 0  # capture_frac >= %50: hareketin COGUNLUGUNDE dogru tarafta
    for start, end, yon, _degisim in events:
        pencere = shib_pct_gecmisi[start:end + 1]
        dogru_taraf = [(p > 50) if yon == "YUKARI" else (p < 50) for p in pencere]
        capture_frac = sum(dogru_taraf) / len(dogru_taraf) if dogru_taraf else 0.0
        capture_fracs.append(capture_frac)
        if capture_frac >= 0.5:
            dogru_rejimle_yakalanan += 1
        else:
            # "Dogru Rejimle Yakalanan"in TAMAMLAYICISI - toplamlari her
            # zaman buyuk_hareket_sayisi'na esit olsun diye AYNI esik (%50)
            # kullanilir (onceki surumde farkli esik + ekstra kosul vardi,
            # bu da iki sayinin toplamini olaylardan az gosteriyordu).
            kacirilan_guclu += 1
            if yon == "YUKARI":
                kacirilan_yukselis += 1
            else:
                onlenemeyen_dusus += 1

        hedef_regimeler = {"YUKSELIS", "GUCLU_YUKSELIS"} if yon == "YUKARI" else {"DUSUS", "GUCLU_DUSUS"}
        lag_saat = None
        for idx, ts, regime, _score, _target in regime_gecmisi:
            if idx < start:
                continue
            if regime in hedef_regimeler:
                lag_saat = (ts - zamanlar[start]) / 3_600_000
                break
        if lag_saat is not None:
            lags.append(lag_saat)

    guclu_sinyaller = [r for r in regime_gecmisi if r[2] in ("GUCLU_YUKSELIS", "GUCLU_DUSUS")]
    yanlis_pozitif = 0
    for idx, _ts, regime, _score, _target in guclu_sinyaller:
        beklenen_yon = "YUKARI" if regime == "GUCLU_YUKSELIS" else "ASAGI"
        eslesen = any(start <= idx <= end and yon == beklenen_yon for start, end, yon, _d in events)
        if not eslesen:
            yanlis_pozitif += 1
    fp_orani = (yanlis_pozitif / len(guclu_sinyaller) * 100) if guclu_sinyaller else None

    return {
        "trend_yakalama_orani": sum(capture_fracs) / len(capture_fracs) * 100,
        "kacirilan_guclu_trend": kacirilan_guclu,
        "kacirilan_yukselis": kacirilan_yukselis,
        "onlenemeyen_dusus": onlenemeyen_dusus,
        "tespit_gecikmesi_saat": (sum(lags) / len(lags)) if lags else None,
        "yanlis_pozitif_orani": fp_orani,
        "buyuk_hareket_sayisi": len(events),
        "guclu_sinyal_sayisi": len(guclu_sinyaller),
        "dogru_rejimle_yakalanan": dogru_rejimle_yakalanan,
        "ort_shib_pct_guclu_yukselis": _rejim_bazli_ort_pct(shib_pct_gecmisi, regime_gecmisi, "GUCLU_YUKSELIS"),
        "ort_usdt_pct_guclu_dusus": (
            100 - _rejim_bazli_ort_pct(shib_pct_gecmisi, regime_gecmisi, "GUCLU_DUSUS")
            if _rejim_bazli_ort_pct(shib_pct_gecmisi, regime_gecmisi, "GUCLU_DUSUS") is not None else None
        ),
    }


def _ozet_yazdir(etiket, sonuc, sonuc_brut, zamanlar, kapanislar):
    equity_egrisi = sonuc["equity_egrisi"]
    coin_egrisi = sonuc["coin_egrisi"]
    baslangic_coin = sonuc["baslangic_coin"]
    trades = sonuc["trades"]

    toplam_deger = equity_egrisi[-1] if equity_egrisi else BACKTEST_START_CAPITAL
    getiri_yuzde = (toplam_deger - BACKTEST_START_CAPITAL) / BACKTEST_START_CAPITAL * 100
    brut_deger = sonuc_brut["equity_egrisi"][-1] if sonuc_brut["equity_egrisi"] else BACKTEST_START_CAPITAL
    brut_getiri_yuzde = (brut_deger - BACKTEST_START_CAPITAL) / BACKTEST_START_CAPITAL * 100
    bitis_coin = coin_egrisi[-1] if coin_egrisi else baslangic_coin
    token_degisim = (bitis_coin - baslangic_coin) / baslangic_coin * 100 if baslangic_coin else 0
    max_dusus = _max_drawdown(equity_egrisi)
    metrikler = _trend_metrics(zamanlar, kapanislar, sonuc["shib_pct_gecmisi"], sonuc["regime_gecmisi"])

    print(f"\n--- {etiket} ---")
    print(f"Baslangic sermaye       : {BACKTEST_START_CAPITAL:,.2f} USDT")
    print(f"Bitis degeri (NET)      : {toplam_deger:,.2f} USDT  (NET getiri {getiri_yuzde:+.2f}%, "
          f"BRUT getiri {brut_getiri_yuzde:+.2f}%, komisyon+kayma maliyeti {brut_getiri_yuzde - getiri_yuzde:.2f} puan)")
    print(f"TOKEN ADEDI DEGISIMI    : {token_degisim:+.2f}%")
    print(f"En buyuk gerileme (DD)  : {max_dusus:.2f}%")
    print(f"Toplam islem            : {len(trades)}")
    yakalama = metrikler["trend_yakalama_orani"]
    print(f"Trend Yakalama Orani    : {yakalama:.1f}%" if yakalama is not None else "Trend Yakalama Orani    : (buyuk hareket yok)")
    print(f"Kacirilan Guclu Trend   : {metrikler['kacirilan_guclu_trend']} / {metrikler['buyuk_hareket_sayisi']} buyuk hareket "
          f"(kacirilan yukselis: {metrikler['kacirilan_yukselis']}, onlenemeyen dusus: {metrikler['onlenemeyen_dusus']})")
    print(f"Dogru Rejimle Yakalanan : {metrikler['dogru_rejimle_yakalanan']} / {metrikler['buyuk_hareket_sayisi']} buyuk hareket "
          f"(>=%5 hareketin cogunlugunda dogru tarafta olunan - bu ikisi HER ZAMAN toplami buyuk_hareket_sayisina esittir)")
    gecikme = metrikler["tespit_gecikmesi_saat"]
    print(f"Trend Tespit Gecikmesi  : {gecikme:.1f} saat (ortalama)" if gecikme is not None else "Trend Tespit Gecikmesi  : (olcum yok)")
    fp = metrikler["yanlis_pozitif_orani"]
    print(f"Yanlis Pozitif Orani    : %{fp:.1f}  ({metrikler['guclu_sinyal_sayisi']} GUCLU_ sinyalinden)" if fp is not None else "Yanlis Pozitif Orani    : (GUCLU_ sinyali yok)")
    gy = metrikler["ort_shib_pct_guclu_yukselis"]
    print(f"Guclu YUKSELIS'te ort. SHIB payi : %{gy:.1f}" if gy is not None else "Guclu YUKSELIS'te ort. SHIB payi : (bu rejime hic girilmedi)")
    gd = metrikler["ort_usdt_pct_guclu_dusus"]
    print(f"Guclu DUSUS'te ort. USDT payi    : %{gd:.1f}" if gd is not None else "Guclu DUSUS'te ort. USDT payi    : (bu rejime hic girilmedi)")
    return {
        "getiri": getiri_yuzde, "brut_getiri": brut_getiri_yuzde, "token_degisim": token_degisim,
        "max_dusus": max_dusus, "islem": len(trades), **metrikler,
    }


def run_for_days(days):
    sonuclar = {}
    for detector in te.DETECTORS:
        shib_series, majors_series, zamanlar, kapanislar = _fetch_all(days, detector)
        sonuc = simulate(shib_series, majors_series, zamanlar, kapanislar)
        sonuc_brut = simulate(shib_series, majors_series, zamanlar, kapanislar, cost_percent=0)
        sonuclar[detector] = (sonuc, sonuc_brut, zamanlar, kapanislar)

    print(f"\n{'=' * 62}\nTREND INTELLIGENCE ENGINE BACKTEST: {SYMBOL} - son {days} gun ({BASE_INTERVAL} saat)")
    print(f"Rebalance: her {REBALANCE_INTERVAL_MINUTES} dk, max adim %{MAX_STEP_PERCENT}, min esik %{MIN_REBALANCE_DELTA_PERCENT}")
    print(f"Buyuk hareket tanimi: >=%{BIG_MOVE_THRESHOLD_PERCENT} / {BIG_MOVE_WINDOW_HOURS:.0f} saat")
    print("=" * 62)

    ozetler = {}
    for detector, (sonuc, sonuc_brut, zamanlar, kapanislar) in sonuclar.items():
        ozetler[detector] = _ozet_yazdir(f"DEDEKTOR: {detector}", sonuc, sonuc_brut, zamanlar, kapanislar)

    print(f"\n--- {days} GUN KARSILASTIRMA (token birikimine gore, A-D kriteri birlikte) ---")
    siralama = sorted(ozetler.items(), key=lambda kv: kv[1]["token_degisim"], reverse=True)
    for detector, ozet in siralama:
        print(f"  {detector:<11} token {ozet['token_degisim']:+.2f}%  DD {ozet['max_dusus']:.2f}%  "
              f"yakalama {'%.1f%%' % ozet['trend_yakalama_orani'] if ozet['trend_yakalama_orani'] is not None else 'n/a'}  "
              f"gecikme {'%.1fsa' % ozet['tespit_gecikmesi_saat'] if ozet['tespit_gecikmesi_saat'] is not None else 'n/a'}  "
              f"yanlisPoz {'%%%.1f' % ozet['yanlis_pozitif_orani'] if ozet['yanlis_pozitif_orani'] is not None else 'n/a'}")
    print(f"En iyi (sadece token birikimi - A/B/D'yi ayrica yukarida karsilastirin): {siralama[0][0]}")
    print("(Not: gecmis performans gelecegi garanti etmez, tek donem yeterli kanit degildir.)")
    return ozetler


def main():
    run_for_days(BACKTEST_DAYS)


if __name__ == "__main__":
    main()
