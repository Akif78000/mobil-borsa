"""
Ucunu karsilastiran backtest: SAF-TREND (trade_bot.py mantigi), SAF-GRID
(grid_bot.py mantigi) ve HIBRIT (adaptive_bot.py mantigi - rejime gore
otomatik secim) stratejilerini AYNI gecmis veri uzerinde ayni anda calistirip
sonuclari yan yana raporlar.

Amac: "hangisi tutarli, bot kendini rejime gore mi yenilesin" sorusuna
somut sayilarla cevap vermek.

Kullanim:
    python3 adaptive_backtest.py
"""

import os
import time

from trade_bot import (
    rsi_hesapla,
    verimlilik_orani,
    _load_dotenv,
)
from backtest import fetch_history, compute_ema_series, INTERVAL_MS

_load_dotenv()

SYMBOL = os.environ.get("SYMBOL", "SHIBUSDT")
KLINE_INTERVAL = os.environ.get("KLINE_INTERVAL", "15m")
BACKTEST_DAYS = int(os.environ.get("BACKTEST_DAYS", "30"))
BACKTEST_START_CAPITAL = float(os.environ.get("BACKTEST_START_CAPITAL", "1000"))
TRADING_FEE_PERCENT = float(os.environ.get("TRADING_FEE_PERCENT", "0.1"))

RSI_BUY_THRESHOLD = float(os.environ.get("RSI_BUY_THRESHOLD", "30"))
RSI_SELL_THRESHOLD = float(os.environ.get("RSI_SELL_THRESHOLD", "70"))
EMA_TREND_SHORT = int(os.environ.get("EMA_TREND_SHORT", "50"))
EMA_TREND_LONG = int(os.environ.get("EMA_TREND_LONG", "200"))
EMA_SLOPE_LOOKBACK = int(os.environ.get("EMA_SLOPE_LOOKBACK", "5"))
ATR_PERIOD = int(os.environ.get("ATR_PERIOD", "14"))
ATR_STOP_MULTIPLIER = float(os.environ.get("ATR_STOP_MULTIPLIER", "2.0"))
ATR_TAKE_PROFIT_MULTIPLIER = float(os.environ.get("ATR_TAKE_PROFIT_MULTIPLIER", "3.0"))
VOLUME_PERIOD = int(os.environ.get("VOLUME_PERIOD", "20"))
VOLUME_MULTIPLIER = float(os.environ.get("VOLUME_MULTIPLIER", "1.20"))
TRADE_PERCENT = float(os.environ.get("TRADE_PERCENT", "33"))

GRID_STEP_DOWN_PERCENT = float(os.environ.get("GRID_STEP_DOWN_PERCENT", "1"))
GRID_STEP_UP_PERCENT = float(os.environ.get("GRID_STEP_UP_PERCENT", "1"))
GRID_ORDER_PERCENT = float(os.environ.get("GRID_ORDER_PERCENT", "10"))
GRID_MAX_OPEN_LOTS = int(os.environ.get("GRID_MAX_OPEN_LOTS", "8"))
GRID_RESERVE_PERCENT = float(os.environ.get("GRID_RESERVE_PERCENT", "20"))
GRID_LOT_STOP_PERCENT = float(os.environ.get("GRID_LOT_STOP_PERCENT", "15"))

REGIME_LOOKBACK = int(os.environ.get("REGIME_LOOKBACK", "20"))
REGIME_TREND_THRESHOLD = float(os.environ.get("REGIME_TREND_THRESHOLD", "0.3"))

START_IN_SHIB = os.environ.get("START_IN_SHIB", "false").lower() in ("1", "true", "evet")


def atr_hesapla(yuksekler, dusukler, kapanislar, i, periyot):
    basi = max(1, i - periyot + 1)
    if i - basi + 1 < periyot:
        return None
    true_ranges = []
    for j in range(basi, i + 1):
        true_ranges.append(max(
            yuksekler[j] - dusukler[j],
            abs(yuksekler[j] - kapanislar[j - 1]),
            abs(dusukler[j] - kapanislar[j - 1]),
        ))
    return sum(true_ranges) / periyot


class Portfoy:
    """Uc stratejinin ortak sanal cuzdan/istatistik iskeleti."""

    def __init__(self, isim, baslangic_fiyat):
        self.isim = isim
        if START_IN_SHIB:
            self.usdt = 0.0
            self.coin = BACKTEST_START_CAPITAL / baslangic_fiyat
        else:
            self.usdt = BACKTEST_START_CAPITAL
            self.coin = 0.0
        self.baslangic_coin_esdeger = self.coin + self.usdt / baslangic_fiyat
        self.open_lots = []  # {"strategy","qty","entry_price","quote_spent", stop/tp opsiyonel}
        self.grid_ref = baslangic_fiyat
        self.trades = []
        self.equity_egrisi = []
        self.coin_egrisi = []

        if START_IN_SHIB:
            self.open_lots.append({
                "strategy": "grid", "qty": self.coin, "entry_price": baslangic_fiyat,
                "quote_spent": BACKTEST_START_CAPITAL,
            })

    def _sat(self, lot, fiyat, tarih, sebep):
        brut = lot["qty"] * fiyat
        net = brut * (1 - TRADING_FEE_PERCENT / 100)
        self.usdt += net
        self.coin -= lot["qty"]
        kar_yuzde = (fiyat - lot["entry_price"]) / lot["entry_price"] * 100
        self.trades.append({
            "tip": "SAT", "strategy": lot["strategy"], "tarih": tarih, "fiyat": fiyat,
            "kar_yuzde": kar_yuzde, "sebep": sebep,
        })
        self.open_lots.remove(lot)
        if lot["strategy"] == "grid":
            self.grid_ref = fiyat

    def _al(self, strategy, fiyat, tarih, harcanacak, stop_fiyati=None, kar_al_fiyati=None):
        if harcanacak <= 0:
            return
        ucret = harcanacak * TRADING_FEE_PERCENT / 100
        qty = (harcanacak - ucret) / fiyat
        self.usdt -= harcanacak
        self.coin += qty
        lot = {"strategy": strategy, "qty": qty, "entry_price": fiyat, "quote_spent": harcanacak}
        if strategy == "trend":
            lot["stop_price"] = stop_fiyati
            lot["take_profit_price"] = kar_al_fiyati
        self.open_lots.append(lot)
        if strategy == "grid":
            self.grid_ref = fiyat
        self.trades.append({"tip": "AL", "strategy": strategy, "tarih": tarih, "fiyat": fiyat})

    def adim_kapat(self, fiyat):
        self.equity_egrisi.append(self.usdt + self.coin * fiyat)
        self.coin_egrisi.append(self.coin + self.usdt / fiyat)


def simulate(kapanislar, yuksekler, dusukler, hacimler, zamanlar):
    min_gerekli = max(EMA_TREND_LONG + EMA_SLOPE_LOOKBACK, ATR_PERIOD, VOLUME_PERIOD, REGIME_LOOKBACK) + 15
    if len(kapanislar) < min_gerekli:
        raise SystemExit(
            f"Yetersiz veri: {len(kapanislar)} mum var, en az {min_gerekli} lazim. "
            f"BACKTEST_DAYS degerini artirin."
        )

    ema_kisa_serisi = compute_ema_series(kapanislar, EMA_TREND_SHORT)
    ema_uzun_serisi = compute_ema_series(kapanislar, EMA_TREND_LONG)

    baslangic_fiyat = kapanislar[min_gerekli]
    trend_p = Portfoy("SAF-TREND", baslangic_fiyat)
    grid_p = Portfoy("SAF-GRID", baslangic_fiyat)
    hibrit_p = Portfoy("HIBRIT", baslangic_fiyat)

    rejim_sayaci = {"TREND": 0, "RANGE": 0}

    for i in range(min_gerekli, len(kapanislar)):
        fiyat = kapanislar[i]
        tarih = time.strftime("%Y-%m-%d %H:%M", time.localtime(zamanlar[i] / 1000))

        rsi = rsi_hesapla(kapanislar[max(0, i - 59): i + 1])
        ek, eu = ema_kisa_serisi[i], ema_uzun_serisi[i]
        trend_yukari = None if ek is None or eu is None else ek > eu
        onceki_ema = ema_kisa_serisi[i - EMA_SLOPE_LOOKBACK] if i >= EMA_SLOPE_LOOKBACK else None
        ema_egimi_yukari = ek is not None and onceki_ema is not None and ek > onceki_ema
        hacim_ort = sum(hacimler[i - VOLUME_PERIOD:i]) / VOLUME_PERIOD
        hacim_onayi = hacimler[i] >= hacim_ort * VOLUME_MULTIPLIER
        atr = atr_hesapla(yuksekler, dusukler, kapanislar, i, ATR_PERIOD)
        er = verimlilik_orani(kapanislar[: i + 1], REGIME_LOOKBACK)
        rejim = "TREND" if (er is not None and er >= REGIME_TREND_THRESHOLD) else "RANGE"
        rejim_sayaci[rejim] += 1

        trend_al_sinyali = (
            rsi < RSI_BUY_THRESHOLD and trend_yukari is True
            and ema_egimi_yukari and hacim_onayi and atr is not None
        )

        # --- SAF-TREND portfoyu (trade_bot.py mantigi) ---
        _adim_trend_only(trend_p, fiyat, tarih, rsi, trend_yukari, trend_al_sinyali, atr)

        # --- SAF-GRID portfoyu (grid_bot.py mantigi) ---
        _adim_grid_only(grid_p, fiyat, tarih)

        # --- HIBRIT portfoy (rejime gore) ---
        _adim_hibrit(hibrit_p, fiyat, tarih, rsi, trend_yukari, trend_al_sinyali, atr, rejim)

        trend_p.adim_kapat(fiyat)
        grid_p.adim_kapat(fiyat)
        hibrit_p.adim_kapat(fiyat)

    return trend_p, grid_p, hibrit_p, baslangic_fiyat, rejim_sayaci


def _adim_trend_only(p, fiyat, tarih, rsi, trend_yukari, al_sinyali, atr):
    for lot in list(p.open_lots):
        stop_ok = lot.get("stop_price") is not None and fiyat <= lot["stop_price"]
        tp_ok = lot.get("take_profit_price") is not None and fiyat >= lot["take_profit_price"]
        if stop_ok or tp_ok or rsi > RSI_SELL_THRESHOLD or trend_yukari is False:
            sebep = "stop_loss" if stop_ok else "kar_al" if tp_ok else (
                "asiri_alim" if rsi > RSI_SELL_THRESHOLD else "trend_bozuldu"
            )
            p._sat(lot, fiyat, tarih, sebep)
            return
    if not p.open_lots and al_sinyali:
        harcanacak = p.usdt * (TRADE_PERCENT / 100)
        stop_fiyati = fiyat - ATR_STOP_MULTIPLIER * atr
        kar_al_fiyati = fiyat + ATR_TAKE_PROFIT_MULTIPLIER * atr
        p._al("trend", fiyat, tarih, harcanacak, stop_fiyati, kar_al_fiyati)


def _adim_grid_only(p, fiyat, tarih):
    for lot in sorted(p.open_lots, key=lambda l: l["entry_price"]):
        hedef = lot["entry_price"] * (1 + GRID_STEP_UP_PERCENT / 100)
        stop_seviyesi = lot["entry_price"] * (1 - GRID_LOT_STOP_PERCENT / 100)
        if fiyat >= hedef or fiyat <= stop_seviyesi:
            p._sat(lot, fiyat, tarih, "grid_hedef" if fiyat >= hedef else "grid_stop_loss")
            return
    rezerv = BACKTEST_START_CAPITAL * (GRID_RESERVE_PERCENT / 100)
    harcanacak = p.usdt * (GRID_ORDER_PERCENT / 100)
    al_tetik = fiyat <= p.grid_ref * (1 - GRID_STEP_DOWN_PERCENT / 100)
    if al_tetik and len(p.open_lots) < GRID_MAX_OPEN_LOTS and harcanacak > 0 and (p.usdt - harcanacak) >= rezerv:
        p._al("grid", fiyat, tarih, harcanacak)


def _adim_hibrit(p, fiyat, tarih, rsi, trend_yukari, trend_al_sinyali, atr, rejim):
    for lot in sorted(p.open_lots, key=lambda l: l["entry_price"]):
        if lot["strategy"] == "trend":
            stop_ok = lot.get("stop_price") is not None and fiyat <= lot["stop_price"]
            tp_ok = lot.get("take_profit_price") is not None and fiyat >= lot["take_profit_price"]
            if stop_ok or tp_ok or rsi > RSI_SELL_THRESHOLD or trend_yukari is False:
                sebep = "stop_loss" if stop_ok else "kar_al" if tp_ok else (
                    "asiri_alim" if rsi > RSI_SELL_THRESHOLD else "trend_bozuldu"
                )
                p._sat(lot, fiyat, tarih, sebep)
                return
        else:
            hedef = lot["entry_price"] * (1 + GRID_STEP_UP_PERCENT / 100)
            stop_seviyesi = lot["entry_price"] * (1 - GRID_LOT_STOP_PERCENT / 100)
            if fiyat >= hedef or fiyat <= stop_seviyesi:
                p._sat(lot, fiyat, tarih, "grid_hedef" if fiyat >= hedef else "grid_stop_loss")
                return

    trend_lot_var = any(l["strategy"] == "trend" for l in p.open_lots)
    grid_lot_sayisi = sum(1 for l in p.open_lots if l["strategy"] == "grid")

    if rejim == "TREND" and not trend_lot_var and trend_al_sinyali:
        harcanacak = p.usdt * (TRADE_PERCENT / 100)
        stop_fiyati = fiyat - ATR_STOP_MULTIPLIER * atr
        kar_al_fiyati = fiyat + ATR_TAKE_PROFIT_MULTIPLIER * atr
        p._al("trend", fiyat, tarih, harcanacak, stop_fiyati, kar_al_fiyati)
    elif rejim == "RANGE" and grid_lot_sayisi < GRID_MAX_OPEN_LOTS:
        rezerv = BACKTEST_START_CAPITAL * (GRID_RESERVE_PERCENT / 100)
        harcanacak = p.usdt * (GRID_ORDER_PERCENT / 100)
        al_tetik = fiyat <= p.grid_ref * (1 - GRID_STEP_DOWN_PERCENT / 100)
        if al_tetik and harcanacak > 0 and (p.usdt - harcanacak) >= rezerv:
            p._al("grid", fiyat, tarih, harcanacak)


def _ozet_sat(p, son_fiyat):
    toplam_deger = p.equity_egrisi[-1] if p.equity_egrisi else BACKTEST_START_CAPITAL
    getiri = (toplam_deger - BACKTEST_START_CAPITAL) / BACKTEST_START_CAPITAL * 100
    satislar = [t for t in p.trades if t["tip"] == "SAT"]
    alimlar = [t for t in p.trades if t["tip"] == "AL"]
    karli = [t for t in satislar if t["kar_yuzde"] > 0]
    kazanma = (len(karli) / len(satislar) * 100) if satislar else 0
    ort_kar = (sum(t["kar_yuzde"] for t in satislar) / len(satislar)) if satislar else 0
    tepe, max_dd = float("-inf"), 0.0
    for d in p.equity_egrisi:
        tepe = max(tepe, d)
        if tepe > 0:
            max_dd = min(max_dd, (d - tepe) / tepe * 100)
    bitis_coin = p.coin_egrisi[-1] if p.coin_egrisi else p.baslangic_coin_esdeger
    token_degisim = (
        (bitis_coin - p.baslangic_coin_esdeger) / p.baslangic_coin_esdeger * 100
        if p.baslangic_coin_esdeger else 0
    )
    return {
        "isim": p.isim, "getiri": getiri, "toplam_islem": len(p.trades),
        "alim": len(alimlar), "satis": len(satislar), "kazanma": kazanma,
        "ort_kar": ort_kar, "max_dd": max_dd, "acik_lot": len(p.open_lots),
        "token_degisim": token_degisim,
    }


def ozet_yazdir(trend_p, grid_p, hibrit_p, son_fiyat, mum_sayisi, rejim_sayaci):
    sonuclar = [_ozet_sat(trend_p, son_fiyat), _ozet_sat(grid_p, son_fiyat), _ozet_sat(hibrit_p, son_fiyat)]

    toplam_rejim = sum(rejim_sayaci.values()) or 1
    print()
    print("=" * 78)
    print(f"HIBRIT KARSILASTIRMA: {SYMBOL} {KLINE_INTERVAL} - son {BACKTEST_DAYS} gun ({mum_sayisi} mum)")
    print(f"Rejim dagilimi: TREND=%{rejim_sayaci['TREND']/toplam_rejim*100:.1f}  "
          f"RANGE=%{rejim_sayaci['RANGE']/toplam_rejim*100:.1f}  (ER esigi={REGIME_TREND_THRESHOLD})")
    print("=" * 78)
    baslik = f"{'Strateji':<10} {'Getiri':>8} {'Islem':>6} {'Kazan.':>7} {'Ort.Kar':>8} {'MaksDD':>8} {'Token%':>8}"
    print(baslik)
    print("-" * 78)
    for s in sonuclar:
        print(
            f"{s['isim']:<10} {s['getiri']:>+7.2f}% {s['toplam_islem']:>6} "
            f"%{s['kazanma']:>5.1f} {s['ort_kar']:>+7.2f}% {s['max_dd']:>7.2f}% {s['token_degisim']:>+7.2f}%"
        )
    print("=" * 78)

    en_iyi_token = max(sonuclar, key=lambda s: s["token_degisim"])
    en_iyi_getiri = max(sonuclar, key=lambda s: s["getiri"])
    print(f"Token adedinde en iyi: {en_iyi_token['isim']} ({en_iyi_token['token_degisim']:+.2f}%)")
    print(f"Dolar getirisinde en iyi: {en_iyi_getiri['isim']} ({en_iyi_getiri['getiri']:+.2f}%)")
    print("(Not: gecmis performans gelecegi garanti etmez, tek donem yeterli kanit degildir.)")

    csv_path = "adaptive_backtest_trades.csv"
    with open(csv_path, "w") as f:
        f.write("portfoy,tip,strategy,tarih,fiyat,kar_yuzde,sebep\n")
        for p in (trend_p, grid_p, hibrit_p):
            for t in p.trades:
                f.write(
                    f"{p.isim},{t['tip']},{t.get('strategy','')},{t['tarih']},{t['fiyat']},"
                    f"{t.get('kar_yuzde','')},{t.get('sebep','')}\n"
                )
    print(f"\nIslem detaylari kaydedildi: {csv_path}")


def main():
    print(f"Gecmis veri cekiliyor: {SYMBOL} {KLINE_INTERVAL} son {BACKTEST_DAYS} gun...")
    candles = fetch_history(SYMBOL, KLINE_INTERVAL, BACKTEST_DAYS)
    kapanislar = [float(c[4]) for c in candles]
    yuksekler = [float(c[2]) for c in candles]
    dusukler = [float(c[3]) for c in candles]
    hacimler = [float(c[5]) for c in candles]
    zamanlar = [c[0] for c in candles]
    trend_p, grid_p, hibrit_p, baslangic_fiyat, rejim_sayaci = simulate(
        kapanislar, yuksekler, dusukler, hacimler, zamanlar
    )
    ozet_yazdir(trend_p, grid_p, hibrit_p, kapanislar[-1], len(candles), rejim_sayaci)


if __name__ == "__main__":
    main()
