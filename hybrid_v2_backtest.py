"""
HYBRID v2 (EARLY-ENTRY) - DENEYSEL backtest, hybrid_v1'in causal diagnostic
sonucuna (hybrid_causal_diagnostics.csv, 180 gunluk gercek veri) dogrudan
cevap vermek icin.

BU DOSYA v1'E DOKUNMAZ: hybrid_engine.py, portfolio_manager.py,
hybrid_backtest.py, grid_backtest.py, trend_engine*.py DEGISMEDEN,
OLDUKLARI GIBI import edilip tekrar kullanilir. "HYBRID v1" burada
raporlanan satir, hybrid_backtest.py'yi dogrudan calistirdiginizda
goreceginiz sayilarin AYNISIDIR (hb.simulate() BIREBIR ayni cagriyla
calisir) - v1 hicbir sekilde degistirilmedi.

NEDEN BU DOSYA VAR (teshis -> mimari degisiklik):
  180 gunluk causal diagnostic (hybrid_causal_diagnostics.csv) matematiksel
  olarak gosterdi ki:
    - 22 buyuk yukselisin 21'i (%95) 24 saat sonra bile TAMAMEN KACIRILMIS
      (SHIB payi hala %50 altinda).
    - SCORE_THRESHOLD (temel agirlikli-cok-zaman-dilimli + ADX skorunun
      kendisi): median 25.0sa, 18/22 olayda gec kaliyor, 10/21 tamamen
      kacirilan olayda TEK BASINA en buyuk gecikme nedeni.
    - MAJOR_CONFIRMATION: median 50.5sa (nadir ama agir: sadece 4/22 olay).
    - EMA/SUPERTREND: median ~20sa. KAMA/NW: ~9-10sa. WAVETREND: ~5sa.
    - Saf HYSTERESIS sorunu DEGIL: reset_count=0 olaylarda hysteresis_extra
      ~0.82sa (beklenenle tutarli) - buyuk gecikmeler (56sa+) ham skorun
      esik civarinda salinip sayaci sifirlamasindan geliyor, mekanizmanin
      kendisinden degil.

  Yani asil sorun: TAM ONAY (KAMA+SuperTrend+Majors+ADX-tabanli guc +
  1d/4h agirlikli MTF ortalamasi hepsi ayni anda hizalanana kadar)
  BEKLEMEK, hizli (%5/24sa) hareketler icin fazla YAVAS. Kullanicinin
  istedigi mimari degisiklik: "tam onaydan ONCE, SINIRLI bir erken pozisyon
  alabilen bir katman ekle; KAMA/SuperTrend/Majors onayı ORTADAN
  KALDIRILMAZ, sadece 'once GEC ONAY sonra POZISYON' sirasini 'once KUCUK
  POZISYON sonra onayla BUYUT' sirasina cevirir."

MIMARI (v1'in dual-sleeve yapisi AYNEN korunur - bkz. hybrid_backtest.py
docstring'i):

  GRID SLEEVE (hb.HYBRID_GRID_SLEEVE_PERCENT) - DEGISMEDI, hb._grid_sleeve_
  step() DOGRUDAN import edilip cagrilir (kod tekrari YOK).

  TREND SLEEVE - iki katmanli karar zinciri:
    1) EARLY ENTRY LAYER (_early_signal): SADECE hizli/bagimsiz gostergeler
       - kisa/orta TF (15m+30m) EMA(20/50) yonu, WaveTrend egimi,
       Nadaraya-Watson egimi. KAMA / SuperTrend / BTC-ETH-BNB / ADX-tabanli
       guc-carpani BILEREK DAHIL EDILMEZ - causal diagnostic'e gore
       gecikmenin asil kaynagi bunlarin TAM HIZALANMASINI beklemekti.
       Sadece REJIM hala v1'in tam-onay motoruna gore YATAY iken devreye
       girer (v1 zaten YUKSELIS/DUSUS/... derse onun sozune guvenilir,
       early layer'a gerek yoktur).
    2) CONFIRMATION / SCALE-IN LAYER: v1'in kendi hybrid_engine.classify()
       + hysteresis'i (hb.simulate() cagrilarak GERCEK v1 confirmed-rejim
       akisindan okunur - v2 kendi hysteresis mantigini YENIDEN YAZMAZ).
       KAMA/SuperTrend/Majors burada AYNI ROLDE kalir: erken pozisyonu
       BUYUTMEK / guclu trendde daha yuksek SHIB'e gecmek icin "zorunlu
       kapi" degil "buyutme onayi".
    3) REGIME TARGET ALLOCATION: v1'in tek YATAY/YUKSELIS/GUCLU_YUKSELIS/
       DUSUS/GUCLU_DUSUS/TOPARLANMA/DAGITIM tablosuna iki ARA kademe
       eklenir: ERKEN_YUKSELIS / ERKEN_DUSUS (bkz. V2_REGIME_RANGES).
       Kademeler arasi gecis GENE pm.smooth_target() ile (hb.
       HYBRID_MAX_STEP_PERCENT, v1 ile AYNI) - ani %0->%100 sicramasi YOK.

  NOT (kasitli, ONEMLI): V2_REGIME_RANGES sayilari ("bu asamada 25-40,
  85-100 gibi") kucuk, MANTIKLI bir aday kume - kullanicinin acik istegi
  geregi TEK TEK curve-fit / kor optimizasyon YAPILMADI. Ileride
  "30 yerine 35 dene" gibi bir backtest-sweep istenirse AYRI bir adim
  olarak yapilmali.

LOOKAHEAD BIAS: hicbir yeni mekanizma eklenmedi - early layer da v1 ile
AYNI TimeframeSeries/HybridSeries.index_at() (close_time = open_time +
interval ile "bilinir" olma) kuralina tabi, sadece DAHA HIZLI zaman
dilimlerinde (15m/30m) calisiyor. NW causal (bkz. hybrid_engine.py
docstring'i, degismedi). Karar ani her zaman bar KAPANIS ani
(zamanlar[i] + interval), v1 ile birebir ayni desen.

Kullanim:
    python3 hybrid_v2_backtest.py         (varsayilan 30,90,180 gun, otomatik)
    V2_BACKTEST_DAYS_LIST=180 python3 hybrid_v2_backtest.py

Cikti: konsolda GRID / HYBRID v1 / HYBRID v2 master tablosu (30/90/180),
capraz-pencere NET tablosu, olay-bazli hybrid_v2_events.csv, ve
"KARAR KRITERI" (kullanicinin madde 8) bolumu - PASS/FAIL degil, ham
sayilari yan yana koyup yorumu kullaniciya birakir.
"""

import csv
import os
import statistics
import time

from backtest import INTERVAL_MS
from trade_bot import _load_dotenv
import hybrid_engine as he
import hybrid_backtest as hb
import portfolio_manager as pm
import trend_engine_backtest as teb

_load_dotenv()

SYMBOL = hb.SYMBOL  # AYNI sembol, v1'den okunuyor (tekrar tanimlanmadi)

# --- EARLY ENTRY LAYER ayarlari (kucuk, mantikli sabit aday kume) ----------
V2_EARLY_TIMEFRAMES = [s.strip() for s in os.environ.get("V2_EARLY_TIMEFRAMES", "15m,30m").split(",") if s.strip()]
# 2 zaman dilimi x 3 gosterge (EMA + WaveTrend-egim + NW-egim) = en fazla 6 oy.
# >=3 net oy = "hizli sinyallerin COGUNLUGU ayni yonde" (gurultu filtresi).
V2_EARLY_MIN_NET_VOTE = int(os.environ.get("V2_EARLY_MIN_NET_VOTE", "3"))

# --- REGIME TARGET ALLOCATION (v1'in HYBRID_REGIME_RANGES'inin genisletilmisi) ---
# BULLISH-STIL (alt=dusuk-guven ucu, ust=yuksek-guven ucu): guven/guc ARTTIKCA
# hedef UST'e yaklasir.
# BEARISH-STIL (alt=EN savunmaci, ust=EN AZ savunmaci): guven/guc ARTTIKCA
# hedef ALT'a yaklasir (v1'in DUSUS/GUCLU_DUSUS tablosunun tersi yonde
# calisan versiyonu - kullanicinin acik istegi: "bearish: azalt, strong
# bearish: USDT agirligini belirgin artir").
V2_REGIME_RANGES = {
    "GUCLU_YUKSELIS": (85.0, 100.0),
    "YUKSELIS": (55.0, 80.0),
    "ERKEN_YUKSELIS": (25.0, 40.0),
    "DAGITIM": (30.0, 50.0),
    "ERKEN_DUSUS": (15.0, 30.0),
    "DUSUS": (10.0, 30.0),
    "GUCLU_DUSUS": (0.0, 10.0),
}
_BEARISH_STYLE = {"ERKEN_DUSUS", "DUSUS", "GUCLU_DUSUS"}

V2_BACKTEST_DAYS_LIST = [int(g.strip()) for g in os.environ.get("V2_BACKTEST_DAYS_LIST", "30,90,180").split(",") if g.strip()]


def _nw_slope_vote(ts, idx, lookback=3):
    if ts.nw is None or idx is None or idx < lookback:
        return None
    a, b = ts.nw[idx], ts.nw[idx - lookback]
    if a is None or b is None:
        return None
    return 1 if a > b else (-1 if a < b else 0)


def _wt_slope_vote(ts, idx, lookback=2):
    if ts.wt1 is None or idx is None or idx < lookback:
        return None
    a, b = ts.wt1[idx], ts.wt1[idx - lookback]
    if a is None or b is None:
        return None
    return 1 if a > b else (-1 if a < b else 0)


def _fetch_early_series(days):
    """15m/30m icin NW+WT DAHIL HybridSeries. hb._fetch_hybrid_series()
    DOGRUDAN cagrilir (yeniden yazilmadi) - ayni sembol/interval/days
    icin backtest.py'nin _KLINES_CACHE'i zaten dolu oldugundan (hb.
    _fetch_hybrid_all zaten 15m/30m'yi indirmis olur) BU EKSTRA AGA ISTEGI
    YARATMAZ, sadece NW/WT hesaplamasini (ucuz, O(n)) tekrarlar."""
    return {tf: hb._fetch_hybrid_series(SYMBOL, tf, days, compute_nw=True, compute_wt=True)
            for tf in V2_EARLY_TIMEFRAMES}


def _early_signal(early_series, timestamp_ms):
    """KAMA / SuperTrend / Majors / ADX-guc-carpani BILEREK KULLANILMAZ -
    causal diagnostic'e gore gecikmenin kaynagi tam da bunlarin hizalanmasini
    beklemekti. Sadece hizli/bagimsiz 3 sinyal (EMA yonu, WT egimi, NW
    egimi) x 2 hizli zaman dilimi -> net oy >= esik ise yon dondurur."""
    oylar = []
    for ts in early_series.values():
        idx = ts.index_at(timestamp_ms)
        if idx is None:
            continue
        for oy in (ts.ema_vote(idx), _wt_slope_vote(ts, idx), _nw_slope_vote(ts, idx)):
            if oy is not None:
                oylar.append(oy)
    if not oylar:
        return 0, 0.0
    net = sum(oylar)
    guc = abs(net) / len(oylar)
    if net >= V2_EARLY_MIN_NET_VOTE:
        return 1, guc
    if net <= -V2_EARLY_MIN_NET_VOTE:
        return -1, guc
    return 0, guc


def _v2_target_allocation(etkin_regime, guc_0_100, prev_target):
    if etkin_regime in (None, "YATAY"):
        return None
    if etkin_regime == "TOPARLANMA":
        return min(100.0, prev_target + pm.TOPARLANMA_GIRIS_ADIMI_PERCENT)
    aralik = V2_REGIME_RANGES.get(etkin_regime)
    if aralik is None:
        return None
    alt, ust = aralik
    oran = max(0.0, min(1.0, guc_0_100 / 100))
    if etkin_regime in _BEARISH_STYLE:
        return ust - (ust - alt) * oran
    return alt + (ust - alt) * oran


def _trend_sleeve_rebalance(trend_usdt, trend_coin, target, fiyat, cost_pct, trades, tarih, min_delta):
    """hb.simulate()'in trend-sleeve rebalance blogunun AYNI formulleri -
    hb.py DEGISTIRILEMEDIGI icin (v1'e dokunma kurali) burada YENIDEN
    yazildi, ama mantik BIREBIR ayni (hesap hatasi riskini azaltmak icin
    hb.simulate() ile satir satir karsilastirilarak yazildi)."""
    deger = trend_usdt + trend_coin * fiyat
    simdiki_pct = (trend_coin * fiyat / deger * 100) if deger > 0 else 0.0
    fark = target - simdiki_pct
    if abs(fark) < min_delta or deger <= 0:
        return trend_usdt, trend_coin
    hedef_deger = deger * (target / 100)
    simdiki_deger = trend_coin * fiyat
    delta_deger = hedef_deger - simdiki_deger
    if delta_deger > 0:
        harcanacak = min(delta_deger, trend_usdt)
        if harcanacak > 0:
            maliyet = harcanacak * cost_pct / 100
            trend_usdt -= harcanacak
            trend_coin += (harcanacak - maliyet) / fiyat
            trades.append({"tip": "TREND_SLEEVE_AL", "tarih": tarih, "fiyat": fiyat})
    else:
        satilacak = min(trend_coin, -delta_deger / fiyat)
        if satilacak > 0:
            net = satilacak * fiyat * (1 - cost_pct / 100)
            trend_usdt += net
            trend_coin -= satilacak
            trades.append({"tip": "TREND_SLEEVE_SAT", "tarih": tarih, "fiyat": fiyat})
    return trend_usdt, trend_coin


def simulate_v2(shib_series, majors_series, early_series, zamanlar, kapanislar, v1_hysteresis_gecmisi, cost_percent=None):
    cost_pct = (hb.TRADING_FEE_PERCENT + hb.SLIPPAGE_PERCENT) if cost_percent is None else cost_percent
    grid_deger = hb.BACKTEST_START_CAPITAL * (hb.HYBRID_GRID_SLEEVE_PERCENT / 100)
    trend_deger = hb.BACKTEST_START_CAPITAL - grid_deger

    if hb.START_IN_SHIB:
        ilk_qty = grid_deger / kapanislar[0]
        grid_state = {"usdt": 0.0, "coin": ilk_qty,
                      "open_lots": [{"qty": ilk_qty, "entry_price": kapanislar[0]}],
                      "reference_price": kapanislar[0], "baslangic_deger": grid_deger}
        trend_usdt, trend_coin = 0.0, trend_deger / kapanislar[0]
        trend_target = 100.0
    else:
        grid_state = {"usdt": grid_deger, "coin": 0.0, "open_lots": [],
                      "reference_price": kapanislar[0], "baslangic_deger": grid_deger}
        trend_usdt, trend_coin = trend_deger, 0.0
        trend_target = 0.0

    baslangic_coin_esdeger = (grid_state["coin"] + trend_coin) + (grid_state["usdt"] + trend_usdt) / kapanislar[0]

    base_interval_ms = INTERVAL_MS["15m"]
    steps_per_rebalance = max(1, round(hb.HYBRID_REBALANCE_MINUTES * 60_000 / base_interval_ms))
    v1_by_idx = {t["idx"]: t for t in v1_hysteresis_gecmisi}

    trades, equity_egrisi, coin_egrisi, shib_pct_gecmisi = [], [], [], []
    regime_gecmisi, early_gecmisi = [], []

    for i, fiyat in enumerate(kapanislar):
        tarih = time.strftime("%Y-%m-%d %H:%M", time.localtime(zamanlar[i] / 1000))
        karar_zamani = zamanlar[i] + base_interval_ms

        hb._grid_sleeve_step(grid_state, fiyat, tarih, trades, cost_pct)

        if i % steps_per_rebalance == 0:
            v1_tick = v1_by_idx.get(i)
            confirmed_regime = v1_tick["confirmed_regime"] if v1_tick else "YATAY"
            confirmed_score = v1_tick["confirmed_score"] if v1_tick else 0.0
            early_dir, early_guc = _early_signal(early_series, karar_zamani)

            etkin_regime = confirmed_regime
            if confirmed_regime == "YATAY":
                if early_dir == 1:
                    etkin_regime = "ERKEN_YUKSELIS"
                elif early_dir == -1:
                    etkin_regime = "ERKEN_DUSUS"

            guc = (early_guc * 100) if etkin_regime in ("ERKEN_YUKSELIS", "ERKEN_DUSUS") else abs(confirmed_score)
            hedef_raw = _v2_target_allocation(etkin_regime, guc, trend_target)
            if hedef_raw is not None:
                trend_target = (hedef_raw if etkin_regime == "TOPARLANMA"
                                 else pm.smooth_target(trend_target, hedef_raw, hb.HYBRID_MAX_STEP_PERCENT))

            trend_usdt, trend_coin = _trend_sleeve_rebalance(
                trend_usdt, trend_coin, trend_target, fiyat, cost_pct, trades, tarih, hb.HYBRID_MIN_REBALANCE_DELTA)

            regime_gecmisi.append((i, karar_zamani, confirmed_regime, confirmed_score, trend_target))
            early_gecmisi.append({"idx": i, "ts_ms": karar_zamani, "early_dir": early_dir,
                                   "early_guc": round(early_guc, 3), "etkin_regime": etkin_regime,
                                   "trend_target": round(trend_target, 2)})

        toplam_usdt = grid_state["usdt"] + trend_usdt
        toplam_coin = grid_state["coin"] + trend_coin
        toplam_deger = toplam_usdt + toplam_coin * fiyat
        actual_pct = (toplam_coin * fiyat / toplam_deger * 100) if toplam_deger > 0 else 0.0
        equity_egrisi.append(toplam_deger)
        coin_egrisi.append(toplam_coin + toplam_usdt / fiyat)
        shib_pct_gecmisi.append(actual_pct)

    return {
        "trades": trades, "equity_egrisi": equity_egrisi, "coin_egrisi": coin_egrisi,
        "baslangic_coin": baslangic_coin_esdeger, "shib_pct_gecmisi": shib_pct_gecmisi,
        "regime_gecmisi": regime_gecmisi, "early_gecmisi": early_gecmisi,
    }


def _fmt(ts_ms):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts_ms / 1000)) if ts_ms is not None else None


def _missed_strong_up_count(shib_pct_gecmisi, events):
    """24 saat sonra SHIB payi hala %50 altinda kalan buyuk YUKSELIS sayisi -
    hybrid_diagnostic.py'deki 'fully_missed' ile AYNI tanim, v1 VE v2 icin
    ayni fonksiyonla hesaplanir (adil karsilastirma)."""
    kacirilan, toplam = 0, 0
    for start_idx, _end_idx, yon, _d in events:
        if yon != "YUKARI":
            continue
        toplam += 1
        j = start_idx + 96  # 15dk adimla 24 saat = 96 adim
        pct24 = shib_pct_gecmisi[j] if j < len(shib_pct_gecmisi) else shib_pct_gecmisi[-1]
        if pct24 < 50:
            kacirilan += 1
    return kacirilan, toplam


def _early_false_positive_rate(early_gecmisi, zamanlar, events, pencere_saat=24):
    """Erken katman ('ERKEN_YUKSELIS'/'ERKEN_DUSUS') kac kez ates etti VE
    o anin +-pencere_saat civarinda GERCEK bir buyuk hareket YOKTU (yanlis
    alarm). v1'de bu kavram yok (early layer v1'de yok) - SADECE v2'ye
    ozel, "erken katman gereksiz yere cok mu al-sat yapiyor" sorusuna
    cevap."""
    ates_edenler = [e for e in early_gecmisi if e["etkin_regime"] in ("ERKEN_YUKSELIS", "ERKEN_DUSUS")]
    if not ates_edenler:
        return None, 0
    pencere_ms = pencere_saat * 3_600_000
    yanlis = 0
    for e in ates_edenler:
        beklenen_yon = "YUKARI" if e["etkin_regime"] == "ERKEN_YUKSELIS" else "ASAGI"
        eslesen = any(
            zamanlar[start] - pencere_ms <= e["ts_ms"] <= zamanlar[end] + pencere_ms and yon == beklenen_yon
            for start, end, yon, _d in events
        )
        if not eslesen:
            yanlis += 1
    return yanlis / len(ates_edenler) * 100, len(ates_edenler)


def _event_report(gun, zamanlar, regime_gecmisi, early_gecmisi, shib_pct_gecmisi, events):
    satirlar = []
    for i, (start_idx, end_idx, yon, degisim) in enumerate(events):
        event_start_ms = zamanlar[start_idx]
        hedef_dogru = {"YUKSELIS", "GUCLU_YUKSELIS"} if yon == "YUKARI" else {"DUSUS", "GUCLU_DUSUS"}
        hedef_guclu = {"GUCLU_YUKSELIS"} if yon == "YUKARI" else {"GUCLU_DUSUS"}
        hedef_early = 1 if yon == "YUKARI" else -1

        early_ms = next((e["ts_ms"] for e in early_gecmisi if e["ts_ms"] >= event_start_ms and e["early_dir"] == hedef_early), None)
        confirmed_ms = next((r[1] for r in regime_gecmisi if r[1] >= event_start_ms and r[2] in hedef_dogru), None)
        strong_ms = next((r[1] for r in regime_gecmisi if r[1] >= event_start_ms and r[2] in hedef_guclu), None)

        def pct_at(offset_h):
            j = start_idx + int(offset_h * 4)
            return shib_pct_gecmisi[j] if 0 <= j < len(shib_pct_gecmisi) else None

        pencere24 = shib_pct_gecmisi[start_idx:min(start_idx + 96, len(shib_pct_gecmisi))]
        max_alloc_24h = (max(pencere24) if yon == "YUKARI" else min(pencere24)) if pencere24 else None
        pct24 = pct_at(24)
        caught = None
        if pct24 is not None:
            caught = "CAUGHT" if ((pct24 >= 50) if yon == "YUKARI" else (pct24 <= 50)) else "MISSED"

        satirlar.append({
            "window_days": gun, "event_id": i, "event_start": _fmt(event_start_ms),
            "direction": yon, "move_pct": round(degisim, 2),
            "early_entry_time": _fmt(early_ms), "confirmed_entry_time": _fmt(confirmed_ms),
            "strong_entry_time": _fmt(strong_ms),
            "early_delay_h": round((early_ms - event_start_ms) / 3_600_000, 1) if early_ms is not None else None,
            "confirmed_delay_h": round((confirmed_ms - event_start_ms) / 3_600_000, 1) if confirmed_ms is not None else None,
            "allocation_at_event_start": pct_at(0), "allocation_6h": pct_at(6),
            "allocation_12h": pct_at(12), "allocation_24h": pct24,
            "max_allocation_24h": max_alloc_24h, "caught_or_missed": caught,
        })
    return satirlar


def run_for_days(days, tum_event_satirlari):
    print(f"\n{'#' * 70}\nHYBRID v2 (EARLY-ENTRY) DENEY BACKTEST: {SYMBOL} - son {days} gun\n{'#' * 70}")

    # --- GRID baseline: hb._grid_only_ozet ile BIREBIR ayni (reuse) ---
    grid_row = hb._grid_only_ozet(days)

    # --- Veri: hb._fetch_hybrid_all ile BIREBIR ayni cagriyla (reuse) ---
    shib_series, majors_series, zamanlar, kapanislar = hb._fetch_hybrid_all(days)
    early_series = _fetch_early_series(days)

    # --- HYBRID v1: hb.simulate() DOGRUDAN, DEGISTIRILMEDEN cagrilir ---
    v1_sonuc = hb.simulate(shib_series, majors_series, zamanlar, kapanislar)
    v1_sonuc_brut = hb.simulate(shib_series, majors_series, zamanlar, kapanislar, cost_percent=0)
    v1_row = hb._ozet("HYBRID v1", v1_sonuc, v1_sonuc_brut, zamanlar, kapanislar)

    # --- HYBRID v2: v1'in GERCEK confirmed-rejim akisi + early-entry katmani ---
    v2_sonuc = simulate_v2(shib_series, majors_series, early_series, zamanlar, kapanislar, v1_sonuc["hysteresis_gecmisi"])
    v2_sonuc_brut = simulate_v2(shib_series, majors_series, early_series, zamanlar, kapanislar,
                                 v1_sonuc["hysteresis_gecmisi"], cost_percent=0)
    v2_row = hb._ozet("HYBRID v2 (Early-Entry)", v2_sonuc, v2_sonuc_brut, zamanlar, kapanislar)

    satirlar = [grid_row, v1_row, v2_row]
    hb._master_tablo(satirlar)

    events = teb._big_move_events(zamanlar, kapanislar, hb.BIG_MOVE_THRESHOLD_PERCENT, hb.BIG_MOVE_WINDOW_HOURS)

    v1_kacirilan, toplam_yukselis = _missed_strong_up_count(v1_sonuc["shib_pct_gecmisi"], events)
    v2_kacirilan, _ = _missed_strong_up_count(v2_sonuc["shib_pct_gecmisi"], events)
    v2_fp_early, v2_early_ates = _early_false_positive_rate(v2_sonuc["early_gecmisi"], zamanlar, events)

    event_satirlari = _event_report(days, zamanlar, v2_sonuc["regime_gecmisi"], v2_sonuc["early_gecmisi"],
                                     v2_sonuc["shib_pct_gecmisi"], events)
    tum_event_satirlari.extend(event_satirlari)

    yukselis_gecikmeler = [s["early_delay_h"] for s in event_satirlari if s["direction"] == "YUKARI" and s["early_delay_h"] is not None]
    yukselis_confirmed_gecikmeler = [s["confirmed_delay_h"] for s in event_satirlari if s["direction"] == "YUKARI" and s["confirmed_delay_h"] is not None]

    print(f"\n--- {days} GUN: v1 vs v2 EK KARSILASTIRMA (kullanicinin madde 7/8 metrikleri) ---")
    print(f"24sa sonra hala kacirilan buyuk YUKSELIS : v1={v1_kacirilan}/{toplam_yukselis}   v2={v2_kacirilan}/{toplam_yukselis}")
    if yukselis_gecikmeler:
        print(f"v2 ERKEN sinyal gecikmesi (buyuk yukselis) : median {statistics.median(yukselis_gecikmeler):.1f}sa, "
              f"ortalama {statistics.mean(yukselis_gecikmeler):.1f}sa (n={len(yukselis_gecikmeler)})")
    if yukselis_confirmed_gecikmeler:
        print(f"CONFIRMED sinyal gecikmesi (v1=v2, degismedi) : median {statistics.median(yukselis_confirmed_gecikmeler):.1f}sa, "
              f"ortalama {statistics.mean(yukselis_confirmed_gecikmeler):.1f}sa (n={len(yukselis_confirmed_gecikmeler)})")
    if v2_fp_early is not None:
        print(f"v2 ERKEN katman yanlis-pozitif orani (yaklasik) : %{v2_fp_early:.1f} ({v2_early_ates} erken-tetiklemeden)")
    print(f"Islem sayisi: GRID={grid_row['islem']}  v1={v1_row['islem']}  v2={v2_row['islem']}  "
          f"(turnover artisi: v2/v1 = {v2_row['islem'] / v1_row['islem']:.2f}x)" if v1_row['islem'] else "")
    print("NOT: Trend Tespit Gecikmesi / Yanlis Pozitif Orani v1=v2 CIKAR (beklenen) - confirmation "
          "motoru (hybrid_engine.classify + hysteresis) DEGISTIRILMEDI; farkli olan SADECE erken katmanin "
          "sagladigi ON-POZISYONLAMA zamanlamasidir (yukaridaki 'v2 ERKEN sinyal gecikmesi' satirina bakin).")

    return {"GRID": grid_row, "HYBRID v1": v1_row, "HYBRID v2 (Early-Entry)": v2_row}


def main():
    tum_sonuclar = {}
    tum_event_satirlari = []
    for gun in V2_BACKTEST_DAYS_LIST:
        tum_sonuclar[gun] = run_for_days(gun, tum_event_satirlari)

    if tum_event_satirlari:
        with open("hybrid_v2_events.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(tum_event_satirlari[0].keys()))
            writer.writeheader()
            writer.writerows(tum_event_satirlari)
        print(f"\n[V2] Olay-bazli CSV: hybrid_v2_events.csv ({len(tum_event_satirlari)} satir, "
              f"{len(V2_BACKTEST_DAYS_LIST)} pencere birlikte)")

    if len(tum_sonuclar) > 1:
        print(f"\n{'#' * 70}\nCAPRAZ-PENCERE NET GETIRI TABLOSU (sistem x gun)\n{'#' * 70}")
        sistemler = ["GRID", "HYBRID v1", "HYBRID v2 (Early-Entry)"]
        baslik = f"{'SISTEM':<28}" + "".join(f"{str(g) + 'g NET%':>14}" for g in V2_BACKTEST_DAYS_LIST)
        print(baslik)
        print("-" * len(baslik))
        for isim in sistemler:
            satir = f"{isim:<28}"
            for gun in V2_BACKTEST_DAYS_LIST:
                v = tum_sonuclar[gun][isim]["net_getiri"]
                satir += f"{(f'{v:+.1f}' if v is not None else 'n/a'):>14}"
            print(satir)

        print(f"\n{'#' * 70}\nKARAR KRITERI (kullanicinin madde 8) - 180 GUN ODAKLI, PASS/FAIL YOK,\nHAM SAYILAR yan yana - yorum kullaniciya birakilir\n{'#' * 70}")
        son_gun = max(V2_BACKTEST_DAYS_LIST)
        g, v1, v2 = tum_sonuclar[son_gun]["GRID"], tum_sonuclar[son_gun]["HYBRID v1"], tum_sonuclar[son_gun]["HYBRID v2 (Early-Entry)"]
        print(f"(Kriterler {son_gun} gunluk pencereye gore; digerlerini yukaridaki tablodan da izleyin.)\n")
        print(f"1) v2 180g NET getiri v1'den belirgin iyi mi?      v1={v1['net_getiri']:+.2f}%   v2={v2['net_getiri']:+.2f}%   fark={v2['net_getiri'] - v1['net_getiri']:+.2f} puan")
        print(f"2) v2 riski GRID'e gore asiri bozulmus mu?          GRID_DD={g['max_dusus']:.2f}%   v1_DD={v1['max_dusus']:.2f}%   v2_DD={v2['max_dusus']:.2f}%")
        gy1, gy2 = v1.get("ort_shib_pct_guclu_yukselis"), v2.get("ort_shib_pct_guclu_yukselis")
        print(f"3) Guclu yukseliste ort. SHIB payi arttı mı?        v1={'%.1f%%' % gy1 if gy1 is not None else 'n/a'}   v2={'%.1f%%' % gy2 if gy2 is not None else 'n/a'}")
        print(f"4) Kacirilan (24sa) buyuk yukselis azaldi mi?       (yukaridaki '{son_gun} GUN: v1 vs v2 EK KARSILASTIRMA' satirina bakin)")
        print(f"5) Yanlis pozitif / turnover patlamis mi?           v1_islem={v1['islem']}   v2_islem={v2['islem']}   v1_FP={v1.get('yanlis_pozitif_orani')}   v2_FP={v2.get('yanlis_pozitif_orani')} (FP AYNI cikmasi beklenir - yukaridaki NOT'a bakin)")
        print("\n(Not: bu sadece HAM SAYI karsilastirmasi - 'belirgin iyi' esigi kasitli olarak koda gomulmedi;")
        print("kullanicinin kendi risk toleransina gore degerlendirmesi icin.)")


if __name__ == "__main__":
    main()
