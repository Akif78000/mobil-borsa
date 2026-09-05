"""
Hybrid Trend Detector.

Tek bir gostergeye guvenmek yerine, her birinin GUCLU oldugu role
hapsedilmis bir kombinasyon:
  EMA (20/50)      - ERKEN UYARI. Hizli tepki verir ama tek basina cok
                     yalan sinyal uretir (bkz. trend_engine_backtest.py
                     sonuclari: EMA'nin yanlis pozitif orani en yuksekti).
  KAMA             - TEYIT/KALITE. EMA'nin erken sinyalini "gercekten
                     trend mi, gurultu mu" diye suzer - ayni yondeyse
                     sinyali GUCLENDIRIR, ters yondeyse ZAYIFLATIR (iptal
                     ETMEZ - KAMA yavas oldugu icin henuz onaylamamis
                     olmasi normaldir, erken asamada beklenir).
  Nadaraya-Watson  - Ana egilim + fiyatin trend bandina gore KONUMU.
                     Causal (repaint YOK - bkz. asagidaki not). Bandin
                     cok disina tasan fiyat "asiri uzama" sayilip sinyali
                     hafifce sonumler (tukenme riski).
  WaveTrend        - MOMENTUM TEYIDI, tek basina AL/SAT degil. Momentum
                     yonu ana skorla CELISIYORSA hafifletir; asiri alim/
                     satimda da benzer sekilde hafifletir.
  BTC/ETH/BNB      - DIS PIYASA TEYIDI. Ayni yondeyse skoru hafifce
                     GUCLENDIRIR, ters yondeyse YARIYA INDIRIR - ama SHIB
                     pozisyonunu TEK BASINA KAPATMAZ (cok agir ters durum
                     disinda skor sifirlanmaz, sadece kuculur).

COKLU ZAMAN DILIMI - HER BIRI AYNI AGIRLIKTA DEGIL:
  1d, 4h  = BUYUK PIYASA YONU (en yuksek agirlik - kucuk zaman diliminin
            gurultusu bunu EZEMEZ).
  1h      = ANA TREND YONU.
  30m,15m = TREND TEYIDI / momentum.
  5m, 3m  = erken donus / uygulama zamanlamasi (en dusuk agirlik).

REPAINT / LOOKAHEAD KORUMASI:
  - TimeframeSeries (bkz. trend_engine.py) ile ayni prensip: her bar
    close_time = open_time + interval kadar sonra "bilinir" hale gelir;
    index_at() sadece o ana kadar GERCEKTEN kapanmis barlari dondurur.
  - Nadaraya-Watson burada (regime_indicators.nadaraya_watson) her nokta
    SADECE KENDISINE KADAR olan `lookback` bari kullanarak hesaplanir -
    yani ileri bakan/repaint eden bir kernel regresyon DEGIL, gecmise
    bakan (causal) bir agirlikli ortalamadir. Klasik TradingView
    "Nadaraya-Watson Envelope" gostergesinin bazi surumleri butun seriye
    bakip geriye dogru "repaint" edebilir - burada KULLANILMADI, bilerek
    tek-yonlu (causal) versiyon tercih edildi.
  - WaveTrend ve EMA/KAMA'nin hepsi de sadece o ana kadarki kapanislarla
    hesaplanir (regime_indicators.py'deki tum fonksiyonlar O(n) ileri
    tarama, geriye donus/repaint icermez).
"""

import bisect
import os

import regime_indicators as ri

TIMEFRAMES = ["3m", "5m", "15m", "30m", "1h", "4h", "1d"]
TIMEFRAME_WEIGHTS = {"1d": 5, "4h": 4, "1h": 3, "30m": 2, "15m": 2, "5m": 1, "3m": 1}
REGIMES = ("GUCLU_YUKSELIS", "YUKSELIS", "YATAY", "DUSUS", "GUCLU_DUSUS", "TOPARLANMA", "DAGITIM")

# Rejim esikleri (skor -100..100 uzerinde). Varsayilan +-70/+-30 kullanicinin
# verdigi referans araligi - kesin optimum degil, backtest ile ayarlanabilir.
HYBRID_ESIK_GUCLU = float(os.environ.get("HYBRID_ESIK_GUCLU", "70"))
HYBRID_ESIK_YON = float(os.environ.get("HYBRID_ESIK_YON", "30"))


class HybridSeries:
    """Bir sembol+zaman dilimi icin TUM hibrit gostergeler bir arada.
    NW/WT sadece ihtiyac duyulan zaman diliminde (cagiran `compute_nw`/
    `compute_wt` ile acar) hesaplanir - gereksiz islem yukunu onler."""

    def __init__(self, open_times, highs, lows, closes, volumes, interval_ms,
                 compute_nw=False, compute_wt=False):
        self.open_times = open_times
        self.close_times = [t + interval_ms for t in open_times]
        self.closes = closes
        self.ema_fast = ri.ema(closes, 20)
        self.ema_slow = ri.ema(closes, 50)
        self.kama_line = ri.kama(closes, period=10, fast=2, slow=30)
        self.st_yon, _st_cizgi = ri.supertrend(highs, lows, closes, period=10, multiplier=3.0)
        self.adx = ri.adx(highs, lows, closes, 14)
        self.atr = ri.atr(highs, lows, closes, 14)
        self.nw = ri.nadaraya_watson(closes, bandwidth=8.0, lookback=50) if compute_nw else None
        self.wt1 = None
        if compute_wt:
            self.wt1, _wt2 = ri.wavetrend(highs, lows, closes)

    def index_at(self, timestamp_ms):
        idx = bisect.bisect_right(self.close_times, timestamp_ms) - 1
        return idx if idx >= 0 else None

    def ema_vote(self, idx):
        if idx is None:
            return None
        f, s = self.ema_fast[idx], self.ema_slow[idx]
        if f is None or s is None:
            return None
        return 1 if f > s else (-1 if f < s else 0)

    def kama_vote(self, idx, lookback=3):
        if idx is None or idx < lookback:
            return None
        k_now, k_once = self.kama_line[idx], self.kama_line[idx - lookback]
        if k_now is None or k_once is None:
            return None
        return 1 if k_now > k_once else (-1 if k_now < k_once else 0)

    def supertrend_vote(self, idx):
        if idx is None:
            return None
        return self.st_yon[idx]


def _timeframe_vote(ts, idx):
    """UC KATMANLI teyit zinciri, her biri kendi rolunde:
      EMA        - ERKEN UYARI (baz yon, -1/0/+1).
      KAMA       - TREND KALITESI teyidi: ayni yonde ise guc korunur/artar,
                   notr ise hafif azalir, TERS ise yalanci-alarm olasiligi
                   yuksek sayilip belirgin azalir (ama SIFIRLANMAZ - KAMA
                   yavas oldugu icin erken asamada dogal olarak geridedir).
      SUPERTREND - RISK FILTRESI / trend devamliligi: ayni yonde ise ekstra
                   guven, TERS yonde ise (ATR bandini henuz kirmamis demektir)
                   guc ayrica kirpilir - "yanlis erken donus" filtresi budur."""
    ema_v = ts.ema_vote(idx)
    if ema_v is None or ema_v == 0:
        return ema_v

    guc = 1.0
    kama_v = ts.kama_vote(idx)
    if kama_v is None:
        guc *= 0.7
    elif kama_v == ema_v:
        guc *= 1.0
    elif kama_v == 0:
        guc *= 0.7
    else:
        guc *= 0.4

    st_v = ts.supertrend_vote(idx)
    if st_v is None:
        guc *= 0.8
    elif st_v == ema_v:
        guc *= 1.0
    else:
        guc *= 0.5

    return ema_v * guc


def _weighted_direction(series_map, timestamp_ms, timeframes=TIMEFRAMES):
    total_weight = 0.0
    total_vote = 0.0
    for tf in timeframes:
        ts = series_map.get(tf)
        if ts is None:
            continue
        vote = _timeframe_vote(ts, ts.index_at(timestamp_ms))
        if vote is None:
            continue
        w = TIMEFRAME_WEIGHTS.get(tf, 1)
        total_vote += vote * w
        total_weight += w
    return (total_vote / total_weight) if total_weight else 0.0


def _majors_direction(majors_series, timestamp_ms, timeframes=("1h", "4h", "1d")):
    total_weight = 0.0
    total_vote = 0.0
    for _symbol, tf_map in majors_series.items():
        for tf in timeframes:
            ts = tf_map.get(tf)
            if ts is None:
                continue
            vote = ts.ema_vote(ts.index_at(timestamp_ms))
            if vote is None:
                continue
            w = TIMEFRAME_WEIGHTS.get(tf, 1)
            total_vote += vote * w
            total_weight += w
    return (total_vote / total_weight) if total_weight else 0.0


def _nw_adjustment(ts_1h, idx_1h, composite):
    if ts_1h is None or ts_1h.nw is None or idx_1h is None or idx_1h < 3:
        return composite, None
    nw_now, nw_once = ts_1h.nw[idx_1h], ts_1h.nw[idx_1h - 3]
    atr_val = ts_1h.atr[idx_1h]
    price = ts_1h.closes[idx_1h]
    if nw_now is None or nw_once is None or not atr_val:
        return composite, None
    nw_slope = 1 if nw_now > nw_once else (-1 if nw_now < nw_once else 0)
    not_ = None
    if nw_slope != 0 and nw_slope * composite < 0:
        composite *= 0.7
        not_ = "nw_egim_ters"
    sapma = (price - nw_now) / atr_val
    if sapma > 3 and composite > 0:
        composite *= 0.6
        not_ = "nw_asiri_uzak_yukari"
    elif sapma < -3 and composite < 0:
        composite *= 0.6
        not_ = "nw_asiri_uzak_asagi"
    return composite, not_


def _wt_adjustment(ts_1h, idx_1h, composite):
    if ts_1h is None or ts_1h.wt1 is None or idx_1h is None or idx_1h < 2:
        return composite, None
    wt_now, wt_once = ts_1h.wt1[idx_1h], ts_1h.wt1[idx_1h - 2]
    if wt_now is None or wt_once is None:
        return composite, None
    wt_yon = 1 if wt_now > wt_once else (-1 if wt_now < wt_once else 0)
    not_ = None
    if wt_yon != 0 and wt_yon * composite < 0:
        composite *= 0.75
        not_ = "momentum_ters"
    if wt_now > 60 and composite > 0:
        composite *= 0.7
        not_ = "asiri_alim"
    elif wt_now < -60 and composite < 0:
        composite *= 0.7
        not_ = "asiri_satim"
    return composite, not_


def classify(shib_series, majors_series, timestamp_ms, prev_score=0.0, use_majors_filter=True):
    """Donen: regime (7 seviye), confidence (0-100), score (-100..100),
    ayrica hangi katmanlarin skoru degistirdigini gosteren notlar."""
    mtf_direction = _weighted_direction(shib_series, timestamp_ms)

    ts_1h = shib_series.get("1h")
    idx_1h = ts_1h.index_at(timestamp_ms) if ts_1h else None
    adx_1h = ts_1h.adx[idx_1h] if (ts_1h and idx_1h is not None) else None
    strength_mult = min(adx_1h / 40.0, 1.0) if adx_1h is not None else 0.3

    composite = 100 * mtf_direction * strength_mult
    composite, nw_note = _nw_adjustment(ts_1h, idx_1h, composite)
    composite, wt_note = _wt_adjustment(ts_1h, idx_1h, composite)

    majors_dir = _majors_direction(majors_series, timestamp_ms) if use_majors_filter else 0.0
    majors_note = None
    if use_majors_filter and abs(majors_dir) > 0.2:
        if majors_dir * composite < 0:
            composite *= 0.5
            majors_note = "majors_ters"
        elif majors_dir * composite > 0 and abs(majors_dir) > 0.3:
            composite = max(-100.0, min(100.0, composite * 1.15))
            majors_note = "majors_teyit"

    composite = max(-100.0, min(100.0, composite))
    confidence = abs(composite)

    # Esikler: +-70 / +-30 (kullanicinin verdigi referans araligi). Kesin
    # optimal degil - hybrid_backtest.py'de HYBRID_ESIK_GUCLU / HYBRID_ESIK_
    # YON ortam degiskenleriyle degistirilip backtest edilebilir.
    if composite >= HYBRID_ESIK_GUCLU:
        regime = "GUCLU_YUKSELIS"
    elif composite >= HYBRID_ESIK_YON:
        regime = "YUKSELIS"
    elif composite <= -HYBRID_ESIK_GUCLU:
        regime = "GUCLU_DUSUS"
    elif composite <= -HYBRID_ESIK_YON:
        regime = "DUSUS"
    else:
        if composite > 0 and prev_score <= -HYBRID_ESIK_YON:
            regime = "TOPARLANMA"
        elif composite < 0 and prev_score >= HYBRID_ESIK_YON:
            regime = "DAGITIM"
        else:
            regime = "YATAY"

    return {
        "regime": regime, "confidence": confidence, "score": composite,
        "mtf_direction": mtf_direction, "majors_direction": majors_dir,
        "nw_note": nw_note, "wt_note": wt_note, "majors_note": majors_note,
    }
