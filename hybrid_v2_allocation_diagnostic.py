"""
HYBRID v2 (Early-Entry) ALLOCATION AUDIT - SALT TESHIS.

Kullanicinin sordugu soru: "Early signal 8 saatte geldigi halde neden
allocation 24 saatte hala %50'nin altinda kaliyor?" Bu script hicbir
parametre/esik/allocation/hysteresis/indikator DEGISTIRMEZ, hybrid_v2_
backtest.py / hybrid_backtest.py / hybrid_engine.py / portfolio_manager.py
DOSYALARINA DOKUNMAZ - SADECE onlarin ZATEN urettigi (hybrid_v2_backtest.py'
ye bu tur teshisler icin eklenmis, SALT-TESHIS amacli, karari degistirmeyen
grid_pct_gecmisi/trend_pct_gecmisi/early_gecmisi/trades[].ts_ms alanlarini)
veriyi okuyup her buyuk YUKSELIS icin nedensel bir "allocation audit"
uretir.

YONTEM (her buyuk YUKSELIS icin):
  1) Early sinyalin ilk ates aldigi an + o andaki HEDEF (trend_target) ve
     GERCEK (blended) allocation.
  2) +3/6/12/24 saatteki GERCEK allocation + 24 saatlik pencerede ulasilan
     TEPE allocation.
  3) Early sinyalin o pencerede kac kez ON/OFF salindigi (reset_count,
     en uzun kesintisiz ON suresi) - "sinyal cok mu gurultulu" sorusuna
     dogrudan kanit.
  4) O pencerede kac AL/SAT islemi oldugu + brut ciro + tahmini komisyon+
     kayma maliyeti (SADECE trend sleeve - grid sleeve v1 ile AYNI
     mekanizma oldugundan, v1-v2 FARKININ kaynagi olamaz, bu yuzden
     odak trend sleeve'te).
  5) v1'in GERCEK confirmed-rejim akisi (early tetiklendigi anda, +6/12/
     24 saatte) - "ana rejim early'yi mi bastırıyor" sorusuna kanit.
  6) Grid ve trend sleeve'in KENDI SHIB paylari ayri ayri (toplam neden
     dusuk kaldiginin GRID mi TREND mi kaynakli oldugunu ayirmak icin).
  7) Yukaridaki KANITLARDAN, ONCEDEN TANIMLANMIS bir kural-zincirine gore
     (asagida _siniflandir()) HER "24h kacirilan" olay icin TEK bir baskin
     neden (A-H) secilir. Bu KESIN bir matematiksel ispat DEGIL (aksine
     hybrid_causal_diagnostics.csv'deki saat-bazli delay analizi gibi) -
     kategori bazli, kural-tabanli bir SINIFLANDIRMADIR; ayni olayda birden
     fazla kosul dogru olabilir, kural zinciri EN SPESIFIK/EN BOZUCU
     nedeni ONCE kontrol edecek sekilde sıralanmistir (bkz. yorum satirlari).

Cikti: hybrid_v2_allocation_diagnostic.csv + konsolda CAUSE ozet tablosu +
kullanicinin 10 sorusuna dogrudan cevap.

Kullanim: BACKTEST_DAYS=180 python3 hybrid_v2_allocation_diagnostic.py
"""

import csv
import os
import statistics
import time

from backtest import INTERVAL_MS
from trade_bot import _load_dotenv
import hybrid_backtest as hb
import hybrid_v2_backtest as v2b
import trend_engine_backtest as teb

_load_dotenv()

BACKTEST_DAYS = int(os.environ.get("BACKTEST_DAYS", "180"))
REBALANCE_HOURS = hb.HYBRID_REBALANCE_MINUTES / 60.0
COST_PCT = hb.TRADING_FEE_PERCENT + hb.SLIPPAGE_PERCENT


def _fmt(ts_ms):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts_ms / 1000)) if ts_ms is not None else None


def _idx_at_offset(start_idx, offset_h, n):
    j = start_idx + int(offset_h * 4)  # 15dk adim
    return j if 0 <= j < n else None


def _regime_at_or_before(ticks, ts_ms):
    """ticks: (idx, ts_ms, regime, score, target) listesi - ts_ms'e kadar
    (dahil) GECERLI olan SON kaydi dondurur (henuz gelmemis bilgi
    KULLANILMAZ - lookahead yok)."""
    secilen = None
    for t in ticks:
        if t[1] <= ts_ms:
            secilen = t
        else:
            break
    return secilen[2] if secilen else None


def _siniflandir(olay):
    """A-H tek-neden siniflandirmasi. Sira ONEMLI: en spesifik/en bozucu
    aciklama ONCE kontrol edilir (orn. 'pozisyon aciktan kisa surede
    satildi' varsa, bunun ustune 'hedef kucuktu' gibi daha genel bir
    aciklama YAZILMAZ)."""
    if olay["first_early_trigger"] is None:
        return "H", "Bu 24 saatlik pencerede early sinyal HIC tetiklenmedi (yon oyu esigi hic gecmedi)."

    if olay["time_early_to_first_sell_h"] is not None and olay["time_early_to_first_sell_h"] <= 6:
        return "D", (f"Early alimdan sadece {olay['time_early_to_first_sell_h']:.1f} saat sonra TREND_SLEEVE_SAT "
                      f"islemi oldu - pozisyon acilir acilmaz geri kapatildi.")

    if olay["early_signal_reset_count"] >= 3 or (
            olay["early_trigger_on_hours"] and olay["early_signal_longest_continuous_run_h"] is not None
            and olay["early_signal_longest_continuous_run_h"] < 6):
        return "A", (f"Early sinyal 24 saatte {olay['early_signal_reset_count']} kez ON/OFF salindi, en uzun "
                      f"kesintisiz ON suresi {olay['early_signal_longest_continuous_run_h']:.1f} saat - sinyal "
                      f"cok gurultulu, kararli bir pozisyon birikimine izin vermiyor.")

    if olay["target_reduced_by_grid_logic"]:
        return "G", "Grid sleeve, trend sleeve SHIB biriktirirken TERS yonde (SHIB azaltarak) islem yapti."

    if olay["target_reduced_by_confirmed_bearish_regime"]:
        return "E", (f"Ana (confirmed) rejim pencerede DUSUS/GUCLU_DUSUS'e girdi (etkin_regime early'yi "
                      f"BASTIRDI - confirmed YATAY disina cikinca early katman devre disi kalir).")

    if olay["target_limited_by_sleeve_cap"]:
        return "F", (f"Trend sleeve kendi ici SHIB payi %{olay['trend_shib_pct']:.0f}'a ulasti (kendi tavanina "
                      f"yakin) ama grid sleeve dusuk SHIB payinda kaldigindan TOPLAM allocation hala dusuk.")

    if olay["target_limited_by_allocation_step"]:
        return "C", "Hedef (trend_target) yeterince yuksekti ama max-step limiti gercek allocation'in yetismesini yavaslatti."

    return "B", (f"Early tetiklendigindeki hedef allocation (%{olay['target_allocation_at_early_trigger']:.0f}) "
                 f"ERKEN_YUKSELIS kademesinin ust siniri civarinda kaldi - sinyal dogruydu ama hedefin kendisi "
                 f"mimari olarak %50 esigine ulasmaya yetecek kadar yuksek degildi.")


def main():
    print(f"[V2-AUDIT] Veri cekiliyor: {v2b.SYMBOL} + majors, son {BACKTEST_DAYS} gun...")
    shib_series, majors_series, zamanlar, kapanislar = hb._fetch_hybrid_all(BACKTEST_DAYS)
    early_series = v2b._fetch_early_series(BACKTEST_DAYS)

    print("[V2-AUDIT] v1 confirmed-rejim akisi icin hb.simulate() calistiriliyor...")
    v1_sonuc = hb.simulate(shib_series, majors_series, zamanlar, kapanislar)

    print("[V2-AUDIT] v2 simulate_v2() calistiriliyor (telemetri dahil)...")
    v2_sonuc = v2b.simulate_v2(shib_series, majors_series, early_series, zamanlar, kapanislar,
                                v1_sonuc["hysteresis_gecmisi"])

    # Soru 10 icin (BRUT vs NET maliyet karsilastirmasi) - GROSS/NET ayni
    # karar dizisiyle, sadece maliyetsiz tekrar kosum (v1/v2 raporlarindaki
    # ile AYNI yontem, tekrar YAZILMADI).
    v1_sonuc_brut = hb.simulate(shib_series, majors_series, zamanlar, kapanislar, cost_percent=0)
    v2_sonuc_brut = v2b.simulate_v2(shib_series, majors_series, early_series, zamanlar, kapanislar,
                                     v1_sonuc["hysteresis_gecmisi"], cost_percent=0)
    v1_row = hb._ozet("HYBRID v1", v1_sonuc, v1_sonuc_brut, zamanlar, kapanislar)
    v2_row = hb._ozet("HYBRID v2", v2_sonuc, v2_sonuc_brut, zamanlar, kapanislar)

    shib_pct = v2_sonuc["shib_pct_gecmisi"]
    grid_pct = v2_sonuc["grid_pct_gecmisi"]
    trend_pct = v2_sonuc["trend_pct_gecmisi"]
    early_gecmisi = v2_sonuc["early_gecmisi"]
    regime_gecmisi = v2_sonuc["regime_gecmisi"]
    trades = v2_sonuc["trades"]
    n = len(shib_pct)

    events = teb._big_move_events(zamanlar, kapanislar, hb.BIG_MOVE_THRESHOLD_PERCENT, hb.BIG_MOVE_WINDOW_HOURS)
    yukselisler = [(i, ev) for i, ev in enumerate(events) if ev[2] == "YUKARI"]
    print(f"[V2-AUDIT] {len(yukselisler)} buyuk YUKSELIS bulundu (toplam {len(events)} buyuk hareketten).\n")

    satirlar = []
    for event_id, (start_idx, end_idx, yon, degisim) in yukselisler:
        event_start_ms = zamanlar[start_idx]
        pencere_bitis_ms = event_start_ms + 24 * 3_600_000

        pencere_ticks = [e for e in early_gecmisi if event_start_ms <= e["ts_ms"] <= pencere_bitis_ms]
        first_early = next((e for e in pencere_ticks if e["early_dir"] == 1), None)
        first_early_ms = first_early["ts_ms"] if first_early else None
        early_delay_h = (first_early_ms - event_start_ms) / 3_600_000 if first_early_ms is not None else None

        idx_trigger = _idx_at_offset(start_idx, early_delay_h, n) if early_delay_h is not None else None
        actual_at_trigger = shib_pct[idx_trigger] if idx_trigger is not None else None
        target_at_trigger = first_early["trend_target"] if first_early else None

        def pct_at(offset_h):
            j = _idx_at_offset(start_idx, offset_h, n)
            return shib_pct[j] if j is not None else None

        pencere24_pct = shib_pct[start_idx:min(start_idx + 96, n)]
        max_alloc_24h = max(pencere24_pct) if pencere24_pct else None
        pct24 = pct_at(24)
        missed = (pct24 < 50) if pct24 is not None else None

        # --- Early sinyal ON/OFF istatistikleri (sadece bu 24sa penceresinde) ---
        on_count, off_count, reset_count, longest_run, cur_run = 0, 0, 0, 0.0, 0.0
        prev_on = None
        for e in pencere_ticks:
            is_on = (e["etkin_regime"] == "ERKEN_YUKSELIS")
            if is_on:
                on_count += 1
                cur_run += REBALANCE_HOURS
                longest_run = max(longest_run, cur_run)
            else:
                off_count += 1
                cur_run = 0.0
            if prev_on is True and not is_on:
                reset_count += 1
            prev_on = is_on
        on_hours, off_hours = on_count * REBALANCE_HOURS, off_count * REBALANCE_HOURS

        capped_any = any(e["capped_by_step"] for e in pencere_ticks)

        # --- Islemler (SADECE trend sleeve - grid v1 ile ayni mekanizma) ---
        pencere_trades = [t for t in trades if t.get("ts_ms") is not None and event_start_ms <= t["ts_ms"] <= pencere_bitis_ms]
        trend_trades = [t for t in pencere_trades if t["tip"].startswith("TREND_SLEEVE")]
        buy_trades = [t for t in trend_trades if t["tip"] == "TREND_SLEEVE_AL"]
        sell_trades = [t for t in trend_trades if t["tip"] == "TREND_SLEEVE_SAT"]
        gross_turnover = sum(t.get("usdt_tutari", 0.0) for t in trend_trades)
        fee_cost = gross_turnover * COST_PCT / 100  # yaklasik (bkz. docstring)

        first_sell_ms = next((t["ts_ms"] for t in sell_trades if first_early_ms is None or t["ts_ms"] >= first_early_ms), None)
        time_to_sell_h = ((first_sell_ms - first_early_ms) / 3_600_000) if (first_sell_ms and first_early_ms) else None

        confirmed_at_early = first_early["confirmed_regime"] if first_early else None
        confirmed_6h = _regime_at_or_before(regime_gecmisi, event_start_ms + 6 * 3_600_000)
        confirmed_12h = _regime_at_or_before(regime_gecmisi, event_start_ms + 12 * 3_600_000)
        confirmed_24h = _regime_at_or_before(regime_gecmisi, event_start_ms + 24 * 3_600_000)

        grid_pct_start = grid_pct[start_idx]
        j24 = _idx_at_offset(start_idx, 24, n)
        grid_pct_24h = grid_pct[j24] if j24 is not None else None
        trend_pct_24h = trend_pct[j24] if j24 is not None else None
        total_pct_24h = shib_pct[j24] if j24 is not None else None

        olay = {
            "event_id": event_id, "event_start": _fmt(event_start_ms), "move_pct": round(degisim, 2),
            "first_early_trigger": _fmt(first_early_ms), "early_delay_h": round(early_delay_h, 1) if early_delay_h is not None else None,
            "allocation_at_event_start": round(shib_pct[start_idx], 1),
            "target_allocation_at_early_trigger": round(target_at_trigger, 1) if target_at_trigger is not None else None,
            "actual_allocation_at_early_trigger": round(actual_at_trigger, 1) if actual_at_trigger is not None else None,
            "allocation_3h": round(pct_at(3), 1) if pct_at(3) is not None else None,
            "allocation_6h": round(pct_at(6), 1) if pct_at(6) is not None else None,
            "allocation_12h": round(pct_at(12), 1) if pct_at(12) is not None else None,
            "allocation_24h": round(pct24, 1) if pct24 is not None else None,
            "max_allocation_24h": round(max_alloc_24h, 1) if max_alloc_24h is not None else None,
            "early_trigger_count_24h": on_count, "early_trigger_on_hours": round(on_hours, 1),
            "early_trigger_off_hours": round(off_hours, 1),
            "early_signal_reset_count": reset_count, "early_signal_longest_continuous_run_h": round(longest_run, 1),
            "buy_trade_count_24h": len(buy_trades), "sell_trade_count_24h": len(sell_trades),
            "gross_turnover_24h": round(gross_turnover, 2), "fee_slippage_cost_24h": round(fee_cost, 4),
            "first_sell_after_early": _fmt(first_sell_ms), "time_early_to_first_sell_h": round(time_to_sell_h, 1) if time_to_sell_h is not None else None,
            "confirmed_regime_at_early": confirmed_at_early, "confirmed_regime_6h": confirmed_6h,
            "confirmed_regime_12h": confirmed_12h, "confirmed_regime_24h": confirmed_24h,
            "grid_shib_pct": round(grid_pct_24h, 1) if grid_pct_24h is not None else None,
            "trend_shib_pct": round(trend_pct_24h, 1) if trend_pct_24h is not None else None,
            "total_shib_pct": round(total_pct_24h, 1) if total_pct_24h is not None else None,
            "target_limited_by_sleeve_cap": bool(trend_pct_24h is not None and trend_pct_24h >= 90 and total_pct_24h is not None and total_pct_24h < 50),
            "target_limited_by_allocation_step": capped_any,
            "target_reduced_by_signal_reset": reset_count >= 1,
            "target_reduced_by_confirmed_bearish_regime": any(r in ("DUSUS", "GUCLU_DUSUS") for r in (confirmed_at_early, confirmed_6h, confirmed_12h, confirmed_24h) if r),
            "target_reduced_by_grid_logic": bool(grid_pct_24h is not None and grid_pct_24h < grid_pct_start - 5 and trend_pct_24h is not None and trend_pct_24h > grid_pct_24h),
            "24h_missed": missed,
        }
        if missed:
            neden, aciklama = _siniflandir(olay)
        else:
            neden, aciklama = "CAUGHT", "24 saat sonra SHIB payi >=%50 - bu olay kacirilmadi."
        olay["cause"] = neden
        olay["cause_aciklama"] = aciklama
        satirlar.append(olay)
        print(f"[{event_id}] {olay['event_start']} +{degisim:.1f}% -> early={olay['early_delay_h']}sa "
              f"alloc24h={olay['allocation_24h']}% missed={missed} cause={neden}")

    if satirlar:
        with open("hybrid_v2_allocation_diagnostic.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(satirlar[0].keys()))
            writer.writeheader()
            writer.writerows(satirlar)
        print(f"\n[V2-AUDIT] CSV kaydedildi: hybrid_v2_allocation_diagnostic.csv ({len(satirlar)} olay)")

    # ============== CAUSE OZET TABLOSU (sadece kacirilanlar) ==============
    kacirilanlar = [s for s in satirlar if s["24h_missed"]]
    print(f"\n{'=' * 100}\nCAUSE OZET TABLOSU ({len(kacirilanlar)} / {len(satirlar)} buyuk yukselis kacirildi)\n{'=' * 100}")
    print(f"{'CAUSE':<8} {'COUNT':>6} {'MED. KAYIP ALLOC%':>18} {'MED. DELAY(sa)':>15} {'MED. TRADES':>12} {'MED. COST($)':>13}")
    print("-" * 100)
    by_cause = {}
    for s in kacirilanlar:
        by_cause.setdefault(s["cause"], []).append(s)
    for cause in sorted(by_cause, key=lambda c: -len(by_cause[c])):
        grup = by_cause[cause]
        kayip = [50 - s["allocation_24h"] for s in grup if s["allocation_24h"] is not None]
        delay = [s["early_delay_h"] for s in grup if s["early_delay_h"] is not None]
        trades_n = [s["buy_trade_count_24h"] + s["sell_trade_count_24h"] for s in grup]
        cost = [s["fee_slippage_cost_24h"] for s in grup]
        print(f"{cause:<8} {len(grup):>6} {statistics.median(kayip) if kayip else float('nan'):>18.1f} "
              f"{statistics.median(delay) if delay else float('nan'):>15.1f} "
              f"{statistics.median(trades_n) if trades_n else float('nan'):>12.1f} "
              f"{statistics.median(cost) if cost else float('nan'):>13.4f}")
    print("\nNeden aciklamalari (ornek, ilk olay her cause icin):")
    gorulen = set()
    for s in kacirilanlar:
        if s["cause"] not in gorulen:
            gorulen.add(s["cause"])
            print(f"  [{s['cause']}] olay #{s['event_id']}: {s['cause_aciklama']}")

    # ============== 10 SORUYA DOGRUDAN CEVAP ==============
    tum_hedef_trigger = [s["target_allocation_at_early_trigger"] for s in satirlar if s["target_allocation_at_early_trigger"] is not None]
    tum_actual_trigger = [s["actual_allocation_at_early_trigger"] for s in satirlar if s["actual_allocation_at_early_trigger"] is not None]
    a6 = [s["allocation_6h"] for s in satirlar if s["allocation_6h"] is not None]
    a12 = [s["allocation_12h"] for s in satirlar if s["allocation_12h"] is not None]
    a24 = [s["allocation_24h"] for s in satirlar if s["allocation_24h"] is not None]
    satis_olan = [s for s in satirlar if s["sell_trade_count_24h"] > 0]
    ilk_satis_saatleri = [s["time_early_to_first_sell_h"] for s in satirlar if s["time_early_to_first_sell_h"] is not None]
    toplam_early_reset = sum(s["early_signal_reset_count"] for s in satirlar)
    sleeve_cap_sayisi = sum(1 for s in satirlar if s["target_limited_by_sleeve_cap"])
    grid_conflict_sayisi = sum(1 for s in satirlar if s["target_reduced_by_grid_logic"])
    sinyal_kaynakli = sum(1 for s in kacirilanlar if s["cause"] in ("A", "D"))
    mimari_kaynakli = sum(1 for s in kacirilanlar if s["cause"] in ("B", "C", "F"))

    print(f"\n{'=' * 100}\n10 SORUYA DOGRUDAN CEVAP\n{'=' * 100}")
    print(f"1) Early sinyal geldiginde ORTALAMA hedef (target) SHIB%      : "
          f"{statistics.mean(tum_hedef_trigger):.1f}%  (median {statistics.median(tum_hedef_trigger):.1f}%, n={len(tum_hedef_trigger)})" if tum_hedef_trigger else "1) veri yok")
    print(f"2) Ayni anda ORTALAMA GERCEK (actual) SHIB%                   : "
          f"{statistics.mean(tum_actual_trigger):.1f}%  (median {statistics.median(tum_actual_trigger):.1f}%)" if tum_actual_trigger else "2) veri yok")
    print(f"3) 6h / 12h / 24h sonra ORTALAMA SHIB%                        : "
          f"6h={statistics.mean(a6):.1f}%  12h={statistics.mean(a12):.1f}%  24h={statistics.mean(a24):.1f}%" if (a6 and a12 and a24) else "3) veri yok")
    print(f"4) Early sinyal sonrasi TEKRAR SATIS yapilan olay sayisi      : {len(satis_olan)} / {len(satirlar)}")
    print(f"5) Ilk satisa kadar MEDIAN sure                               : "
          f"{statistics.median(ilk_satis_saatleri):.1f} saat (n={len(ilk_satis_saatleri)})" if ilk_satis_saatleri else "5) hic satis yok")
    toplam_trend_trade = sum(s["buy_trade_count_24h"] + s["sell_trade_count_24h"] for s in satirlar)
    print(f"6) Toplam trend-sleeve islem sayisi (bu {len(satirlar)} olayin 24sa penceresinde) : {toplam_trend_trade} "
          f"(early ON/OFF resetleri: {toplam_early_reset} - her reset potansiyel olarak fazladan AL+SAT ciftine yol acar)")
    print(f"7) Sabit sleeve (grid/trend) toplam allocation'i MEKANIK sinirliyor mu? : "
          f"{sleeve_cap_sayisi} / {len(satirlar)} olayda trend sleeve kendi tavanina yakinken grid dusuk SHIB'de kalip toplami dusurdu.")
    print(f"8) GRID ile early-trend katmani birbirine karsi mi islem yapiyor?      : "
          f"{grid_conflict_sayisi} / {len(satirlar)} olayda GRID'in SHIB payi trend sleeve YUKSELIRKEN belirgin DUSTU.")
    print(f"9) {len(kacirilanlar)} kacirilan olaydan kacinda sorun SINYAL KALITESI (A/D), kacinda ALLOCATION MIMARISI (B/C/F)? : "
          f"sinyal={sinyal_kaynakli}  mimari={mimari_kaynakli}  (digerleri E/G rejim-catismasi/grid-catismasi)")
    v1_maliyet_puan = v1_row["brut_getiri"] - v1_row["net_getiri"]
    v2_maliyet_puan = v2_row["brut_getiri"] - v2_row["net_getiri"]
    v2_trend_islem = sum(1 for t in trades if t["tip"].startswith("TREND_SLEEVE"))
    print(f"10) v1: BRUT {v1_row['brut_getiri']:+.2f}%  NET {v1_row['net_getiri']:+.2f}%  maliyet {v1_maliyet_puan:.2f} puan  ({v1_row['islem']} islem)")
    print(f"    v2: BRUT {v2_row['brut_getiri']:+.2f}%  NET {v2_row['net_getiri']:+.2f}%  maliyet {v2_maliyet_puan:.2f} puan  ({v2_row['islem']} islem, {v2_trend_islem} trend-sleeve)")
    print(f"    v2 maliyet/v1 maliyet orani = {v2_maliyet_puan / v1_maliyet_puan:.2f}x  (islem sayisi orani = {v2_row['islem'] / v1_row['islem']:.2f}x)")
    print(f"    -> maliyet artisi islem-sayisi artisiyla ORANTILI mi kontrol edin: orantiliysa maliyet patlamasi")
    print(f"    dogrudan turnover'dan (buyuk olcude early sinyal ON/OFF salinimindan, satir 6'ya bakin) geliyor demektir.")
    print("\n(Not: bu SADECE teshis - hicbir parametre/strateji/threshold degistirilmedi.)")


if __name__ == "__main__":
    main()
