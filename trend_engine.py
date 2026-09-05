"""
Trend Intelligence Engine

Al-sat SINYALI URETMEZ. Amac piyasa REJIMINI siniflandirmak:
  YUKSELIS / DUSUS / YATAY / TOPARLANMA / DAGITIM

Girdi: SHIB'in coklu zaman dilimi (3m/5m/15m/30m/1h/4h/1d) gostergeleri +
BTC/ETH/BNB trend filtresi (SHIB genelde majors'i takip eder).

Cikti: guven skoru (0-100) ve HEDEF portfoy dagilimi (%0-100 SHIB). Gecisler
kademelidir (smooth_target) - tek harekette %0<->%100 sicramasi yapilmaz.

grid_bot.py'nin grid mantigina hic dokunmaz; bagimsiz, ayri bir karar
katmanidir (bkz. trend_engine_backtest.py - kullanim/backtest araci).
"""

import bisect

import regime_indicators as ri

TIMEFRAME_WEIGHTS = {
    "3m": 1, "5m": 1, "15m": 2, "30m": 2, "1h": 3, "4h": 4, "1d": 5,
}


class TimeframeSeries:
    """Bir sembol+zaman dilimi icin kapanis/yuksek/dusuk/hacim serileri ve
    onceden hesaplanmis gostergeler. `index_at(timestamp_ms)` ile o ana kadar
    KAPANMIS olan son barin index'ini bulur (lookahead/repaint riski yok)."""

    def __init__(self, open_times, highs, lows, closes, volumes,
                 ema_fast=20, ema_slow=50, adx_period=14,
                 nw_bandwidth=8.0, nw_lookback=50,
                 compute_wt=False, compute_nw=False):
        self.open_times = open_times
        self.closes = closes
        self.ema_fast = ri.ema(closes, ema_fast)
        self.ema_slow = ri.ema(closes, ema_slow)
        self.adx = ri.adx(highs, lows, closes, adx_period)
        self.atr = ri.atr(highs, lows, closes, adx_period)
        self.wt1 = None
        self.nw = None
        if compute_wt:
            self.wt1, _wt2 = ri.wavetrend(highs, lows, closes)
        if compute_nw:
            self.nw = ri.nadaraya_watson(closes, nw_bandwidth, nw_lookback)

    def index_at(self, timestamp_ms):
        idx = bisect.bisect_right(self.open_times, timestamp_ms) - 1
        return idx if idx >= 0 else None

    def trend_vote(self, idx):
        """+1 yukselis, -1 dusus, 0 gercekten notr, None = veri YOK (EMA henuz
        hazir degil - orn. 1 gunluk EMA(50) icin 50 gunluk gecmis gerekir).
        None ile 0'in AYRI tutulmasi onemli: veri yoksa bu zaman dilimi
        agirlikli ortalamadan TAMAMEN CIKARILMALI, "notr oy" gibi sayilip
        sinyali sulandirmamali."""
        if idx is None:
            return None
        f, s = self.ema_fast[idx], self.ema_slow[idx]
        if f is None or s is None:
            return None
        return 1 if f > s else (-1 if f < s else 0)


def _weighted_direction(series_map, timestamp_ms, timeframes):
    """{tf: TimeframeSeries} icin agirlikli trend yonu, -1..+1 arasi. Veri
    olmayan (None oy donen) zaman dilimleri agirliktan tamamen dislanir."""
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


def _majors_direction(majors_series, timestamp_ms, timeframes=("1h", "4h", "1d")):
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


def classify(shib_series, majors_series, timestamp_ms, timeframes,
             use_wt=True, use_nw=True, use_majors_filter=True, prev_score=0.0):
    """Bir andaki rejimi siniflandirir. `prev_score`: bir onceki classify()
    cagrisinin dondurdugu 'score' - TOPARLANMA/DAGITIM ayrimi icin gerekli
    (rejim GECISI, tek anlik degerden anlasilamaz)."""
    mtf_direction = _weighted_direction(shib_series, timestamp_ms, timeframes)

    ts_1h = shib_series.get("1h")
    idx_1h = ts_1h.index_at(timestamp_ms) if ts_1h else None
    adx_1h = ts_1h.adx[idx_1h] if (ts_1h and idx_1h is not None) else None
    strength_mult = min(adx_1h / 40.0, 1.0) if adx_1h is not None else 0.3

    composite = 100 * mtf_direction * strength_mult

    wt_note = None
    if use_wt and ts_1h is not None and ts_1h.wt1 is not None and idx_1h is not None:
        wt_val = ts_1h.wt1[idx_1h]
        if wt_val is not None:
            if wt_val > 60 and composite > 0:
                composite *= 0.6
                wt_note = "asiri_alim"
            elif wt_val < -60 and composite < 0:
                composite *= 0.6
                wt_note = "asiri_satim"

    nw_note = None
    if use_nw and ts_1h is not None and ts_1h.nw is not None and idx_1h is not None:
        nw_val = ts_1h.nw[idx_1h]
        atr_val = ts_1h.atr[idx_1h]
        price = ts_1h.closes[idx_1h]
        if nw_val and atr_val and atr_val > 0:
            sapma = (price - nw_val) / atr_val
            if sapma > 3 and composite > 0:
                composite *= 0.7
                nw_note = "trendden_asiri_uzak_yukari"
            elif sapma < -3 and composite < 0:
                composite *= 0.7
                nw_note = "trendden_asiri_uzak_asagi"

    majors_dir = _majors_direction(majors_series, timestamp_ms) if use_majors_filter else 0.0
    if use_majors_filter and majors_dir * composite < 0 and abs(majors_dir) > 0.2:
        composite *= 0.5

    composite = max(-100.0, min(100.0, composite))
    confidence = abs(composite)

    if confidence < 20:
        regime = "YATAY"
    elif composite >= 40:
        regime = "YUKSELIS"
    elif composite <= -40:
        regime = "DUSUS"
    elif composite > 0:
        regime = "TOPARLANMA" if prev_score < 0 else "YATAY"
    else:
        regime = "DAGITIM" if prev_score > 0 else "YATAY"

    target_shib_pct_raw = max(0.0, min(100.0, 50 + composite / 2))

    return {
        "regime": regime,
        "confidence": confidence,
        "score": composite,
        "mtf_direction": mtf_direction,
        "majors_direction": majors_dir,
        "wt_note": wt_note,
        "nw_note": nw_note,
        "target_shib_pct_raw": target_shib_pct_raw,
    }


def smooth_target(prev_target, new_target_raw, max_step):
    """Kademeli gecis: bir rebalance kontrolunde hedef en fazla `max_step`
    puan degisir (ani %0<->%100 sicramasini onler)."""
    delta = max(-max_step, min(max_step, new_target_raw - prev_target))
    return max(0.0, min(100.0, prev_target + delta))
