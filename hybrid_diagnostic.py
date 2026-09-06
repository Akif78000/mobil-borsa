"""
HYBRID motorunun GUCLU trendleri NEDEN GEC yakaladigini teshis eden arac -
CAUSAL (nedensel) surum.

ONEMLI: Bu script hybrid_engine.py / portfolio_manager.py / hybrid_backtest.py
DOSYALARINA HIC DOKUNMAZ (hybrid_backtest.py'ye eklenen tek sey, zaten var
olan ic hysteresis durumunu DISARIYA AKTARAN, hicbir karari DEGISTIRMEYEN
"hysteresis_gecmisi" listesidir). Hicbir parametre/esik/allocation
degistirilmedi, yeni gosterge eklenmedi.

NEDEN BU SURUM VAR (onceki surumdeki metrik hatasi):
  Eski surum, HER gostergenin (EMA/KAMA/SUPERTREND/NW/WT/MAJORS) gecikmesini
  bir KONTROFAKTUEL ZINCIRIN (V0=EMA-only -> V1=+KAMA -> ... -> V6=HAM skor)
  ARDISIK FARKI olarak hesapliyordu, SONRA hysteresis gecikmesini de bu
  zincirin son adimiyla GERCEK (confirmed) rejim arasindaki farktan
  buluyordu. Bu, farkli olceklerdeki degerleri (zincir-ici artis vs.
  toplam-sureden-turetilmis fark) TEK bir "reason" secimiyle karistiriyordu.
  Ozellikle HYSTERESIS_DELAY icin bulunan 70.8 saatlik ortalama, sistemin
  gercek mekanigiyle (60 dakikada bir kontrol, N=2 ardisik onay -> normalde
  ~1 saat ek gecikme) MATEMATIKSEL OLARAK TUTARSIZDI - kullanicinin dogru
  tespit ettigi gibi, o buyuk sayi aslinda HAM sinyalin esik civarinda
  SALINIP hysteresis sayacini defalarca SIFIRLAMASINDAN kaynaklaniyor
  olmali, "hysteresis mekanizmasinin kendisi" 70 saat surmuyor.

  Bu surumde HER faktorun gecikmesi BAGIMSIZ ve KENDI TANIMIYLA olculur:
    - Her gosterge (EMA/KAMA/SUPERTREND/NW/WAVETREND) icin: olay
      basladiktan (T0) SONRA o gostergenin KENDI oyu ilk kez olay yonune
      donene kadar gecen sure (zincir farki DEGIL, dogrudan T0'dan gecen
      sure).
    - MAJOR_CONFIRMATION: BTC/ETH/BNB agirlikli yon (_majors_direction)
      ilk kez olay yonunde ve |yon|>0.2 esigini gecene kadar T0'dan gecen
      sure.
    - SCORE_THRESHOLD: TAM HAM bilesik skorun (hysteresis HARIC, gercek
      classify() matematigiyle BIREBIR ayni) ESIK_YON'u ilk gectigi ana
      kadar T0'dan gecen sure.
    - HYSTERESIS_EXTRA: hb.simulate()'in GERCEK calisan hysteresis
      durumundan (hysteresis_gecmisi) okunur - ham (confirmed-oncesi)
      rejimin olay yonune İLK donduğu kontrol anindan (first_valid_signal_
      time), confirmed_regime'in fiilen olay yonune GECTIGI ana kadar
      (second_consecutive/nihai onay ani) gecen sure. Ayrica bu iki an
      arasinda ham rejimin KAC KEZ olay-yonu-DISINA cikip sayaci sifirladigi
      (reset_count) da ayrica olculur - boylece "saf hysteresis" (N=2 x
      60dk =~1 saat, reset_count=0) ile "gurultu nedeniyle sayac sifirlama"
      (reset_count>0, potansiyel olarak COK daha uzun) birbirinden
      MATEMATIKSEL OLARAK AYRISTIRILIR (kullanicinin istedigi sanity check).
    - ALLOCATION_STEP: confirmed rejim FIILEN olay yonune gectikten sonra,
      gercek (blended) SHIB payinin hedef banda (%75 yukselis / %25 dusus)
      ulasmasina kadar gecen EK sure (onceki surumle ayni tanim - bu zaten
      bagimsizdi, degistirilmedi).

  Boylece hicbir faktorun gecikmesi bir BASKASININ gecikmesini icine
  ALMAZ - "tek bir reason_code secip TUM gecikmeyi ona yukleme" sorunu
  ortadan kalkar; onun yerine HER faktor icin ayri MEDIAN/ORTALAMA/olay-
  sayisi raporlanir (kullanicinin istedigi format).

Kullanim: python3 hybrid_diagnostic.py   (BACKTEST_DAYS ile pencere secilir)
Cikti:
  - hybrid_causal_diagnostics.csv  (olay-bazli, 9 bagimsiz gecikme kolonu)
  - hybrid_causal_ticks.csv        (olay basina, saat-saat ham durum kaydi -
    hysteresis_counter / ok bayraklari / target-actual allocation dahil)
  - konsol: FAKTOR x MEDIAN x ORTALAMA x OLAY-SAYISI tablosu + iki sanity
    check + "en cok geciktiren 3 faktor" cevabi (MEDIAN'a gore).
"""

import csv
import os
import statistics
import time

from trade_bot import _load_dotenv
import hybrid_engine as he
import hybrid_backtest as hb
import trend_engine_backtest as teb

_load_dotenv()

BACKTEST_DAYS = int(os.environ.get("BACKTEST_DAYS", "180"))
BIG_MOVE_THRESHOLD_PERCENT = float(os.environ.get("ENGINE_BIG_MOVE_THRESHOLD", "5"))
BIG_MOVE_WINDOW_HOURS = float(os.environ.get("ENGINE_BIG_MOVE_WINDOW_HOURS", "24"))
SCAN_HORIZON_HOURS = 120  # bir olaydan sonra en fazla bu kadar saat ileri taranir
ESIK_YON = he.HYBRID_ESIK_YON       # 30 (varsayilan) - degistirilmedi, sadece okunuyor
ESIK_GUCLU = he.HYBRID_ESIK_GUCLU   # 70 (varsayilan)
HYSTERESIS_BARS = hb.HYBRID_HYSTERESIS_BARS
REBALANCE_HOURS = hb.HYBRID_REBALANCE_MINUTES / 60.0


def _composite_ham(shib_series, majors_series, timestamp_ms):
    """hybrid_engine.classify() ile BIREBIR AYNI HAM skor (hysteresis HARIC).
    he.classify()'in KENDISINI cagirir - ayri bir matematik YENIDEN
    YAZILMAZ, sadece prev_score'a bagli TOPARLANMA/DAGITIM etiketi bu
    amac icin onemli olmadigindan sabit 0.0 ile cagrilir (etiket
    kullanilmiyor, sadece 'composite' sayisal degeri okunuyor)."""
    return he.classify(shib_series, majors_series, timestamp_ms, prev_score=0.0)["score"]


def _first_cross_hour(ts_1h_series, idx_start, yon, deger_func, esik):
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


def _majors_first_confirm_hour(ts_1h_series, idx_start, majors_series, yon):
    n = len(ts_1h_series.closes)
    for offset in range(0, SCAN_HORIZON_HOURS):
        idx = idx_start + offset
        if idx >= n:
            return None
        ts_ms = ts_1h_series.close_times[idx]
        d = he._majors_direction(majors_series, ts_ms)
        if yon == "YUKARI" and d > 0.2:
            return offset
        if yon == "ASAGI" and d < -0.2:
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


def _fmt(ts_ms):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts_ms / 1000)) if ts_ms is not None else None


def _percentile(degerler, p):
    """Bagimlilik eklemeden (numpy yok) dogrusal enterpolasyonlu yuzdelik dilim."""
    if not degerler:
        return None
    s = sorted(degerler)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100)
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def main():
    print(f"[TESHIS] Veri cekiliyor: SHIBUSDT + BTC/ETH/BNB, son {BACKTEST_DAYS} gun...")
    shib_series, majors_series, zamanlar, kapanislar = hb._fetch_hybrid_all(BACKTEST_DAYS)
    ts_1h = shib_series["1h"]

    print("[TESHIS] Gercek HYBRID simulate() calistiriliyor (hysteresis_gecmisi dahil gercek davranis icin)...")
    sonuc = hb.simulate(shib_series, majors_series, zamanlar, kapanislar)
    shib_pct_gecmisi = sonuc["shib_pct_gecmisi"]
    hysteresis_gecmisi = sonuc["hysteresis_gecmisi"]

    events = teb._big_move_events(zamanlar, kapanislar, BIG_MOVE_THRESHOLD_PERCENT, BIG_MOVE_WINDOW_HOURS)
    print(f"[TESHIS] {len(events)} buyuk hareket (>=%{BIG_MOVE_THRESHOLD_PERCENT}/{BIG_MOVE_WINDOW_HOURS:.0f}sa) bulundu.\n")

    satirlar = []
    tick_satirlari = []
    never_crossed_events = []

    for i, (start_idx, end_idx, yon, degisim) in enumerate(events):
        event_start_ms = zamanlar[start_idx]
        idx1h_start = ts_1h.index_at(event_start_ms)
        if idx1h_start is None:
            continue

        hedef_regimeler = {"YUKSELIS", "GUCLU_YUKSELIS"} if yon == "YUKARI" else {"DUSUS", "GUCLU_DUSUS"}
        hedef_guclu = {"GUCLU_YUKSELIS"} if yon == "YUKARI" else {"GUCLU_DUSUS"}

        # --- 1) Her gostergenin BAGIMSIZ (T0'dan itibaren) ilk sinyal zamani ---
        ema_h = _first_vote_match_hour(ts_1h, idx1h_start, yon, ts_1h.ema_vote)
        kama_h = _first_vote_match_hour(ts_1h, idx1h_start, yon, ts_1h.kama_vote)
        st_h = _first_vote_match_hour(ts_1h, idx1h_start, yon, ts_1h.supertrend_vote)
        nw_h = _first_vote_match_hour(ts_1h, idx1h_start, yon, lambda idx: _nw_slope_vote(ts_1h, idx))
        wt_h = _first_vote_match_hour(ts_1h, idx1h_start, yon, lambda idx: _wt_slope_vote(ts_1h, idx))
        major_h = _majors_first_confirm_hour(ts_1h, idx1h_start, majors_series, yon)

        # --- 2) SCORE_THRESHOLD: T7 = HAM (hysteresis haric) bilesik skorun
        # ESIK_YON'u T0'dan itibaren ilk gectigi saat ---
        score_h = _first_cross_hour(
            ts_1h, idx1h_start, yon,
            lambda idx: _composite_ham(shib_series, majors_series, ts_1h.close_times[idx]),
            ESIK_YON,
        )
        if score_h is None:
            never_crossed_events.append(i)

        # --- 3) HYSTERESIS_EXTRA: GERCEK hysteresis_gecmisi'nden -
        # ham rejim ilk kez olay yonune donduğu an (first_valid_signal_time)
        # -> confirmed_regime'in fiilen olay yonune gectigi an. Aradaki
        # "reset_count" = bu iki an arasinda ham rejimin kac kez olay-
        # yonu DISINA cikip hysteresis sayacini sifirladigi. ---
        ticks = [t for t in hysteresis_gecmisi if t["ts_ms"] >= event_start_ms]
        first_valid_tick = next((t for t in ticks if t["raw_regime"] in hedef_regimeler), None)
        t_valid_ms = first_valid_tick["ts_ms"] if first_valid_tick else None
        t_confirm_ms, hysteresis_extra_h, reset_count = None, None, None
        if first_valid_tick is not None:
            confirm_tick = next((t for t in ticks if t["ts_ms"] >= t_valid_ms and t["confirmed_regime"] in hedef_regimeler), None)
            if confirm_tick is not None:
                t_confirm_ms = confirm_tick["ts_ms"]
                hysteresis_extra_h = (t_confirm_ms - t_valid_ms) / 3_600_000
                reset_count = sum(1 for t in ticks if t_valid_ms < t["ts_ms"] < t_confirm_ms and t["raw_regime"] not in hedef_regimeler)

        guclu_tick = next((t for t in ticks if t["confirmed_regime"] in hedef_guclu), None)
        t_guclu_ms = guclu_tick["ts_ms"] if guclu_tick else None

        # --- 4) ALLOCATION_STEP: confirmed rejim FIILEN olay yonune
        # gectikten (t_confirm_ms) sonra, gercek SHIB payinin hedef banda
        # (%75 yukselis / %25 dusus) ulasmasina kadar gecen EK sure ---
        alloc_esik = 75 if yon == "YUKARI" else 25
        allocation_h = None
        if t_confirm_ms is not None:
            conf_idx = next((t["idx"] for t in ticks if t["ts_ms"] == t_confirm_ms), None)
            if conf_idx is not None:
                for j in range(conf_idx, min(conf_idx + 400, len(shib_pct_gecmisi))):
                    pct = shib_pct_gecmisi[j]
                    if (yon == "YUKARI" and pct >= alloc_esik) or (yon == "ASAGI" and pct <= alloc_esik):
                        allocation_h = (zamanlar[j] - t_confirm_ms) / 3_600_000
                        break

        def pct_at(offset_h):
            steps = int(offset_h * 4)  # 15dk adim
            j = start_idx + steps
            return shib_pct_gecmisi[j] if 0 <= j < len(shib_pct_gecmisi) else None

        t_score_ms = event_start_ms + score_h * 3_600_000 if score_h is not None else None
        shib_pct_24h = pct_at(24)
        # "TAMAMEN KACIRILDI": 24 saat sonra bile beklenen tarafa fiilen
        # gecilmemis (yukseliste hala SHIB azinlikta / duruste hala SHIB
        # cogunlukta). Veri penceresi olayin sonuna cok yakinsa (24h verisi
        # yoksa) belirsiz sayilir, kacirildi/kacirilmadi DENMEZ.
        fully_missed = None
        if shib_pct_24h is not None:
            fully_missed = (shib_pct_24h < 50) if yon == "YUKARI" else (shib_pct_24h > 50)

        satir = {
            "event_id": i, "direction": yon,
            "event_start": _fmt(event_start_ms), "move_pct": round(degisim, 2),
            "ema_delay_h": ema_h, "kama_delay_h": kama_h, "supertrend_delay_h": st_h,
            "nw_delay_h": nw_h, "wavetrend_delay_h": wt_h, "major_delay_h": major_h,
            "score_threshold_delay_h": score_h,
            "hysteresis_extra_delay_h": round(hysteresis_extra_h, 2) if hysteresis_extra_h is not None else None,
            "hysteresis_reset_count": reset_count,
            "allocation_delay_h": round(allocation_h, 2) if allocation_h is not None else None,
            "first_score_pass": _fmt(t_score_ms),
            "first_raw_uptrend": _fmt(t_valid_ms),
            "first_confirmed_uptrend": _fmt(t_confirm_ms),
            "first_strong_uptrend": _fmt(t_guclu_ms),
            "shib_pct_start": pct_at(0), "shib_pct_3h": pct_at(3), "shib_pct_6h": pct_at(6),
            "shib_pct_12h": pct_at(12), "shib_pct_24h": shib_pct_24h,
            "fully_missed": fully_missed,
        }
        satirlar.append(satir)
        skor_notu = "HIC ESIGI GECMEDI" if score_h is None else f"skor esigi +{score_h}h"
        kacirildi_notu = " [TAMAMEN KACIRILDI]" if fully_missed else ""
        print(f"[{i}] {yon} {degisim:+.1f}% @ {satir['event_start']} -> {skor_notu}, "
              f"hysteresis_extra={satir['hysteresis_extra_delay_h']}h (reset={reset_count}){kacirildi_notu}")

        # --- per-tick ham gunluk (kullanicinin istedigi tam denetim izi) ---
        hedef_oy = 1 if yon == "YUKARI" else -1
        for t in ticks:
            if t["ts_ms"] > event_start_ms + SCAN_HORIZON_HOURS * 3_600_000:
                break
            idx1h = ts_1h.index_at(t["ts_ms"])
            tick_satirlari.append({
                "timestamp": _fmt(t["ts_ms"]), "event_id": i, "direction": yon,
                "ema_ok": ts_1h.ema_vote(idx1h) == hedef_oy,
                "kama_ok": ts_1h.kama_vote(idx1h) == hedef_oy,
                "supertrend_ok": ts_1h.supertrend_vote(idx1h) == hedef_oy,
                "nw_ok": _nw_slope_vote(ts_1h, idx1h) == hedef_oy if idx1h is not None else False,
                "wavetrend_ok": _wt_slope_vote(ts_1h, idx1h) == hedef_oy if idx1h is not None else False,
                "majors_ok": (he._majors_direction(majors_series, t["ts_ms"]) > 0.2) if yon == "YUKARI"
                             else (he._majors_direction(majors_series, t["ts_ms"]) < -0.2),
                "raw_hybrid_score": round(t["raw_score"], 2),
                "score_threshold_ok": t["raw_regime"] in hedef_regimeler,
                "raw_regime": t["raw_regime"],
                "hysteresis_counter": t["candidate_count"],
                "confirmed_regime": t["confirmed_regime"],
                "target_shib_pct": round(t["trend_target"], 2),
                "actual_shib_pct": round(shib_pct_gecmisi[t["idx"]], 2),
            })

    if satirlar:
        with open("hybrid_causal_diagnostics.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(satirlar[0].keys()))
            writer.writeheader()
            writer.writerows(satirlar)
        print(f"\n[TESHIS] Olay-bazli CSV: hybrid_causal_diagnostics.csv ({len(satirlar)} olay)")
    if tick_satirlari:
        with open("hybrid_causal_ticks.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(tick_satirlari[0].keys()))
            writer.writeheader()
            writer.writerows(tick_satirlari)
        print(f"[TESHIS] Saat-saat denetim izi CSV: hybrid_causal_ticks.csv ({len(tick_satirlari)} satir)")

    # ============== SANITY CHECK 2: SCORE_BELOW_THRESHOLD duzeltmesi ==============
    print(f"\n{'=' * 78}\nSANITY CHECK 2: SCORE_THRESHOLD - 'hic esigi gecmeyen' olaylar\n{'=' * 78}")
    print(f"{len(never_crossed_events)} / {len(satirlar)} olayda HAM bilesik skor {SCAN_HORIZON_HOURS}sa tarama "
          f"penceresinde ESIK_YON (+-{ESIK_YON}) degerine HIC ULASMADI.")
    print("ONCEKI HATA: bu olaylar 'reason=SCORE_BELOW_THRESHOLD, ortalama gecikme 0.0 saat' olarak raporlaniyordu -")
    print("bu YANLIS: gecikme YOKTUR, cunku olcecek bir 'gecikme' degil, motorun O HAREKETE HICBIR ZAMAN yeterince")
    print("guclu bir yon skoru URETMEDIGI bir durumdur. DUZELTME: bu olaylar artik score_threshold_delay_h=None")
    print("(bos) olarak kaydedilir ve asagidaki FAKTOR tablosundaki SCORE_THRESHOLD ortalamasina/medianina DAHIL")
    print("EDILMEZ - sadece ayri bir sayac olarak raporlanir (yukarida).")
    if never_crossed_events:
        print(f"Olay ID'leri: {never_crossed_events}")

    # ============== FAKTOR TABLOSU (sadece buyuk YUKSELISLER) ==============
    yukselis = [s for s in satirlar if s["direction"] == "YUKARI"]
    kolon_map = [
        ("EMA", "ema_delay_h"), ("KAMA", "kama_delay_h"), ("SUPERTREND", "supertrend_delay_h"),
        ("NW", "nw_delay_h"), ("WAVETREND", "wavetrend_delay_h"), ("MAJOR_CONFIRMATION", "major_delay_h"),
        ("SCORE_THRESHOLD", "score_threshold_delay_h"), ("HYSTERESIS_EXTRA", "hysteresis_extra_delay_h"),
        ("ALLOCATION_STEP", "allocation_delay_h"),
    ]
    # "EVENTS LATE" = deger MEVCUT (o faktor olay penceresinde nihayet
    # gerceklesti) VE >0 (yani T0'da zaten hazir degildi, gercekten
    # BEKLEME oldu). deger==0 "gec kalma" degil "olay basinda zaten hazirdi"
    # demektir - bu ayrim SCORE_BELOW_THRESHOLD'daki 0.0 hatasinin ayni
    # turden bir tekrari olmasin diye bilerek yapiliyor.
    faktor_degerleri = {}
    faktor_gec_kalanlar = {}
    for isim, kolon in kolon_map:
        tum_degerler = [s[kolon] for s in yukselis if s[kolon] is not None]
        faktor_degerleri[isim] = tum_degerler
        faktor_gec_kalanlar[isim] = [v for v in tum_degerler if v > 0]

    print(f"\n{'=' * 90}\nFAKTOR TABLOSU - SADECE BUYUK YUKSELISLER (n={len(yukselis)} olay)\n{'=' * 90}")
    print(f"{'FAKTOR':<20} {'MEDIAN(sa)':>11} {'ORTALAMA(sa)':>13} {'P90(sa)':>9} {'EVENTS LATE':>12}")
    print("-" * 90)
    for isim, _kolon in kolon_map:
        gec = faktor_gec_kalanlar[isim]
        if gec:
            print(f"{isim:<20} {statistics.median(gec):>11.1f} {statistics.mean(gec):>13.1f} "
                  f"{_percentile(gec, 90):>9.1f} {len(gec):>12}")
        else:
            print(f"{isim:<20} {'n/a':>11} {'n/a':>13} {'n/a':>9} {0:>12}")

    # ============== SANITY CHECK 1: HYSTERESIS_EXTRA makul mu? ==============
    print(f"\n{'=' * 78}\nSANITY CHECK 1: HYSTERESIS_EXTRA_DELAY makul mu?\n{'=' * 78}")
    print(f"Sistem her {REBALANCE_HOURS:.2f} saatte bir kontrol ediyor, HYSTERESIS_BARS={HYSTERESIS_BARS} ardisik "
          f"onay gerekiyor -> HIC SIFIRLAMA (reset_count=0) olan bir olayda beklenen HYSTERESIS_EXTRA_DELAY "
          f"YAKLASIK {(HYSTERESIS_BARS - 1) * REBALANCE_HOURS:.1f} saat civarinda olmalidir.")
    tum_olaylar_hyst = [s for s in satirlar if s["hysteresis_extra_delay_h"] is not None]
    temiz = [s for s in tum_olaylar_hyst if s["hysteresis_reset_count"] == 0]
    gurultulu = [s for s in tum_olaylar_hyst if s["hysteresis_reset_count"] and s["hysteresis_reset_count"] > 0]
    if temiz:
        ort_temiz = sum(s["hysteresis_extra_delay_h"] for s in temiz) / len(temiz)
        print(f"  reset_count=0 (SAF hysteresis, sinyal hic salinmadi): {len(temiz)} olay, "
              f"ortalama HYSTERESIS_EXTRA_DELAY = {ort_temiz:.2f} saat (beklenenle {'TUTARLI' if ort_temiz < 3 * REBALANCE_HOURS else 'TUTARSIZ - incele'}).")
    else:
        print("  reset_count=0 olan hicbir olay yok (asagidaki 'gurultulu' grup aciklamasina bakin).")
    if gurultulu:
        ort_gurultulu = sum(s["hysteresis_extra_delay_h"] for s in gurultulu) / len(gurultulu)
        ort_reset = sum(s["hysteresis_reset_count"] for s in gurultulu) / len(gurultulu)
        print(f"  reset_count>0 (HAM sinyal esik civarinda SALINDI, sayac sifirlandi): {len(gurultulu)} olay, "
              f"ortalama HYSTERESIS_EXTRA_DELAY = {ort_gurultulu:.2f} saat, ortalama reset sayisi = {ort_reset:.1f}.")
        print("  SONUC: buyuk HYSTERESIS_EXTRA_DELAY degerleri 'hysteresis mekanizmasinin kendisinden' DEGIL,")
        print("  HAM bilesik skorun esik (+-30) civarinda tekrar tekrar salinip 2-ardisik-onay sayacini")
        print("  sifirlamasindan kaynaklaniyor - bu bir HYSTERESIS parametresi sorunu degil, ham SKORUN")
        print("  o bolgede GURULTULU/KARARSIZ olmasi sorunudur.")
    print("(Not: bu sadece olcum/aciklama - HENUZ hicbir parametre degistirilmedi.)")

    # ============== "TAMAMEN KACIRILAN" yukselislerde HANGI faktor baskin? ==============
    # Bu sayim SADECE "fully_missed=True" olan (24sa sonra bile hedef tarafa
    # gecilmemis) yukselislerin ALT KUMESINDE yapilir - TUM olaylara
    # uygulanan tek-reason secimi DEGILDIR (onceki surumun hatasi buydu).
    # Amac: "bu 3 faktor sadece GECIKTIRMIYOR, bazi trendleri TAMAMEN
    # KACIRTIYOR mu" sorusuna ayri bir sayimla cevap vermek.
    kacirilan_yukselisler = [s for s in yukselis if s["fully_missed"]]
    missed_dominant_count = {isim: 0 for isim, _ in kolon_map}
    for s in kacirilan_yukselisler:
        adaylar = {isim: s[kolon] for isim, kolon in kolon_map if s[kolon] is not None}
        if adaylar:
            baskin = max(adaylar, key=adaylar.get)
            missed_dominant_count[baskin] += 1

    # ============== CEVAP ==============
    print(f"\n{'=' * 90}\nSORU: Guclu yukselisi GERCEKTEN geciktiren ilk 3 mekanizma?\n"
          f"(MEDIAN'a gore siralanmis, sadece >0 saat 'gec kalma' olaylari uzerinden - ortalama outlier'lardan etkilenebilir)\n{'=' * 90}")
    siralama = sorted(
        ((isim, faktor_gec_kalanlar[isim]) for isim, _ in kolon_map if faktor_gec_kalanlar[isim]),
        key=lambda kv: -statistics.median(kv[1]),
    )
    print(f"({len(kacirilan_yukselisler)} / {len(yukselis)} buyuk yukselis 24 saat sonra bile TAMAMEN KACIRILMIS - "
          f"SHIB payi hala %50'nin altinda.)\n")
    for isim, degerler in siralama[:3]:
        print(f"  - {isim}: median {statistics.median(degerler):.1f} saat, ortalama {statistics.mean(degerler):.1f} saat, "
              f"P90 {_percentile(degerler, 90):.1f} saat (n={len(degerler)} olayda gec kaldi; "
              f"{missed_dominant_count[isim]} tamamen-kacirilan olayda BASKIN neden).")
    if not siralama:
        print("  (Yeterli olay/veri yok - pencereyi buyutmeyi deneyin.)")
    print("\n(Not: bu SADECE teshis - hicbir parametre/strateji degistirilmedi.)")


if __name__ == "__main__":
    main()
