"""
HYBRID v4 (EARLY SIGNAL QUALITY / ACTIVATION LAYER) - DENEYSEL, 3 aday.

BU DOSYA v1/v2/v3 dosyalarina DOKUNMAZ: hybrid_engine.py, portfolio_
manager.py, hybrid_backtest.py, hybrid_v2_backtest.py, hybrid_v3_backtest.py
DEGISMEDEN import edilip TEKRAR KULLANILIR. V4, V3-C'nin (persistence+
asymmetric-exit) DURUM MAKINESINI (hybrid_v3_backtest._v3_step, ayni
V3_REVERSAL_CONFIRM_TICKS/V3_NEUTRAL_DECAY_TICKS) OLDUGU GIBI kullanir -
SADECE bu duruma GIRIS (entry/activation) kosulunu degistirir.

NEDEN (hybrid_v4_early_signal_audit.py'nin bulgusu):
  V3-C'nin urettigi HER EARLY_BULLISH episode'u, giris anindaki HAM
  komponent oylariyla (EMA/WaveTrend/NW - entry'de zaten kullanilan;
  KAMA/SuperTrend/Majors - entry'de kullanilmayan ama "kalite sinyali"
  olarak okunan) birlikte, GERCEKTE ne oldugu (6/12/24 saatlik ileri
  getiri) ile etiketlenip 4 soruya cevap arandi: faydali/kucuk/basarisiz/
  gurultu episode'lari hangi komponent kombinasyonlari ayiriyor. Bu
  script o bulguya gore, YENI INDIKATOR EKLEMEDEN, mevcut EMA/WaveTrend/
  NW/(gerekirse HAM skor) uzerinden EN FAZLA 3 aciklanabilir "giris
  kalite filtresi" adayi dener - grid-search/threshold-optimize YAPILMAZ.

3 ADAY (kullanicinin literal tanimlari - esik optimize edilmedi):
  V4-A: EMA + WaveTrend ZORUNLU (ikisi de ayni yonde), NW DESTEK (guc'e
        katkida bulunur ama giris sartI degil).
  V4-B: EMA + NW ZORUNLU, WaveTrend DESTEK.
  V4-C: EMA/WaveTrend/NW'den EN AZ 2'si ayni yonde (basit cogunluk) VE
        HAM bilesik skor (he.classify() - degistirilmedi, sadece OKUNUR)
        zaten guclu TERS yonde DEGIL (yani "confirmed rejim strong
        bearish degil" kosulu, esik olarak MEVCUT HYBRID_ESIK_YON
        degeri - he.HYBRID_ESIK_YON - TEKRAR KULLANILDI, yeni sayi
        uydurulmadi).

  Ucu de v2b._early_signal()'in YERINE gecer - KAMA/SuperTrend/Majors
  entry KARARINA hala DAHIL EDILMEZ (audit'in bulgusuna gore bunlarin
  "gate" olarak degil sadece "kalite olcum" olarak bakildigi, literal
  aday tanimlarinda da yer almadigi icin) - sadece EMA/WT/NW'nin NASIL
  BIRLESTIRILDIGI degisiyor (v2/v3'un "6 oydan >=3 net" kuralindan,
  "hangi komponent ZORUNLU hangisi DESTEK" mantigina geciliyor).

DURUM MAKINESI (persistence/exit) DEGISMEDI - v3b._v3_step, v3b.
_yeni_latch_state, v3b._kapat_episode, v3b.V3_REVERSAL_CONFIRM_TICKS,
v3b.V3_NEUTRAL_DECAY_TICKS DOGRUDAN import edilip kullanilir. Allocation
tablosu (v2b.V2_REGIME_RANGES) DEGISTIRILMEDI - v2b._v2_target_allocation
DOGRUDAN cagrilir. Grid sleeve DEGISMEDI - hb._grid_sleeve_step DOGRUDAN
cagrilir.

Kullanim:
    python3 hybrid_v4_backtest.py           (varsayilan 30,90,180 gun)
    V4_BACKTEST_DAYS_LIST=180 python3 hybrid_v4_backtest.py
"""

import csv
import os
import statistics
import time

from backtest import INTERVAL_MS
from trade_bot import _load_dotenv
import hybrid_engine as he
import hybrid_backtest as hb
import hybrid_v2_backtest as v2b
import hybrid_v3_backtest as v3b
import hybrid_v4_early_signal_audit as v4audit
import portfolio_manager as pm
import trend_engine_backtest as teb

_load_dotenv()

SYMBOL = hb.SYMBOL

V4_CANDIDATES = ["A", "B", "C"]
V4_VARIANT_NAMES = {"A": "HYBRID v4-A (EMA+WT zorunlu)", "B": "HYBRID v4-B (EMA+NW zorunlu)",
                    "C": "HYBRID v4-C (>=2/3 uzlasi+HAM-skor filtresi)"}

V4_BACKTEST_DAYS_LIST = [int(g.strip()) for g in os.environ.get("V4_BACKTEST_DAYS_LIST", "30,90,180").split(",") if g.strip()]


def _v4_entry_signal(shib_series, early_series, majors_series, ts_ms, candidate):
    """v2b._early_signal()'in YERINE gecen, ADAYA gore EMA/WT/NW'yi
    ZORUNLU/DESTEK olarak ayiran giris fonksiyonu. Donen: (raw_dir,
    raw_guc) - v2b._early_signal ile AYNI sekil, boylece v3b._v3_step
    DEGISTIRILMEDEN calisir."""
    v = v4audit._component_votes(early_series, shib_series, majors_series, ts_ms)
    ema, wt, nw = v["ema"], v["wavetrend"], v["nw"]

    if candidate == "C":
        oylar = [x for x in (ema, wt, nw) if x is not None]
        toplam = sum(oylar)
        if toplam >= 2:
            yon = 1
        elif toplam <= -2:
            yon = -1
        else:
            return 0, 0.0
        composite = v["raw_score"]
        if yon == 1 and composite <= -he.HYBRID_ESIK_YON:
            return 0, 0.0
        if yon == -1 and composite >= he.HYBRID_ESIK_YON:
            return 0, 0.0
        return yon, abs(toplam) / 3.0

    zorunlu, destek = ((ema, wt), (nw,)) if candidate == "A" else ((ema, nw), (wt,))
    if any(x is None or x == 0 for x in zorunlu):
        return 0, 0.0
    if not (all(x == 1 for x in zorunlu) or all(x == -1 for x in zorunlu)):
        return 0, 0.0
    yon = zorunlu[0]
    destek_gecerli = [x for x in destek if x is not None]
    destek_uyum = sum(1 for x in destek_gecerli if x == yon)
    guc = (len(zorunlu) + destek_uyum) / (len(zorunlu) + len(destek))
    return yon, guc


def simulate_v4(shib_series, majors_series, early_series, zamanlar, kapanislar, v1_hysteresis_gecmisi,
                 candidate, cost_percent=None):
    """v3b.simulate_v3'un AYNISI - SADECE giris sinyali v2b._early_signal
    yerine _v4_entry_signal(candidate). Durum makinesi (persistence+
    asymmetric-exit, V3-C sabit) v3b'den DOGRUDAN reuse edilir."""
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
    latch_state = v3b._yeni_latch_state()
    latch_episodes = []
    raw_avoided_ticks = 0

    trades, equity_egrisi, coin_egrisi, shib_pct_gecmisi = [], [], [], []
    grid_pct_gecmisi, trend_pct_gecmisi = [], []
    regime_gecmisi, early_gecmisi = [], []

    for i, fiyat in enumerate(kapanislar):
        tarih = time.strftime("%Y-%m-%d %H:%M", time.localtime(zamanlar[i] / 1000))
        karar_zamani = zamanlar[i] + base_interval_ms

        _n = len(trades)
        hb._grid_sleeve_step(grid_state, fiyat, tarih, trades, cost_pct)
        for t in trades[_n:]:
            t["ts_ms"] = zamanlar[i]

        if i % steps_per_rebalance == 0:
            v1_tick = v1_by_idx.get(i)
            confirmed_regime = v1_tick["confirmed_regime"] if v1_tick else "YATAY"
            confirmed_score = v1_tick["confirmed_score"] if v1_tick else 0.0
            raw_dir, raw_guc = _v4_entry_signal(shib_series, early_series, majors_series, karar_zamani, candidate)

            etkin_regime, exit_reason = v3b._v3_step(latch_state, raw_dir, raw_guc, confirmed_regime,
                                                      True, True, karar_zamani, latch_episodes)

            raw_etkin = confirmed_regime
            if confirmed_regime == "YATAY":
                raw_etkin = "ERKEN_YUKSELIS" if raw_dir == 1 else ("ERKEN_DUSUS" if raw_dir == -1 else "YATAY")
            if raw_etkin != etkin_regime:
                raw_avoided_ticks += 1

            guc = (latch_state["guc"] * 100) if etkin_regime in ("ERKEN_YUKSELIS", "ERKEN_DUSUS") else abs(confirmed_score)
            hedef_raw = v2b._v2_target_allocation(etkin_regime, guc, trend_target)
            capped_by_step = False
            if hedef_raw is not None:
                if etkin_regime == "TOPARLANMA":
                    trend_target = hedef_raw
                else:
                    onceki = trend_target
                    trend_target = pm.smooth_target(trend_target, hedef_raw, hb.HYBRID_MAX_STEP_PERCENT)
                    capped_by_step = abs(hedef_raw - onceki) > hb.HYBRID_MAX_STEP_PERCENT + 1e-9

            trend_usdt, trend_coin = v2b._trend_sleeve_rebalance(
                trend_usdt, trend_coin, trend_target, fiyat, cost_pct, trades, tarih,
                hb.HYBRID_MIN_REBALANCE_DELTA, ts_ms=karar_zamani)

            regime_gecmisi.append((i, karar_zamani, confirmed_regime, confirmed_score, trend_target))
            early_gecmisi.append({
                "idx": i, "ts_ms": karar_zamani, "early_dir": raw_dir, "early_guc": round(raw_guc, 3),
                "etkin_regime": etkin_regime, "confirmed_regime": confirmed_regime,
                "latched_state": latch_state["latched"],
                "hedef_raw": round(hedef_raw, 2) if hedef_raw is not None else None,
                "trend_target": round(trend_target, 2), "capped_by_step": capped_by_step,
                "exit_reason": exit_reason if exit_reason != "ENTERED" else None,
            })

        toplam_usdt = grid_state["usdt"] + trend_usdt
        toplam_coin = grid_state["coin"] + trend_coin
        toplam_deger = toplam_usdt + toplam_coin * fiyat
        actual_pct = (toplam_coin * fiyat / toplam_deger * 100) if toplam_deger > 0 else 0.0
        grid_deger_simdi = grid_state["usdt"] + grid_state["coin"] * fiyat
        grid_pct = (grid_state["coin"] * fiyat / grid_deger_simdi * 100) if grid_deger_simdi > 0 else 0.0
        trend_deger_simdi = trend_usdt + trend_coin * fiyat
        trend_pct = (trend_coin * fiyat / trend_deger_simdi * 100) if trend_deger_simdi > 0 else 0.0
        equity_egrisi.append(toplam_deger)
        coin_egrisi.append(toplam_coin + toplam_usdt / fiyat)
        shib_pct_gecmisi.append(actual_pct)
        grid_pct_gecmisi.append(grid_pct)
        trend_pct_gecmisi.append(trend_pct)

    if latch_state["latched"] != "NEUTRAL":
        v3b._kapat_episode(latch_state, zamanlar[-1] + base_interval_ms, "BACKTEST_SONU", latch_episodes)

    return {
        "trades": trades, "equity_egrisi": equity_egrisi, "coin_egrisi": coin_egrisi,
        "baslangic_coin": baslangic_coin_esdeger, "shib_pct_gecmisi": shib_pct_gecmisi,
        "grid_pct_gecmisi": grid_pct_gecmisi, "trend_pct_gecmisi": trend_pct_gecmisi,
        "regime_gecmisi": regime_gecmisi, "early_gecmisi": early_gecmisi,
        "latch_episodes": latch_episodes, "raw_avoided_ticks": raw_avoided_ticks,
    }


def _episode_quality_stats(latch_episodes, zamanlar, kapanislar):
    """Her EARLY_BULLISH episode'u icin (v4audit._kategori ile AYNI esik/
    tanim - tutarlilik icin REUSE edilir) faydali/kucuk/basarisiz/gurultu
    siniflandirmasi + oranlar."""
    n = len(kapanislar)
    ts_to_idx = {zamanlar[i] + INTERVAL_MS["15m"]: i for i in range(n)}
    kategoriler = {}
    for ep in latch_episodes:
        if ep["latched_state"] != "EARLY_BULLISH" or ep["state_entry_time"] is None:
            continue
        idx_entry = ts_to_idx.get(ep["state_entry_time"])
        if idx_entry is None:
            continue
        entry_price = kapanislar[idx_entry]
        adim24 = int(24 * 4)
        pencere = kapanislar[idx_entry:min(idx_entry + adim24 + 1, n)]
        if not pencere:
            continue
        max24 = (max(pencere) - entry_price) / entry_price * 100
        min24 = (min(pencere) - entry_price) / entry_price * 100
        kat = v4audit._kategori(max24, min24)
        kategoriler.setdefault(kat, 0)
        kategoriler[kat] += 1
    toplam = sum(kategoriler.values())
    useful_pct = (kategoriler.get("USEFUL_BIG_MOVE", 0) / toplam * 100) if toplam else None
    yanlis_pct = ((kategoriler.get("FAILED_REVERSED", 0) + kategoriler.get("NOISE_FLAT", 0)) / toplam * 100) if toplam else None
    return toplam, useful_pct, yanlis_pct, kategoriler


def run_for_days(days):
    print(f"\n{'#' * 78}\nHYBRID v4 (EARLY SIGNAL QUALITY) DENEY BACKTEST: {SYMBOL} - son {days} gun\n{'#' * 78}")

    grid_row = hb._grid_only_ozet(days)
    shib_series, majors_series, zamanlar, kapanislar = hb._fetch_hybrid_all(days)
    early_series = v2b._fetch_early_series(days)
    events = teb._big_move_events(zamanlar, kapanislar, hb.BIG_MOVE_THRESHOLD_PERCENT, hb.BIG_MOVE_WINDOW_HOURS)

    v1_sonuc = hb.simulate(shib_series, majors_series, zamanlar, kapanislar)
    v1_sonuc_brut = hb.simulate(shib_series, majors_series, zamanlar, kapanislar, cost_percent=0)
    v1_row = hb._ozet("HYBRID v1", v1_sonuc, v1_sonuc_brut, zamanlar, kapanislar)

    v2_sonuc = v2b.simulate_v2(shib_series, majors_series, early_series, zamanlar, kapanislar, v1_sonuc["hysteresis_gecmisi"])
    v2_sonuc_brut = v2b.simulate_v2(shib_series, majors_series, early_series, zamanlar, kapanislar,
                                     v1_sonuc["hysteresis_gecmisi"], cost_percent=0)
    v2_row = hb._ozet("HYBRID v2", v2_sonuc, v2_sonuc_brut, zamanlar, kapanislar)

    v3c_sonuc = v3b.simulate_v3(shib_series, majors_series, early_series, zamanlar, kapanislar,
                                 v1_sonuc["hysteresis_gecmisi"], use_persistence=True, use_asymmetric_exit=True)
    v3c_sonuc_brut = v3b.simulate_v3(shib_series, majors_series, early_series, zamanlar, kapanislar,
                                      v1_sonuc["hysteresis_gecmisi"], use_persistence=True, use_asymmetric_exit=True, cost_percent=0)
    v3c_row = hb._ozet("HYBRID v3-C", v3c_sonuc, v3c_sonuc_brut, zamanlar, kapanislar)

    satirlar = [grid_row, v1_row, v2_row, v3c_row]

    print(f"\n--- {days} GUN: EARLY EPISODE KALITE TABLOSU (v3-C referans + 3 v4 adayi) ---")
    print(f"{'SISTEM':<38} {'episodeN':>9} {'faydali%':>9} {'yanlis%':>8} {'islem':>6} {'maliyet.p':>9} {'kacirilanYuks24h':>17}")

    v3c_n, v3c_useful, v3c_yanlis, _ = _episode_quality_stats(v3c_sonuc["latch_episodes"], zamanlar, kapanislar)
    v3c_kacirilan, v3c_toplam_yuks = v2b._missed_strong_up_count(v3c_sonuc["shib_pct_gecmisi"], events)
    v3c_maliyet = v3c_row["brut_getiri"] - v3c_row["net_getiri"]
    print(f"{'HYBRID v3-C (referans)':<38} {v3c_n:>9} {(v3c_useful if v3c_useful is not None else float('nan')):>9.1f} "
          f"{(v3c_yanlis if v3c_yanlis is not None else float('nan')):>8.1f} {v3c_row['islem']:>6} {v3c_maliyet:>9.2f} "
          f"{f'{v3c_kacirilan}/{v3c_toplam_yuks}':>17}")

    for cand in V4_CANDIDATES:
        isim = V4_VARIANT_NAMES[cand]
        v4_sonuc = simulate_v4(shib_series, majors_series, early_series, zamanlar, kapanislar, v1_sonuc["hysteresis_gecmisi"], cand)
        v4_sonuc_brut = simulate_v4(shib_series, majors_series, early_series, zamanlar, kapanislar,
                                     v1_sonuc["hysteresis_gecmisi"], cand, cost_percent=0)
        v4_row = hb._ozet(isim, v4_sonuc, v4_sonuc_brut, zamanlar, kapanislar)
        satirlar.append(v4_row)

        n_ep, useful_pct, yanlis_pct, _ = _episode_quality_stats(v4_sonuc["latch_episodes"], zamanlar, kapanislar)
        kacirilan, toplam_yuks = v2b._missed_strong_up_count(v4_sonuc["shib_pct_gecmisi"], events)
        maliyet = v4_row["brut_getiri"] - v4_row["net_getiri"]
        print(f"{isim:<38} {n_ep:>9} {(useful_pct if useful_pct is not None else float('nan')):>9.1f} "
              f"{(yanlis_pct if yanlis_pct is not None else float('nan')):>8.1f} {v4_row['islem']:>6} {maliyet:>9.2f} "
              f"{f'{kacirilan}/{toplam_yuks}':>17}")

    hb._master_tablo(satirlar)
    return satirlar


def main():
    tum_sonuclar = {}
    for gun in V4_BACKTEST_DAYS_LIST:
        satirlar = run_for_days(gun)
        tum_sonuclar[gun] = {s["sistem"]: s for s in satirlar}

    if len(tum_sonuclar) > 1:
        print(f"\n{'#' * 78}\nCAPRAZ-PENCERE NET GETIRI TABLOSU (sistem x gun)\n{'#' * 78}")
        sistemler = ["GRID", "HYBRID v1", "HYBRID v2", "HYBRID v3-C"] + [V4_VARIANT_NAMES[c] for c in V4_CANDIDATES]
        baslik = f"{'SISTEM':<38}" + "".join(f"{str(g) + 'g NET%':>12}" for g in V4_BACKTEST_DAYS_LIST)
        print(baslik)
        print("-" * len(baslik))
        for isim in sistemler:
            satir = f"{isim:<38}"
            for gun in V4_BACKTEST_DAYS_LIST:
                v = tum_sonuclar[gun].get(isim, {}).get("net_getiri")
                satir += f"{(f'{v:+.1f}' if v is not None else 'n/a'):>12}"
            print(satir)

        son_gun = max(V4_BACKTEST_DAYS_LIST)
        v3c = tum_sonuclar[son_gun]["HYBRID v3-C"]
        print(f"\n{'#' * 78}\nKARAR KRITERI (madde 7) - {son_gun} GUN ODAKLI, HAM SAYILAR\n{'#' * 78}")
        print(f"(V3-C referans: islem={v3c['islem']}  maliyet={v3c['brut_getiri'] - v3c['net_getiri']:.2f}p  "
              f"NET={v3c['net_getiri']:+.2f}%  GYuksSHIB%={v3c.get('ort_shib_pct_guclu_yukselis')})")
        for cand in V4_CANDIDATES:
            isim = V4_VARIANT_NAMES[cand]
            s = tum_sonuclar[son_gun].get(isim)
            if not s:
                continue
            maliyet = s["brut_getiri"] - s["net_getiri"]
            print(f"  {isim}: islem={s['islem']} ({s['islem'] / v3c['islem']:.2f}x)  maliyet={maliyet:.2f}p "
                  f"({maliyet / (v3c['brut_getiri'] - v3c['net_getiri']):.2f}x)  NET={s['net_getiri']:+.2f}% (v3c={v3c['net_getiri']:+.2f}%)  "
                  f"GYuksSHIB%={s.get('ort_shib_pct_guclu_yukselis')} (v3c={v3c.get('ort_shib_pct_guclu_yukselis')})  DD={s['max_dusus']:.2f}% (v3c={v3c['max_dusus']:.2f}%)")
        print("\n(Not: 'episode sayisi' ve 'faydali%'/'yanlis%' icin yukaridaki EARLY EPISODE KALITE TABLOSU'na bakin -")
        print(" 'basari sadece NET ile secilmez' (madde 8): episode sayisi COK dustuyse ama faydali% de dusukse,")
        print(" bu 'az ama iyi sinyal' degil 'nadiren ates alan ama hala kotu' bir filtre olabilir - ikisini BIRLIKTE okuyun.)")
        print("\n(Not: bu SADECE deney - hicbir allocation yuzdesi buyutulmedi, grid/live bot degistirilmedi.)")


if __name__ == "__main__":
    main()
