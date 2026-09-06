"""
ATAE (ADAPTIVE TREND ALLOCATION ENGINE) BACKTEST - orchestrator.

BAGIMSIZLIK (madde 37): ATAE'nin KENDI karar/allocation/execution mantigi
TAMAMEN atae_engine.py + atae_allocation.py + atae_diagnostics.py icinde -
bunlarin HICBIRI hybrid_engine.py/portfolio_manager.py/hybrid_backtest.py'ye
BAGIMLI DEGIL. GRID/HYBRID v1/v3-C SADECE BASELINE satirlari icin
DEGISTIRILMEDEN import edilip cagirilir (madde 21: "GRID yeni sistemin
ana parcasi DEGILDIR... sadece BASELINE olarak korunacaktir").

METODOLOJI (madde 0 - onceki denetimden tasinanlar, TEKRAR dogrulandi):
  - trend_engine_backtest.py'nin DUZELTILMIS (cross-event-contamination
    fix'i uygulanmis) _big_move_events/_max_drawdown fonksiyonlari REUSE
    edilir - eski sinirsiz-arama mantigi KULLANILMAZ.
  - No lookahead: AtaeSeries.index_at() sadece close_time<=sorgu anindaki
    barlari "bilinir" sayar (atae_engine.py'de bagimsiz uygulanmis, ayrica
    dogrulandi).
  - Future-return/MFE/MAE hesaplari SADECE post-hoc episode/event
    raporlamasi icin - simulate_atae()'nin KARAR dongusune ASLA girmez
    (finansal simulasyon ile diagnostic KESIN ayri, madde 0 sonu).
  - BRUT/NET AYNI karar dizisini kullanir (cost_percent sadece islem
    miktarini etkiler, kararlari degil).
  - 30/60/90/180 hepsi AYNI parametre setiyle calisir (gun-ozel dallanma
    yok - kod incelemesiyle dogrulanabilir).

Kullanim:
    python3 atae_backtest.py                (varsayilan 30,60,90,180 gun)
    ATAE_BACKTEST_DAYS_LIST=180 python3 atae_backtest.py
"""

import csv
import os
import statistics
import time

from backtest import fetch_history, INTERVAL_MS
from trade_bot import _load_dotenv
import atae_engine as ae
import atae_allocation as aa
import atae_diagnostics as ad
import trend_engine_backtest as teb
import hybrid_backtest as hb
import hybrid_v3_backtest as v3b

_load_dotenv()

SYMBOL = os.environ.get("SYMBOL", "SHIBUSDT")
MAJOR_SYMBOLS = [s.strip() for s in os.environ.get("ENGINE_MAJOR_SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT").split(",") if s.strip()]
BACKTEST_START_CAPITAL = float(os.environ.get("BACKTEST_START_CAPITAL", "1000"))
TRADING_FEE_PERCENT = float(os.environ.get("TRADING_FEE_PERCENT", "0.1"))
SLIPPAGE_PERCENT = float(os.environ.get("SLIPPAGE_PERCENT", "0.05"))
START_IN_SHIB = os.environ.get("START_IN_SHIB", "false").lower() in ("1", "true", "evet")

BASE_INTERVAL = "15m"
REBALANCE_MINUTES = int(os.environ.get("ATAE_REBALANCE_MINUTES", "60"))
ALL_TIMEFRAMES = ["5m", "15m", "30m", "1h", "4h", "1d"]
WARMUP_DAYS = {"5m": 2, "15m": 2, "30m": 3, "1h": 6, "4h": 16, "1d": 60}

BIG_MOVE_THRESHOLD_PERCENT = float(os.environ.get("ENGINE_BIG_MOVE_THRESHOLD", "5"))
BIG_MOVE_WINDOW_HOURS = float(os.environ.get("ENGINE_BIG_MOVE_WINDOW_HOURS", "24"))

ATAE_PROFILE = os.environ.get("ATAE_PROFILE", "BASE")  # BASE / CONSERVATIVE / AGGRESSIVE (bkz. atae_allocation.py)

# 3 ABLATION VARYANTI (madde 22) - artan karmasiklik sirasiyla:
ATAE_VARIANTS = {
    "ATAE-A (state+memory)": dict(use_staged_allocation=False, use_asymmetric_exit=False, use_recovery_distribution=False),
    "ATAE-B (+staged alloc+deadband)": dict(use_staged_allocation=True, use_asymmetric_exit=False, use_recovery_distribution=False),
    "ATAE-C (+asymmetric exit+Recovery/Distribution)": dict(use_staged_allocation=True, use_asymmetric_exit=True, use_recovery_distribution=True),
}

ATAE_BACKTEST_DAYS_LIST = [int(g.strip()) for g in os.environ.get("ATAE_BACKTEST_DAYS_LIST", "30,60,90,180").split(",") if g.strip()]

OUTPUT_DIR = os.environ.get("ATAE_OUTPUT_DIR", "atae_reports")


def _fetch_series(symbol, interval, days):
    fetch_days = days + WARMUP_DAYS.get(interval, 6)
    candles = fetch_history(symbol, interval, fetch_days)
    open_times = [c[0] for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    closes = [float(c[4]) for c in candles]
    volumes = [float(c[5]) for c in candles]
    onemli = interval in ae.FAST_TIMEFRAMES  # NW/WT sadece fast_vote'un kullandigi TF'lerde hesaplanir
    return ae.AtaeSeries(open_times, highs, lows, closes, volumes, INTERVAL_MS[interval],
                          compute_nw=onemli, compute_wt=onemli)


def _fetch_all(days):
    print(f"[ATAE] Gecmis veri cekiliyor: {SYMBOL} ({','.join(ALL_TIMEFRAMES)}) + "
          f"{', '.join(MAJOR_SYMBOLS)} (1h/4h/1d), pencere son {days} gun...")
    shib_series = {tf: _fetch_series(SYMBOL, tf, days) for tf in ALL_TIMEFRAMES}
    majors_series = {}
    for sym in MAJOR_SYMBOLS:
        majors_series[sym] = {tf: _fetch_series(sym, tf, days) for tf in ("1h", "4h", "1d")}

    base_full = shib_series[BASE_INTERVAL]
    kesim_zamani = base_full.open_times[-1] - days * 86_400_000
    kesim_idx = 0
    while kesim_idx < len(base_full.open_times) and base_full.open_times[kesim_idx] < kesim_zamani:
        kesim_idx += 1
    zamanlar = base_full.open_times[kesim_idx:]
    kapanislar = base_full.closes[kesim_idx:]
    return shib_series, majors_series, zamanlar, kapanislar


def simulate_atae(shib_series, majors_series, zamanlar, kapanislar, profile_name,
                   use_staged_allocation, use_asymmetric_exit, use_recovery_distribution, cost_percent=None):
    cost_pct = (TRADING_FEE_PERCENT + SLIPPAGE_PERCENT) if cost_percent is None else cost_percent
    if START_IN_SHIB:
        usdt, coin, actual_pct = 0.0, BACKTEST_START_CAPITAL / kapanislar[0], 100.0
    else:
        usdt, coin, actual_pct = BACKTEST_START_CAPITAL, 0.0, 0.0
    baslangic_coin = coin + usdt / kapanislar[0]

    base_interval_ms = INTERVAL_MS[BASE_INTERVAL]
    steps_per_rebalance = max(1, round(REBALANCE_MINUTES * 60_000 / base_interval_ms))

    evidence_state = ae.yeni_evidence_state()
    target = actual_pct
    ticks_since_last_trade = 999

    trades, equity_egrisi, coin_egrisi, shib_pct_gecmisi = [], [], [], []
    state_gecmisi = []  # (idx, ts_ms, state, bull_ev, bear_ev, target)
    tick_gecmisi = []

    for i, fiyat in enumerate(kapanislar):
        tarih = time.strftime("%Y-%m-%d %H:%M", time.localtime(zamanlar[i] / 1000))
        karar_zamani = zamanlar[i] + base_interval_ms

        if i % steps_per_rebalance == 0:
            sonuc = ae.step(evidence_state, shib_series, majors_series, karar_zamani,
                             use_asymmetric_exit, use_recovery_distribution)

            conf = aa.state_confidence(sonuc["state"], sonuc["bull_evidence"], sonuc["bear_evidence"])
            raw_target = aa.raw_target_allocation(profile_name, sonuc["state"], conf)

            if use_staged_allocation:
                target = aa.staged_step(target, raw_target)
            else:
                # ABLATION (ATAE-A): sabit/simetrik tek-hizli klemp - staged/
                # deadband/cooldown REFINE'i henuz YOK (madde 22).
                delta = max(-10.0, min(10.0, raw_target - target))
                target = max(0.0, min(100.0, target + delta))

            is_override = sonuc["reason"] == "OVERRIDE_BEAR" or (sonuc["changed"] and sonuc["state"] in ("STRONG_BEAR", "BEAR"))
            if use_staged_allocation:
                execute, gap = aa.should_execute(actual_pct, target, ticks_since_last_trade, is_override)
            else:
                gap = target - actual_pct
                execute = abs(gap) >= 0.01

            trade_side, trade_notional, fee_tutari = None, 0.0, 0.0
            if execute:
                toplam_deger = usdt + coin * fiyat
                hedef_deger = toplam_deger * (target / 100)
                delta_deger = hedef_deger - coin * fiyat
                if delta_deger > 0:
                    harcanacak = min(delta_deger, usdt)
                    if harcanacak > 0:
                        maliyet = harcanacak * cost_pct / 100
                        usdt -= harcanacak
                        coin += (harcanacak - maliyet) / fiyat
                        trades.append({"tip": "ATAE_AL", "tarih": tarih, "ts_ms": karar_zamani, "fiyat": fiyat,
                                       "usdt_tutari": harcanacak, "state": sonuc["state"]})
                        trade_side, trade_notional, fee_tutari = "AL", harcanacak, maliyet
                        ticks_since_last_trade = 0
                else:
                    satilacak = min(coin, -delta_deger / fiyat)
                    if satilacak > 0:
                        brut = satilacak * fiyat
                        net = brut * (1 - cost_pct / 100)
                        usdt += net
                        coin -= satilacak
                        trades.append({"tip": "ATAE_SAT", "tarih": tarih, "ts_ms": karar_zamani, "fiyat": fiyat,
                                       "usdt_tutari": net, "state": sonuc["state"]})
                        trade_side, trade_notional, fee_tutari = "SAT", brut, brut - net
                        ticks_since_last_trade = 0
            if not execute:
                ticks_since_last_trade += 1

            state_gecmisi.append((i, karar_zamani, sonuc["state"], sonuc["bull_evidence"], sonuc["bear_evidence"], target))
            state_age_h = ((karar_zamani - evidence_state["state_entry_time"]) / 3_600_000
                           if evidence_state["state_entry_time"] is not None else 0.0)
            tick_gecmisi.append({
                "timestamp": tarih, "price": fiyat,
                "bull_evidence": round(sonuc["bull_evidence"], 1), "bear_evidence": round(sonuc["bear_evidence"], 1),
                "current_state": sonuc["state"], "previous_state": evidence_state.get("prev_state"),
                "state_age_h": round(state_age_h, 2),
                "fast_dir": sonuc["fast_dir"], "fast_strength": round(sonuc["fast_strength"], 2),
                "slow_agree_up": round(sonuc["slow_agree_up"], 2), "slow_agree_down": round(sonuc["slow_agree_down"], 2),
                "majors_dir": round(sonuc["majors_dir"], 3),
                "target_shib_pct": round(target, 2), "actual_shib_pct": round(actual_pct, 2),
                "allocation_gap": round(gap, 2), "trade_triggered": execute, "trade_side": trade_side,
                "trade_notional": round(trade_notional, 2), "fee": round(fee_tutari, 4),
                "state_change_reason": sonuc["reason"] if sonuc["changed"] else None,
            })

        toplam_deger = usdt + coin * fiyat
        actual_pct = (coin * fiyat / toplam_deger * 100) if toplam_deger > 0 else 0.0
        equity_egrisi.append(toplam_deger)
        coin_egrisi.append(coin + usdt / fiyat)
        shib_pct_gecmisi.append(actual_pct)

    return {
        "trades": trades, "equity_egrisi": equity_egrisi, "coin_egrisi": coin_egrisi,
        "baslangic_coin": baslangic_coin, "shib_pct_gecmisi": shib_pct_gecmisi,
        "state_gecmisi": state_gecmisi, "tick_gecmisi": tick_gecmisi,
    }


def _build_episodes(state_gecmisi, zamanlar, kapanislar, trades):
    """State_gecmisi'nden (post-hoc) EPISODE listesi cikarir - her episode
    icin MFE/MAE/cost/turnover. Karar dongusune HIC girmez (madde 0/17)."""
    if not state_gecmisi:
        return []
    episodes = []
    onceki_idx, onceki_state, onceki_ts = state_gecmisi[0][0], state_gecmisi[0][2], state_gecmisi[0][1]
    for k in range(1, len(state_gecmisi)):
        idx, ts, state, _bull, _bear, _target = state_gecmisi[k]
        if state != onceki_state:
            episodes.append(_tek_episode(onceki_state, onceki_idx, idx, onceki_ts, ts, zamanlar, kapanislar, trades))
            onceki_idx, onceki_state, onceki_ts = idx, state, ts
    son_idx = state_gecmisi[-1][0]
    episodes.append(_tek_episode(onceki_state, onceki_idx, son_idx, onceki_ts, zamanlar[son_idx], zamanlar, kapanislar, trades))
    return episodes


def _tek_episode(state, start_idx, end_idx, start_ts, end_ts, zamanlar, kapanislar, trades):
    giris_fiyati = kapanislar[start_idx]
    pencere = kapanislar[start_idx:end_idx + 1] or [giris_fiyati]
    mfe = (max(pencere) - giris_fiyati) / giris_fiyati * 100
    mae = (min(pencere) - giris_fiyati) / giris_fiyati * 100
    yon_getiri = mfe if state in ("BULL_EARLY", "BULL", "STRONG_BULL", "RECOVERY") else -mae
    episode_trades = [t for t in trades if start_ts <= t["ts_ms"] < end_ts]
    cost = sum(t["usdt_tutari"] * (TRADING_FEE_PERCENT + SLIPPAGE_PERCENT) / 100 for t in episode_trades)
    turnover = sum(t["usdt_tutari"] for t in episode_trades)
    return {
        "state": state, "start_time": start_ts, "end_time": end_ts,
        "duration_h": (end_ts - start_ts) / 3_600_000, "entry_price": giris_fiyati,
        "mfe_pct": round(mfe, 2), "mae_pct": round(mae, 2),
        "trade_count": len(episode_trades), "cost": round(cost, 4), "turnover": round(turnover, 2),
        "useful": ad.classify_episode_outcome(yon_getiri),
    }


def _ozet_atae(etiket, sonuc, sonuc_brut, zamanlar, kapanislar, events):
    equity, coin_egrisi, baslangic_coin = sonuc["equity_egrisi"], sonuc["coin_egrisi"], sonuc["baslangic_coin"]
    toplam_deger = equity[-1] if equity else BACKTEST_START_CAPITAL
    net_getiri = (toplam_deger - BACKTEST_START_CAPITAL) / BACKTEST_START_CAPITAL * 100
    brut_deger = sonuc_brut["equity_egrisi"][-1] if sonuc_brut["equity_egrisi"] else BACKTEST_START_CAPITAL
    brut_getiri = (brut_deger - BACKTEST_START_CAPITAL) / BACKTEST_START_CAPITAL * 100
    bitis_coin = coin_egrisi[-1] if coin_egrisi else baslangic_coin
    token_degisim = (bitis_coin - baslangic_coin) / baslangic_coin * 100 if baslangic_coin else 0
    max_dusus = teb._max_drawdown(equity)
    toplam_maliyet = sum(t["usdt_tutari"] * (TRADING_FEE_PERCENT + SLIPPAGE_PERCENT) / 100 for t in sonuc["trades"])
    turnover = sum(t["usdt_tutari"] for t in sonuc["trades"])

    print(f"\n--- {etiket} ---")
    print(f"Bitis degeri (NET) : {toplam_deger:,.2f} USDT (NET {net_getiri:+.2f}%, BRUT {brut_getiri:+.2f}%, "
          f"maliyet {brut_getiri - net_getiri:.2f} puan)")
    print(f"TOKEN DEGISIMI     : {token_degisim:+.2f}%   Max DD: {max_dusus:.2f}%")
    print(f"Islem: {len(sonuc['trades'])}   Toplam maliyet(USDT): {toplam_maliyet:.2f}   Turnover(USDT): {turnover:.2f}")

    return {
        "sistem": etiket, "net_getiri": net_getiri, "brut_getiri": brut_getiri, "maliyet_puan": brut_getiri - net_getiri,
        "token_degisim": token_degisim, "max_dusus": max_dusus, "islem": len(sonuc["trades"]),
        "toplam_maliyet_usdt": toplam_maliyet, "turnover_usdt": turnover,
    }


def run_for_days(days, tum_event_satirlari, tum_transition_satirlari, tum_state_kalite_satirlari):
    print(f"\n{'#' * 78}\nATAE BACKTEST: {SYMBOL} - son {days} gun\n{'#' * 78}")

    grid_row = hb._grid_only_ozet(days)
    grid_row["maliyet_puan"] = None
    shib_series, majors_series, zamanlar, kapanislar = _fetch_all(days)
    events = teb._big_move_events(zamanlar, kapanislar, BIG_MOVE_THRESHOLD_PERCENT, BIG_MOVE_WINDOW_HOURS)

    # BUY & HOLD baseline (madde 21)
    bh_coin = BACKTEST_START_CAPITAL / kapanislar[0]
    bh_deger = bh_coin * kapanislar[-1]
    bh_row = {"sistem": "BUY_AND_HOLD", "net_getiri": (bh_deger - BACKTEST_START_CAPITAL) / BACKTEST_START_CAPITAL * 100,
              "brut_getiri": None, "maliyet_puan": 0.0, "token_degisim": 0.0,
              "max_dusus": teb._max_drawdown([bh_coin * f for f in kapanislar]), "islem": 0,
              "toplam_maliyet_usdt": 0.0, "turnover_usdt": 0.0}

    # HYBRID v1 / v3-C referans (DEGISTIRILMEDEN, sadece baseline satiri icin).
    # NOT: HYBRID kendi ayri veri seklini (HybridSeries) gerektirdigi icin
    # ATAE'nin AtaeSeries'i ile DOGRUDAN paylasilamaz - ayri bir fetch ile
    # (backtest.py'nin cache'i sayesinde EKSTRA AG ISTEGI YOK, sadece ayri
    # bir HybridSeries insasi) referans satirlari ekleniyor.
    import hybrid_v2_backtest as v2b_local
    hyb_shib, hyb_majors, hyb_zamanlar, hyb_kapanislar = hb._fetch_hybrid_all(days)
    v1_sonuc = hb.simulate(hyb_shib, hyb_majors, hyb_zamanlar, hyb_kapanislar)
    v1_sonuc_brut = hb.simulate(hyb_shib, hyb_majors, hyb_zamanlar, hyb_kapanislar, cost_percent=0)
    v1_row = hb._ozet("HYBRID v1 (baseline)", v1_sonuc, v1_sonuc_brut, hyb_zamanlar, hyb_kapanislar)
    v1_row["maliyet_puan"] = v1_row["brut_getiri"] - v1_row["net_getiri"]
    v1_row["toplam_maliyet_usdt"] = None
    v1_row["turnover_usdt"] = None

    early_series = v2b_local._fetch_early_series(days)
    v3c_sonuc = v3b.simulate_v3(hyb_shib, hyb_majors, early_series, hyb_zamanlar, hyb_kapanislar,
                                 v1_sonuc["hysteresis_gecmisi"], use_persistence=True, use_asymmetric_exit=True)
    v3c_sonuc_brut = v3b.simulate_v3(hyb_shib, hyb_majors, early_series, hyb_zamanlar, hyb_kapanislar,
                                      v1_sonuc["hysteresis_gecmisi"], use_persistence=True, use_asymmetric_exit=True, cost_percent=0)
    v3c_row = hb._ozet("HYBRID v3-C (baseline)", v3c_sonuc, v3c_sonuc_brut, hyb_zamanlar, hyb_kapanislar)
    v3c_row["maliyet_puan"] = v3c_row["brut_getiri"] - v3c_row["net_getiri"]
    v3c_row["toplam_maliyet_usdt"] = None
    v3c_row["turnover_usdt"] = None

    satirlar = [grid_row, bh_row, v1_row, v3c_row]

    for isim, params in ATAE_VARIANTS.items():
        sonuc = simulate_atae(shib_series, majors_series, zamanlar, kapanislar, ATAE_PROFILE, **params)
        sonuc_brut = simulate_atae(shib_series, majors_series, zamanlar, kapanislar, ATAE_PROFILE, cost_percent=0, **params)
        row = _ozet_atae(isim, sonuc, sonuc_brut, zamanlar, kapanislar, events)
        satirlar.append(row)

        episodes = _build_episodes(sonuc["state_gecmisi"], zamanlar, kapanislar, sonuc["trades"])
        fp = ad.false_positive_stats(episodes)
        print(f"False-positive: count={fp['false_positive_count']} cost={fp['false_positive_cost']:.2f} "
              f"turnover={fp['false_positive_turnover']:.2f} avgMAE={fp['false_positive_mae']} avgSure(sa)={fp['false_positive_duration_h']}")

        # STATE KALITE RAPORU (madde 27)
        by_state = {}
        for ep in episodes:
            by_state.setdefault(ep["state"], []).append(ep)
        for state, grup in by_state.items():
            sureler = [e["duration_h"] for e in grup]
            mfeler = [e["mfe_pct"] for e in grup]
            tum_state_kalite_satirlari.append({
                "window_days": days, "system": isim, "state": state, "episode_count": len(grup),
                "median_duration_h": round(statistics.median(sureler), 2),
                "avg_mfe_pct": round(statistics.mean(mfeler), 2), "median_mfe_pct": round(statistics.median(mfeler), 2),
                "useful_pct": round(sum(1 for e in grup if e["useful"]) / len(grup) * 100, 1),
                "total_cost": round(sum(e["cost"] for e in grup), 4), "total_turnover": round(sum(e["turnover"] for e in grup), 2),
            })

        # TRANSITION MATRIX (madde 28)
        sg = sonuc["state_gecmisi"]
        for k in range(1, len(sg)):
            if sg[k][2] != sg[k - 1][2]:
                tum_transition_satirlari.append({
                    "window_days": days, "system": isim, "from_state": sg[k - 1][2], "to_state": sg[k][2],
                    "at_time": time.strftime("%Y-%m-%d %H:%M", time.localtime(sg[k][1] / 1000)),
                })

        # STRONG EVENT ANALIZI (madde 19, 30)
        for event_id, (start_idx, end_idx, yon, degisim) in enumerate(events):
            event_satirlari = _event_report(days, isim, event_id, start_idx, end_idx, yon, degisim,
                                             zamanlar, sonuc["shib_pct_gecmisi"], sonuc["state_gecmisi"], sonuc["trades"])
            tum_event_satirlari.append(event_satirlari)

    _master_tablo(satirlar)

    # WALK-FORWARD (madde 24) - SADECE ATAE-C icin (en zengin mimari)
    print(f"\n--- {days} GUN WALK-FORWARD (erken/orta/son 1/3, PARAMETRE DEGISMEDI) ---")
    atae_c_sonuc = simulate_atae(shib_series, majors_series, zamanlar, kapanislar, ATAE_PROFILE, **ATAE_VARIANTS["ATAE-C (+asymmetric exit+Recovery/Distribution)"])
    for parca in ad.walk_forward_split(zamanlar, atae_c_sonuc["equity_egrisi"], n_parca=3):
        print(f"  Parca {parca['parca']}: NET {parca['net_getiri']:+.2f}%")

    return satirlar


def _event_report(days, system, event_id, start_idx, end_idx, yon, degisim, zamanlar, shib_pct_gecmisi, state_gecmisi, trades):
    event_start_ms = zamanlar[start_idx]
    n = len(shib_pct_gecmisi)

    def pct_at(offset_h):
        j = start_idx + int(offset_h * 4)
        return shib_pct_gecmisi[j] if 0 <= j < n else None

    j24 = start_idx + 96
    pencere24 = shib_pct_gecmisi[start_idx:min(j24 + 1, n)]
    max_shib_24h = max(pencere24) if pencere24 else None
    min_usdt_24h = (100 - min(pencere24)) if pencere24 else None

    def time_to(esik, yon_yukari=True):
        for j in range(start_idx, min(j24 + 1, n)):
            deger = shib_pct_gecmisi[j] if yon_yukari else (100 - shib_pct_gecmisi[j])
            if deger >= esik:
                return (zamanlar[j] - event_start_ms) / 3_600_000
        return None

    hedef_bull = {"BULL_EARLY", "BULL", "STRONG_BULL"}
    hedef_bear = {"BEAR", "STRONG_BEAR"}
    first_bull_ms = next((r[1] for r in state_gecmisi if r[1] >= event_start_ms and r[2] in hedef_bull), None)
    first_bear_ms = next((r[1] for r in state_gecmisi if r[1] >= event_start_ms and r[2] in hedef_bear), None)
    state_at_start = next((r[2] for r in state_gecmisi if r[1] <= event_start_ms), state_gecmisi[0][2] if state_gecmisi else None)

    def state_at(offset_h):
        hedef_ts = event_start_ms + offset_h * 3_600_000
        secilen = None
        for r in state_gecmisi:
            if r[1] <= hedef_ts:
                secilen = r
            else:
                break
        return secilen[2] if secilen else None

    pencere_trades = [t for t in trades if event_start_ms <= t["ts_ms"] <= event_start_ms + 24 * 3_600_000]
    cost = sum(t["usdt_tutari"] * (TRADING_FEE_PERCENT + SLIPPAGE_PERCENT) / 100 for t in pencere_trades)
    turnover = sum(t["usdt_tutari"] for t in pencere_trades)

    if yon == "YUKARI":
        siniflandirma = ad.classify_capture(max_shib_24h, time_to(70, True))
    else:
        siniflandirma = ad.classify_capture_down(min_usdt_24h, time_to(70, False))

    return {
        "window_days": days, "system": system, "event_id": event_id, "direction": yon,
        "start": time.strftime("%Y-%m-%d %H:%M", time.localtime(event_start_ms / 1000)),
        "threshold_cross": time.strftime("%Y-%m-%d %H:%M", time.localtime(zamanlar[end_idx] / 1000)),
        "end": time.strftime("%Y-%m-%d %H:%M", time.localtime(zamanlar[end_idx] / 1000)), "move_pct": round(degisim, 2),
        "state_at_start": state_at_start, "state_6h": state_at(6), "state_12h": state_at(12), "state_24h": state_at(24),
        "SHIB_at_start": round(shib_pct_gecmisi[start_idx], 1), "SHIB_6h": pct_at(6) and round(pct_at(6), 1),
        "SHIB_12h": pct_at(12) and round(pct_at(12), 1), "SHIB_24h": pct_at(24) and round(pct_at(24), 1),
        "first_bull_state_time": time.strftime("%Y-%m-%d %H:%M", time.localtime(first_bull_ms / 1000)) if first_bull_ms else None,
        "first_bear_state_time": time.strftime("%Y-%m-%d %H:%M", time.localtime(first_bear_ms / 1000)) if first_bear_ms else None,
        "time_to_60": time_to(60, yon == "YUKARI"), "time_to_70": time_to(70, yon == "YUKARI"),
        "time_to_80": time_to(80, yon == "YUKARI"), "time_to_90": time_to(90, yon == "YUKARI"),
        "cost_during_event": round(cost, 4), "turnover_during_event": round(turnover, 2),
        "classification": siniflandirma,
    }


def _master_tablo(satirlar):
    genislik = 130
    print(f"\n{'=' * genislik}\nMASTER KARSILASTIRMA (ayni pencere, ayni baslangic sermayesi)\n{'=' * genislik}")
    print(f"{'Sistem':<48} {'NET%':>8} {'BRUT%':>8} {'maliyet.p':>10} {'Token%':>8} {'DD%':>8} {'Islem':>6}")
    print("-" * genislik)
    for s in satirlar:
        def f(v):
            return f"{v:.2f}" if v is not None else "n/a"
        print(f"{s['sistem']:<48} {f(s['net_getiri']):>8} {f(s['brut_getiri']):>8} {f(s.get('maliyet_puan')):>10} "
              f"{f(s['token_degisim']):>8} {f(s['max_dusus']):>8} {s['islem']:>6}")
    print("=" * genislik)


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    tum_sonuclar = {}
    tum_event_satirlari, tum_transition_satirlari, tum_state_kalite_satirlari = [], [], []
    for gun in ATAE_BACKTEST_DAYS_LIST:
        satirlar = run_for_days(gun, tum_event_satirlari, tum_transition_satirlari, tum_state_kalite_satirlari)
        tum_sonuclar[gun] = {s["sistem"]: s for s in satirlar}

    for isim, satirlar_list, alanlar in (
        ("events.csv", tum_event_satirlari, None), ("transitions.csv", tum_transition_satirlari, None),
        ("state_quality.csv", tum_state_kalite_satirlari, None),
    ):
        if satirlar_list:
            yol = os.path.join(OUTPUT_DIR, isim)
            with open(yol, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(satirlar_list[0].keys()))
                writer.writeheader()
                writer.writerows(satirlar_list)
            print(f"[ATAE] Yazildi: {yol} ({len(satirlar_list)} satir)")

    if len(tum_sonuclar) > 1:
        print(f"\n{'#' * 78}\nCAPRAZ-PENCERE NET GETIRI TABLOSU\n{'#' * 78}")
        sistemler = ["GRID", "BUY_AND_HOLD", "HYBRID v1 (baseline)", "HYBRID v3-C (baseline)"] + list(ATAE_VARIANTS.keys())
        baslik = f"{'SISTEM':<48}" + "".join(f"{str(g) + 'g NET%':>12}" for g in ATAE_BACKTEST_DAYS_LIST)
        print(baslik)
        print("-" * len(baslik))
        for isim in sistemler:
            satir = f"{isim:<48}"
            for gun in ATAE_BACKTEST_DAYS_LIST:
                v = tum_sonuclar[gun].get(isim, {}).get("net_getiri")
                satir += f"{(f'{v:+.1f}' if v is not None else 'n/a'):>12}"
            print(satir)


if __name__ == "__main__":
    main()
