"""
HYBRID v4 hazirlik adimi: EARLY SIGNAL QUALITY AUDIT - SALT TESHIS.

v1/v2/v3 dosyalarina DOKUNMAZ - hybrid_v3_backtest.py'nin V3-C (persistence+
asymmetric-exit) konfigurasyonunu DEGISTIRMEDEN calistirir, urettigi HER
EARLY_BULLISH episode'unu (latch_episodes) alip, giris anindaki HAM
komponent oylarini (EMA/WaveTrend/NW - entry'de zaten kullanilanlar; KAMA/
SuperTrend/Majors - entry'de KULLANILMAYAN ama "kalite sinyali olarak
degeri var mi" sorusu icin OKUNAN, gate OLARAK KULLANILMAYAN ek veriler)
ve GERCEKTE ne oldugunu (6/12/24 saatlik ileri getiri) kaydedip, "faydali
early giris" ile "yanlis early giris"i hangi komponent kombinasyonlarinin
ayirdigini raporlar.

Yeni indikator EKLENMEDI - regime_indicators.py / hybrid_engine.py'nin
ZATEN hesapladigi degerler (ts.ema_vote/kama_vote/supertrend_vote,
he._majors_direction, he.classify()) OKUNUR, degistirilmez.

Siniflandirma (max/min ileri getiri - 24 saatlik pencerede, SABIT/
mantikli esikler, optimize edilmedi):
  USEFUL_BIG_MOVE : max_forward_return_24h >= %5  (hb.BIG_MOVE_THRESHOLD_
                     PERCENT ile AYNI - "buyuk hareket" tanimi kodun geri
                     kalaniyla TUTARLI, yeni bir sayi uydurulmadi)
  SMALL_UP        : %1 <= max_forward_return_24h < %5
  FAILED_REVERSED : yukaridakiler degil VE min_forward_return_24h <= -%1
  NOISE_FLAT      : digerleri (fiyat kabaca yatay kaldi)

Cikti: hybrid_v4_early_episode_audit.csv + konsolda CATEGORY x component-
uyum-orani tablosu + kullanicinin 4 sorusuna dogrudan cevap.

Kullanim: BACKTEST_DAYS=180 python3 hybrid_v4_early_signal_audit.py
"""

import csv
import os
import statistics
import time

from trade_bot import _load_dotenv
import hybrid_engine as he
import hybrid_backtest as hb
import hybrid_v2_backtest as v2b
import hybrid_v3_backtest as v3b

_load_dotenv()

BACKTEST_DAYS = int(os.environ.get("BACKTEST_DAYS", "180"))
BIG_MOVE_PCT = hb.BIG_MOVE_THRESHOLD_PERCENT  # %5 - kodun geri kalaniyla AYNI tanim
SMALL_MOVE_PCT = 1.0  # "yatay/gurultu" ile "kucuk yukselis" ayrimi icin sabit, kucuk bir esik


def _wt_slope_vote(ts, idx, lookback=2):
    if ts.wt1 is None or idx is None or idx < lookback:
        return None
    a, b = ts.wt1[idx], ts.wt1[idx - lookback]
    if a is None or b is None:
        return None
    return 1 if a > b else (-1 if a < b else 0)


def _nw_slope_vote(ts, idx, lookback=3):
    if ts.nw is None or idx is None or idx < lookback:
        return None
    a, b = ts.nw[idx], ts.nw[idx - lookback]
    if a is None or b is None:
        return None
    return 1 if a > b else (-1 if a < b else 0)


def _oy_yonu(oylar):
    oylar = [o for o in oylar if o is not None]
    if not oylar:
        return None
    toplam = sum(oylar)
    return 1 if toplam > 0 else (-1 if toplam < 0 else 0)


def _component_votes(early_series, shib_series, majors_series, ts_ms):
    """Giris anindaki TUM komponentlerin HAM oyu - bazilari (EMA/WT/NW)
    entry KARARINA zaten giriyor, bazilari (KAMA/SuperTrend/Majors/HAM
    skor) SADECE bu audit icin okunuyor, hicbir karari ETKILEMEZ."""
    ema_votes, wt_votes, nw_votes = [], [], []
    for ts in early_series.values():
        idx = ts.index_at(ts_ms)
        if idx is None:
            continue
        ema_votes.append(ts.ema_vote(idx))
        wt_votes.append(_wt_slope_vote(ts, idx))
        nw_votes.append(_nw_slope_vote(ts, idx))

    ts1h = shib_series.get("1h")
    idx1h = ts1h.index_at(ts_ms) if ts1h else None
    kama_v = ts1h.kama_vote(idx1h) if (ts1h and idx1h is not None) else None
    st_v = ts1h.supertrend_vote(idx1h) if (ts1h and idx1h is not None) else None
    majors_dir = he._majors_direction(majors_series, ts_ms)
    composite = he.classify(shib_series, majors_series, ts_ms, prev_score=0.0)["score"]

    return {
        "ema": _oy_yonu(ema_votes), "wavetrend": _oy_yonu(wt_votes), "nw": _oy_yonu(nw_votes),
        "kama": kama_v, "supertrend": st_v, "majors_dir": round(majors_dir, 3), "raw_score": round(composite, 1),
    }


def _fmt(ts_ms):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts_ms / 1000)) if ts_ms is not None else None


def _kategori(max24, min24):
    if max24 is None:
        return "VERI_YOK"
    if max24 >= BIG_MOVE_PCT:
        return "USEFUL_BIG_MOVE"
    if max24 >= SMALL_MOVE_PCT:
        return "SMALL_UP"
    if min24 is not None and min24 <= -SMALL_MOVE_PCT:
        return "FAILED_REVERSED"
    return "NOISE_FLAT"


def main():
    print(f"[V4-AUDIT] Veri cekiliyor: {v3b.SYMBOL} + majors, son {BACKTEST_DAYS} gun...")
    shib_series, majors_series, zamanlar, kapanislar = hb._fetch_hybrid_all(BACKTEST_DAYS)
    early_series = v2b._fetch_early_series(BACKTEST_DAYS)
    n = len(kapanislar)

    print("[V4-AUDIT] v1 confirmed-rejim akisi icin hb.simulate() calistiriliyor...")
    v1_sonuc = hb.simulate(shib_series, majors_series, zamanlar, kapanislar)

    print("[V4-AUDIT] V3-C (persistence+asymmetric-exit) calistiriliyor (DEGISTIRILMEDEN)...")
    v3c_sonuc = v3b.simulate_v3(shib_series, majors_series, early_series, zamanlar, kapanislar,
                                 v1_sonuc["hysteresis_gecmisi"], use_persistence=True, use_asymmetric_exit=True)

    ts_to_idx = {e["ts_ms"]: e["idx"] for e in v3c_sonuc["early_gecmisi"]}
    bullish_episodes = [ep for ep in v3c_sonuc["latch_episodes"]
                         if ep["latched_state"] == "EARLY_BULLISH" and ep["state_entry_time"] is not None]
    print(f"[V4-AUDIT] {len(bullish_episodes)} EARLY_BULLISH episode bulundu (V3-C, {BACKTEST_DAYS} gun).\n")

    satirlar = []
    for i, ep in enumerate(bullish_episodes):
        entry_ms = ep["state_entry_time"]
        idx_entry = ts_to_idx.get(entry_ms)
        if idx_entry is None:
            continue
        entry_price = kapanislar[idx_entry]

        def ileri_getiri(saat):
            adim = int(saat * 4)
            pencere = kapanislar[idx_entry:min(idx_entry + adim + 1, n)]
            if not pencere:
                return None, None
            return (max(pencere) - entry_price) / entry_price * 100, (min(pencere) - entry_price) / entry_price * 100

        max6, min6 = ileri_getiri(6)
        max12, min12 = ileri_getiri(12)
        max24, min24 = ileri_getiri(24)
        kategori = _kategori(max24, min24)

        bilesenler = _component_votes(early_series, shib_series, majors_series, entry_ms)

        satir = {
            "episode_id": i, "start_time": _fmt(entry_ms), "end_time": _fmt(ep["state_exit_time"]),
            "duration_h": ep["state_duration_h"], "entry_price": entry_price,
            "max_forward_return_6h": round(max6, 2) if max6 is not None else None,
            "min_forward_return_6h": round(min6, 2) if min6 is not None else None,
            "max_forward_return_12h": round(max12, 2) if max12 is not None else None,
            "min_forward_return_12h": round(min12, 2) if min12 is not None else None,
            "max_forward_return_24h": round(max24, 2) if max24 is not None else None,
            "min_forward_return_24h": round(min24, 2) if min24 is not None else None,
            "ema": bilesenler["ema"], "wavetrend": bilesenler["wavetrend"], "nw": bilesenler["nw"],
            "kama": bilesenler["kama"], "supertrend": bilesenler["supertrend"],
            "majors_dir": bilesenler["majors_dir"], "raw_score": bilesenler["raw_score"],
            "reset_count_while_latched": ep["reset_count_while_latched"], "exit_reason": ep["exit_reason"],
            "category": kategori,
        }
        satirlar.append(satir)

    if satirlar:
        with open("hybrid_v4_early_episode_audit.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(satirlar[0].keys()))
            writer.writeheader()
            writer.writerows(satirlar)
        print(f"[V4-AUDIT] CSV kaydedildi: hybrid_v4_early_episode_audit.csv ({len(satirlar)} episode)")

    # ============== KATEGORI DAGILIMI + KOMPONENT UYUM ORANLARI ==============
    kategoriler = {}
    for s in satirlar:
        kategoriler.setdefault(s["category"], []).append(s)

    print(f"\n{'=' * 108}\nKATEGORI DAGILIMI + KOMPONENT UYUM ORANLARI (bullish yonle AYNI oy verme orani)\n{'=' * 108}")
    print(f"{'CATEGORY':<18} {'N':>5} {'%TOPLAM':>8} {'EMA%':>7} {'WT%':>7} {'NW%':>7} {'KAMA%':>7} {'ST%':>7} {'MAJORS%':>8} {'ort.sure(sa)':>13}")
    print("-" * 108)

    def _uyum_orani(grup, kolon, hedef_test):
        degerler = [s[kolon] for s in grup if s[kolon] is not None]
        if not degerler:
            return None
        return sum(1 for d in degerler if hedef_test(d)) / len(degerler) * 100

    toplam_n = len(satirlar)
    ozet = {}
    for kat in ("USEFUL_BIG_MOVE", "SMALL_UP", "FAILED_REVERSED", "NOISE_FLAT", "VERI_YOK"):
        grup = kategoriler.get(kat, [])
        if not grup:
            continue
        ema_p = _uyum_orani(grup, "ema", lambda d: d == 1)
        wt_p = _uyum_orani(grup, "wavetrend", lambda d: d == 1)
        nw_p = _uyum_orani(grup, "nw", lambda d: d == 1)
        kama_p = _uyum_orani(grup, "kama", lambda d: d == 1)
        st_p = _uyum_orani(grup, "supertrend", lambda d: d == 1)
        majors_p = _uyum_orani(grup, "majors_dir", lambda d: d > 0.2)
        sureler = [s["duration_h"] for s in grup if s["duration_h"] is not None]
        ozet[kat] = {"ema": ema_p, "wt": wt_p, "nw": nw_p, "kama": kama_p, "st": st_p, "majors": majors_p, "n": len(grup)}

        def f(v):
            return f"{v:.0f}" if v is not None else "n/a"
        print(f"{kat:<18} {len(grup):>5} {len(grup) / toplam_n * 100:>7.1f}% {f(ema_p):>7} {f(wt_p):>7} {f(nw_p):>7} "
              f"{f(kama_p):>7} {f(st_p):>7} {f(majors_p):>8} {(statistics.mean(sureler) if sureler else float('nan')):>13.1f}")

    # ============== 4 SORUYA DOGRUDAN CEVAP ==============
    # ONEMLI DUZELTME: "yanlis" (basarisiz+gurultu) grubunun bilesen-uyum
    # oranlari, FAILED_REVERSED (buyuk n) ile NOISE_FLAT (kucuk n) grup
    # YUZDELERININ DUZ ORTALAMASI (statistics.mean) DEGIL, OLAY SAYISINA
    # GORE AGIRLIKLI ortalamasi olmali - iki grubun n'i cok farkliysa
    # (orn. 53 vs 5) duz ortalama kucuk grubun rastgele degerine asiri
    # agirlik verip yanlis sonuc uretebilir (bu hata ilk surumde vardi ve
    # duzeltildi - bkz. commit mesaji).
    print(f"\n{'=' * 108}\nFEATURE SEPARATION - 4 SORUYA DOGRUDAN CEVAP\n{'=' * 108}")
    faydali = ozet.get("USEFUL_BIG_MOVE")
    yanlis_gruplari = [g for k, g in ozet.items() if k in ("FAILED_REVERSED", "NOISE_FLAT")]

    def _agirlikli_ortalama(kolon):
        toplam_n, toplam_deger = 0, 0.0
        for g in yanlis_gruplari:
            if g[kolon] is not None:
                toplam_n += g["n"]
                toplam_deger += g[kolon] * g["n"]
        return (toplam_deger / toplam_n) if toplam_n else None

    if faydali and yanlis_gruplari:
        yanlis_ema = _agirlikli_ortalama("ema")
        yanlis_wt = _agirlikli_ortalama("wt")
        yanlis_nw = _agirlikli_ortalama("nw")
        yanlis_kama = _agirlikli_ortalama("kama")
        yanlis_st = _agirlikli_ortalama("st")
        yanlis_majors = _agirlikli_ortalama("majors")

        print(f"1) Faydali girislerde EMA/WT/NW birlikte oy verme orani : EMA=%{faydali['ema']:.0f} WT=%{faydali['wt']:.0f} NW=%{faydali['nw']:.0f}"
              if faydali['ema'] is not None else "1) veri yok")
        print(f"   (Basarisiz+gurultu gruplarinda)                     : EMA=%{yanlis_ema:.0f} WT=%{yanlis_wt:.0f} NW=%{yanlis_nw:.0f}"
              if yanlis_ema is not None else "")
        farklar = {"EMA": (faydali["ema"] or 0) - (yanlis_ema or 0), "WAVETREND": (faydali["wt"] or 0) - (yanlis_wt or 0),
                   "NW": (faydali["nw"] or 0) - (yanlis_nw or 0)}
        en_ayirici = max(farklar, key=farklar.get)
        print(f"2) Yanlis-pozitif girislerde EN COK EKSIK olan komponent (faydali-yanlis farki en buyuk): {en_ayirici} "
              f"({farklar[en_ayirici]:+.0f} puan fark)")
        if yanlis_majors is not None and faydali["majors"] is not None:
            fark_majors = faydali["majors"] - yanlis_majors
            print(f"3) MAJORS uyum orani: faydali=%{faydali['majors']:.0f} vs yanlis=%{yanlis_majors:.0f} (fark {fark_majors:+.0f} puan) - "
                  f"{'majors GERCEKTEN ayirt edici bir filtre gibi davraniyor' if fark_majors > 15 else 'majors bu veri setinde belirgin ayirt edici degil (fark kucuk)'}")
        else:
            print("3) MAJORS icin yeterli veri yok")
        if yanlis_kama is not None and faydali["kama"] is not None:
            fark_kama = faydali["kama"] - yanlis_kama
            fark_st = (faydali["st"] - yanlis_st) if (yanlis_st is not None and faydali["st"] is not None) else None
            print(f"4) KAMA uyum farki (faydali-yanlis): {fark_kama:+.0f} puan.  SuperTrend uyum farki: "
                  f"{f'{fark_st:+.0f} puan' if fark_st is not None else 'n/a'} - "
                  f"{'ikisi de kalite-skoru olarak ANLAMLI ayirt edici' if (fark_kama > 15 and (fark_st or 0) > 15) else 'en az biri bu veri setinde zayif ayirt edici - gate DEGIL ama guclu bir quality-sinyali de degil'}")
        else:
            print("4) KAMA/SuperTrend icin yeterli veri yok")
    else:
        print("Yeterli USEFUL_BIG_MOVE veya karsilastirma grubu yok - pencereyi buyutmeyi deneyin.")

    print("\n(Not: bu SADECE teshis/feature-separation - hicbir threshold/gate/parametre henuz degistirilmedi.)")


if __name__ == "__main__":
    main()
