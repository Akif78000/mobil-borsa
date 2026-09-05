"""
trend_engine.py icin saf (I/O yapmayan) gosterge fonksiyonlari.

Hepsi "None-onde gelebilir liste" (bir onek boyunca None, sonra float)
mantigiyla calisir - eksik veri durumunda hesaplama otomatik olarak
None dondurur, hicbir yerde IndexError/ZeroDivision riski yoktur.

Al-sat SINYALI URETMEZLER; trend_engine.py bunlari rejim siniflandirmasi
icin GIRDI olarak kullanir.
"""

import math


def _first_valid(values):
    for i, v in enumerate(values):
        if v is not None:
            return i
    return len(values)


def ema(values, period):
    """Genel EMA - values basinda None olabilir (once ki gosterge henuz
    hazir degilse). Ilk gecerli degerden itibaren standart EMA hesaplar."""
    n = len(values)
    result = [None] * n
    start = _first_valid(values)
    sub = values[start:]
    if len(sub) < period:
        return result
    seed = sum(sub[:period]) / period
    result[start + period - 1] = seed
    k = 2 / (period + 1)
    ema_val = seed
    for idx in range(period, len(sub)):
        ema_val = sub[idx] * k + ema_val * (1 - k)
        result[start + idx] = ema_val
    return result


def sma(values, period):
    n = len(values)
    result = [None] * n
    window = []
    for i in range(n):
        if values[i] is None:
            window = []
            continue
        window.append(values[i])
        if len(window) > period:
            window.pop(0)
        if len(window) == period:
            result[i] = sum(window) / period
    return result


def _sub(a, b):
    return [x - y if x is not None and y is not None else None for x, y in zip(a, b)]


def _abs_diff(a, b):
    return [abs(x - y) if x is not None and y is not None else None for x, y in zip(a, b)]


def _div(a, b, scale=1.0):
    return [
        (x / (scale * y)) if x is not None and y not in (None, 0) else None
        for x, y in zip(a, b)
    ]


def atr(highs, lows, closes, period=14):
    """Wilder ATR. Ilk `period` bar icin None (yeterli veri yok)."""
    n = len(closes)
    tr = [None] * n
    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    result = [None] * n
    if n < period + 1:
        return result
    atr_val = sum(tr[1:period + 1]) / period
    result[period] = atr_val
    for i in range(period + 1, n):
        atr_val = (atr_val * (period - 1) + tr[i]) / period
        result[i] = atr_val
    return result


def adx(highs, lows, closes, period=14):
    """Wilder ADX (trend GUCU, yon vermez - 0..100). ~2*period bar isinma
    suresi gerektirir; oncesi None."""
    n = len(closes)
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    result = [None] * n
    if n < period * 2 + 1:
        return result

    def dx_of(atr_val, pdm, mdm):
        if atr_val == 0:
            return 0.0
        pdi = 100 * pdm / atr_val
        mdi = 100 * mdm / atr_val
        if pdi + mdi == 0:
            return 0.0
        return 100 * abs(pdi - mdi) / (pdi + mdi)

    atr_val = sum(tr[1:period + 1])
    pdm_val = sum(plus_dm[1:period + 1])
    mdm_val = sum(minus_dm[1:period + 1])
    dx_list = [None] * n
    dx_list[period] = dx_of(atr_val, pdm_val, mdm_val)
    for i in range(period + 1, n):
        atr_val = atr_val - atr_val / period + tr[i]
        pdm_val = pdm_val - pdm_val / period + plus_dm[i]
        mdm_val = mdm_val - mdm_val / period + minus_dm[i]
        dx_list[i] = dx_of(atr_val, pdm_val, mdm_val)

    dx_window = [v for v in dx_list[period + 1:period * 2 + 1] if v is not None]
    if len(dx_window) < period:
        return result
    adx_val = sum(dx_window) / period
    result[period * 2] = adx_val
    for i in range(period * 2 + 1, n):
        if dx_list[i] is None:
            continue
        adx_val = (adx_val * (period - 1) + dx_list[i]) / period
        result[i] = adx_val
    return result


def nadaraya_watson(closes, bandwidth=8.0, lookback=50):
    """Gauss cekirdekli agirlikli ortalama - repaint YAPMAZ (her nokta sadece
    KENDISINE KADAR olan `lookback` bar ile hesaplanir). Trend/yatay ayrimi ve
    fiyatin trendden ne kadar 'uzaklastigini' (deviation) olcmek icin
    kullanilir; al-sat sinyali degildir."""
    n = len(closes)
    result = [None] * n
    for i in range(n):
        start = max(0, i - lookback + 1)
        window = closes[start:i + 1]
        m = len(window)
        weights = [math.exp(-((m - 1 - j) ** 2) / (2 * bandwidth * bandwidth)) for j in range(m)]
        wsum = sum(weights)
        result[i] = sum(w * c for w, c in zip(weights, window)) / wsum
    return result


def wavetrend(highs, lows, closes, n1=10, n2=21, n3=4):
    """LazyBear'in WaveTrend osilatoru (WT1/WT2). Asiri-alim/asiri-satim ve
    momentum yonu icin kullanilir; -100..+100 civarinda dolasir (kesin sinir
    yok, pratikte +-60 asiri bolgeler kabul edilir)."""
    ap = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
    esa = ema(ap, n1)
    d = ema(_abs_diff(ap, esa), n1)
    ci = _div(_sub(ap, esa), d, scale=0.015)
    wt1 = ema(ci, n2)
    wt2 = sma(wt1, n3)
    return wt1, wt2


def supertrend(highs, lows, closes, period=10, multiplier=3.0):
    """SuperTrend - ATR bantlariyla trend YONU (+1/-1) uretir, EMA kesisimine
    kiyasla trend donuslerini genelde daha erken yakalar (fiyat bandi kirinca
    ANINDA doner, iki ortalamanin kesismesini beklemez). Donen: (yon_serisi,
    supertrend_cizgisi). Isinma suresi boyunca yon=None."""
    n = len(closes)
    atr_series = atr(highs, lows, closes, period)
    yon = [None] * n
    cizgi = [None] * n
    ilk_idx = None
    for i in range(n):
        if atr_series[i] is None:
            continue
        hl2 = (highs[i] + lows[i]) / 2
        ust_band = hl2 + multiplier * atr_series[i]
        alt_band = hl2 - multiplier * atr_series[i]
        if ilk_idx is None:
            ilk_idx = i
            final_ust, final_alt = ust_band, alt_band
            yon[i] = 1
            cizgi[i] = final_alt
            continue
        onceki_kapanis = closes[i - 1]
        final_ust = ust_band if (ust_band < final_ust or onceki_kapanis > final_ust) else final_ust
        final_alt = alt_band if (alt_band > final_alt or onceki_kapanis < final_alt) else final_alt
        onceki_yon = yon[i - 1] if yon[i - 1] is not None else 1
        if closes[i] > final_ust:
            yon[i] = 1
        elif closes[i] < final_alt:
            yon[i] = -1
        else:
            yon[i] = onceki_yon
        cizgi[i] = final_alt if yon[i] == 1 else final_ust
    return yon, cizgi


def kama(closes, period=10, fast=2, slow=30):
    """Kaufman Adaptive Moving Average - Kaufman Verimlilik Orani'na (bkz.
    adaptive_bot.py) gore hizini kendi ayarlar: piyasa duz/trendliyken hizli
    (fiyati yakindan takip eder), yatay/gurultuluyken yavas (duz kalir, sahte
    sinyal uretmez). SHIB gibi cogunlukla yatay-ile-ani-sicrama dogasindaki
    bir varlik icin EMA'dan daha az "yalan sinyal" vermesi beklenir."""
    n = len(closes)
    result = [None] * n
    if n < period + 1:
        return result
    fastest_sc = 2 / (fast + 1)
    slowest_sc = 2 / (slow + 1)
    result[period] = closes[period]
    for i in range(period + 1, n):
        degisim = abs(closes[i] - closes[i - period])
        gurultu = sum(abs(closes[j] - closes[j - 1]) for j in range(i - period + 1, i + 1))
        er = (degisim / gurultu) if gurultu != 0 else 0.0
        sc = (er * (fastest_sc - slowest_sc) + slowest_sc) ** 2
        result[i] = result[i - 1] + sc * (closes[i] - result[i - 1])
    return result


def volume_ratio(volumes, period=20):
    """Guncel hacim / son `period` barin ortalama hacmi. >1 = ortalamanin
    ustunde katilim."""
    n = len(volumes)
    result = [None] * n
    for i in range(period, n):
        ortalama = sum(volumes[i - period:i]) / period
        result[i] = volumes[i] / ortalama if ortalama > 0 else None
    return result
