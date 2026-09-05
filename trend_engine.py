"""
Trend Intelligence Engine (v2) - katman 1.

Uc katmanli mimari:
  1) trend_engine.py (bu dosya)  - REJIM + GUVEN SKORU uretir, al-sat
     sinyali URETMEZ.
  2) portfolio_manager.py        - o skoru HEDEF SHIB/USDT dagilimina
     cevirir, islem YAPMAZ.
  3) executor (grid_bot.py / trend_engine_backtest.py'deki simulate())
     - sadece o hedefe dogru islem yapar, KARAR VERMEZ.

Girdi: SHIB'in 1sa/4sa/1gun gostergeleri + BTC/ETH/BNB (ayni 3 zaman
dilimi) trend filtresi (SHIB genelde majors'i takip eder).

Cikti: 5 seviyeli rejim (GUCLU_YUKSELIS/YUKSELIS/YATAY/DUSUS/GUCLU_DUSUS)
ve 0-100 guven skoru.

Birden fazla DEDEKTOR desteklenir - trend YONUNU hangi gostergeyle
turetecegimiz:
  EMA        - klasik kisa(20)/uzun(50) EMA kesisimi.
  SUPERTREND - ATR bantli; fiyat bandi kirinca ANINDA doner, iki
               ortalamanin kesismesini beklemez (genelde EMA'dan daha
               erken tepki verir).
  KAMA       - Kaufman Adaptif Ortalama; piyasa yatay/gurultuluyken
               otomatik yavaslar (sahte sinyali azaltir), trendliyken
               hizlanir.
trend_engine_backtest.py ucunu ayni veride yaristirip SHIB icin hangisinin
daha iyi calistigini otomatik raporlar.
"""

import bisect

import regime_indicators as ri

TIMEFRAMES = ["1h", "4h", "1d"]
TIMEFRAME_WEIGHTS = {"1h": 1, "4h": 2, "1d": 3}
DETECTORS = ("EMA", "SUPERTREND", "KAMA")


class TimeframeSeries:
    """Bir sembol+zaman dilimi icin gostergeler. `index_at()` bir zaman
    damgasina kadar KAPANMIS son barin index'ini bulur.

    ONEMLI DUZELTME: eskiden bisect open_times uzerinde yapiliyordu - bu,
    henuz KAPANMAMIS bir barin (orn. saat tam 15:00'te, 15:00-16:00 1sa
    barinin ACILDIGI an) yanlislikla "bilinen son bar" sayilmasina yol
    aciyordu (bir tur lookahead/repaint riski). Simdi close_time = open_time
    + interval_ms uzerinden bisect yapiliyor - bir bar sadece GERCEKTEN
    kapandiktan sonra (timestamp >= close_time) kullanilabiliyor."""

    def __init__(self, open_times, highs, lows, closes, volumes, interval_ms, detector="EMA"):
        if detector not in DETECTORS:
            raise ValueError(f"Bilinmeyen dedektor: {detector}")
        self.open_times = open_times
        self.close_times = [t + interval_ms for t in open_times]
        self.closes = closes
        self.detector = detector
        if detector == "EMA":
            self.ema_fast = ri.ema(closes, 20)
            self.ema_slow = ri.ema(closes, 50)
        elif detector == "SUPERTREND":
            self.st_yon, _cizgi = ri.supertrend(highs, lows, closes, period=10, multiplier=3.0)
        elif detector == "KAMA":
            self.kama_line = ri.kama(closes, period=10, fast=2, slow=30)
        self.adx = ri.adx(highs, lows, closes, 14)

    def index_at(self, timestamp_ms):
        idx = bisect.bisect_right(self.close_times, timestamp_ms) - 1
        return idx if idx >= 0 else None

    def trend_vote(self, idx):
        """+1 yukselis, -1 dusus, 0 gercekten notr, None = veri YOK."""
        if idx is None:
            return None
        if self.detector == "EMA":
            f, s = self.ema_fast[idx], self.ema_slow[idx]
            if f is None or s is None:
                return None
            return 1 if f > s else (-1 if f < s else 0)
        if self.detector == "SUPERTREND":
            return self.st_yon[idx]
        if self.detector == "KAMA":
            if idx < 3:
                return None
            k_now, k_once = self.kama_line[idx], self.kama_line[idx - 3]
            if k_now is None or k_once is None:
                return None
            return 1 if k_now > k_once else (-1 if k_now < k_once else 0)
        return None


def _weighted_direction(series_map, timestamp_ms, timeframes=TIMEFRAMES):
    """Veri olmayan zaman dilimleri agirliktan TAMAMEN CIKARILIR (notr oy
    gibi sayilip sinyali sulandirmasin diye)."""
    total_weight = 0
    total_vote = 0
    for tf in timeframes:
        ts = series_map.get(tf)
        if ts is None:
            continue
        vote = ts.trend_vote(ts.index_at(timestamp_ms))
        if vote is None:
            continue
        w = TIMEFRAME_WEIGHTS.get(tf, 1)
        total_vote += vote * w
        total_weight += w
    return (total_vote / total_weight) if total_weight else 0.0


def _majors_direction(majors_series, timestamp_ms, timeframes=TIMEFRAMES):
    total_weight = 0
    total_vote = 0
    for _symbol, tf_map in majors_series.items():
        for tf in timeframes:
            ts = tf_map.get(tf)
            if ts is None:
                continue
            vote = ts.trend_vote(ts.index_at(timestamp_ms))
            if vote is None:
                continue
            w = TIMEFRAME_WEIGHTS.get(tf, 1)
            total_vote += vote * w
            total_weight += w
    return (total_vote / total_weight) if total_weight else 0.0


def classify(shib_series, majors_series, timestamp_ms, use_majors_filter=True):
    """Bir andaki rejimi siniflandirir. Donen sozluk: regime (5 seviye),
    confidence (0-100), score (-100..100, isaretli), mtf_direction,
    majors_direction."""
    mtf_direction = _weighted_direction(shib_series, timestamp_ms)

    ts_1h = shib_series.get("1h")
    idx_1h = ts_1h.index_at(timestamp_ms) if ts_1h else None
    adx_1h = ts_1h.adx[idx_1h] if (ts_1h and idx_1h is not None) else None
    strength_mult = min(adx_1h / 40.0, 1.0) if adx_1h is not None else 0.3

    composite = 100 * mtf_direction * strength_mult

    majors_dir = _majors_direction(majors_series, timestamp_ms) if use_majors_filter else 0.0
    if use_majors_filter and majors_dir * composite < 0 and abs(majors_dir) > 0.2:
        composite *= 0.5

    composite = max(-100.0, min(100.0, composite))
    confidence = abs(composite)

    if composite >= 60:
        regime = "GUCLU_YUKSELIS"
    elif composite >= 20:
        regime = "YUKSELIS"
    elif composite > -20:
        regime = "YATAY"
    elif composite > -60:
        regime = "DUSUS"
    else:
        regime = "GUCLU_DUSUS"

    return {
        "regime": regime,
        "confidence": confidence,
        "score": composite,
        "mtf_direction": mtf_direction,
        "majors_direction": majors_dir,
    }
