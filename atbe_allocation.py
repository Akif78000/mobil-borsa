"""
ATBE ALLOCATION - booster ladder_state -> BOOSTER SLEEVE YUZDESI (0..100,
TOPLAM PORTFOY DEGIL), staged gecis, execution deadband, TURNOVER BUDGET
(madde 7) ve ECONOMIC REBALANCE FILTER (madde 8). atbe_engine.py'ye
BAGIMLI (LADDER/BOOSTER_TARGET) ama hicbir eski HYBRID/GRID/ATAE dosyasina
BAGLI DEGIL - staged-step/deadband SAYILARI ATAE'nin atae_allocation.py'sinden
(MAX_STEP_UP/DOWN, DEADBAND_PERCENT) DEGERLERI kopyalanarak (import DEGIL,
kendi kopyasi - ATBE dosya bagimsizligi icin) tekrar kullanilir, YENI sayi
UYDURULMADI.
"""

import collections
import os

import atbe_engine as abe

# ATAE'nin atae_allocation.py'sindeki AYNI DEGERLER (reused, not invented).
MAX_STEP_UP = float(os.environ.get("ATBE_MAX_STEP_UP", "10"))
MAX_STEP_DOWN = float(os.environ.get("ATBE_MAX_STEP_DOWN", "20"))
DEADBAND_PERCENT = float(os.environ.get("ATBE_DEADBAND_PERCENT", "5"))
COOLDOWN_TICKS = int(os.environ.get("ATBE_COOLDOWN_TICKS", "2"))

# TURNOVER BUDGET (madde 7) - booster SERMAYESININ (toplam portfoyun DEGIL)
# yuzdesi olarak rolling 24h/7g turnover tavani. Bu YENI bir kavram (mevcut
# kodda karsiligi yok, kullanicinin bu oturumda ilk kez istedigi bir kontrol)
# - ornek/baslangic degerleri acikca boyle isaretlenmistir, optimize
# EDILMEMISTIR (madde 12: "baska parametre grid-search yapma").
MAX_24H_TURNOVER_PCT_OF_BOOSTER = float(os.environ.get("ATBE_MAX_24H_TURNOVER_PCT", "50"))
MAX_7D_TURNOVER_PCT_OF_BOOSTER = float(os.environ.get("ATBE_MAX_7D_TURNOVER_PCT", "150"))

# ECONOMIC REBALANCE FILTER (madde 8) - islem, booster sermayesinin bu
# yuzdesinden KUCUK bir notional uretecekse EKONOMIK DEGIL sayilir ve
# yapilmaz (dust-trade onleme). Yeni kavram, ornek deger.
MIN_TRADE_NOTIONAL_PCT_OF_BOOSTER = float(os.environ.get("ATBE_MIN_TRADE_NOTIONAL_PCT", "2.0"))


def booster_target_pct(ladder_state):
    return abe.BOOSTER_TARGET.get(ladder_state, 15.0)


def staged_step(prev_target, raw_target):
    delta = raw_target - prev_target
    if delta > 0:
        delta = min(delta, MAX_STEP_UP)
    else:
        delta = max(delta, -MAX_STEP_DOWN)
    return max(0.0, min(100.0, prev_target + delta))


class TurnoverTracker:
    """Rolling 24h/7g turnover (USDT notional) - sadece OLCUM/BUTCE amacli,
    hicbir karar mantigi ICERMEZ (madde 0 ayrimi burada da korunur: butce
    asimi kontrolu should_execute icinde, TRACKER SADECE sayar)."""

    def __init__(self):
        self._events = collections.deque()  # (ts_ms, notional)

    def add(self, ts_ms, notional):
        self._events.append((ts_ms, notional))

    def _prune_and_sum(self, ts_ms, window_ms):
        sinir = ts_ms - window_ms
        while self._events and self._events[0][0] < sinir - 7 * 86_400_000:
            # 7 gunden eski hicbir pencereye giremez - kalici temizlik
            self._events.popleft()
        return sum(n for t, n in self._events if t >= sinir)

    def turnover_24h(self, ts_ms):
        return self._prune_and_sum(ts_ms, 24 * 3_600_000)

    def turnover_7d(self, ts_ms):
        return self._prune_and_sum(ts_ms, 7 * 86_400_000)


def should_execute(actual_pct, target_pct, ticks_since_last_trade, is_risk_reduction,
                    booster_capital, turnover_tracker, ts_ms):
    """Deadband + cooldown (ATAE ile ayni desen) + TURNOVER BUDGET (madde 7)
    + ECONOMIC FILTER (madde 8) birlikte. is_risk_reduction=True (booster
    exposure'i AZALTAN islem) ise cooldown VE turnover budget'i BYPASS eder
    - guvenlik/risk-azaltma HICBIR zaman bir butce tarafindan engellenmez
    (ATAE'nin is_risk_override bypass deseniyle AYNI ilke)."""
    gap = target_pct - actual_pct
    if abs(gap) < DEADBAND_PERCENT:
        return False, gap, "DEADBAND"
    if not is_risk_reduction and ticks_since_last_trade < COOLDOWN_TICKS:
        return False, gap, "COOLDOWN"

    tahmini_notional = abs(gap) / 100.0 * booster_capital
    if not is_risk_reduction and tahmini_notional < booster_capital * (MIN_TRADE_NOTIONAL_PCT_OF_BOOSTER / 100):
        return False, gap, "ECONOMIC_TOO_SMALL"

    if not is_risk_reduction:
        mevcut_24h = turnover_tracker.turnover_24h(ts_ms)
        mevcut_7d = turnover_tracker.turnover_7d(ts_ms)
        if mevcut_24h + tahmini_notional > booster_capital * (MAX_24H_TURNOVER_PCT_OF_BOOSTER / 100):
            return False, gap, "TURNOVER_BUDGET_24H"
        if mevcut_7d + tahmini_notional > booster_capital * (MAX_7D_TURNOVER_PCT_OF_BOOSTER / 100):
            return False, gap, "TURNOVER_BUDGET_7D"
    return True, gap, None
