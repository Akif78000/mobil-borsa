"""
ATAE ALLOCATION ENGINE - state -> hedef SHIB yuzdesi, STAGED (kademeli)
gecis, EXECUTION DEADBAND, REBALANCE COOLDOWN. atae_engine.py'ye BAGIMLI
(STATES adlarini kullanir) ama HICBIR eski HYBRID/GRID dosyasina bagli
DEGIL - tamamen bagimsiz, kucuk, saf fonksiyonlar.

3 ADAY ALLOCATION PROFILI (madde 12 - kullanicinin verdigi ORNEK sayilar,
FINAL/optimize edilmis DEGIL, kod icinde acikca boyle isaretlendi):
  PROFILE_BASE          - kullanicinin literal ornegi
  PROFILE_CONSERVATIVE  - daha dar bullish maruziyet, daha savunmaci
  PROFILE_AGGRESSIVE    - daha genis bullish maruziyet, daha hizli olcekleme
Varsayilan olarak TUM ATAE varyantlari (A/B/C) PROFILE_BASE kullanir -
diger ikisi ileri arastirma icin HAZIR ama ana karsilastirmada calistirilmaz
(madde 31/32: ayni anda cok fazla degisken degistirip sonucu aciklanamaz
hale getirmemek icin).

STAGED SCALE-IN/OUT (madde 14): hedef her tik'te en fazla MAX_STEP_UP (yukari)
/ MAX_STEP_DOWN (asagi, DAHA BUYUK - risk azaltma daha hizli olabilir,
kullanicinin acik istegi) kadar degisebilir - ani %20->%90 sicramasi YOK.

EXECUTION DEADBAND (madde 15): |target-actual| < DEADBAND ise HICBIR ISLEM
yapilmaz - turnover kontrolunun ana parcasi.

REBALANCE COOLDOWN (madde 16): ayni yonde kucuk degisiklikler icin ardisik
tik'lerde tekrar tekrar islem YAPILMAZ (COOLDOWN_TICKS beklenir) - ANCAK
guclu risk-reversal (OVERRIDE_BEAR / BEAR-STRONG_BEAR'a giris) bu bekleme
suresini BYPASS eder (guvenlik icin bekleme ZORUNLU degil).
"""

import os

MAX_STEP_UP = float(os.environ.get("ATAE_MAX_STEP_UP", "10"))
MAX_STEP_DOWN = float(os.environ.get("ATAE_MAX_STEP_DOWN", "20"))  # asimetrik: risk azaltma daha hizli
DEADBAND_PERCENT = float(os.environ.get("ATAE_DEADBAND_PERCENT", "5"))
COOLDOWN_TICKS = int(os.environ.get("ATAE_COOLDOWN_TICKS", "2"))

# state -> "bull" (confidence arttikca UST'e yaklasir) / "bear" (confidence
# arttikca ALT'a yaklasir - DAHA SAVUNMACI)
_STYLE = {
    "STRONG_BEAR": "bear", "BEAR": "bear", "RECOVERY": "bull", "NEUTRAL": "bull",
    "BULL_EARLY": "bull", "BULL": "bull", "STRONG_BULL": "bull", "DISTRIBUTION": "bear",
}

PROFILE_BASE = {
    "STRONG_BEAR": (10.0, 20.0), "BEAR": (25.0, 35.0), "RECOVERY": (40.0, 55.0),
    "NEUTRAL": (45.0, 55.0), "BULL_EARLY": (55.0, 65.0), "BULL": (70.0, 80.0),
    "STRONG_BULL": (85.0, 95.0), "DISTRIBUTION": (60.0, 75.0),
}
PROFILE_CONSERVATIVE = {
    "STRONG_BEAR": (5.0, 12.0), "BEAR": (15.0, 25.0), "RECOVERY": (30.0, 45.0),
    "NEUTRAL": (40.0, 50.0), "BULL_EARLY": (45.0, 55.0), "BULL": (58.0, 68.0),
    "STRONG_BULL": (72.0, 85.0), "DISTRIBUTION": (45.0, 60.0),
}
PROFILE_AGGRESSIVE = {
    "STRONG_BEAR": (15.0, 25.0), "BEAR": (30.0, 42.0), "RECOVERY": (48.0, 62.0),
    "NEUTRAL": (48.0, 58.0), "BULL_EARLY": (62.0, 74.0), "BULL": (78.0, 88.0),
    "STRONG_BULL": (90.0, 100.0), "DISTRIBUTION": (68.0, 82.0),
}
PROFILES = {"BASE": PROFILE_BASE, "CONSERVATIVE": PROFILE_CONSERVATIVE, "AGGRESSIVE": PROFILE_AGGRESSIVE}


def state_confidence(state, bull_evidence, bear_evidence):
    """Durum ICINDE 0..1 guven bandi (madde 13) - state'in KENDISI TEK
    BASINA sabit allocation uretmez."""
    if _STYLE.get(state, "bull") == "bear":
        return max(0.0, min(1.0, bear_evidence / 100.0))
    return max(0.0, min(1.0, bull_evidence / 100.0))


def raw_target_allocation(profile_name, state, confidence_0_1):
    profile = PROFILES.get(profile_name, PROFILE_BASE)
    aralik = profile.get(state)
    if aralik is None:
        return 50.0
    alt, ust = aralik
    if _STYLE.get(state, "bull") == "bear":
        return ust - (ust - alt) * confidence_0_1
    return alt + (ust - alt) * confidence_0_1


def staged_step(prev_target, raw_target):
    """Kademeli gecis - YUKARI ve ASAGI icin FARKLI (ASIMETRIK) max-adim."""
    delta = raw_target - prev_target
    if delta > 0:
        delta = min(delta, MAX_STEP_UP)
    else:
        delta = max(delta, -MAX_STEP_DOWN)
    return max(0.0, min(100.0, prev_target + delta))


def should_execute(actual_pct, target_pct, ticks_since_last_trade, is_risk_override):
    """Deadband + cooldown birlikte - turnover kontrolunun cekirdegi
    (madde 15, 16). is_risk_override=True ise (guclu risk-reversal)
    cooldown BYPASS edilir."""
    gap = target_pct - actual_pct
    if abs(gap) < DEADBAND_PERCENT:
        return False, gap
    if not is_risk_override and ticks_since_last_trade < COOLDOWN_TICKS:
        return False, gap
    return True, gap
