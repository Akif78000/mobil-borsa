"""
ATAE DIAGNOSTICS - salt olcum/siniflandirma yardimcilari. atae_engine.py/
atae_allocation.py'ye BAGIMLI DEGIL (sadece hazir sayisal diziler alir) -
atae_backtest.py'nin simulate() TAMAMLANDIKTAN SONRA cagirdigi, POST-HOC
bir katman. Hicbir fonksiyon burada KARAR VERMEZ / islem tetiklemez -
madde 0'in acik istegi geregi finansal simulasyon ile teshis KESIN olarak
ayrilmistir.

MISSED TREND SINIFLANDIRMASI (madde 20 - tek boolean YERINE 4 kategori):
  GOOD_CAPTURE      : 24 saat icinde SHIB payi >=%70'e ULASTI VE bu %12
                      saatten once oldu (hem dogru YON hem dogru HIZ).
  LATE_CAPTURE      : sonunda >=%70'e ulasti ama >12 saat surdu (dogru
                      yon, sinyal KALITESI degil TIMING sorunu).
  UNDERALLOCATED    : en az %50'ye ulasti (motor DOGRU tarafa gecti) ama
                      24 saat icinde HIC %70'e cikamadi (sinyal DOGRU,
                      allocation MIKTARI/mimarisi yetersiz).
  COMPLETELY_MISSED : 24 saat icinde HIC %50'ye ulasamadi (sinyalin
                      KENDISI basarisiz - motor bu harekete hic tepki
                      vermedi).
  Esikler (%50/%70) kodun geri kalaninda ZATEN kullanilan "yakalama"
  (%50) ve ATAE'nin kendi BULL tier alt siniri (%70, PROFILE_BASE'den)
  konvansiyonlaridir - yeni sayi UYDURULMADI.
"""

MISS_BAR = 50.0
GOOD_BAR = 70.0
LATE_HOURS = 12.0


def classify_capture(max_shib_24h, time_to_70_hours):
    """max_shib_24h: olay basindan 24 saat sonrasina kadar ulasilan TEPE
    SHIB payi. time_to_70_hours: %70'e ilk ulasim saati (hic ulasmadiysa
    None)."""
    if max_shib_24h is None or max_shib_24h < MISS_BAR:
        return "COMPLETELY_MISSED"
    if max_shib_24h >= GOOD_BAR:
        if time_to_70_hours is not None and time_to_70_hours <= LATE_HOURS:
            return "GOOD_CAPTURE"
        return "LATE_CAPTURE"
    return "UNDERALLOCATED"


def classify_capture_down(min_usdt_24h, time_to_70_usdt_hours):
    """Guclu DUSUS'ler icin ayna-simetrik siniflandirma (USDT payi
    uzerinden - "korunma" basarisi)."""
    if min_usdt_24h is None or min_usdt_24h < MISS_BAR:
        return "COMPLETELY_MISSED"
    if min_usdt_24h >= GOOD_BAR:
        if time_to_70_usdt_hours is not None and time_to_70_usdt_hours <= LATE_HOURS:
            return "GOOD_CAPTURE"
        return "LATE_CAPTURE"
    return "UNDERALLOCATED"


def false_positive_stats(episodes):
    """episodes: dict listesi, her biri en az {"mfe_pct", "cost", "turnover",
    "mae_pct", "duration_h", "useful": bool} icermeli (bkz. atae_backtest.py
    _episode_report). "useful=False" olan (bkz. atae_diagnostics.
    classify_episode_outcome) episode'lar icin AYRI metrikler - madde 18'in
    acik istegi: SADECE sayim degil, MALIYET/TURNOVER/MAE/SURE de raporla,
    boylece kucuk-maliyetli bir yanlis-alarm ile buyuk-maliyetli bir
    yanlis-alarm AYNI kefeye konmasin."""
    yanlislar = [e for e in episodes if not e.get("useful", True)]
    if not yanlislar:
        return {"false_positive_count": 0, "false_positive_cost": 0.0,
                "false_positive_turnover": 0.0, "false_positive_mae": None,
                "false_positive_duration_h": None}
    return {
        "false_positive_count": len(yanlislar),
        "false_positive_cost": sum(e.get("cost", 0.0) for e in yanlislar),
        "false_positive_turnover": sum(e.get("turnover", 0.0) for e in yanlislar),
        "false_positive_mae": sum(e.get("mae_pct", 0.0) for e in yanlislar) / len(yanlislar),
        "false_positive_duration_h": sum(e.get("duration_h", 0.0) for e in yanlislar) / len(yanlislar),
    }


def classify_episode_outcome(mfe_pct, useful_move_threshold=1.0):
    """Bir state-episode'unun (orn. BULL_EARLY/RECOVERY suresi) 'faydali'
    sayilip sayilmayacagi - MFE (max favorable excursion, fiyatin episode
    SURESI boyunca giris fiyatina gore ulastigi EN IYI nokta) esikli.
    KUCUK, sabit bir esik (%1) - optimize EDILMEDI (madde 31)."""
    return mfe_pct is not None and mfe_pct >= useful_move_threshold


def walk_forward_split(zamanlar, equity_egrisi, n_parca=3):
    """180 gunluk pencereyi n_parca esit bolgeye ayirip HER BOLGENIN KENDI
    NET getirisini raporlar - parametre DEGISTIRILMEDEN (madde 24), sadece
    ayni mantigin farkli alt-donemlerde ayakta kalip kalmadigini gormek
    icin. Robustness OLCUMU - optimizasyon ARACI DEGIL."""
    n = len(equity_egrisi)
    if n < n_parca * 2:
        return []
    parca_uzunluk = n // n_parca
    sonuclar = []
    for i in range(n_parca):
        start = i * parca_uzunluk
        end = n - 1 if i == n_parca - 1 else (i + 1) * parca_uzunluk
        baslangic_deger = equity_egrisi[start]
        bitis_deger = equity_egrisi[end]
        net = (bitis_deger - baslangic_deger) / baslangic_deger * 100 if baslangic_deger else 0.0
        sonuclar.append({
            "parca": i + 1, "start_idx": start, "end_idx": end,
            "start_time": zamanlar[start], "end_time": zamanlar[end], "net_getiri": net,
        })
    return sonuclar
