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
