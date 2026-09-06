"""
ATAE (ADAPTIVE TREND ALLOCATION ENGINE) - cekirdek motor: EVIDENCE + HAFIZALI
STATE MACHINE. Saf mantik (I/O yok). HYBRID v1/v2/v3/v4 ailesinin KUCUK bir
revizyonu DEGIL - TAMAMEN BAGIMSIZ yeni bir mimari (kullanicinin acik
istegi). hybrid_engine.py/portfolio_manager.py/hybrid_backtest.py'ye HIC
BAGIMLI DEGIL - SADECE regime_indicators.py'nin (indikator MATEMATIGI,
strateji mantigi ICERMEYEN saf fonksiyonlar) tekrar kullanilmasi disinda
hicbir eski dosyaya dokunmaz/bagli degildir.

MIMARI ZINCIRI (eskisinden KASITLI FARKLI):
  INDICATORS -> EVIDENCE (bull_evidence/bear_evidence, ZAMAN ICINDE COZULEN/
  hafizali - HYBRID'in "anlik agirlikli skor" mantigi DEGIL)
  -> PERSISTENT STATE MACHINE (8 durum, DOGAL KOMSULUK grafigi uzerinde
  ADIM ADIM ilerler - "score>=30 -> anlik rejim" mantigi DEGIL)
  -> CONFIDENCE (durum icinde bant, bkz. atae_allocation.py)

8 DURUM VE DOGAL AKIS (kullanicinin verdigi iki akis):
  STRONG_BEAR -> BEAR -> RECOVERY -> NEUTRAL -> BULL_EARLY -> BULL -> STRONG_BULL
  STRONG_BULL -> DISTRIBUTION -> NEUTRAL -> BEAR -> STRONG_BEAR
  Gecisler NORMALDE komsu-adima sinirlidir (UP_NEIGHBOR/DOWN_NEIGHBOR) -
  "tek mumla sürekli ileri/geri zıplama" ENGELLENIR. SADECE evidence
  OVERRIDE_ESIK'i (85) gecerse dogrudan STRONG_BULL/STRONG_BEAR'a ATLAMA
  izni var (kullanicinin acik istegi: "gercek güçlü ters kanit varsa
  state atlamasina izin verilebilir").

EVIDENCE MODELI (skor DEGIL, hafizali IKI ayri havuz):
  bull_evidence, bear_evidence in [0,100], HER TIK'TE:
    - yon-uyumlu ham oy geldiginde ARTAR (push)
    - HER durumda (uyumlu/notr/ters oy farketmeksizin) bir miktar ustel
      COZULUR (decay) - "72 -> 0 tek bir ters mumla" YASAK, kademeli
      cozulme ZORUNLU (kullanicinin acik istegi, madde 7).
  DECAY_MODE ile 2 aciklanabilir aday sunulur (EXPONENTIAL/LINEAR) - ikisi
  de SABIT, optimize EDILMEMIS parametrelerle (madde 31: grid-search YOK).

ASIMETRI (madde 9 - COK KRITIK, kullanicinin kendi ifadesi):
  Her "YUKARI" (bullish yone dogru) adimin esigi/persistence gereksinimi,
  ayni durumdan "ASAGI" (bearish yone) cikisin gereksiniminden DAHA
  DUSUKTUR - yani girmek cikmaktan HER ZAMAN daha kolay. Tek bir zayif
  ters tik pozisyonu KAPATMAZ; ama OVERRIDE_ESIK'in uzerindeki GERCEKTEN
  guclu ters kanit HIZLI cikisi ENGELLEMEZ (kor sabit minimum-hold YOK).

SLOW LAYER (KAMA/SuperTrend) - HARD GATE DEGIL (madde 4):
  Sadece "ORTA"/"GUCLU" yukari adimlarda (BULL_EARLY->BULL, BULL->
  STRONG_BULL) EK bir "trend kalitesi" kosulu olarak kontrol edilir -
  TUM komponentlerin ayni anda bullish olmasi GEREKMEZ (madde 8), sadece
  COGUNLUGU (>=%50) ayni yonde olmali.

MAJORS (BTC/ETH/BNB) - HIC BIR ZAMAN HARD GATE DEGIL (madde 5):
  Ayni yondeyse evidence push'unu HAFIFCE guclendirir; GUCLU ters
  yondeyse push'u hafifletir VE STRONG_BULL/STRONG_BEAR'a gecis esigine
  ekstra tampon ekler - ama hicbir zaman pozisyonu TEK BASINA kapatmaz.

TIMEFRAME ROLLERI (madde 6):
  5m/15m: erken momentum (dusuk agirlik, ozellikle RECOVERY/BULL_EARLY/
          DISTRIBUTION yakalamada).
  30m/1h: ANA karar katmani (en yuksek agirlik).
  4h    : trend yapisi (SLOW layer + majors ile birlikte).
  1d    : makro yon (SLOW layer'a hafif ek girdi).

LOOKAHEAD YOK: AtaeSeries.index_at() SADECE close_time (=open_time+
interval) <= sorgu anindaki barlari "bilinir" sayar - trend_engine.py/
hybrid_engine.py'deki AYNI ilke (bagimsiz olarak burada da uygulanir).
Nadaraya-Watson causal (regime_indicators.nadaraya_watson - degistirilmedi,
her nokta sadece kendisine kadarki lookback ile hesaplaniyor, dogrulandi).
"""

import bisect
import os

import regime_indicators as ri

STATES = ("STRONG_BEAR", "BEAR", "RECOVERY", "NEUTRAL", "BULL_EARLY", "BULL", "STRONG_BULL", "DISTRIBUTION")
BULLISH_STATES = ("BULL_EARLY", "BULL", "STRONG_BULL")
BEARISH_STATES = ("BEAR", "STRONG_BEAR")

FAST_TIMEFRAMES = ["5m", "15m", "30m", "1h"]
FAST_WEIGHTS = {"5m": 1, "15m": 1, "30m": 3, "1h": 3}  # 30m/1h = ana karar katmani (madde 6)
SLOW_TIMEFRAMES = ["1h", "4h"]
SLOW_WEIGHTS = {"1h": 1, "4h": 2}  # 4h = trend yapisi, biraz daha agir
MACRO_TIMEFRAME = "1d"

UP_NEIGHBOR = {
    "STRONG_BEAR": "BEAR", "BEAR": "RECOVERY", "RECOVERY": "NEUTRAL",
    "NEUTRAL": "BULL_EARLY", "BULL_EARLY": "BULL", "BULL": "STRONG_BULL",
    "STRONG_BULL": "STRONG_BULL", "DISTRIBUTION": "BULL",
}
DOWN_NEIGHBOR = {
    "STRONG_BULL": "DISTRIBUTION", "DISTRIBUTION": "NEUTRAL", "NEUTRAL": "BEAR",
    "BEAR": "STRONG_BEAR", "STRONG_BEAR": "STRONG_BEAR",
    "BULL": "DISTRIBUTION", "BULL_EARLY": "NEUTRAL", "RECOVERY": "BEAR",
}
# ABLATION icin BASITLESTIRILMIS 6-durum grafigi (RECOVERY/DISTRIBUTION
# YOK - dogrudan NEUTRAL'e gecer). ATAE-A/B bunu kullanir, ATAE-C tam
# 8-durum grafigini (yukaridaki) kullanir (madde 22).
SIMPLE_UP_NEIGHBOR = {
    "STRONG_BEAR": "BEAR", "BEAR": "NEUTRAL", "NEUTRAL": "BULL_EARLY",
    "BULL_EARLY": "BULL", "BULL": "STRONG_BULL", "STRONG_BULL": "STRONG_BULL",
}
SIMPLE_DOWN_NEIGHBOR = {
    "STRONG_BULL": "NEUTRAL", "BULL": "NEUTRAL", "BULL_EARLY": "NEUTRAL",
    "NEUTRAL": "BEAR", "BEAR": "STRONG_BEAR", "STRONG_BEAR": "STRONG_BEAR",
}

# Mevcut kodun (hybrid_engine.py) ZATEN kullandigi esik konvansiyonu (30/70)
# TEKRAR KULLANILIYOR - yeni sayi uydurulmadi (madde 31).
ENTRY_ESIK_ERKEN = float(os.environ.get("ATAE_ENTRY_ESIK_ERKEN", "30"))
ENTRY_ESIK_ORTA = float(os.environ.get("ATAE_ENTRY_ESIK_ORTA", "50"))
ENTRY_ESIK_GUCLU = float(os.environ.get("ATAE_ENTRY_ESIK_GUCLU", "70"))
# ASIMETRI (madde 9): cikis esikleri girisin ayni-seviye esdegerinden YUKSEK.
EXIT_ESIK_ERKEN = float(os.environ.get("ATAE_EXIT_ESIK_ERKEN", "40"))
EXIT_ESIK_ORTA = float(os.environ.get("ATAE_EXIT_ESIK_ORTA", "55"))
EXIT_ESIK_GUCLU = float(os.environ.get("ATAE_EXIT_ESIK_GUCLU", "60"))
OVERRIDE_ESIK = float(os.environ.get("ATAE_OVERRIDE_ESIK", "85"))
MAJORS_TAMPON = float(os.environ.get("ATAE_MAJORS_TAMPON", "10"))

# Persistence (ardisik tik) - mevcut kodun HYSTERESIS_BARS=2 konvansiyonu
# TEKRAR KULLANILIYOR (v3'un V3_REVERSAL_CONFIRM_TICKS ile AYNI mantik).
PERSISTENCE_ERKEN = 1
PERSISTENCE_ORTA = 2
PERSISTENCE_GUCLU = 4
PERSISTENCE_CIKIS = 2

DECAY_MODE = os.environ.get("ATAE_DECAY_MODE", "EXPONENTIAL")  # veya "LINEAR" - 2 aciklanabilir aday (madde 7)
DECAY_PER_TICK = float(os.environ.get("ATAE_DECAY_PER_TICK", "0.90"))
LINEAR_DECAY_STEP = float(os.environ.get("ATAE_LINEAR_DECAY_STEP", "8"))
PUSH_MAX = float(os.environ.get("ATAE_PUSH_MAX", "40"))  # tam-uyumlu bir tik en fazla bu kadar evidence ekler


class AtaeSeries:
    """Bir sembol+zaman dilimi icin gosterge seti. Sadece regime_indicators.py
    (saf matematik, strateji icermez) kullanilir - eski hybrid_engine.py'ye
    BAGIMLI DEGIL, kendi basina TAMAMEN bagimsiz hesaplar."""

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
        a, b = self.kama_line[idx], self.kama_line[idx - lookback]
        if a is None or b is None:
            return None
        return 1 if a > b else (-1 if a < b else 0)

    def supertrend_vote(self, idx):
        return self.st_yon[idx] if idx is not None else None

    def nw_slope_vote(self, idx, lookback=3):
        if self.nw is None or idx is None or idx < lookback:
            return None
        a, b = self.nw[idx], self.nw[idx - lookback]
        if a is None or b is None:
            return None
        return 1 if a > b else (-1 if a < b else 0)

    def wt_slope_vote(self, idx, lookback=2):
        if self.wt1 is None or idx is None or idx < lookback:
            return None
        a, b = self.wt1[idx], self.wt1[idx - lookback]
        if a is None or b is None:
            return None
        return 1 if a > b else (-1 if a < b else 0)


def _agirlikli_yon(oy_agirlik_liste):
    """[(oy, agirlik), ...] -> (-1..1 arasi net yon, 0..1 arasi katilim gucu)."""
    toplam_agirlik = sum(w for v, w in oy_agirlik_liste if v is not None)
    if toplam_agirlik == 0:
        return 0.0, 0.0
    net = sum(v * w for v, w in oy_agirlik_liste if v is not None) / toplam_agirlik
    return net, min(1.0, abs(net))


def fast_vote(series_map, timestamp_ms):
    """EMA + WaveTrend-egim + NW-egim, FAST_TIMEFRAMES agirlikli. Donen:
    (yon -1/0/1, guc 0..1). RECOVERY/BULL_EARLY/DISTRIBUTION yakalamak icin
    tasarlandi (madde 3, 6)."""
    oylar = []
    for tf in FAST_TIMEFRAMES:
        ts = series_map.get(tf)
        if ts is None:
            continue
        idx = ts.index_at(timestamp_ms)
        if idx is None:
            continue
        w = FAST_WEIGHTS.get(tf, 1)
        for oy in (ts.ema_vote(idx), ts.wt_slope_vote(idx), ts.nw_slope_vote(idx)):
            oylar.append((oy, w))
    net, guc = _agirlikli_yon(oylar)
    yon = 1 if net > 0.15 else (-1 if net < -0.15 else 0)
    return yon, guc


def slow_agreement(series_map, timestamp_ms, aranan_yon):
    """KAMA + SuperTrend + 4h EMA (trend yapisi) - HARD GATE degil, sadece
    "kac tanesi ayni yonde" oranini dondurur (0..1). Tumu ayni anda
    bullish olmak ZORUNDA DEGIL (madde 8) - >=%50 yeterli sayilir."""
    oylar = []
    ts_1h = series_map.get("1h")
    if ts_1h is not None:
        idx = ts_1h.index_at(timestamp_ms)
        oylar.append(ts_1h.kama_vote(idx))
        oylar.append(ts_1h.supertrend_vote(idx))
    ts_4h = series_map.get("4h")
    if ts_4h is not None:
        idx4 = ts_4h.index_at(timestamp_ms)
        oylar.append(ts_4h.ema_vote(idx4))
        oylar.append(ts_4h.supertrend_vote(idx4))
    ts_1d = series_map.get("1d")
    if ts_1d is not None:
        idx1d = ts_1d.index_at(timestamp_ms)
        oylar.append(ts_1d.ema_vote(idx1d))
    gecerli = [o for o in oylar if o is not None]
    if not gecerli:
        return 0.5  # veri yoksa notr - ne engeller ne zorlar
    uyumlu = sum(1 for o in gecerli if o == aranan_yon)
    return uyumlu / len(gecerli)


def majors_direction(majors_series, timestamp_ms, timeframes=("1h", "4h", "1d")):
    """BTC/ETH/BNB agirlikli EMA yonu - -1..1. HICBIR ZAMAN hard gate
    olarak kullanilmaz (madde 5) - sadece push carpani + STRONG_* esigine
    tampon olarak."""
    oylar = []
    for _sym, tf_map in majors_series.items():
        for tf in timeframes:
            ts = tf_map.get(tf)
            if ts is None:
                continue
            idx = ts.index_at(timestamp_ms)
            w = {"1h": 1, "4h": 2, "1d": 2}.get(tf, 1)
            oylar.append((ts.ema_vote(idx), w))
    net, _guc = _agirlikli_yon(oylar)
    return net


def yeni_evidence_state():
    return {
        "bull_evidence": 0.0, "bear_evidence": 0.0,
        "state": "NEUTRAL", "state_entry_time": None, "state_entry_price": None,
        "prev_state": None,
        "persist_up": 0, "persist_down": 0,
        "max_bull_evidence": 0.0, "max_bear_evidence": 0.0,
        "last_supporting_signal_time": None, "last_opposing_signal_time": None,
    }


def _decay(deger):
    if DECAY_MODE == "LINEAR":
        return max(0.0, deger - LINEAR_DECAY_STEP)
    return deger * DECAY_PER_TICK  # EXPONENTIAL (varsayilan)


def _yukari_gereksinim(hedef_state, majors_dir):
    if hedef_state in ("RECOVERY", "BULL_EARLY", "NEUTRAL"):
        return ENTRY_ESIK_ERKEN, PERSISTENCE_ERKEN, False
    if hedef_state == "BULL":
        return ENTRY_ESIK_ORTA, PERSISTENCE_ORTA, True
    if hedef_state == "STRONG_BULL":
        tampon = MAJORS_TAMPON if majors_dir < -0.3 else 0.0
        return ENTRY_ESIK_GUCLU + tampon, PERSISTENCE_GUCLU, True
    return ENTRY_ESIK_ERKEN, PERSISTENCE_ERKEN, False


def _asagi_gereksinim(state, majors_dir, use_asymmetric_exit=True):
    if not use_asymmetric_exit:
        # SIMETRIK mod (ATAE-A/B icin ablation): cikis esigi GIRIS esigiyle
        # AYNI - asimetrinin KENDISININ deger katip katmadigini izole eder.
        if state == "BULL_EARLY":
            return ENTRY_ESIK_ERKEN, PERSISTENCE_ERKEN
        if state == "BULL":
            return ENTRY_ESIK_ORTA, PERSISTENCE_ORTA
        if state == "STRONG_BULL":
            return ENTRY_ESIK_GUCLU, PERSISTENCE_GUCLU
        return ENTRY_ESIK_ERKEN, PERSISTENCE_ERKEN
    if state == "BULL_EARLY":
        return EXIT_ESIK_ERKEN, PERSISTENCE_CIKIS
    if state == "BULL":
        return EXIT_ESIK_ORTA, PERSISTENCE_CIKIS
    if state == "STRONG_BULL":
        tampon = MAJORS_TAMPON if majors_dir > 0.3 else 0.0  # majors HALA bullish ise cikis biraz zorlasir
        return EXIT_ESIK_GUCLU + tampon, PERSISTENCE_CIKIS
    if state == "BEAR":
        return ENTRY_ESIK_ORTA, PERSISTENCE_ORTA  # STRONG_BEAR'a gecis icin ekstra teyit
    return ENTRY_ESIK_ERKEN, PERSISTENCE_ERKEN  # RECOVERY/DISTRIBUTION/NEUTRAL - basarisiz olursa kolay geri don


def step(evidence_state, shib_series, majors_series, timestamp_ms,
         use_asymmetric_exit=True, use_recovery_distribution=True):
    """TEK bir karar tik'i. evidence_state YERINDE (in-place) guncellenir.
    use_asymmetric_exit=False / use_recovery_distribution=False -> ATAE-A/B
    icin ABLATION modlari (madde 22) - varsayilan (ikisi de True) ATAE-C'nin
    tam mimarisidir. Donen dict telemetri icin tum ara degerleri tasir."""
    up_map = UP_NEIGHBOR if use_recovery_distribution else SIMPLE_UP_NEIGHBOR
    down_map = DOWN_NEIGHBOR if use_recovery_distribution else SIMPLE_DOWN_NEIGHBOR
    fast_yon, fast_guc = fast_vote(shib_series, timestamp_ms)
    m_dir = majors_direction(majors_series, timestamp_ms)

    push = PUSH_MAX * fast_guc
    if fast_yon == 1:
        if abs(m_dir) > 0.3:
            push *= 1.15 if m_dir > 0 else 0.7  # majors uyumluysa guclendir, terse ise hafiflet (asla sifirlama)
        evidence_state["bull_evidence"] = min(100.0, _decay(evidence_state["bull_evidence"]) + push)
        evidence_state["bear_evidence"] = _decay(evidence_state["bear_evidence"])
        evidence_state["last_supporting_signal_time"] = timestamp_ms
    elif fast_yon == -1:
        if abs(m_dir) > 0.3:
            push *= 1.15 if m_dir < 0 else 0.7
        evidence_state["bear_evidence"] = min(100.0, _decay(evidence_state["bear_evidence"]) + push)
        evidence_state["bull_evidence"] = _decay(evidence_state["bull_evidence"])
        evidence_state["last_opposing_signal_time"] = timestamp_ms
    else:
        evidence_state["bull_evidence"] = _decay(evidence_state["bull_evidence"])
        evidence_state["bear_evidence"] = _decay(evidence_state["bear_evidence"])

    evidence_state["max_bull_evidence"] = max(evidence_state["max_bull_evidence"], evidence_state["bull_evidence"])
    evidence_state["max_bear_evidence"] = max(evidence_state["max_bear_evidence"], evidence_state["bear_evidence"])

    bull_ev, bear_ev = evidence_state["bull_evidence"], evidence_state["bear_evidence"]
    state = evidence_state["state"]

    # --- GUCLU TERS KANIT: dogal komsuluk sirasini atlayip DOGRUDAN
    # STRONG_BULL/STRONG_BEAR'a gecis (madde 2 sonu, kullanicinin acik istegi) ---
    if bull_ev >= OVERRIDE_ESIK and state != "STRONG_BULL":
        yeni_state, neden = "STRONG_BULL", "OVERRIDE_BULL"
    elif bear_ev >= OVERRIDE_ESIK and state != "STRONG_BEAR":
        yeni_state, neden = "STRONG_BEAR", "OVERRIDE_BEAR"
    else:
        hedef_up = up_map.get(state)
        hedef_down = down_map.get(state)

        up_esik, up_persist_gerek, slow_gerekli = (None, None, False)
        if hedef_up and hedef_up != state:
            up_esik, up_persist_gerek, slow_gerekli = _yukari_gereksinim(hedef_up, m_dir)
        down_esik, down_persist_gerek = _asagi_gereksinim(state, m_dir, use_asymmetric_exit)

        evidence_state["persist_up"] = evidence_state["persist_up"] + 1 if (up_esik is not None and bull_ev >= up_esik) else 0
        evidence_state["persist_down"] = evidence_state["persist_down"] + 1 if bear_ev >= down_esik else 0

        yeni_state, neden = state, None
        if up_esik is not None and bull_ev >= up_esik and evidence_state["persist_up"] >= up_persist_gerek:
            slow_ok = (not slow_gerekli) or (slow_agreement(shib_series, timestamp_ms, 1) >= 0.5)
            if slow_ok:
                yeni_state, neden = hedef_up, "EVIDENCE_UP"
        if yeni_state == state and hedef_down and hedef_down != state:
            if bear_ev >= down_esik and evidence_state["persist_down"] >= down_persist_gerek:
                yeni_state, neden = hedef_down, "EVIDENCE_DOWN"

    degisti = yeni_state != state
    if degisti:
        evidence_state["prev_state"] = state
        evidence_state["state"] = yeni_state
        evidence_state["state_entry_time"] = timestamp_ms
        evidence_state["persist_up"] = 0
        evidence_state["persist_down"] = 0

    slow_up = slow_agreement(shib_series, timestamp_ms, 1)
    slow_down = slow_agreement(shib_series, timestamp_ms, -1)
    return {
        "state": evidence_state["state"], "changed": degisti, "reason": neden,
        "bull_evidence": bull_ev, "bear_evidence": bear_ev,
        "fast_dir": fast_yon, "fast_strength": fast_guc,
        "slow_agree_up": slow_up, "slow_agree_down": slow_down, "majors_dir": m_dir,
    }
