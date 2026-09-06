"""
HYBRID v3 (PERSISTENCE / ASYMMETRIC EXIT) - DENEYSEL, 3 ablation varyanti.

BU DOSYA v1 ve v2 dosyalarina DOKUNMAZ: hybrid_engine.py, portfolio_manager.py,
hybrid_backtest.py, grid_backtest.py, trend_engine*.py, hybrid_v2_backtest.py,
hybrid_v2_allocation_diagnostic.py DEGISMEDEN import edilip TEKRAR KULLANILIR.
"HYBRID v1" ve "HYBRID v2" satirlari kendi backtest dosyalarini dogrudan
calistirmakla BIREBIR ayni sonucu verir.

NEDEN BU DOSYA VAR (hybrid_v2_allocation_diagnostic.csv'nin bulgusu):
  180 gunluk allocation audit'i gosterdi ki 21 kacirilan buyuk yukselisin
  18'inde (%86) kok neden ALLOCATION MIMARISI (hedef yuzdeler, sleeve-cap,
  step hizi) DEGIL, SINYAL KALITESI/PERSISTENCE: erken pozisyon acilir
  acilmaz (medyan 4.5 saatte) geri satiliyor (cause D, 11/21) veya erken
  sinyal 24 saat icinde surekli ON/OFF salinip (cause A, 7/21) kararli bir
  birikime izin vermiyor. v2'nin BRUT getirisi v1'den DAHA IYIYDI (+1.87%
  vs -0.76%) ama 2.84x maliyet artisi (turnover'la neredeyse birebir
  orantili - 2.56x islem artisi) bu kenari (edge) tamamen yiyordu.

  Bu dosya STRATEJI PARAMETRESI (esik, allocation yuzdesi, hysteresis,
  indikator) DEGISTIRMEDEN, SADECE erken sinyalin PORTFOYE nasil
  "cevrildigini" (persistence/exit davranisini) degistiren bir STATE
  MACHINE / LATCH katmani ekliyor.

MIMARI - LATCH (kilit) durum makinesi:
  Durumlar: NEUTRAL / EARLY_BULLISH / EARLY_BEARISH (CONFIRMED_BULLISH ve
  CONFIRMED_BEARISH ayri "durum" olarak MODELLENMEDI - onlar zaten v1'in
  KENDI confirmed-rejim akisidir, hb.simulate()'in hysteresis_gecmisi'nden
  OLDUGU GIBI okunur, degistirilmez; confirmed_regime != YATAY oldugu an
  latch otomatik sifirlanir ve v1'in soyledigine gecilir - v2 ile AYNI
  oncelik kurali).

  GIRIS (ENTRY) - v2 ile TAMAMEN AYNI esik/kosul (degistirilmedi):
    NEUTRAL --[ham early_dir == +1/-1]--> EARLY_BULLISH / EARLY_BEARISH
    (v2b._early_signal() DOGRUDAN cagrilir, oy esigi V2_EARLY_MIN_NET_VOTE
    AYNEN kalir).

  CIKIS (EXIT) - burasi YENI, iki BAGIMSIZ anahtarla kontrol edilir:
    - use_persistence: latched durumdayken TEK bir "notr" (ham sinyal artik
      yon vermiyor, ama TERS de degil) tik yuzunden HEMEN NEUTRAL'e
      DONULMEZ; V3_NEUTRAL_DECAY_TICKS (=2 x HYBRID_HYSTERESIS_BARS, yani
      mevcut kodun hysteresis konvansiyonunun "notr icin 2 kat temkinli"
      hali - YENI bir sayi UYDURULMADI) ardisik notr tik gerekir.
    - use_asymmetric_exit: latched durumdayken TERS yonlu (bullish
      latch'teyken bearish ham sinyal) bir tik HEMEN reversal SAYILMAZ;
      V3_REVERSAL_CONFIRM_TICKS (= HYBRID_HYSTERESIS_BARS = 2, mevcut
      kodun KENDI hysteresis sayisi TEKRAR KULLANILIYOR) ardisik ters tik
      gerekir - "guclu karsi kanit gelirse HEMEN cik" (kullanicinin acik
      istegi) ama TEK bir whipsaw tik'i yetmez.
  Ikisi de KAPALIYSA davranis v2 ile AYNIDIR (tek tik = anlik reversal/
  decay) - bu yuzden "HYBRID v2" ayrica calistirilmiyor, zaten v2b.
  simulate_v2() DOGRUDAN import/cagirilarak raporda referans olarak kalir.

  3 ABLATION VARYANTI (kullanicinin acik istegi - grid-search YOK, TEK
  aciklanabilir varsayilan tasarim, 2x2'nin 3 kosesi):
    V3-A (Persistence ONLY)      : use_persistence=True,  use_asymmetric_exit=False
    V3-B (Asymmetric Exit ONLY)  : use_persistence=False, use_asymmetric_exit=True
    V3-C (Persistence+AsymExit)  : use_persistence=True,  use_asymmetric_exit=True

  Latched durumdayken hedef allocation (guc) HAM sinyal her notr/ters
  tikte SIFIRLANMAZ - son "pekistiren" (latch yonuyle AYNI) tikteki guc
  DEGERI korunur (state["guc"]) - boylece latch ayakta dururken hedef
  yuzde rasgele sicramaz.

  ALLOCATION TABLOSU (V2_REGIME_RANGES) VE ENTRY ESIKLERI DEGISTIRILMEDI -
  v2b._v2_target_allocation() ve v2b._early_signal() DOGRUDAN, AYNEN
  cagrilir. Sadece HANGI etkin_regime'e besleneceklerini belirleyen
  PERSISTENCE/EXIT mantigi yeni.

TELEMETRI (kullanicinin istedigi, salt-teshis, karari etkilemez):
  early_gecmisi: raw_dir/raw_guc (ham sinyal), etkin_regime (latch sonrasi),
  latched_state, exit_reason, capped_by_step - her rebalance kontrolunde.
  latch_episodes: HER latch GIRIS->CIKIS'i icin state_entry_time/
  state_exit_time/state_duration_h/reset_count_while_latched/exit_reason.
  raw_avoided_ticks: v2'nin HAM (latch'siz) mantigi neyi secerdi ile
  latch'li secim FARKLI oldugu kontrol sayisi (yaklasik "onlenen trade"
  proxy'si).

Kullanim:
    python3 hybrid_v3_backtest.py           (varsayilan 30,90,180 gun)
    V3_BACKTEST_DAYS_LIST=180 python3 hybrid_v3_backtest.py
"""

import bisect
import csv
import os
import statistics
import time

from backtest import INTERVAL_MS
from trade_bot import _load_dotenv
import hybrid_backtest as hb
import hybrid_v2_backtest as v2b
import portfolio_manager as pm
import trend_engine_backtest as teb

_load_dotenv()

SYMBOL = hb.SYMBOL

# Mevcut kodun KENDI hysteresis konvansiyonu tekrar kullaniliyor - YENI bir
# sayi UYDURULMADI (kullanicinin acik istegi: "exact esik secip optimize
# etme, mevcut gostergeler uzerinden basit ve aciklanabilir bir state
# transition tasarla"):
V3_REVERSAL_CONFIRM_TICKS = hb.HYBRID_HYSTERESIS_BARS       # = 2
V3_NEUTRAL_DECAY_TICKS = hb.HYBRID_HYSTERESIS_BARS * 2      # = 4 (notr icin 2x temkinli)

V3_VARIANTS = {
    "HYBRID v3-A (Persistence)": {"use_persistence": True, "use_asymmetric_exit": False},
    "HYBRID v3-B (AsymExit)": {"use_persistence": False, "use_asymmetric_exit": True},
    "HYBRID v3-C (Persist+AsymExit)": {"use_persistence": True, "use_asymmetric_exit": True},
}

V3_BACKTEST_DAYS_LIST = [int(g.strip()) for g in os.environ.get("V3_BACKTEST_DAYS_LIST", "30,90,180").split(",") if g.strip()]


def _yeni_latch_state():
    return {"latched": "NEUTRAL", "entry_time": None, "guc": 0.0,
            "neutral_run": 0, "opposite_run": 0, "reset_count_while_latched": 0}


def _kapat_episode(state, ts_ms, neden, episodes):
    episodes.append({
        "latched_state": state["latched"], "state_entry_time": state["entry_time"], "state_exit_time": ts_ms,
        "state_duration_h": round((ts_ms - state["entry_time"]) / 3_600_000, 1) if state["entry_time"] is not None else None,
        "reset_count_while_latched": state["reset_count_while_latched"], "exit_reason": neden,
    })
    state.update(latched="NEUTRAL", entry_time=None, guc=0.0, neutral_run=0, opposite_run=0, reset_count_while_latched=0)


def _v3_step(state, raw_dir, raw_guc, confirmed_regime, use_persistence, use_asymmetric_exit, ts_ms, episodes):
    """Donen: (etkin_regime, exit_reason_or_None). Guncel guc degeri
    cagiran tarafindan state['guc'] uzerinden okunur (yan etkiyle
    guncellenir)."""
    if confirmed_regime != "YATAY":
        if state["latched"] != "NEUTRAL":
            _kapat_episode(state, ts_ms, f"CONFIRMED_REGIME_{confirmed_regime}", episodes)
        return confirmed_regime, None

    if state["latched"] == "NEUTRAL":
        if raw_dir == 1:
            state.update(latched="EARLY_BULLISH", entry_time=ts_ms, guc=raw_guc, neutral_run=0, opposite_run=0, reset_count_while_latched=0)
            return "ERKEN_YUKSELIS", "ENTERED"
        if raw_dir == -1:
            state.update(latched="EARLY_BEARISH", entry_time=ts_ms, guc=raw_guc, neutral_run=0, opposite_run=0, reset_count_while_latched=0)
            return "ERKEN_DUSUS", "ENTERED"
        return "YATAY", None

    beklenen = 1 if state["latched"] == "EARLY_BULLISH" else -1
    if raw_dir == beklenen:
        state["neutral_run"], state["opposite_run"], state["guc"] = 0, 0, raw_guc
    elif raw_dir == 0:
        state["neutral_run"] += 1
        state["opposite_run"] = 0
        state["reset_count_while_latched"] += 1
    else:
        state["opposite_run"] += 1
        state["neutral_run"] = 0
        state["reset_count_while_latched"] += 1

    reversal_esik = V3_REVERSAL_CONFIRM_TICKS if use_asymmetric_exit else 1
    neutral_esik = V3_NEUTRAL_DECAY_TICKS if use_persistence else 1

    if state["opposite_run"] >= reversal_esik:
        neden = "REVERSAL_CONFIRMED" if use_asymmetric_exit else "REVERSAL_IMMEDIATE"
        _kapat_episode(state, ts_ms, neden, episodes)
        return "YATAY", neden
    if state["neutral_run"] >= neutral_esik:
        neden = "NEUTRAL_DECAY" if use_persistence else "NEUTRAL_IMMEDIATE"
        _kapat_episode(state, ts_ms, neden, episodes)
        return "YATAY", neden

    return ("ERKEN_YUKSELIS" if state["latched"] == "EARLY_BULLISH" else "ERKEN_DUSUS"), None


def simulate_v3(shib_series, majors_series, early_series, zamanlar, kapanislar, v1_hysteresis_gecmisi,
                 use_persistence, use_asymmetric_exit, cost_percent=None):
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
    latch_state = _yeni_latch_state()
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
            raw_dir, raw_guc = v2b._early_signal(early_series, karar_zamani)

            etkin_regime, exit_reason = _v3_step(latch_state, raw_dir, raw_guc, confirmed_regime,
                                                  use_persistence, use_asymmetric_exit, karar_zamani, latch_episodes)

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
        _kapat_episode(latch_state, zamanlar[-1] + base_interval_ms, "BACKTEST_SONU", latch_episodes)

    return {
        "trades": trades, "equity_egrisi": equity_egrisi, "coin_egrisi": coin_egrisi,
        "baslangic_coin": baslangic_coin_esdeger, "shib_pct_gecmisi": shib_pct_gecmisi,
        "grid_pct_gecmisi": grid_pct_gecmisi, "trend_pct_gecmisi": trend_pct_gecmisi,
        "regime_gecmisi": regime_gecmisi, "early_gecmisi": early_gecmisi,
        "latch_episodes": latch_episodes, "raw_avoided_ticks": raw_avoided_ticks,
    }


def _entry_to_sell_stats(latch_episodes, trades):
    """Her EARLY_BULLISH episode'unun GIRIS anindan sonraki ILK
    TREND_SLEEVE_SAT'a kadar gecen sure (saat) - v2'nin 4.5 saatlik
    medyaniyla DOGRUDAN karsilastirilabilir metrik."""
    satis_zamanlari = sorted(t["ts_ms"] for t in trades if t["tip"] == "TREND_SLEEVE_SAT" and t.get("ts_ms") is not None)
    deltalar = []
    for ep in latch_episodes:
        if ep["latched_state"] != "EARLY_BULLISH" or ep["state_entry_time"] is None:
            continue
        idx = bisect.bisect_left(satis_zamanlari, ep["state_entry_time"])
        if idx < len(satis_zamanlari):
            ts = satis_zamanlari[idx]
            sinir = (ep["state_exit_time"] or ep["state_entry_time"]) + 24 * 3_600_000
            if ts <= sinir:
                deltalar.append((ts - ep["state_entry_time"]) / 3_600_000)
    return deltalar


def run_for_days(days, tum_v3_event_satirlari, tum_latch_satirlari):
    print(f"\n{'#' * 78}\nHYBRID v3 (PERSISTENCE/ASYMMETRIC EXIT) DENEY BACKTEST: {SYMBOL} - son {days} gun\n{'#' * 78}")

    grid_row = hb._grid_only_ozet(days)
    shib_series, majors_series, zamanlar, kapanislar = hb._fetch_hybrid_all(days)
    early_series = v2b._fetch_early_series(days)

    v1_sonuc = hb.simulate(shib_series, majors_series, zamanlar, kapanislar)
    v1_sonuc_brut = hb.simulate(shib_series, majors_series, zamanlar, kapanislar, cost_percent=0)
    v1_row = hb._ozet("HYBRID v1", v1_sonuc, v1_sonuc_brut, zamanlar, kapanislar)

    v2_sonuc = v2b.simulate_v2(shib_series, majors_series, early_series, zamanlar, kapanislar, v1_sonuc["hysteresis_gecmisi"])
    v2_sonuc_brut = v2b.simulate_v2(shib_series, majors_series, early_series, zamanlar, kapanislar,
                                     v1_sonuc["hysteresis_gecmisi"], cost_percent=0)
    v2_row = hb._ozet("HYBRID v2", v2_sonuc, v2_sonuc_brut, zamanlar, kapanislar)

    satirlar = [grid_row, v1_row, v2_row]
    events = teb._big_move_events(zamanlar, kapanislar, hb.BIG_MOVE_THRESHOLD_PERCENT, hb.BIG_MOVE_WINDOW_HOURS)

    print(f"\n--- {days} GUN: EARLY-KATMAN OZEL METRIKLER (v2 referans + 3 v3 varyanti) ---")
    print(f"{'SISTEM':<32} {'islem':>6} {'maliyet%':>9} {'medyanGiris->Satis(sa)':>22} {'medyanEpisodeSure(sa)':>21} {'kacirilanYuks24h':>17} {'onlenenTikSayisi':>16}")

    v2_pseudo_episodes = []
    onceki_ts = None
    for e in v2_sonuc["early_gecmisi"]:
        if e["etkin_regime"] == "ERKEN_YUKSELIS" and onceki_ts is None:
            onceki_ts = e["ts_ms"]
        elif e["etkin_regime"] != "ERKEN_YUKSELIS" and onceki_ts is not None:
            v2_pseudo_episodes.append({"latched_state": "EARLY_BULLISH", "state_entry_time": onceki_ts,
                                        "state_exit_time": e["ts_ms"], "state_duration_h": (e["ts_ms"] - onceki_ts) / 3_600_000})
            onceki_ts = None
    v2_satis_gecikmeleri = _entry_to_sell_stats(v2_pseudo_episodes, v2_sonuc["trades"])
    v2_durations = [e["state_duration_h"] for e in v2_pseudo_episodes]
    v2_kacirilan, v2_toplam_yuks = v2b._missed_strong_up_count(v2_sonuc["shib_pct_gecmisi"], events)
    v2_maliyet = v2_row["brut_getiri"] - v2_row["net_getiri"]
    print(f"{'HYBRID v2 (referans)':<32} {v2_row['islem']:>6} {v2_maliyet:>9.2f} "
          f"{(statistics.median(v2_satis_gecikmeleri) if v2_satis_gecikmeleri else float('nan')):>22.1f} "
          f"{(statistics.median(v2_durations) if v2_durations else float('nan')):>21.1f} "
          f"{f'{v2_kacirilan}/{v2_toplam_yuks}':>17} {'n/a':>16}")

    for isim, params in V3_VARIANTS.items():
        v3_sonuc = simulate_v3(shib_series, majors_series, early_series, zamanlar, kapanislar,
                                v1_sonuc["hysteresis_gecmisi"], **params)
        v3_sonuc_brut = simulate_v3(shib_series, majors_series, early_series, zamanlar, kapanislar,
                                     v1_sonuc["hysteresis_gecmisi"], cost_percent=0, **params)
        v3_row = hb._ozet(isim, v3_sonuc, v3_sonuc_brut, zamanlar, kapanislar)
        satirlar.append(v3_row)

        satis_gecikmeleri = _entry_to_sell_stats(v3_sonuc["latch_episodes"], v3_sonuc["trades"])
        bullish_episodes = [e for e in v3_sonuc["latch_episodes"] if e["latched_state"] == "EARLY_BULLISH" and e["state_duration_h"] is not None]
        durations = [e["state_duration_h"] for e in bullish_episodes]
        kacirilan, toplam_yuks = v2b._missed_strong_up_count(v3_sonuc["shib_pct_gecmisi"], events)
        maliyet_puan = v3_row["brut_getiri"] - v3_row["net_getiri"]

        print(f"{isim:<32} {v3_row['islem']:>6} {maliyet_puan:>9.2f} "
              f"{(statistics.median(satis_gecikmeleri) if satis_gecikmeleri else float('nan')):>22.1f} "
              f"{(statistics.median(durations) if durations else float('nan')):>21.1f} "
              f"{f'{kacirilan}/{toplam_yuks}':>17} {v3_sonuc['raw_avoided_ticks']:>16}")

        event_satirlari = v2b._event_report(days, zamanlar, v3_sonuc["regime_gecmisi"], v3_sonuc["early_gecmisi"],
                                             v3_sonuc["shib_pct_gecmisi"], events)
        for s in event_satirlari:
            s["system"] = isim
        tum_v3_event_satirlari.extend(event_satirlari)

        for ep in v3_sonuc["latch_episodes"]:
            satir = dict(ep)
            satir["window_days"] = days
            satir["system"] = isim
            satir["state_entry_time_fmt"] = v2b._fmt(ep["state_entry_time"])
            satir["state_exit_time_fmt"] = v2b._fmt(ep["state_exit_time"])
            tum_latch_satirlari.append(satir)

    hb._master_tablo(satirlar)

    print(f"\n--- {days} GUN OZET: '4.5 saatte satildi' sorunu cozuldu mu? ---")
    print("(v2'nin KENDI 'ilk erken pozisyon->ilk satis' medyani onceki allocation-audit'te 4.5 saatti;")
    print(" yukaridaki tabloda v3 varyantlarinin 'medyanGiris->Satis' ve 'medyanEpisodeSure' degerleriyle")
    print(" DOGRUDAN karsilastirin - sure uzadiysa VE ayni zamanda NET/Token/kacirilanYuks24h de iyilestiyse")
    print(" pozisyon GERCEKTEN trend boyunca tutuluyor demektir; sure uzadi ama NET kotulestiyse/DD patladiysa")
    print(" 'satisi geciktirip zarari baska yere tasima' riski var demektir - asagidaki KARAR KRITERI'ne bakin.")

    return satirlar


def main():
    tum_sonuclar = {}
    tum_v3_event_satirlari = []
    tum_latch_satirlari = []
    for gun in V3_BACKTEST_DAYS_LIST:
        satirlar = run_for_days(gun, tum_v3_event_satirlari, tum_latch_satirlari)
        tum_sonuclar[gun] = {s["sistem"]: s for s in satirlar}

    if tum_v3_event_satirlari:
        with open("hybrid_v3_events.csv", "w", newline="") as f:
            fieldnames = list(tum_v3_event_satirlari[0].keys())
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(tum_v3_event_satirlari)
        print(f"\n[V3] Olay-bazli CSV: hybrid_v3_events.csv ({len(tum_v3_event_satirlari)} satir)")

    if tum_latch_satirlari:
        with open("hybrid_v3_latch_episodes.csv", "w", newline="") as f:
            fieldnames = list(tum_latch_satirlari[0].keys())
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(tum_latch_satirlari)
        print(f"[V3] Latch-episode CSV: hybrid_v3_latch_episodes.csv ({len(tum_latch_satirlari)} episode)")

    if len(tum_sonuclar) > 1:
        print(f"\n{'#' * 78}\nCAPRAZ-PENCERE NET GETIRI TABLOSU (sistem x gun)\n{'#' * 78}")
        sistemler = ["GRID", "HYBRID v1", "HYBRID v2"] + list(V3_VARIANTS.keys())
        baslik = f"{'SISTEM':<32}" + "".join(f"{str(g) + 'g NET%':>12}" for g in V3_BACKTEST_DAYS_LIST)
        print(baslik)
        print("-" * len(baslik))
        for isim in sistemler:
            satir = f"{isim:<32}"
            for gun in V3_BACKTEST_DAYS_LIST:
                v = tum_sonuclar[gun].get(isim, {}).get("net_getiri")
                satir += f"{(f'{v:+.1f}' if v is not None else 'n/a'):>12}"
            print(satir)

        son_gun = max(V3_BACKTEST_DAYS_LIST)
        g = tum_sonuclar[son_gun]["GRID"]
        v1 = tum_sonuclar[son_gun]["HYBRID v1"]
        v2 = tum_sonuclar[son_gun]["HYBRID v2"]
        print(f"\n{'#' * 78}\nKARAR KRITERI (madde 10) - {son_gun} GUN ODAKLI, HAM SAYILAR, YORUM KULLANICIYA BIRAKILIR\n{'#' * 78}")
        print(f"{'SISTEM':<32} {'NET%':>8} {'BRUT%':>8} {'maliyet.p':>10} {'Token%':>8} {'DD%':>8} {'islem':>7}")
        for isim in ["GRID", "HYBRID v1", "HYBRID v2"] + list(V3_VARIANTS.keys()):
            s = tum_sonuclar[son_gun].get(isim)
            if not s:
                continue
            maliyet = (s["brut_getiri"] - s["net_getiri"]) if s.get("brut_getiri") is not None else None
            print(f"{isim:<32} {s['net_getiri']:>8.2f} {(s['brut_getiri'] if s['brut_getiri'] is not None else float('nan')):>8.2f} "
                  f"{(maliyet if maliyet is not None else float('nan')):>10.2f} {s['token_degisim']:>8.2f} {s['max_dusus']:>8.2f} {s['islem']:>7}")
        print(f"\nKontrol listesi (v2'ye gore, {son_gun}g):")
        for isim in V3_VARIANTS.keys():
            s = tum_sonuclar[son_gun].get(isim)
            if not s:
                continue
            print(f"  {isim}: islem {s['islem']} (v2={v2['islem']}, oran {s['islem'] / v2['islem']:.2f}x)  "
                  f"NET {s['net_getiri']:+.2f}% (v2={v2['net_getiri']:+.2f}%)  DD {s['max_dusus']:.2f}% (v2={v2['max_dusus']:.2f}%, GRID={g['max_dusus']:.2f}%)")
        print("\n(Not: BRUT edge korundu mu / early gecikme avantaji kayboldu mu icin yukaridaki 'EARLY-KATMAN OZEL")
        print("METRIKLER' tablosundaki medyanGiris->Satis ve medyanEpisodeSure satirlarina bakin.)")


if __name__ == "__main__":
    main()
