"""
Portfolio Manager - uc katmanli mimarinin 2. katmani.

trend_engine.py'nin urettigi rejim etiketi + guven skorunu alip HEDEF
SHIB/USDT dagilimini (%0-100 SHIB) hesaplar. Kendisi HICBIR ISLEM YAPMAZ -
sadece "su an ideal dagilim bu olurdu" der. Uygulama (grid_bot.py'nin
executor'u / trend_engine_backtest.py'deki simulate()) bu hedefe dogru,
kademeli olarak (smooth_target) yeniden dengeler.

Boylece grid_bot.py gelecekte bu katmani devreye alsa bile KARAR VERMEZ,
sadece PortfolioManager'in soyledigi hedefi UYGULAR.
"""

REGIME_BASE_ALLOCATION = {
    "GUCLU_YUKSELIS": 100.0,
    "YUKSELIS": 75.0,
    "YATAY": 50.0,
    "DUSUS": 25.0,
    "GUCLU_DUSUS": 0.0,
}


def target_allocation(regime, confidence, score):
    """Rejim etiketi bir TABAN hedef verir (basamakli - sinir civarindaki
    (orn. skor 19 vs 21) kucuk skor degisimlerinin dagilimi sicratmasini
    onler); skorun kendisi o tabanin etrafinda +-12.5 puanlik ince ayar
    yapar. `confidence` su an ayri kullanilmiyor (score zaten isaretli
    buyuklugunu tasiyor) - ileride "dusuk guvende daha temkinli ol" gibi
    bir kural eklenirse buraya girer."""
    taban = REGIME_BASE_ALLOCATION.get(regime, 50.0)
    ince_ayar = (score / 100) * 12.5
    return max(0.0, min(100.0, taban + ince_ayar))


def smooth_target(prev_target, new_target_raw, max_step):
    """Kademeli gecis: hedef bir rebalance kontrolunde en fazla `max_step`
    puan degisebilir (ani %0<->%100 sicramasini onler, asiri islemi
    engeller)."""
    delta = max(-max_step, min(max_step, new_target_raw - prev_target))
    return max(0.0, min(100.0, prev_target + delta))


# --- HYBRID (hybrid_engine.py, 7 seviyeli rejim) icin ayri tablo -----------
# Kullanicinin verdigi "baslangic referansi" - SABIT gercek olarak
# alinmamali, backtest ile farkli araliklar denenip en iyi risk/getiri
# kombinasyonu bulunmali (bkz. hybrid_backtest.py --sweep). Asagidaki
# (alt, ust) araliklari GUVEN SKORUYLA (0-100) ic ice orantilanir: 0 guven
# -> alt sinir, 100 guven -> ust sinir.
HYBRID_REGIME_RANGES = {
    "GUCLU_YUKSELIS": (90.0, 100.0),
    "YUKSELIS": (70.0, 90.0),
    "DUSUS": (40.0, 60.0),        # SHIB'in kabaca yarisi USDT'ye - agresif yeni alim YOK
    "GUCLU_DUSUS": (5.0, 15.0),   # USDT agirlikli ama SHIB TAMAMEN sifirlanmaz (cekirdek pozisyon)
    "DAGITIM": (50.0, 70.0),
}
# YATAY ve TOPARLANMA tabloda YOK - ozel muamele gerekiyor (asagida).
TOPARLANMA_GIRIS_ADIMI_PERCENT = 15.0  # her rebalance kontrolunde kademeli giris miktari


def hybrid_target_allocation(regime, confidence, score, prev_target):
    """HYBRID rejimini hedef SHIB yuzdesine cevirir.

    YATAY icin None doner - bu "hedefi DEGISTIRME" anlamina gelir, cunku
    kullanicinin istegi acik: "YATAY: mevcut grid sistemi aktif olsun".
    Yani bu katman YATAY'da devre disi kalir, karar VERMEZ - grid sleeve
    (bkz. hybrid_backtest.py) zaten calismaya devam eder.

    TOPARLANMA icin de None-DEGIL ama ozel: tek seferde hedefe SICRAMAZ,
    onceki hedeften TOPARLANMA_GIRIS_ADIMI_PERCENT kadar KADEMELI artar -
    "dip/toparlanma bolgesinde kademeli tekrar SHIB'e gecis" istegi boyle
    karsilanir (smooth_target'in genel max_step'inden BAGIMSIZ, cunku
    toparlanma kendi icinde zaten temkinli/kademeli olmali)."""
    if regime == "YATAY":
        return None
    if regime == "TOPARLANMA":
        return min(100.0, prev_target + TOPARLANMA_GIRIS_ADIMI_PERCENT)
    aralik = HYBRID_REGIME_RANGES.get(regime)
    if aralik is None:
        return None
    alt, ust = aralik
    return alt + (ust - alt) * (confidence / 100)
