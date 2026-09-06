"""
ATBE DIAGNOSTICS - salt olcum/siniflandirma (KARAR VERMEZ, atbe_backtest.py
simulate() TAMAMLANDIKTAN SONRA cagrilan POST-HOC katman - madde 0/15/16
metodoloji ayrimi burada da korunur). atae_diagnostics.py'nin walk_forward_split
ve classify_capture/classify_capture_down fonksiyonlari DEGISTIRILMEDEN
tekrar kullanilir (import) - ayni olcum mantigi iki kez yazilmaz.

REGIME ATTRIBUTION (madde 11): regime etiketi CAUSAL olmali - burada
atbe_engine.htf_risk_filter()'in HER TIK'TE ureteceginiz RISK_ON/NEUTRAL/
RISK_OFF ciktisi (backtest sirasinda zaten hesaplanan, gelecek veri
KULLANMAYAN bir sinyal) regime kovasi olarak kullanilir - trend_engine_
backtest._big_move_events (GELECEGE bakarak "buyuk hareket" bulan, SADECE
teshis amacli mevcut fonksiyon) SADECE ikincil/post-hoc capraz-kontrol
tablosunda, "bu regime etiketi gercekten buyuk hareketlerle ortusuyor mu"
sorusunu cevaplamak icin kullanilir - HICBIR ZAMAN birincil attribution
kovasi olarak degil (madde 11 sonu: "future data sadece sonradan diagnostic
labeling icin kullanilabilir").

FALSE POSITIVE DAMAGE (madde 9): sadece SAYIM yerine, her false-positive
booster episode'u icin COST/TURNOVER/REALIZED PNL (booster'in KENDI USDT
degerinden, fiyattan degil) /MAX ADVERSE EXCURSION (booster equity'sinin
episode SURESINCE giristen en kotu sapmasi, USDT bazinda) raporlanir.
"""

import atae_diagnostics as ad  # walk_forward_split / classify_capture(_down) reuse - DEGISTIRILMEDI

walk_forward_split = ad.walk_forward_split
classify_capture = ad.classify_capture
classify_capture_down = ad.classify_capture_down

USEFUL_MOVE_THRESHOLD_PCT = 1.0  # ad.classify_episode_outcome ile AYNI kucuk sabit esik (reused)


def build_booster_episodes(ladder_gecmisi, booster_equity_egrisi, zamanlar, trades):
    """ladder_gecmisi: (idx, ts_ms, ladder_state) listesi. booster_equity_egrisi:
    SADECE booster sleeve'inin (core GRID DAHIL DEGIL) kendi USDT+coin*fiyat
    degeri, ayni index uzeriden. Her ladder-state suresi bir 'episode'."""
    if not ladder_gecmisi:
        return []
    episodes = []
    onceki_idx, onceki_state, onceki_ts = ladder_gecmisi[0][0], ladder_gecmisi[0][2], ladder_gecmisi[0][1]
    for k in range(1, len(ladder_gecmisi)):
        idx, ts, state = ladder_gecmisi[k][0], ladder_gecmisi[k][1], ladder_gecmisi[k][2]
        if state != onceki_state:
            episodes.append(_tek_booster_episode(onceki_state, onceki_idx, idx, onceki_ts, ts, booster_equity_egrisi, trades))
            onceki_idx, onceki_state, onceki_ts = idx, state, ts
    son_idx = ladder_gecmisi[-1][0]
    episodes.append(_tek_booster_episode(onceki_state, onceki_idx, son_idx, onceki_ts, zamanlar[son_idx],
                                          booster_equity_egrisi, trades))
    return episodes


def _tek_booster_episode(state, start_idx, end_idx, start_ts, end_ts, booster_equity, trades):
    giris_deger = booster_equity[start_idx] if start_idx < len(booster_equity) else booster_equity[-1]
    pencere = booster_equity[start_idx:end_idx + 1] or [giris_deger]
    bitis_deger = pencere[-1]
    realized_pnl = bitis_deger - giris_deger
    realized_pnl_pct = (realized_pnl / giris_deger * 100) if giris_deger else 0.0
    mae_usdt = min((v - giris_deger) for v in pencere) if giris_deger else 0.0
    mfe_usdt = max((v - giris_deger) for v in pencere) if giris_deger else 0.0

    episode_trades = [t for t in trades if start_ts <= t.get("ts_ms", -1) < end_ts]
    cost = sum(t.get("fee", 0.0) for t in episode_trades)
    turnover = sum(t.get("usdt_tutari", 0.0) for t in episode_trades)

    useful = (mfe_usdt / giris_deger * 100 if giris_deger else 0.0) >= USEFUL_MOVE_THRESHOLD_PCT if state != "NEUTRAL" else True
    return {
        "state": state, "start_time": start_ts, "end_time": end_ts,
        "duration_h": (end_ts - start_ts) / 3_600_000,
        "realized_pnl_usdt": round(realized_pnl, 4), "realized_pnl_pct": round(realized_pnl_pct, 3),
        "mae_usdt": round(mae_usdt, 4), "mfe_usdt": round(mfe_usdt, 4),
        "cost": round(cost, 4), "turnover": round(turnover, 2),
        "trade_count": len(episode_trades), "useful": useful,
    }


def false_positive_damage(episodes):
    """madde 9: sadece count DEGIL, FP_COST/FP_TURNOVER/FP_REALIZED_PNL/
    FP_MAX_ADVERSE_EXCURSION."""
    yanlislar = [e for e in episodes if not e.get("useful", True)]
    if not yanlislar:
        return {"fp_count": 0, "fp_cost": 0.0, "fp_turnover": 0.0,
                "fp_realized_pnl": 0.0, "fp_max_adverse_excursion": 0.0}
    return {
        "fp_count": len(yanlislar),
        "fp_cost": round(sum(e["cost"] for e in yanlislar), 4),
        "fp_turnover": round(sum(e["turnover"] for e in yanlislar), 2),
        "fp_realized_pnl": round(sum(e["realized_pnl_usdt"] for e in yanlislar), 4),
        "fp_max_adverse_excursion": round(min(e["mae_usdt"] for e in yanlislar), 4),
    }


def regime_attribution(zamanlar, htf_regime_gecmisi, core_equity, booster_equity):
    """CAUSAL regime (RISK_ON/NEUTRAL/RISK_OFF, htf_risk_filter'dan) basina
    GRID/BOOSTER/TOTAL PnL + turnover + DD (madde 11). htf_regime_gecmisi:
    (idx, ts_ms, regime) listesi, backtest SIRASINDA (gelecek veri
    kullanmadan) uretilmis - post-hoc bir etiketleme DEGIL."""
    if not htf_regime_gecmisi:
        return []
    by_regime = {}
    for k in range(len(htf_regime_gecmisi)):
        idx, _ts, regime = htf_regime_gecmisi[k]
        bitis_idx = htf_regime_gecmisi[k + 1][0] if k + 1 < len(htf_regime_gecmisi) else len(core_equity) - 1
        by_regime.setdefault(regime, []).append((idx, bitis_idx))

    sonuc = []
    for regime, araliklar in by_regime.items():
        core_pnl = sum(core_equity[min(b, len(core_equity) - 1)] - core_equity[a] for a, b in araliklar)
        booster_pnl = sum(booster_equity[min(b, len(booster_equity) - 1)] - booster_equity[a] for a, b in araliklar)
        tik_sayisi = sum(b - a for a, b in araliklar)
        sonuc.append({
            "regime": regime, "tick_count": tik_sayisi,
            "core_pnl_usdt": round(core_pnl, 2), "booster_pnl_usdt": round(booster_pnl, 2),
            "total_pnl_usdt": round(core_pnl + booster_pnl, 2),
        })
    return sonuc
