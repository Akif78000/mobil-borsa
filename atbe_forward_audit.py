"""
ATBE FORWARD/CAUSAL-LAG AUDIT (madde 1-7, kullanicinin acik talimati:
"STATE PROGRESSION'IN NEDEN TERS EXPECTANCY URETTIGINI KANITLA - once bu
audit'i yap, YENI STRATEJI/THRESHOLD/ALLOCATION denemesi YAPMA").

TAMAMEN POST-HOC/diagnostic - hicbir fonksiyon KARAR VERMEZ, atbe_engine.py'ye
YAZMAZ. Forward return / gelecek fiyat verisi SADECE bu dosyadaki raporlama
fonksiyonlarinda kullanilir (madde 1 sonu: "State kararinda kesinlikle
kullanilmayacak").

DEDUP KURALI (madde 1 sonu): "state entry event" = SADECE GERCEK transition
(onceki tik'te FARKLI bir state'teyken bu tik'te BU state'e GECIS) - ayni
state icinde her saatlik kontrol tik'i AYRI bir event SAYILMAZ.
"""

import csv
import os
import statistics

import regime_indicators as ri
from backtest import fetch_history, INTERVAL_MS
import atbe_engine as abe
import atbe_diagnostics as ad

OUTPUT_DIR = os.environ.get("ATBE_FORWARD_AUDIT_DIR", "atbe_forward_audit_reports")
FORWARD_HOURS = [1, 3, 6, 12, 24, 48, 72]
BULLISH_TIERS = abe.BULLISH_TIERS  # ("EARLY_BULL","PERSISTENT_BULL","CONFIRMED_BULL","STRONG_BULL")
BULL_ORDER = {"EARLY_BULL": 1, "PERSISTENT_BULL": 2, "CONFIRMED_BULL": 3, "STRONG_BULL": 4}


def _dedup_entries(ladder_gecmisi):
    """madde 1 sonu: SADECE gercek transition-INTO-state event'leri, her biri
    icin (onceki state'te GECIRILEN sure de dahil)."""
    if not ladder_gecmisi:
        return []
    entries = []
    onceki_idx, onceki_ts, onceki_state = ladder_gecmisi[0]
    baslangic_ts_bu_state = onceki_ts
    for k in range(1, len(ladder_gecmisi)):
        idx, ts, state = ladder_gecmisi[k]
        if state != onceki_state:
            entries.append({
                "idx": idx, "ts_ms": ts, "state": state, "prev_state": onceki_state,
                "prev_state_start_ts": baslangic_ts_bu_state,
                "prev_state_duration_h": (ts - baslangic_ts_bu_state) / 3_600_000,
            })
            baslangic_ts_bu_state = ts
        onceki_idx, onceki_ts, onceki_state = idx, ts, state
    return entries


def _fiyat_at(ts_ms, zamanlar, kapanislar):
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


def _forward_return(ts_ms, saat, zamanlar, kapanislar):
    p0 = _fiyat_at(ts_ms, zamanlar, kapanislar)
    p1 = _fiyat_at(ts_ms + saat * 3_600_000, zamanlar, kapanislar)
    return (p1 - p0) / p0 * 100 if p0 else None


def _backward_return(ts_ms, saat, zamanlar, kapanislar):
    p0 = _fiyat_at(ts_ms - saat * 3_600_000, zamanlar, kapanislar)
    p1 = _fiyat_at(ts_ms, zamanlar, kapanislar)
    return (p1 - p0) / p0 * 100 if p0 else None


def _mfe_mae(ts_ms, saat, zamanlar, kapanislar):
    p0 = _fiyat_at(ts_ms, zamanlar, kapanislar)
    j0 = 0
    lo, hi = 0, len(zamanlar) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if zamanlar[mid] < ts_ms:
            lo = mid + 1
        else:
            hi = mid
    j0 = lo
    j1 = j0
    hedef_ts = ts_ms + saat * 3_600_000
    while j1 < len(zamanlar) - 1 and zamanlar[j1] < hedef_ts:
        j1 += 1
    pencere = kapanislar[j0:j1 + 1] or [p0]
    mfe = (max(pencere) - p0) / p0 * 100 if p0 else None
    mae = (min(pencere) - p0) / p0 * 100 if p0 else None
    return mfe, mae


def _percentile(degerler, p):
    if not degerler:
        return None
    s = sorted(degerler)
    k = (len(s) - 1) * (p / 100)
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def state_forward_expectancy(entries, zamanlar, kapanislar):
    """madde 1: state_level_expectancy'deki EXECUTION-attribution net_pnl'den
    TAMAMEN AYRI - SADECE fiyatin state'e GIRERKEN, sonraki N saatte NE
    YAPTIGI (booster'in o an ne kadar allocate ettigine hic bakmadan)."""
    by_state = {}
    for e in entries:
        by_state.setdefault(e["state"], []).append(e)
    tablo = []
    for state, grup in by_state.items():
        satir = {"state": state, "n": len(grup)}
        for h in FORWARD_HOURS:
            degerler = [_forward_return(e["ts_ms"], h, zamanlar, kapanislar) for e in grup]
            degerler = [d for d in degerler if d is not None]
            satir[f"median_fwd_{h}h"] = round(statistics.median(degerler), 4) if degerler else None
            satir[f"mean_fwd_{h}h"] = round(statistics.mean(degerler), 4) if degerler else None
            satir[f"pos_rate_{h}h"] = round(sum(1 for d in degerler if d > 0) / len(degerler) * 100, 1) if degerler else None
            satir[f"p25_fwd_{h}h"] = round(_percentile(degerler, 25), 4) if degerler else None
            satir[f"p75_fwd_{h}h"] = round(_percentile(degerler, 75), 4) if degerler else None
        mfe_list, mae_list = [], []
        for e in grup:
            mfe, mae = _mfe_mae(e["ts_ms"], 24, zamanlar, kapanislar)
            if mfe is not None:
                mfe_list.append(mfe)
                mae_list.append(mae)
        satir["mfe_24h"] = round(statistics.mean(mfe_list), 4) if mfe_list else None
        satir["mae_24h"] = round(statistics.mean(mae_list), 4) if mae_list else None
        tablo.append(satir)
    return tablo


def transition_matrix(entries, zamanlar, kapanislar):
    """madde 2: FROM/TO/count/median_duration_before_transition/
    median_return_during_state/median_forward_6h_after/24h_after."""
    by_pair = {}
    for e in entries:
        key = (e["prev_state"], e["state"])
        by_pair.setdefault(key, []).append(e)
    satirlar = []
    for (frm, to), grup in by_pair.items():
        surelar = [e["prev_state_duration_h"] for e in grup]
        return_during = [_backward_return(e["ts_ms"], e["prev_state_duration_h"], zamanlar, kapanislar) for e in grup]
        return_during = [r for r in return_during if r is not None]
        fwd6 = [f for f in (_forward_return(e["ts_ms"], 6, zamanlar, kapanislar) for e in grup) if f is not None]
        fwd24 = [f for f in (_forward_return(e["ts_ms"], 24, zamanlar, kapanislar) for e in grup) if f is not None]
        satirlar.append({
            "from_state": frm, "to_state": to, "count": len(grup),
            "median_duration_before_transition_h": round(statistics.median(surelar), 2),
            "median_return_during_state_pct": round(statistics.median(return_during), 4) if return_during else None,
            "median_forward_6h_after_pct": round(statistics.median(fwd6), 4) if fwd6 else None,
            "median_forward_24h_after_pct": round(statistics.median(fwd24), 4) if fwd24 else None,
        })
    return sorted(satirlar, key=lambda r: -r["count"])


def _1h_series_lookup(shib_series):
    return shib_series.get("1h")


def _volume_ratio_lookup(days):
    candles = fetch_history(os.environ.get("SYMBOL", "SHIBUSDT"), "1h", days + 10)
    zamanlar_1h = [c[0] for c in candles]
    hacimler = [float(c[5]) for c in candles]
    oranlar = ri.volume_ratio(hacimler, 20)
    close_times_1h = [t + INTERVAL_MS["1h"] for t in zamanlar_1h]
    return close_times_1h, oranlar


def price_extension_at_entries(entries, shib_series, majors_series, zamanlar, kapanislar, days):
    """madde 3: her BULLISH_TIER entry'sinde price vs EMA/KAMA/NW, ATR-normalize
    extension, onceki 6h/12h/24h return, WT level/slope, volume expansion,
    BTC/ETH/BNB son getirisi - YENI THRESHOLD OPTIMIZE ETMEK ICIN DEGIL,
    SADECE 'confirmation mi, overextension mi' sorusuna veri saglamak icin."""
    ts1h = _1h_series_lookup(shib_series)
    if ts1h is None:
        return []
    vol_close_times, vol_oranlari = _volume_ratio_lookup(days)

    def vol_ratio_at(ts_ms):
        import bisect
        i = bisect.bisect_right(vol_close_times, ts_ms) - 1
        return vol_oranlari[i] if 0 <= i < len(vol_oranlari) else None

    def majors_return_24h(sym, ts_ms):
        tf = majors_series.get(sym, {}).get("1h")
        if tf is None:
            return None
        i1 = tf.index_at(ts_ms)
        i0 = tf.index_at(ts_ms - 24 * 3_600_000)
        if i0 is None or i1 is None or tf.closes[i0] == 0:
            return None
        return (tf.closes[i1] - tf.closes[i0]) / tf.closes[i0] * 100

    satirlar = []
    for e in entries:
        if e["state"] not in BULLISH_TIERS:
            continue
        idx = ts1h.index_at(e["ts_ms"])
        if idx is None:
            continue
        price = ts1h.closes[idx]
        ema_f, ema_s = ts1h.ema_fast[idx], ts1h.ema_slow[idx]
        kama_v = ts1h.kama_line[idx]
        nw_v = ts1h.nw[idx] if ts1h.nw else None
        atr_v = ts1h.atr[idx]
        wt_v = ts1h.wt1[idx] if ts1h.wt1 else None
        wt_prev = ts1h.wt1[idx - 2] if ts1h.wt1 and idx >= 2 else None
        satirlar.append({
            "ts_ms": e["ts_ms"], "state": e["state"], "price": price,
            "price_vs_ema_fast_pct": round((price - ema_f) / price * 100, 4) if ema_f else None,
            "price_vs_ema_slow_pct": round((price - ema_s) / price * 100, 4) if ema_s else None,
            "price_vs_kama_pct": round((price - kama_v) / price * 100, 4) if kama_v else None,
            "price_vs_nw_pct": round((price - nw_v) / price * 100, 4) if nw_v else None,
            "atr_normalized_extension": round((price - ema_f) / atr_v, 4) if (ema_f and atr_v) else None,
            "prev_return_6h": _r(_backward_return(e["ts_ms"], 6, zamanlar, kapanislar)),
            "prev_return_12h": _r(_backward_return(e["ts_ms"], 12, zamanlar, kapanislar)),
            "prev_return_24h": _r(_backward_return(e["ts_ms"], 24, zamanlar, kapanislar)),
            "wt_level": round(wt_v, 3) if wt_v is not None else None,
            "wt_slope": round(wt_v - wt_prev, 3) if (wt_v is not None and wt_prev is not None) else None,
            "volume_ratio": _r(vol_ratio_at(e["ts_ms"])),
            "btc_return_24h": _r(majors_return_24h("BTCUSDT", e["ts_ms"])),
            "eth_return_24h": _r(majors_return_24h("ETHUSDT", e["ts_ms"])),
            "bnb_return_24h": _r(majors_return_24h("BNBUSDT", e["ts_ms"])),
        })
    return satirlar


def _r(x):
    return round(x, 4) if x is not None else None


def extension_summary_by_tier(extension_rows):
    """madde 3 sonucunu ozetler: CONFIRMED_BULL/STRONG_BULL girisleri
    EARLY_BULL'a gore SISTEMATIK olarak daha 'extended' mi?"""
    by_state = {}
    for r in extension_rows:
        by_state.setdefault(r["state"], []).append(r)
    ozet = []
    for state, grup in by_state.items():
        ext = [r["atr_normalized_extension"] for r in grup if r["atr_normalized_extension"] is not None]
        prev24 = [r["prev_return_24h"] for r in grup if r["prev_return_24h"] is not None]
        ozet.append({
            "state": state, "n": len(grup),
            "median_atr_extension": round(statistics.median(ext), 3) if ext else None,
            "mean_atr_extension": round(statistics.mean(ext), 3) if ext else None,
            "median_prev_return_24h": round(statistics.median(prev24), 3) if prev24 else None,
        })
    return sorted(ozet, key=lambda r: BULL_ORDER.get(r["state"], 99))


def strong_bull_case_dump(entries, extension_rows, ladder_gecmisi, zamanlar, kapanislar):
    """madde 4: her STRONG_BULL entry'si icin TAM CSV satiri (entry + forward
    + MAE/MFE + state duration + prev/next state)."""
    ext_by_ts = {r["ts_ms"]: r for r in extension_rows}
    next_state_by_idx = {}
    for k in range(len(ladder_gecmisi) - 1):
        next_state_by_idx[ladder_gecmisi[k][0]] = ladder_gecmisi[k + 1][2]

    satirlar = []
    for e in entries:
        if e["state"] != "STRONG_BULL":
            continue
        ext = ext_by_ts.get(e["ts_ms"], {})
        # bu STRONG_BULL suresinin ne kadar surdugunu bulmak icin bir sonraki entry'ye bak
        sonraki = next((e2 for e2 in entries if e2["ts_ms"] > e["ts_ms"]), None)
        state_duration_h = ((sonraki["ts_ms"] - e["ts_ms"]) / 3_600_000) if sonraki else None
        mfe6, mae6 = _mfe_mae(e["ts_ms"], 6, zamanlar, kapanislar)
        mfe24, mae24 = _mfe_mae(e["ts_ms"], 24, zamanlar, kapanislar)
        satirlar.append({
            "entry_ts_ms": e["ts_ms"], "entry_price": ext.get("price"),
            "prev_return_24h": ext.get("prev_return_24h"), "prev_return_12h": ext.get("prev_return_12h"),
            "ema_distance_pct": ext.get("price_vs_ema_fast_pct"), "kama_distance_pct": ext.get("price_vs_kama_pct"),
            "nw_distance_pct": ext.get("price_vs_nw_pct"), "atr_extension": ext.get("atr_normalized_extension"),
            "wt_level": ext.get("wt_level"), "btc_return_24h": ext.get("btc_return_24h"),
            "eth_return_24h": ext.get("eth_return_24h"), "bnb_return_24h": ext.get("bnb_return_24h"),
            "next_6h": _forward_return(e["ts_ms"], 6, zamanlar, kapanislar),
            "next_12h": _forward_return(e["ts_ms"], 12, zamanlar, kapanislar),
            "next_24h": _forward_return(e["ts_ms"], 24, zamanlar, kapanislar),
            "next_48h": _forward_return(e["ts_ms"], 48, zamanlar, kapanislar),
            "mae_6h": mae6, "mfe_6h": mfe6, "mae_24h": mae24, "mfe_24h": mfe24,
            "state_duration_h": round(state_duration_h, 2) if state_duration_h else None,
            "prev_state": e["prev_state"], "next_state": next_state_by_idx.get(e["idx"]),
        })
    return satirlar


def bull_campaign_analysis(ladder_gecmisi, booster_equity_ticks, zamanlar):
    """madde 5/6/7 - 'confirmation lag' hipotezini DOGRUDAN test eder:
    TUM bull-tier campaign'i (EARLY_BULL'a giristen, bull tier'lardan tamamen
    CIKANA kadar) TEK birim sayilir - entry HER ZAMAN EARLY_BULL'dur (ladder
    yapisi geregi baska yerden girilemez). Her campaign icin:
      pnl_while_early_only : ladder henuz EARLY_BULL'dan ILERI GITMEDEN
                              onceki equity degisimi (varsa)
      pnl_after_advancing   : ladder EARLY_BULL'un OTESINE gectikten SONRAKI
                              equity degisimi (0 ise hic ilerlememis demektir)
    Boylece 'kazanc EARLY asamasinda mi olustu, CONFIRMED/STRONG SONRADAN mi
    geldi' sorusu (madde 2 sonu) DOGRUDAN, trade-eslestirme gerektirmeden
    olculur."""
    campaigns = []
    n = len(ladder_gecmisi)
    i = 0
    while i < n:
        idx, ts, state = ladder_gecmisi[i]
        if state == "EARLY_BULL" and (i == 0 or ladder_gecmisi[i - 1][2] not in BULLISH_TIERS):
            start_i = i
            j = i
            max_tier = "EARLY_BULL"
            ilk_ilerleme_i = None
            while j < n and ladder_gecmisi[j][2] in BULLISH_TIERS:
                if BULL_ORDER.get(ladder_gecmisi[j][2], 0) > BULL_ORDER.get(max_tier, 0):
                    max_tier = ladder_gecmisi[j][2]
                if ladder_gecmisi[j][2] != "EARLY_BULL" and ilk_ilerleme_i is None:
                    ilk_ilerleme_i = j
                j += 1
            end_i = j - 1
            eq_start = booster_equity_ticks[start_i]
            eq_end = booster_equity_ticks[min(end_i, len(booster_equity_ticks) - 1)]
            if ilk_ilerleme_i is not None:
                eq_ilerleme = booster_equity_ticks[ilk_ilerleme_i]
                pnl_early = eq_ilerleme - eq_start
                pnl_after = eq_end - eq_ilerleme
            else:
                pnl_early = eq_end - eq_start
                pnl_after = 0.0
            campaigns.append({
                "start_ts": ts, "end_ts": ladder_gecmisi[end_i][1], "max_tier_reached": max_tier,
                "duration_h": (ladder_gecmisi[end_i][1] - ts) / 3_600_000,
                "pnl_while_early_only": round(pnl_early, 4), "pnl_after_advancing": round(pnl_after, 4),
                "total_pnl": round(eq_end - eq_start, 4),
            })
            i = j
        else:
            i += 1

    by_tier = {}
    for c in campaigns:
        by_tier.setdefault(c["max_tier_reached"], []).append(c)
    ozet = []
    for tier, grup in by_tier.items():
        ozet.append({
            "max_tier_reached": tier, "campaign_count": len(grup),
            "total_pnl": round(sum(c["total_pnl"] for c in grup), 4),
            "total_pnl_while_early_only": round(sum(c["pnl_while_early_only"] for c in grup), 4),
            "total_pnl_after_advancing": round(sum(c["pnl_after_advancing"] for c in grup), 4),
            "avg_duration_h": round(statistics.mean([c["duration_h"] for c in grup]), 2),
        })
    return sorted(campaigns, key=lambda c: c["start_ts"]), sorted(ozet, key=lambda r: BULL_ORDER.get(r["max_tier_reached"], 99))


def _yaz_csv(satirlar, isim):
    if not satirlar:
        print(f"[FORWARD-AUDIT] {isim}: satir yok, CSV yazilmadi.")
        return
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    yol = os.path.join(OUTPUT_DIR, isim)
    with open(yol, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(satirlar[0].keys()))
        writer.writeheader()
        writer.writerows(satirlar)
    print(f"[FORWARD-AUDIT] Yazildi: {yol} ({len(satirlar)} satir)")


def run(days, atb_module, atbb_module, aud_module):
    print(f"\n{'#' * 78}\nATBE FORWARD/CAUSAL-LAG AUDIT - son {days} gun\n{'#' * 78}")
    shib_series, majors_series, zamanlar, kapanislar = atb_module._fetch_all(days)
    sonuc = aud_module.booster_standalone(shib_series, majors_series, zamanlar, kapanislar)

    entries = _dedup_entries(sonuc["ladder_gecmisi"])
    print(f"\nToplam DEDUP state-entry event: {len(entries)}")

    print("\n[1/6] STATE FORWARD EXPECTANCY (madde 1) - execution attribution'DAN AYRI")
    fwd_tablo = state_forward_expectancy(entries, zamanlar, kapanislar)
    for r in sorted(fwd_tablo, key=lambda r: BULL_ORDER.get(r["state"], 0)):
        print(f"  {r['state']:<18} n={r['n']:<5} med_fwd_24h={r['median_fwd_24h']} "
              f"pos_rate_24h={r['pos_rate_24h']}%  MFE24={r['mfe_24h']}  MAE24={r['mae_24h']}")
    _yaz_csv(fwd_tablo, "state_forward_expectancy.csv")

    print("\n[2/6] TRANSITION MATRIX (madde 2)")
    tm = transition_matrix(entries, zamanlar, kapanislar)
    for r in tm[:12]:
        print(f"  {r['from_state']:<16}->{r['to_state']:<16} n={r['count']:<4} "
              f"dur_before={r['median_duration_before_transition_h']:.1f}h "
              f"ret_during={r['median_return_during_state_pct']}  fwd6h={r['median_forward_6h_after_pct']} "
              f"fwd24h={r['median_forward_24h_after_pct']}")
    _yaz_csv(tm, "transition_matrix.csv")

    print("\n[3/6] PRICE EXTENSION AUDIT (madde 3)")
    ext_rows = price_extension_at_entries(entries, shib_series, majors_series, zamanlar, kapanislar, days)
    ext_ozet = extension_summary_by_tier(ext_rows)
    for r in ext_ozet:
        print(f"  {r['state']:<18} n={r['n']:<5} median_ATR_extension={r['median_atr_extension']}  "
              f"median_prev_return_24h={r['median_prev_return_24h']}%")
    _yaz_csv(ext_rows, "price_extension_entries.csv")
    _yaz_csv(ext_ozet, "price_extension_summary_by_tier.csv")

    print("\n[4/6] STRONG_BULL PER-EVENT DUMP (madde 4)")
    sb_dump = strong_bull_case_dump(entries, ext_rows, sonuc["ladder_gecmisi"], zamanlar, kapanislar)
    _yaz_csv(sb_dump, "strong_bull_events.csv")

    print("\n[5-7/6] BULL CAMPAIGN COHORT + CONFIRMATION-LAG HIPOTEZI (madde 5,6,7)")
    campaigns, campaign_ozet = bull_campaign_analysis(sonuc["ladder_gecmisi"], sonuc["booster_equity_ticks"], zamanlar)
    print(f"  Toplam bull campaign: {len(campaigns)}")
    for r in campaign_ozet:
        print(f"  max_tier={r['max_tier_reached']:<18} n={r['campaign_count']:<4} "
              f"total_pnl={r['total_pnl']:>9.3f}  pnl_early_only={r['total_pnl_while_early_only']:>9.3f}  "
              f"pnl_after_advancing={r['total_pnl_after_advancing']:>9.3f}")
    _yaz_csv(campaigns, "bull_campaigns.csv")
    _yaz_csv(campaign_ozet, "bull_campaign_summary_by_max_tier.csv")

    toplam_early_only = sum(r["total_pnl_while_early_only"] for r in campaign_ozet)
    toplam_after = sum(r["total_pnl_after_advancing"] for r in campaign_ozet)
    print(f"\nTUM CAMPAIGN'LER TOPLAMI: EARLY-asamasi PnL={toplam_early_only:+.3f}  "
          f"ILERLEME-SONRASI PnL={toplam_after:+.3f}")
    if toplam_early_only > 0 and toplam_after < 0:
        print("YORUM: HIPOTEZ DOGRULANDI - kazanc EARLY_BULL asamasinda olusuyor, "
              "PERSISTENT/CONFIRMED/STRONG'a ILERLEME bu kazanci GERI VERIYOR (confirmation lag).")
    elif toplam_after > 0:
        print("YORUM: HIPOTEZ DOGRULANMADI - ilerleme SONRASI donem de POZITIF katki veriyor, "
              "sorun baska bir yerde (orn. belirli bir alt-durum, cost, veya cikis mantigi) olabilir.")
    else:
        print("YORUM: KARISIK - hem early hem ilerleme-sonrasi negatif, ayrintili CSV'ye bakin.")

    return {
        "entries": entries, "forward_expectancy": fwd_tablo, "transition_matrix": tm,
        "extension_summary": ext_ozet, "campaign_summary": campaign_ozet,
    }


if __name__ == "__main__":
    import atae_backtest as atb
    import atbe_backtest as atbb
    import atbe_alpha_audit as aud
    gun = int(os.environ.get("ATBE_FORWARD_AUDIT_DAYS", "180"))
    run(gun, atb, atbb, aud)
