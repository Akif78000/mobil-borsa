"""
ATBE COORDINATOR (madde 10) - GRID CORE ve TREND BOOSTER sleeve'lerinin ayni
saatlik kontrol tik'inde birbirine ZIT yonde islem uretip uretmedigini olcer
VE (GRID'i BOZMADAN) gereksiz self-cross turnover'ini engeller.

TASARIM KARARI: "GRID'i bozma" (madde 10) acik talimati geregi, celiski
durumunda HER ZAMAN booster taraf geri adim atar (grid trade'i asla
suprese edilmez) - booster zaten sadece sinirli bir sleeve, core GRID
motorunun kendi kanitlanmis mantigina karismasi ISTENMIYOR. Booster'in
geri adim atmasi SADECE booster'in BULLISH (risk-artirici) tarafi icin
gecerlidir - booster'in RISK-AZALTICI (satis) taraf islemi ASLA
suprese edilmez (guvenlik/risk-azaltma hicbir zaman geciktirilmez, ATAE'den
tasinan ayni ilke).
"""


class GridBoosterCoordinator:
    def __init__(self):
        self.self_cross_events = []  # {"ts_ms","grid_side","grid_notional","booster_side","booster_notional","overlap","suppressed"}
        self._grid_side_this_tick = None
        self._grid_notional_this_tick = 0.0

    def rapor_grid_trade(self, side, notional):
        """Ayni saatlik tik icinde CORE GRID'in urettigi tum islemleri
        (birden fazla lot ayni tik'te tetiklenebilir) TOPLU olarak biriktirir."""
        if self._grid_side_this_tick is None:
            self._grid_side_this_tick = side
            self._grid_notional_this_tick = notional
        elif self._grid_side_this_tick == side:
            self._grid_notional_this_tick += notional
        # Zit yonde ayni tik'te GRID kendi icinde nadiren cakisir (bir bar'da
        # en fazla bir islem kurali zaten grid_backtest.py'de var) - bu dal
        # pratikte calismaz, sadece savunma amacli.

    def booster_trade_izinli_mi(self, side, notional, ts_ms, is_risk_reduction):
        """Booster bu tik'te `side` yonunde islem yapmak ISTIYOR. GRID bu
        tik'te ZIT yonde islem yaptiysa VE booster'in islemi risk-azaltici
        DEGILSE, booster ERTELENIR (bu tik'te yapilmaz, bir sonraki tik'te
        tekrar degerlendirilir - kalici bir engelleme DEGIL)."""
        izinli = True
        if self._grid_side_this_tick is not None and self._grid_side_this_tick != side and not is_risk_reduction:
            overlap = min(self._grid_notional_this_tick, notional)
            self.self_cross_events.append({
                "ts_ms": ts_ms, "grid_side": self._grid_side_this_tick,
                "grid_notional": round(self._grid_notional_this_tick, 2),
                "booster_side": side, "booster_notional": round(notional, 2),
                "overlap": round(overlap, 2), "suppressed": True,
            })
            izinli = False
        return izinli

    def tick_sifirla(self):
        """Her saatlik kontrol tik'inin SONUNDA cagrilir - sonraki tik icin
        grid-trade birikimini temizler."""
        self._grid_side_this_tick = None
        self._grid_notional_this_tick = 0.0

    def ozet(self):
        toplam_overlap = sum(e["overlap"] for e in self.self_cross_events)
        return {
            "self_cross_count": len(self.self_cross_events),
            "self_cross_overlap_usdt": round(toplam_overlap, 2),
        }
