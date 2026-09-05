"""
HYBRID motorunun GUCLU trendleri NEDEN GEC yakaladigini teshis eden arac.

ONEMLI: Bu script hybrid_engine.py / portfolio_manager.py / hybrid_backtest.py
DOSYALARINA HIC DOKUNMAZ, hicbir parametre/esik/allocation degistirmez, yeni
gosterge eklemez. SADECE o dosyalarin zaten hesapladigi gostergeleri (EMA,
KAMA, SuperTrend, Nadaraya-Watson, WaveTrend, majors) FARKLI ac/kapa
kombinasyonlarinda (kontrfaktuel) yeniden birlestirip "hangi katman kac saat
gecikme ekliyor" sorusuna sayisal/matematiksel cevap arar.

Yontem (her >=%5/24sa buyuk hareket icin):
  1) Her gostergenin KENDI (1sa zaman diliminde) "ilk sinyal zamani" -
     olayin yonuyle ayni yone donen ilk an.
  2) HYBRID motorunun (hysteresis dahil GERCEK davranisi) o rejime ilk
     GECTIGI an.
  3) KONTROFAKTUEL skor zincirleri: EMA-only -> +KAMA -> +SuperTrend ->
     +Nadaraya-Watson -> +WaveTrend -> +Majors (=gercek HAM skor, hysteresis
     HARIC). Her adimda esigi (YON=30) ne zaman gectigini bularak, bir
     onceki adima gore NE KADAR gecikme EKLEDIGINI (veya azalttigini) izole
     eder. Hysteresis'in kendi gecikmesi = (confirmed - ham) farki.
     Allocation-step gecikmesi = (rejim onaylandiktan sonra gercek SHIB
     payinin hedefe yaklasmasi ne kadar surdu).
  4) Her olay icin BASKIN (en buyuk) gecikme nedeni bir REASON CODE olarak
     yazilir; sonda REASON x COUNT x ORTALAMA GECIKME tablosu ve "en cok
     geciktiren 3 faktor" cevabi verilir.

Kullanim: python3 hybrid_diagnostic.py   (BACKTEST_DAYS ile pencere secilir)
Cikti: hybrid_diagnostic_events.csv + konsol ozeti.
"""

import csv
import os
import time

from backtest import fetch_history, INTERVAL_MS
from trade_bot import _load_dotenv
import hybrid_engine as he
import hybrid_backtest as hb
import trend_engine_backtest as teb
import portfolio_manager as pm

_load_dotenv()

BACKTEST_DAYS = int(os.environ.get("BACKTEST_DAYS", "180"))
BIG_MOVE_THRESHOLD_PERCENT = float(os.environ.get("ENGINE_BIG_MOVE_THRESHOLD", "5"))
BIG_MOVE_WINDOW_HOURS = float(os.environ.get("ENGINE_BIG_MOVE_WINDOW_HOURS", "24"))
SCAN_HORIZON_HOURS = 120  # bir olaydan sonra en fazla bu kadar saat ileri taranir
ESIK_YON = he.HYBRID_ESIK_YON       # 30 (varsayilan) - degistirilmedi, sadece okunuyor
ESIK_GUCLU = he.HYBRID_ESIK_GUCLU   # 70 (varsayilan)
HYSTERESIS_BARS = hb.HYBRID_HYSTERESIS_BARS


def _composite_variant(shib_series, majors_series, timestamp_ms,
                        use_kama=True, use_st=True, use_nw=True, use_wt=True, use_majors=True):
    """hybrid_engine.classify() ile AYNI matematik - sadece hangi katmanlarin
    ACIK oldugu parametrik. hybrid_engine.py'nin private yardimci
    fonksiyonlarini (NW/WT/majors icin) OKUYARAK cagirir, DEGISTIRMEZ."""
    total_weight = 0.0
    total_vote = 0.0
    for tf in he.TIMEFRAMES:
        ts = shib_series.get(tf)
        if ts is None:
            continue
        idx = ts.index_at(timestamp_ms)
        if idx is None:
            continue
        ema_v = ts.ema_vote(idx)
        if ema_v is None:
            continue
        if ema_v == 0:
            vote = 0
        else:
            guc = 1.0
            if use_kama:
                kama_v = ts.kama_vote(idx)
                if kama_v is None:
                    guc *= 0.7
                elif kama_v == ema_v:
                    guc *= 1.0
                elif kama_v == 0:
                    guc *= 0.7
                else:
                    guc *= 0.4
            if use_st:
                st_v = ts.supertrend_vote(idx)
                if st_v is None:
                    guc *= 0.8
                elif st_v == ema_v:
                    guc *= 1.0
                else:
                    guc *= 0.5
            vote = ema_v * guc
        w = he.TIMEFRAME_WEIGHTS.get(tf, 1)
        total_vote += vote * w
        total_weight += w
    mtf_direction = (total_vote / total_weight) if total_weight else 0.0

    ts_1h = shib_series.get("1h")
    idx_1h = ts_1h.index_at(timestamp_ms) if ts_1h else None
    adx_1h = ts_1h.adx[idx_1h] if (ts_1h and idx_1h is not None) else None
    strength_mult = min(adx_1h / 40.0, 1.0) if adx_1h is not None else 0.3
    composite = 100 * mtf_direction * strength_mult

    if use_nw:
        composite, _ = he._nw_adjustment(ts_1h, idx_1h, composite)
    if use_wt:
        composite, _ = he._wt_adjustment(ts_1h, idx_1h, composite)
    if use_majors:
        majors_dir = he._majors_direction(majors_series, timestamp_ms)
        if abs(majors_dir) > 0.2:
            if majors_dir * composite < 0:
                composite *= 0.5
            elif majors_dir * composite > 0 and abs(majors_dir) > 0.3:
                composite = max(-100.0, min(100.0, composite * 1.15))
    return max(-100.0, min(100.0, composite))


def _first_cross_hour(ts_1h_series, idx_start, yon, deger_func, esik):
    """idx_start'tan itibaren saat saat ilerleyip deger_func(idx)'in
    `yon`a gore esigi ilk GECTIGI saat farkini dondurur (yoksa None)."""
    n = len(ts_1h_series.closes)
    for offset in range(0, SCAN_HORIZON_HOURS):
        idx = idx_start + offset
        if idx >= n:
            return None
        deger = deger_func(idx)
        if deger is None:
            continue
        if yon == "YUKARI" and deger >= esik:
            return offset
        if yon == "ASAGI" and deger <= -esik:
            return offset
    return None


def _first_vote_match_hour(ts_1h_series, idx_start, yon, vote_func):
    hedef = 1 if yon == "YUKARI" else -1
    n = len(ts_1h_series.closes)
    for offset in range(0, SCAN_HORIZON_HOURS):
        idx = idx_start + offset
        if idx >= n:
            return None
        v = vote_func(idx)
        if v == hedef:
            return offset
    return None


def _nw_slope_vote(ts, idx, lookback=3):
    if ts.nw is None or idx < lookback:
        return None
    a, b = ts.nw[idx], ts.nw[idx - lookback]
    if a is None or b is None:
        return None
    return 1 if a > b else (-1 if a < b else 0)


def _wt_slope_vote(ts, idx, lookback=2):
    if ts.wt1 is None or idx < lookback:
        return None
    a, b = ts.wt1[idx], ts.wt1[idx - lookback]
    if a is None or b is None:
        return None
    return 1 if a > b else (-1 if a < b else 0)


def main():
    print(f"[TESHIS] Veri cekiliyor: SHIBUSDT + BTC/ETH/BNB, son {BACKTEST_DAYS} gun...")
    shib_series, majors_series, zamanlar, kapanislar = hb._fetch_hybrid_all(BACKTEST_DAYS)
    ts_1h = shib_series["1h"]

    print("[TESHIS] Gercek HYBRID simulate() calistiriliyor (hysteresis dahil gercek davranis icin)...")
    sonuc = hb.simulate(shib_series, majors_series, zamanlar, kapanislar)
    regime_gecmisi = sonuc["regime_gecmisi"]  # (idx15m, ts_ms, confirmed_regime, score, target)
    shib_pct_gecmisi = sonuc["shib_pct_gecmisi"]

    events = teb._big_move_events(zamanlar, kapanislar, BIG_MOVE_THRESHOLD_PERCENT, BIG_MOVE_WINDOW_HOURS)
    print(f"[TESHIS] {len(events)} buyuk hareket (>=%{BIG_MOVE_THRESHOLD_PERCENT}/{BIG_MOVE_WINDOW_HOURS:.0f}sa) bulundu.\n")

    satirlar = []
    reason_delays = {}  # reason -> [delay_saat, ...]

    for i, (start_idx, end_idx, yon, degisim) in enumerate(events):
        event_start_ms = zamanlar[start_idx]
        idx1h_start = ts_1h.index_at(event_start_ms)
        if idx1h_start is None:
            continue

        # --- 1) Her gostergenin KENDI ilk sinyal zamani (1sa TF) ---
        ema_h = _first_vote_match_hour(ts_1h, idx1h_start, yon, ts_1h.ema_vote)
        kama_h = _first_vote_match_hour(ts_1h, idx1h_start, yon, ts_1h.kama_vote)
        st_h = _first_vote_match_hour(ts_1h, idx1h_start, yon, ts_1h.supertrend_vote)
        nw_h = _first_vote_match_hour(ts_1h, idx1h_start, yon, lambda idx: _nw_slope_vote(ts_1h, idx))
        wt_h = _first_vote_match_hour(ts_1h, idx1h_start, yon, lambda idx: _wt_slope_vote(ts_1h, idx))
        majors_h = {}
        for sym, tf_map in majors_series.items():
            m_ts = tf_map.get("1h")
            majors_h[sym] = _first_vote_match_hour(m_ts, m_ts.index_at(event_start_ms), yon, m_ts.ema_vote) if m_ts else None

        # --- 2) Kontrofaktuel zincir: V0..V5 (V5 = gercek HAM skor, hysteresis haric) ---
        def cross(**kwargs):
            return _first_cross_hour(
                ts_1h, idx1h_start, yon,
                lambda idx: _composite_variant(shib_series, majors_series, ts_1h.close_times[idx], **kwargs),
                ESIK_YON,
            )
        t0 = cross(use_kama=False, use_st=False, use_nw=False, use_wt=False, use_majors=False)
        t1 = cross(use_kama=True, use_st=False, use_nw=False, use_wt=False, use_majors=False)
        t2 = cross(use_kama=False, use_st=True, use_nw=False, use_wt=False, use_majors=False)
        t3 = cross(use_kama=True, use_st=True, use_nw=False, use_wt=False, use_majors=False)
        t4 = cross(use_kama=True, use_st=True, use_nw=True, use_wt=False, use_majors=False)
        t5 = cross(use_kama=True, use_st=True, use_nw=True, use_wt=True, use_majors=False)
        t6 = cross(use_kama=True, use_st=True, use_nw=True, use_wt=True, use_majors=True)  # = gercek HAM

        def delta(a, b):
            if a is None or b is None:
                return None
            return a - b

        delay_kama = delta(t3, t2)
        delay_st = delta(t3, t1)
        delay_nw = delta(t4, t3)
        delay_wt = delta(t5, t4)
        delay_majors = delta(t6, t5)

        # --- 3) HYBRID'in GERCEK (hysteresis dahil) ilk hedef rejime gecis ani ---
        hedef_regimeler = {"YUKSELIS", "GUCLU_YUKSELIS"} if yon == "YUKARI" else {"DUSUS", "GUCLU_DUSUS"}
        hedef_guclu = {"GUCLU_YUKSELIS"} if yon == "YUKARI" else {"GUCLU_DUSUS"}
        t_confirmed_ms = None
        t_guclu_ms = None
        for idx15, ts_ms, regime, _score, _target in regime_gecmisi:
            if idx15 < start_idx:
                continue
            if t_confirmed_ms is None and regime in hedef_regimeler:
                t_confirmed_ms = ts_ms
            if t_guclu_ms is None and regime in hedef_guclu:
                t_guclu_ms = ts_ms
            if t_confirmed_ms is not None and t_guclu_ms is not None:
                break
        t_confirmed_h = ((t_confirmed_ms - event_start_ms) / 3_600_000) if t_confirmed_ms else None
        t_guclu_h = ((t_guclu_ms - event_start_ms) / 3_600_000) if t_guclu_ms else None
        delay_hysteresis = delta(t_confirmed_h, t6)

        # --- 4) Allocation-step gecikmesi: rejim onaylandiktan sonra gercek
        # (blended) SHIB payi %75'e ne zaman ulasti (yukselis) / %25'in
        # altina ne zaman indi (dusus) ---
        alloc_esik = 75 if yon == "YUKARI" else 25
        t_alloc_h = None
        if t_confirmed_ms is not None:
            conf_idx15 = next((r[0] for r in regime_gecmisi if r[1] == t_confirmed_ms), None)
            if conf_idx15 is not None:
                for j in range(conf_idx15, min(conf_idx15 + 400, len(shib_pct_gecmisi))):
                    pct = shib_pct_gecmisi[j]
                    if (yon == "YUKARI" and pct >= alloc_esik) or (yon == "ASAGI" and pct <= alloc_esik):
                        t_alloc_h = (zamanlar[j] - t_confirmed_ms) / 3_600_000
                        break
        delay_allocation = t_alloc_h  # zaten "confirmed'dan sonraki ek sure"

        # --- 5) SHIB payi: olay basi / +3/6/12/24 saat ---
        def pct_at(offset_h):
            steps = int(offset_h * 4)  # 15dk adim
            j = start_idx + steps
            return shib_pct_gecmisi[j] if 0 <= j < len(shib_pct_gecmisi) else None

        # --- 6) Reason code: en buyuk (pozitif) gecikme katkisi ---
        adaylar = {
            "WAIT_KAMA": delay_kama,
            "WAIT_SUPERTREND": delay_st,
            "WAIT_NW": delay_nw,
            "WAIT_WAVETREND": delay_wt,
            "WAIT_MAJOR_CONFIRMATION": delay_majors,
            "HYSTERESIS_DELAY": delay_hysteresis,
            "ALLOCATION_STEP_TOO_SLOW": delay_allocation,
        }
        gecerli_adaylar = {k: v for k, v in adaylar.items() if v is not None and v > 0}
        if t6 is None:
            reason = "SCORE_BELOW_THRESHOLD"
        elif gecerli_adaylar:
            reason = max(gecerli_adaylar, key=gecerli_adaylar.get)
        else:
            reason = "NONE_ALL_FAST"

        if reason not in ("SCORE_BELOW_THRESHOLD", "NONE_ALL_FAST"):
            reason_delays.setdefault(reason, []).append(gecerli_adaylar[reason])
        elif reason == "SCORE_BELOW_THRESHOLD":
            reason_delays.setdefault(reason, []).append(0)

        satir = {
            "event_id": i, "direction": yon,
            "event_start": time.strftime("%Y-%m-%d %H:%M", time.localtime(event_start_ms / 1000)),
            "event_end": time.strftime("%Y-%m-%d %H:%M", time.localtime(zamanlar[end_idx] / 1000)),
            "start_price": kapanislar[start_idx], "end_price": kapanislar[end_idx],
            "move_percent": round(degisim, 2),
            "ema_signal_h": ema_h, "kama_signal_h": kama_h, "supertrend_signal_h": st_h,
            "nw_signal_h": nw_h, "wavetrend_signal_h": wt_h,
            "btc_confirm_h": majors_h.get("BTCUSDT"), "eth_confirm_h": majors_h.get("ETHUSDT"),
            "bnb_confirm_h": majors_h.get("BNBUSDT"),
            "raw_v0_ema_only_h": t0, "raw_v1_ema_kama_h": t1, "raw_v2_ema_st_h": t2,
            "raw_v3_ema_kama_st_h": t3, "raw_v4_plus_nw_h": t4, "raw_v5_plus_wt_h": t5,
            "raw_v6_full_ham_h": t6,
            "hybrid_confirmed_h": t_confirmed_h, "hybrid_guclu_h": t_guclu_h,
            "delay_kama": delay_kama, "delay_supertrend": delay_st, "delay_nw": delay_nw,
            "delay_wavetrend": delay_wt, "delay_majors": delay_majors,
            "delay_hysteresis": delay_hysteresis, "delay_allocation_step": delay_allocation,
            "reason_code": reason,
            "shib_pct_at_start": pct_at(0), "shib_pct_3h": pct_at(3), "shib_pct_6h": pct_at(6),
            "shib_pct_12h": pct_at(12), "shib_pct_24h": pct_at(24),
        }
        satirlar.append(satir)
        print(f"[{i}] {yon} {degisim:+.1f}% @ {satir['event_start']} -> reason={reason}")

    if satirlar:
        with open("hybrid_diagnostic_events.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(satirlar[0].keys()))
            writer.writeheader()
            writer.writerows(satirlar)
        print(f"\n[TESHIS] Detay CSV kaydedildi: hybrid_diagnostic_events.csv ({len(satirlar)} olay)")

    print(f"\n{'=' * 70}\nREASON KODU DAGILIMI (sadece yukselis+dusus olaylari, tum yonler)\n{'=' * 70}")
    print(f"{'Reason':<28} {'Count':>6} {'Ort.Gecikme(sa)':>16}")
    print("-" * 70)
    for reason, delays in sorted(reason_delays.items(), key=lambda kv: -sum(kv[1])):
        ort = sum(delays) / len(delays) if delays else 0
        print(f"{reason:<28} {len(delays):>6} {ort:>16.1f}")

    print(f"\n{'=' * 70}\nSADECE BUYUK YUKSELISLER icin (kullanicinin ana sorusu)\n{'=' * 70}")
    yukselis_satirlari = [s for s in satirlar if s["direction"] == "YUKARI"]
    yukselis_reasons = {}
    for s in yukselis_satirlari:
        r = s["reason_code"]
        if r in ("SCORE_BELOW_THRESHOLD", "NONE_ALL_FAST"):
            continue
        d = s.get({
            "WAIT_KAMA": "delay_kama", "WAIT_SUPERTREND": "delay_supertrend",
            "WAIT_NW": "delay_nw", "WAIT_WAVETREND": "delay_wavetrend",
            "WAIT_MAJOR_CONFIRMATION": "delay_majors", "HYSTERESIS_DELAY": "delay_hysteresis",
            "ALLOCATION_STEP_TOO_SLOW": "delay_allocation_step",
        }.get(r))
        if d is not None:
            yukselis_reasons.setdefault(r, []).append(d)
    siralama = sorted(yukselis_reasons.items(), key=lambda kv: -sum(kv[1]))
    print(f"{'Reason':<28} {'Count':>6} {'Ort.Gecikme(sa)':>16}")
    print("-" * 70)
    for reason, delays in siralama:
        print(f"{reason:<28} {len(delays):>6} {sum(delays)/len(delays):>16.1f}")

    print(f"\n{'=' * 70}\nSORU: Guclu yukselisi erken yakalamayi EN COK engelleyen 3 faktor?\n{'=' * 70}")
    for reason, delays in siralama[:3]:
        print(f"  - {reason}: ortalama {sum(delays)/len(delays):.1f} saat gecikme ({len(delays)} olayda baskin neden)")
    if not siralama:
        print("  (Yeterli olay/veri yok - pencereyi buyutmeyi deneyin.)")
    print("\n(Not: bu SADECE teshis - hicbir parametre/strateji degistirilmedi.)")


if __name__ == "__main__":
    main()
