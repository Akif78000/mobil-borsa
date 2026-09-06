"""
ATBE ALPHA AUDIT - PARITY dogrulandiktan SONRA calisan katman (kullanicinin
audit mandate'i madde 6-9). Booster'in KENDI (core'dan bagimsiz) alfa'sini,
RISK_ON/RISK_OFF siniflandirmasinin gercekten faydali olup olmadigini, ve
hangi ladder durumunun P&L'i bozdugunu/urettigini olcer. TAMAMEN POST-HOC/
diagnostic - hicbir fonksiyon KARAR VERMEZ, sadece atbe_engine.py'nin
GERCEK (degistirilmemis) state machine'ini FARKLI allowed-state kisitlariyla
yeniden calistirir (bu bir "yeni threshold" DEGIL - zaten var olan BOOSTER_
TARGET tablosunun hangi satirlarinin AKTIF oldugunu degistiren bir ablation,
madde 12'nin acik istegi: "Pozitif olmayan state sermaye kullanmasin").

FUTURE-RETURN KURALI (kullanicinin acik siniri, madde 6 sonu): forward
return hesaplari (3h/6h/12h/24h/48h) SADECE bu dosyadaki POST-HOC
diagnostic fonksiyonlarda kullanilir - atbe_engine.py'nin KARAR ureten
step()/htf_risk_filter() fonksiyonlarina ASLA parametre olarak verilmez,
ASLA cagrilmazlar (bu dosya o fonksiyonlari import bile etmez, sadece
zaten uretilmis htf_regime_gecmisi/ladder_gecmisi ciktisini okur).
"""

import csv
import os
import statistics

import atbe_engine as abe
import atbe_allocation as aa
import atbe_backtest as atbb
import atbe_diagnostics as ad

OUTPUT_DIR = os.environ.get("ATBE_ALPHA_OUTPUT_DIR", "atbe_alpha_reports")


def booster_standalone(shib_series, majors_series, zamanlar, kapanislar, cost_percent=None):
    """madde 7: booster TAMAMEN bagimsiz, core_pct=0 (core sermayesi 0 -
    hb._grid_sleeve_step'in kendi tetikleyicileri hicbir zaman ates almaz,
    coordinator'a bildirecek bir CORE islemi hic olusmaz - booster'in SAF
    kendi alfasi)."""
    return atbb.simulate_atbe(shib_series, majors_series, zamanlar, kapanislar, core_pct=0.0, cost_percent=cost_percent)


def _ozet_booster_only(etiket, sonuc, sonuc_brut, baslangic_sermaye):
    net = (sonuc["equity_egrisi"][-1] - baslangic_sermaye) / baslangic_sermaye * 100
    brut = (sonuc_brut["equity_egrisi"][-1] - baslangic_sermaye) / baslangic_sermaye * 100
    import trend_engine_backtest as teb
    dd = teb._max_drawdown(sonuc["equity_egrisi"])
    booster_trades = [t for t in sonuc["trades"] if t["tip"].startswith("BOOSTER_")]
    turnover = sum(t.get("usdt_tutari", 0.0) for t in booster_trades)
    episodes = ad.build_booster_episodes(sonuc["ladder_gecmisi"], sonuc["booster_equity_ticks"], [r[1] for r in sonuc["ladder_gecmisi"]], sonuc["trades"])
    fp = ad.false_positive_damage(episodes)
    print(f"\n--- {etiket} ---")
    print(f"NET={net:+.2f}%  BRUT={brut:+.2f}%  DD={dd:.2f}%  trades={len(booster_trades)}  turnover={turnover:.2f}")
    print(f"FP: count={fp['fp_count']} cost={fp['fp_cost']:.2f} turnover={fp['fp_turnover']:.2f} "
          f"realized_pnl={fp['fp_realized_pnl']:.2f} MAE={fp['fp_max_adverse_excursion']:.2f}")
    return {"sistem": etiket, "net": net, "brut": brut, "dd": dd, "trades": len(booster_trades),
            "turnover": turnover, **fp}, episodes


def state_level_expectancy(episodes):
    """madde 8: her ladder state icin ayri trade_count/turnover/gross_pnl/
    fees/net_pnl/win_rate/avg_trade/median_trade/MAE/MFE tablosu - VERI
    MADENCILIGI/CURVE-FIT DEGIL, zaten var olan 8 durumun POST-HOC
    performans dokumu."""
    by_state = {}
    for e in episodes:
        by_state.setdefault(e["state"], []).append(e)
    tablo = []
    for state, grup in by_state.items():
        pnl_list = [e["realized_pnl_usdt"] for e in grup]
        kazanan = [p for p in pnl_list if p > 0]
        tablo.append({
            "state": state, "episode_count": len(grup),
            "turnover": round(sum(e["turnover"] for e in grup), 2),
            "gross_pnl": round(sum(e["realized_pnl_usdt"] + e["cost"] for e in grup), 4),
            "fees": round(sum(e["cost"] for e in grup), 4),
            "net_pnl": round(sum(pnl_list), 4),
            "win_rate_pct": round(len(kazanan) / len(grup) * 100, 1) if grup else None,
            "avg_trade": round(statistics.mean(pnl_list), 4) if pnl_list else None,
            "median_trade": round(statistics.median(pnl_list), 4) if pnl_list else None,
            "avg_mae_usdt": round(statistics.mean([e["mae_usdt"] for e in grup]), 4),
            "avg_mfe_usdt": round(statistics.mean([e["mfe_usdt"] for e in grup]), 4),
        })
    return sorted(tablo, key=lambda r: -r["net_pnl"] if r["net_pnl"] is not None else 0)


def counterfactual_state_filter(shib_series, majors_series, zamanlar, kapanislar, allowed_states, cost_percent=None):
    """madde 8: 'sadece X state sermaye kullansin' karsi-olgusu - ZATEN var
    olan BOOSTER_TARGET tablosunun DISLANAN durumlarini GECICI olarak 0'a
    cekip AYNI (degistirilmemis) state machine'i yeniden calistirir - yeni
    esik/indicator/allocation-yuzdesi UYDURULMUYOR, sadece hangi mevcut
    satirin AKTIF oldugu degisiyor (restore ile geri alinir)."""
    onceki = dict(abe.BOOSTER_TARGET)
    try:
        for s in list(abe.BOOSTER_TARGET.keys()):
            if s not in allowed_states:
                abe.BOOSTER_TARGET[s] = 0.0
        return atbb.simulate_atbe(shib_series, majors_series, zamanlar, kapanislar, core_pct=0.0, cost_percent=cost_percent)
    finally:
        abe.BOOSTER_TARGET.clear()
        abe.BOOSTER_TARGET.update(onceki)


def risk_on_episode_audit(zamanlar, kapanislar, majors_series, htf_regime_gecmisi, ladder_gecmisi,
                           core_equity_ticks, booster_equity_ticks):
    """madde 6: her RISK_ON/RISK_OFF episode'u icin start/end/duration/SHIB
    return/BTC-ETH-BNB return/core pnl/booster pnl/MFE/MAE/giris-cikis
    ladder state + forward-return DIAGNOSTIGI (3h/6h/12h/24h/48h medyan) -
    forward return SADECE bu tabloda, KARARA HICBIR ZAMAN girmez."""
    if not htf_regime_gecmisi:
        return [], {}
    n = len(kapanislar)

    def fiyat_at_ms(ts_ms):
        # zamanlar SIRALI - basit ikili arama yerine dogrusal yakin-index (kucuk n, tick sayisi kadar)
        lo, hi = 0, len(zamanlar) - 1
        if ts_ms <= zamanlar[0]:
            return kapanislar[0]
        if ts_ms >= zamanlar[-1]:
            return kapanislar[-1]
        while lo < hi:
            mid = (lo + hi) // 2
            if zamanlar[mid] < ts_ms:
                lo = mid + 1
            else:
                hi = mid
        return kapanislar[lo]

    def major_return(sym, t0, t1):
        tf_map = majors_series.get(sym, {})
        ts1h = tf_map.get("1h")
        if ts1h is None:
            return None
        i0, i1 = ts1h.index_at(t0), ts1h.index_at(t1)
        if i0 is None or i1 is None or ts1h.closes[i0] == 0:
            return None
        return (ts1h.closes[i1] - ts1h.closes[i0]) / ts1h.closes[i0] * 100

    episodes = []
    onceki_regime, onceki_idx = htf_regime_gecmisi[0][2], 0
    ladder_by_idx = {r[0]: r[2] for r in ladder_gecmisi}
    for k in range(1, len(htf_regime_gecmisi)):
        idx, ts, regime = htf_regime_gecmisi[k]
        if regime != onceki_regime:
            episodes.append(_tek_risk_episode(onceki_regime, onceki_idx, idx, htf_regime_gecmisi, ladder_by_idx,
                                               core_equity_ticks, booster_equity_ticks, fiyat_at_ms, major_return, zamanlar))
            onceki_regime, onceki_idx = regime, idx
    episodes.append(_tek_risk_episode(onceki_regime, onceki_idx, htf_regime_gecmisi[-1][0], htf_regime_gecmisi, ladder_by_idx,
                                       core_equity_ticks, booster_equity_ticks, fiyat_at_ms, major_return, zamanlar))

    risk_on_epler = [e for e in episodes if e["regime"] == "RISK_ON"]
    forward_saatler = [3, 6, 12, 24, 48]
    forward_tablo = {}
    for h in forward_saatler:
        degerler = []
        for e in risk_on_epler:
            p0 = fiyat_at_ms(e["start_ms"])
            p1 = fiyat_at_ms(e["start_ms"] + h * 3_600_000)
            if p0:
                degerler.append((p1 - p0) / p0 * 100)
        forward_tablo[h] = round(statistics.median(degerler), 3) if degerler else None

    return episodes, forward_tablo


def _tek_risk_episode(regime, start_idx, end_idx, htf_regime_gecmisi, ladder_by_idx,
                       core_equity_ticks, booster_equity_ticks, fiyat_at_ms, major_return, zamanlar):
    start_ms = htf_regime_gecmisi[start_idx][1]
    end_ms = htf_regime_gecmisi[min(end_idx, len(htf_regime_gecmisi) - 1)][1]
    p0, p1 = fiyat_at_ms(start_ms), fiyat_at_ms(end_ms)
    shib_return = (p1 - p0) / p0 * 100 if p0 else None
    core_pnl = core_equity_ticks[min(end_idx, len(core_equity_ticks) - 1)] - core_equity_ticks[start_idx]
    booster_pnl = booster_equity_ticks[min(end_idx, len(booster_equity_ticks) - 1)] - booster_equity_ticks[start_idx]
    pencere = [fiyat_at_ms(htf_regime_gecmisi[j][1]) for j in range(start_idx, min(end_idx + 1, len(htf_regime_gecmisi)))]
    mfe = (max(pencere) - p0) / p0 * 100 if pencere and p0 else None
    mae = (min(pencere) - p0) / p0 * 100 if pencere and p0 else None
    return {
        "regime": regime, "start_ms": start_ms, "end_ms": end_ms,
        "duration_h": (end_ms - start_ms) / 3_600_000,
        "shib_return_pct": round(shib_return, 3) if shib_return is not None else None,
        "btc_return_pct": _r(major_return("BTCUSDT", start_ms, end_ms)),
        "eth_return_pct": _r(major_return("ETHUSDT", start_ms, end_ms)),
        "bnb_return_pct": _r(major_return("BNBUSDT", start_ms, end_ms)),
        "core_pnl": round(core_pnl, 4), "booster_pnl": round(booster_pnl, 4),
        "mfe_pct": _r(mfe), "mae_pct": _r(mae),
        "state_entering": ladder_by_idx.get(start_idx), "state_leaving": ladder_by_idx.get(end_idx),
    }


def _r(x):
    return round(x, 3) if x is not None else None


def _yaz_csv(satirlar, ad_):
    if not satirlar:
        return
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    yol = os.path.join(OUTPUT_DIR, ad_)
    with open(yol, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(satirlar[0].keys()))
        writer.writeheader()
        writer.writerows(satirlar)
    print(f"[ALPHA-AUDIT] Yazildi: {yol} ({len(satirlar)} satir)")


def run_full_audit(days, atb_module, atbb_module):
    print(f"\n{'#' * 78}\nATBE ALPHA AUDIT (madde 6-9) - son {days} gun\n{'#' * 78}")
    shib_series, majors_series, zamanlar, kapanislar = atb_module._fetch_all(days)

    baslangic = atbb_module.BACKTEST_START_CAPITAL
    print("\n[1/4] BOOSTER STANDALONE ALPHA (madde 7) - core=0, booster=1000 USDT")
    sonuc = booster_standalone(shib_series, majors_series, zamanlar, kapanislar)
    sonuc_brut = booster_standalone(shib_series, majors_series, zamanlar, kapanislar, cost_percent=0)
    ozet, episodes = _ozet_booster_only("BOOSTER STANDALONE", sonuc, sonuc_brut, baslangic)

    print("\n[2/4] STATE-LEVEL EXPECTANCY TABLOSU (madde 8)")
    tablo = state_level_expectancy(episodes)
    print(f"{'STATE':<18}{'n':>5}{'turnover':>12}{'net_pnl':>12}{'win%':>8}{'avg':>10}{'median':>10}{'MAE':>8}{'MFE':>8}")
    for r in tablo:
        print(f"{r['state']:<18}{r['episode_count']:>5}{r['turnover']:>12.2f}{r['net_pnl']:>12.4f}"
              f"{(r['win_rate_pct'] or 0):>8.1f}{(r['avg_trade'] or 0):>10.4f}{(r['median_trade'] or 0):>10.4f}"
              f"{r['avg_mae_usdt']:>8.3f}{r['avg_mfe_usdt']:>8.3f}")
    _yaz_csv(tablo, "state_level_expectancy.csv")
    pozitif_stateler = [r["state"] for r in tablo if (r["net_pnl"] or 0) > 0]
    print(f"\nPOZITIF net_pnl ureten stateler: {pozitif_stateler or '(HICBIRI)'}")

    print("\n[3/4] COUNTERFACTUAL KARSILASTIRMA (madde 8)")
    bh_coin = baslangic / kapanislar[0]
    bh_net = (bh_coin * kapanislar[-1] - baslangic) / baslangic * 100
    print(f"  BUY_AND_HOLD (booster sleeve)     : NET {bh_net:+.2f}%")
    print(f"  HIC TRADE YOK (nakit)             : NET  +0.00%")
    print(f"  GERCEK booster (tum stateler)     : NET {ozet['net']:+.2f}%")
    for aday_isim, aday_stateler in (
        ("SADECE CONFIRMED_BULL+STRONG_BULL", {"CONFIRMED_BULL", "STRONG_BULL"}),
        ("SADECE STRONG_BULL", {"STRONG_BULL"}),
        ("SADECE POZITIF net_pnl stateler", set(pozitif_stateler) or {"__NONE__"}),
    ):
        cf_sonuc = counterfactual_state_filter(shib_series, majors_series, zamanlar, kapanislar, aday_stateler)
        cf_net = (cf_sonuc["equity_egrisi"][-1] - baslangic) / baslangic * 100
        print(f"  {aday_isim:<34}: NET {cf_net:+.2f}%")

    print("\n[4/4] RISK_ON / RISK_OFF EPISODE AUDIT + FORWARD RETURN DIAGNOSTIGI (madde 6)")
    tam_sonuc = booster_standalone(shib_series, majors_series, zamanlar, kapanislar)
    risk_episodes, forward_tablo = risk_on_episode_audit(
        zamanlar, kapanislar, majors_series, tam_sonuc["htf_regime_gecmisi"], tam_sonuc["ladder_gecmisi"],
        tam_sonuc["core_equity_ticks"], tam_sonuc["booster_equity_ticks"])
    _yaz_csv(risk_episodes, "risk_on_off_episodes.csv")
    print(f"Toplam RISK_ON episode: {sum(1 for e in risk_episodes if e['regime'] == 'RISK_ON')}, "
          f"RISK_OFF: {sum(1 for e in risk_episodes if e['regime'] == 'RISK_OFF')}")
    print("RISK_ON baslangicindan itibaren MEDYAN forward SHIB getirisi (SADECE diagnostic label, karara girmez):")
    for h, v in forward_tablo.items():
        print(f"  +{h}h: {v if v is not None else 'n/a'}%")
    if forward_tablo.get(24) is not None and forward_tablo[24] <= 0:
        print("YORUM: RISK_ON baslangicindan sonraki 24h medyan getiri <=0 - classifier YUKSELISI ONCEDEN "
              "OGRETMIYOR OLABILIR (zaten olmus hareketi TEYIT ediyor gibi gorunuyor). Kesin hukum icin n (episode sayisi) yeterli mi kontrol edin.")
    else:
        print("YORUM: RISK_ON sonrasi medyan forward getiri pozitif - classifier en azindan post-hoc olarak yon ile TUTARLI.")

    return {"booster_standalone": ozet, "state_table": tablo, "risk_episodes": risk_episodes, "forward_tablo": forward_tablo}


if __name__ == "__main__":
    import os as _os
    import atae_backtest as atb
    import atbe_backtest as atbb
    _os.makedirs(OUTPUT_DIR, exist_ok=True)
    gun = int(_os.environ.get("ATBE_ALPHA_DAYS", "180"))
    run_full_audit(gun, atb, atbb)
