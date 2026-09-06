"""
ATBE (ADAPTIVE TREND BOOSTER ENGINE) - cekirdek karar motoru.

YON DEGISIKLIGI (kullanicinin acik istegi, ATAE'nin 180 gunluk -27.7% NET /
-44.7% DD sonucundan sonra): "Trend motoruna portfoy uzerinde fazla yetki
verilmis" kok teshisi uzerine, ATBE artik TUM portfoyu YONETMEZ - sadece
sinirli bir BOOSTER sleeve'i yonetir (bkz. atbe_backtest.py'deki CORE/
BOOSTER ayrimi). Bu dosya SADECE booster'in KENDI ic durumunu (8 durumlu
merdiven + evidence havuzlari + risk-permission filtreleri) hesaplar -
portfoyun ne kadarinin booster'a ayrildigina HIC karismaz.

BAGIMSIZLIK: hybrid_engine.py/portfolio_manager.py'ye BAGIMLI DEGIL. ATAE
ailesinden (atae_engine.py) SADECE saf/degistirilmemis yardimci fonksiyonlari
(AtaeSeries, fast_vote, slow_agreement, majors_direction) VE zaten var olan
esik/persistence/decay SABITLERINI tekrar kullanir (import ae) - atae_engine.py
DOSYASI DEGISTIRILMEDI, sadece cagirildi (madde: "state memory / asymmetric
exit / useful early timing fikirlerinden yararlanabilirsin" - kullanicinin
acik izni).

NEDEN 'OVERRIDE' YOK (kasitli mimari secim): ATAE'de STRONG_BULL'a ANLIK
sicrama izni veren OVERRIDE_ESIK mekanizmasi, (once persistence-gated hale
getirilse de) tum "trend motoruna fazla yetki" probleminin bir parcasiydi.
ATBE, ladder'i ASLA atlamaz - her adim TEK TEK, kendi esigi+persistence'i ile
ilerler/geriler (yalnizca sinirli 2-adimlik hizli-geri-cekilme istisnasi
haric, bkz. asagi asagi_gereksinim - bu da ASLA taban durumu atlayip
dogrudan en dip/en tepe durumuna zaplamaz).

8 DURUMLU MERDIVEN (kullanicinin verdigi liste, TEK boyutlu dogal siralama):
  DEFENSIVE - STRONG_BEAR - EARLY_BEAR - NEUTRAL - EARLY_BULL -
  PERSISTENT_BULL - CONFIRMED_BULL - STRONG_BULL
Durum DOGRUDAN toplam portfoy hedefi URETMEZ - SADECE booster sleeve
YUZDESI uretir (bkz. atbe_allocation.py).

MACRO PERMISSION GATE (madde 3) ve HTF RISK FILTER (madde 4): bunlar TIMING
sinyali DEGIL, "bullish booster'a IZIN var mi" sorusuna cevap veren birer
KAPASITE/IZIN filtresidir - her tik'te YENIDEN degerlendirilir (cok saatlik
bir onay-bekleme sayaci YOKTUR, bu yuzden "onlarca saat geciktirme" riski
yapisal olarak yok).
"""

import os

import atae_engine as ae

LADDER = [
    "DEFENSIVE", "STRONG_BEAR", "EARLY_BEAR", "NEUTRAL",
    "EARLY_BULL", "PERSISTENT_BULL", "CONFIRMED_BULL", "STRONG_BULL",
]
LADDER_INDEX = {s: i for i, s in enumerate(LADDER)}
BULLISH_TIERS = ("EARLY_BULL", "PERSISTENT_BULL", "CONFIRMED_BULL", "STRONG_BULL")
BEARISH_TIERS = ("EARLY_BEAR", "STRONG_BEAR", "DEFENSIVE")

# BOOSTER SLEEVE YUZDESI (0..100, BOOSTER SERMAYESININ kendi ici - TOPLAM
# PORTFOY DEGIL). EARLY_BULL/PERSISTENT_BULL/CONFIRMED_BULL/STRONG_BULL =
# kullanicinin LITERAL ornek sayilari (madde 2: "%20/%50/%80/%100").
# DEFENSIVE/STRONG_BEAR/EARLY_BEAR/NEUTRAL = merdivenin geri kalanini
# MONOTON (artan) doldurmak icin secilmis ornek degerler - "exact yuzdeleri
# optimize etme" talimatina uyularak GRID-SEARCH YAPILMADI, sadece verilen
# 4 ankor sayi arasinda mantikli/monoton bir doldurma yapildi.
BOOSTER_TARGET = {
    "DEFENSIVE": 0.0, "STRONG_BEAR": 0.0, "EARLY_BEAR": 8.0, "NEUTRAL": 15.0,
    "EARLY_BULL": 20.0, "PERSISTENT_BULL": 50.0, "CONFIRMED_BULL": 80.0, "STRONG_BULL": 100.0,
}

# RISK-PERMISSION esikleri - ATAE'de ZATEN kullanilan majors-tampon esigi
# (0.3) TEKRAR KULLANILIYOR (atae_engine.py'deki _yukari_gereksinim/
# _asagi_gereksinim'deki AYNI sayi) - yeni sayi UYDURULMADI.
MACRO_STRONGLY_BEARISH = float(os.environ.get("ATBE_MACRO_STRONGLY_BEARISH", "0.3"))
HTF_MAJORITY_RATIO = 0.5  # ae.slow_agreement'in KENDI ">=%50 yeterli" konvansiyonu (madde 8, ATAE)

# HIZLI-GERI-CEKILME ISTISNASI (madde 6 - "explicit bearish evidence varsa
# daha hizli azalt" - AMA sinirsiz degil): bear_evidence GUCLU esigini
# (ae.ENTRY_ESIK_GUCLU=70, reused) PERSISTENCE_CIKIS (2, reused) tik boyunca
# asarsa, normal 1-adim yerine EN FAZLA 2-adim geri cekilme izni verilir -
# ASLA tum merdiveni atlamaz (ATAE'nin OVERRIDE hatasinin AYNISINI
# tekrarlamamak icin YAPISAL olarak sinirlandirilmis).
FAST_RETREAT_STEPS = 2


def yeni_booster_state():
    return {
        "bull_evidence": 0.0, "bear_evidence": 0.0,
        "ladder_state": "NEUTRAL", "prev_ladder_state": None, "state_entry_time": None,
        "persist_up": 0, "persist_down": 0, "fast_retreat_persist": 0,
    }


def _decay(deger):
    return ae._decay(deger)  # AYNI (EXPONENTIAL/LINEAR) fonksiyon - kod tekrari yok


def htf_risk_filter(shib_series, timestamp_ms):
    """SHIB'in KENDI 4h KAMA/SuperTrend/EMA'sindan RISK_ON/NEUTRAL/RISK_OFF
    (madde 4). TIMING degil - booster'in bullish katmanlara ilerleyip
    ilerleyemeyecegine dair bir KAPASITE izni. AtaeSeries'in mevcut oy
    fonksiyonlari (kama_vote/supertrend_vote/ema_vote) DEGISTIRILMEDEN
    kullanilir."""
    ts4h = shib_series.get("4h")
    if ts4h is None:
        return "NEUTRAL"
    idx = ts4h.index_at(timestamp_ms)
    if idx is None:
        return "NEUTRAL"
    oylar = [ts4h.kama_vote(idx), ts4h.supertrend_vote(idx), ts4h.ema_vote(idx)]
    gecerli = [o for o in oylar if o is not None]
    if not gecerli:
        return "NEUTRAL"
    bullish = sum(1 for o in gecerli if o == 1)
    bearish = sum(1 for o in gecerli if o == -1)
    if bullish >= 2 and bullish > bearish:
        return "RISK_ON"
    if bearish >= 2 and bearish > bullish:
        return "RISK_OFF"
    return "NEUTRAL"


def macro_permission(shib_series, majors_series, timestamp_ms):
    """BTC/ETH/BNB'yi ENTRY TIMING olarak DEGIL, RISK PERMISSION GATE olarak
    kullanir (madde 3): SHIB higher-timeframe TAMAMEN bearish DEGIL VE
    majors majority STRONGLY bearish DEGIL ise bullish booster'a izin var.
    Her tik'te YENIDEN hesaplanir - cok-saatlik bir "onay bekleme" katmani
    YOK (madde 3 sonu: "onlarca saat geciktirme")."""
    shib_htf_bearish = ae.slow_agreement(shib_series, timestamp_ms, -1) >= HTF_MAJORITY_RATIO
    majors_dir = ae.majors_direction(majors_series, timestamp_ms)
    majors_strongly_bearish = majors_dir < -MACRO_STRONGLY_BEARISH
    return not (shib_htf_bearish and majors_strongly_bearish), majors_dir


def _entry_gereksinim(hedef_tier):
    """NEUTRAL'dan STRONG_BULL'a giden 4 basamak icin ATAE'de ZATEN var olan
    ERKEN/ORTA/GUCLU esik+persistence uclusunu (madde: 'yeni sayi
    uydurulmadi') 4 basamaga esitleyerek uygular - orta iki basamak ORTA
    esigini paylasir, ama SADECE ust-orta basamak (CONFIRMED->STRONG) EK
    olarak slow-layer 'kalite kontrolu' (ATAE'nin BULL/STRONG_BULL adimlarindaki
    AYNI mantik) VE majors tamponu gerektirir."""
    if hedef_tier == "EARLY_BULL":
        return ae.ENTRY_ESIK_ERKEN, ae.PERSISTENCE_ERKEN, False, False
    if hedef_tier == "PERSISTENT_BULL":
        return ae.ENTRY_ESIK_ORTA, ae.PERSISTENCE_ERKEN, False, False
    if hedef_tier == "CONFIRMED_BULL":
        return ae.ENTRY_ESIK_ORTA, ae.PERSISTENCE_ORTA, True, False
    if hedef_tier == "STRONG_BULL":
        return ae.ENTRY_ESIK_GUCLU, ae.PERSISTENCE_GUCLU, True, True
    return ae.ENTRY_ESIK_ERKEN, ae.PERSISTENCE_ERKEN, False, False


def _bear_entry_gereksinim(hedef_tier):
    """NEUTRAL'dan DEFENSIVE'a giden bear-ladder ilerlemesi - ayni
    ERKEN/ORTA/GUCLU esik ailesini (reused) simetrik kullanir (asimetri
    SADECE cikis/exit tarafinda, madde 9 - bear ladder'in kendi ILERLEMESI
    bull ladder'in ilerlemesiyle es esiktedir, cunku ikisi de 'evidence
    biriktirerek bir sonraki adima gecis', asimetri sadece bullish
    pozisyondan GERI CEKILMEDE devreye girer)."""
    if hedef_tier == "EARLY_BEAR":
        return ae.ENTRY_ESIK_ERKEN, ae.PERSISTENCE_ERKEN
    if hedef_tier == "STRONG_BEAR":
        return ae.ENTRY_ESIK_ORTA, ae.PERSISTENCE_ORTA
    if hedef_tier == "DEFENSIVE":
        return ae.ENTRY_ESIK_GUCLU, ae.PERSISTENCE_GUCLU
    return ae.ENTRY_ESIK_ERKEN, ae.PERSISTENCE_ERKEN


def _exit_gereksinim(mevcut_tier, majors_dir):
    """Bullish katmandan GERI CEKILME esikleri - ATAE'nin EXIT_ESIK ailesini
    (ENTRY'den HER ZAMAN yuksek - asimetri, madde 9) reuse eder."""
    if mevcut_tier == "STRONG_BULL":
        tampon = ae.MAJORS_TAMPON if majors_dir > 0.3 else 0.0
        return ae.EXIT_ESIK_GUCLU + tampon, ae.PERSISTENCE_CIKIS
    if mevcut_tier in ("CONFIRMED_BULL", "PERSISTENT_BULL"):
        return ae.EXIT_ESIK_ORTA, ae.PERSISTENCE_CIKIS
    if mevcut_tier == "EARLY_BULL":
        return ae.EXIT_ESIK_ERKEN, ae.PERSISTENCE_CIKIS
    return ae.EXIT_ESIK_ERKEN, ae.PERSISTENCE_CIKIS


def step(state, shib_series, majors_series, timestamp_ms):
    """TEK karar tik'i. state YERINDE guncellenir. Donen dict telemetri icin
    tum ara degerleri tasir (booster allocation + coordinator + diagnostics
    bunlari OKUR, hicbiri bu fonksiyonu DEGISTIRMEZ)."""
    fast_yon, fast_guc = ae.fast_vote(shib_series, timestamp_ms)
    majors_dir = ae.majors_direction(majors_series, timestamp_ms)

    push = ae.PUSH_MAX * fast_guc
    if fast_yon == 1:
        if abs(majors_dir) > 0.3:
            push *= 1.15 if majors_dir > 0 else 0.7
        state["bull_evidence"] = min(100.0, _decay(state["bull_evidence"]) + push)
        state["bear_evidence"] = _decay(state["bear_evidence"])
    elif fast_yon == -1:
        if abs(majors_dir) > 0.3:
            push *= 1.15 if majors_dir < 0 else 0.7
        state["bear_evidence"] = min(100.0, _decay(state["bear_evidence"]) + push)
        state["bull_evidence"] = _decay(state["bull_evidence"])
    else:
        state["bull_evidence"] = _decay(state["bull_evidence"])
        state["bear_evidence"] = _decay(state["bear_evidence"])

    bull_ev, bear_ev = state["bull_evidence"], state["bear_evidence"]
    cur = state["ladder_state"]
    cur_idx = LADDER_INDEX[cur]

    bullish_allowed, _ = macro_permission(shib_series, majors_series, timestamp_ms)
    htf_regime = htf_risk_filter(shib_series, timestamp_ms)
    if htf_regime == "RISK_OFF":
        bullish_allowed = False

    yeni_state, neden = cur, None

    # --- ILERI (daha bullish) adim - SADECE bullish_allowed ise ---
    if cur_idx < len(LADDER) - 1:
        hedef = LADDER[cur_idx + 1]
        if hedef in BULLISH_TIERS and not bullish_allowed:
            state["persist_up"] = 0  # izin yokken sayac birikmesin (kapi acilinca "biriken kanit" ile aninda atlama olmasin)
        else:
            if hedef in BULLISH_TIERS:
                esik, persist_gerek, slow_gerekli, majors_gerekli = _entry_gereksinim(hedef)
            else:  # NEUTRAL -> EARLY_BULL ust sinirindaki NEUTRAL kendisi bullish degil; bear tarafinda ileri = daha AZ bearish
                esik, persist_gerek = _bear_entry_gereksinim_ters(cur)
                slow_gerekli, majors_gerekli = False, False
            if bull_ev >= esik:
                state["persist_up"] += 1
            else:
                state["persist_up"] = 0
            if bull_ev >= esik and state["persist_up"] >= persist_gerek:
                slow_ok = (not slow_gerekli) or (ae.slow_agreement(shib_series, timestamp_ms, 1) >= HTF_MAJORITY_RATIO)
                majors_ok = (not majors_gerekli) or majors_dir >= -MACRO_STRONGLY_BEARISH
                if slow_ok and majors_ok:
                    yeni_state, neden = hedef, "EVIDENCE_UP"

    # --- GERI (daha bearish/daha az bullish) adim - HICBIR ZAMAN engellenmez ---
    if yeni_state == cur and cur_idx > 0:
        hedef_geri = LADDER[cur_idx - 1]
        if cur in BULLISH_TIERS:
            esik, persist_gerek = _exit_gereksinim(cur, majors_dir)
            if bear_ev >= esik:
                state["persist_down"] += 1
            else:
                state["persist_down"] = 0
            if bear_ev >= esik and state["persist_down"] >= persist_gerek:
                # HIZLI-GERI-CEKILME ISTISNASI (madde 6, sinirli 2-adim, ASLA daha fazla)
                if bear_ev >= ae.ENTRY_ESIK_GUCLU:
                    state["fast_retreat_persist"] += 1
                else:
                    state["fast_retreat_persist"] = 0
                if state["fast_retreat_persist"] >= ae.PERSISTENCE_CIKIS:
                    atlama = min(FAST_RETREAT_STEPS, cur_idx)
                    yeni_state, neden = LADDER[cur_idx - atlama], "FAST_RETREAT"
                else:
                    yeni_state, neden = hedef_geri, "EVIDENCE_DOWN"
        else:
            esik, persist_gerek = _bear_entry_gereksinim(hedef_geri)
            if bear_ev >= esik:
                state["persist_down"] += 1
            else:
                state["persist_down"] = 0
            if bear_ev >= esik and state["persist_down"] >= persist_gerek:
                yeni_state, neden = hedef_geri, "EVIDENCE_DOWN"

    degisti = yeni_state != cur
    if degisti:
        state["prev_ladder_state"] = cur
        state["ladder_state"] = yeni_state
        state["state_entry_time"] = timestamp_ms
        state["persist_up"] = 0
        state["persist_down"] = 0
        state["fast_retreat_persist"] = 0

    return {
        "ladder_state": state["ladder_state"], "changed": degisti, "reason": neden,
        "bull_evidence": bull_ev, "bear_evidence": bear_ev,
        "fast_dir": fast_yon, "fast_strength": fast_guc, "majors_dir": majors_dir,
        "htf_regime": htf_regime, "bullish_allowed": bullish_allowed,
    }


def _bear_entry_gereksinim_ters(cur):
    """cur bearish/NEUTRAL bir katmandayken bir sonraki (daha az bearish)
    katmana ilerlemek icin GEREKEN esik - bear_entry ile AYNI esik ailesi,
    sadece yon ters (bull_evidence uzerinden, cunku 'daha az bearish olma'
    aslinda bullish kanittir)."""
    if cur == "DEFENSIVE":
        return ae.ENTRY_ESIK_GUCLU, ae.PERSISTENCE_GUCLU
    if cur == "STRONG_BEAR":
        return ae.ENTRY_ESIK_ORTA, ae.PERSISTENCE_ORTA
    if cur == "EARLY_BEAR":
        return ae.ENTRY_ESIK_ERKEN, ae.PERSISTENCE_ERKEN
    return ae.ENTRY_ESIK_ERKEN, ae.PERSISTENCE_ERKEN
