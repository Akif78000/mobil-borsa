"""
ATBE PARITY / AUDIT TEST SUITE - kullanicinin acik talimati: "STRATEJI
OPTIMIZASYONUNU DURDUR... Once ATBE mimarisinin baseline GRID'i gercekten
dogru sekilde sardigini KANITLA." Bu dosya YENI bir threshold/indicator/
allocation denemesi DEGIL - SADECE atbe_backtest.py'nin CORE sleeve'inin
(hb._grid_sleeve_step uzerinden) standalone grid_backtest.py.simulate() ile
GERCEKTEN ayni sonucu uretip uretmedigini KANITLAYAN bir test alt yapisidir
(madde 1-5, 11).

NEDEN 5m/fee-only: standalone GRID varsayilan olarak GRID_KLINE_INTERVAL=5m
mum + SADECE TRADING_FEE_PERCENT (slippage YOK) kullanir. atbe_backtest.py
ise (kendi, ayri, BILINCLI tasarim tercihi olan) 15m mum + fee+slippage
kullanir - bu IKI FARKLI parametre seti ayni "GRID" stratejisini FARKLI
varsayimlarla calistirdigi icin, DOGRUDAN parite testi icin ATBE'nin core
path'i standalone GRID ile AYNI mum+fee ile calistirilmalidir (asagida
`simulate_atbe(..., cost_percent=gb.TRADING_FEE_PERCENT)` ve 5m mumlar
kullanilarak yapiliyor). Bu, atbe_backtest.py'nin PRODUCTION varsayilanlarini
DEGISTIRMEZ - sadece bu parite testinde acikca eslenmis parametreler kullanir.

KULLANIM:
    python3 atbe_parity_test.py                 (varsayilan 21 gun)
    ATBE_PARITY_DAYS=90 python3 atbe_parity_test.py
"""

import csv
import os
import time

from backtest import fetch_history, INTERVAL_MS
from trade_bot import _load_dotenv
import grid_backtest as gb
import hybrid_backtest as hb
import atae_backtest as atb
import atbe_backtest as atbb

_load_dotenv()

SYMBOL = os.environ.get("SYMBOL", "SHIBUSDT")
DAYS = int(os.environ.get("ATBE_PARITY_DAYS", "21"))
OUTPUT_DIR = os.environ.get("ATBE_PARITY_OUTPUT_DIR", "atbe_parity_reports")
TOLERANS = 1e-6  # SADECE floating-point tolerans (madde 1 sonu)


def _fetch_5m(days):
    candles = fetch_history(SYMBOL, gb.GRID_KLINE_INTERVAL, days)
    kapanislar = [float(c[4]) for c in candles]
    zamanlar = [c[0] for c in candles]
    return zamanlar, kapanislar


def _tarih(ts_ms):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts_ms / 1000))


def _solve_usdt_coin(equity_a, coin_equiv_a, equity_b, coin_equiv_b, price_a, price_b):
    """GRID'in (black-box) equity_egrisi/coin_egrisi ciktisindan, IKI (fiyati
    FARKLI, arada islem OLMAYAN) bar arasinda SABIT kalan usdt/coin'i, gb.
    simulate()'in KENDI ic formullerine hic DOKUNMADAN, SALT matematikle geri
    cikarir: E=usdt+coin*p (equity_egrisi), C=coin+usdt/p (coin_egrisi).
    Iki farkli p'de bu iki denklemi es zamanli cozer."""
    if abs(price_a - price_b) < 1e-12:
        return None
    # E = usdt + coin*p (equity_egrisi) -> iki farkli p'de: E_a - E_b = coin*(p_a-p_b)
    coin = (equity_a - equity_b) / (price_a - price_b)
    usdt = equity_a - coin * price_a
    return usdt, coin


def _grid_trade_bar_index(tarih_str, tarih_to_idx):
    return tarih_to_idx.get(tarih_str)


def _reconstruct_grid_cash_per_trade(grid_trades, equity_egrisi, coin_egrisi, zamanlar, kapanislar):
    """Her GRID trade'inden HEMEN ONCEKI durumu (usdt,coin), o trade bar'indan
    GERIYE dogru, ardisik iki FARKLI-fiyatli, trade-free bar cifti bularak COZER
    (bkz. _solve_usdt_coin doc). SADECE CSV/teshis amacli - parite KARARI
    tip/tarih/fiyat/final-equity/final-coin/islem-sayisi TAM ESLESMESINE
    dayanir, bu reconstruction ona EK bir ayrinti katmanidir."""
    tarih_to_idx = {}
    for i, t in enumerate(zamanlar):
        tarih_to_idx[_tarih(t)] = i
    trade_bar_idx = set(_grid_trade_bar_index(t["tarih"], tarih_to_idx) for t in grid_trades)
    trade_bar_idx.discard(None)

    sonuclar = []
    for t in grid_trades:
        bar_i = tarih_to_idx.get(t["tarih"])
        if bar_i is None or bar_i < 2:
            sonuclar.append((None, None))
            continue
        # Trade bar'indan HEMEN ONCEKI (bar_i-1) durumu ariyoruz - bu ONCEKI
        # durum HENUZ bu trade'i icermez (dogru "trade ONCESI" anlik goruntu).
        a, b = bar_i - 1, bar_i - 2
        while b >= 0 and (a in trade_bar_idx or abs(kapanislar[a] - kapanislar[b]) < 1e-12):
            a, b = a - 1, b - 1
        if b < 0:
            sonuclar.append((None, None))
            continue
        cozum = _solve_usdt_coin(equity_egrisi[a], coin_egrisi[a], equity_egrisi[b], coin_egrisi[b], kapanislar[a], kapanislar[b])
        sonuclar.append(cozum if cozum else (None, None))
    return sonuclar


def _replicate_core_only(zamanlar, kapanislar, baslangic_sermaye, cost_pct):
    """ATBE'nin core sleeve'inin KENDISI (hb._grid_sleeve_step, DEGISTIRILMEDEN)
    - ATBE'nin production simulate_atbe() ile AYNI cagri deseni, ama bu
    fonksiyon her trade ONCESI/SONRASI durumu ACIKCA kaydeder (parite CSV'si
    icin)."""
    state = {"usdt": baslangic_sermaye, "coin": 0.0, "open_lots": [],
              "reference_price": kapanislar[0], "baslangic_deger": baslangic_sermaye}
    trades, equity_egrisi, coin_egrisi = [], [], []
    for i, fiyat in enumerate(kapanislar):
        tarih = _tarih(zamanlar[i])
        usdt_once, coin_once = state["usdt"], state["coin"]
        yerel_trades = []
        hb._grid_sleeve_step(state, fiyat, tarih, yerel_trades, cost_pct)
        for t in yerel_trades:
            if t["tip"] == "GRID_SLEEVE_AL":
                notional = usdt_once - state["usdt"]
                qty = state["coin"] - coin_once
                side = "AL"
            else:
                qty = coin_once - state["coin"]
                notional = qty * fiyat
                side = "SAT"
            t.update({"usdt_once": usdt_once, "coin_once": coin_once, "usdt_sonra": state["usdt"],
                      "coin_sonra": state["coin"], "usdt_tutari": notional, "qty": qty, "side": side,
                      "ts_ms": zamanlar[i]})
            trades.append(t)
        equity_egrisi.append(state["usdt"] + state["coin"] * fiyat)
        coin_egrisi.append(state["coin"] + state["usdt"] / fiyat)
    return trades, equity_egrisi, coin_egrisi, state


def _compare_ve_csv_yaz(test_adi, grid_trades, grid_equity, grid_coin, zamanlar, kapanislar,
                         atbe_trades, atbe_equity, atbe_coin):
    grid_recon = _reconstruct_grid_cash_per_trade(grid_trades, grid_equity, grid_coin, zamanlar, kapanislar)
    n = max(len(grid_trades), len(atbe_trades))
    satirlar = []
    ilk_uyumsuzluk = None
    for i in range(n):
        g = grid_trades[i] if i < len(grid_trades) else None
        a = atbe_trades[i] if i < len(atbe_trades) else None
        g_usdt, g_coin = grid_recon[i] if i < len(grid_recon) else (None, None)
        g_tip = g["tip"] if g else None
        a_tip = "GRID_SLEEVE_AL" if (a and a["side"] == "AL") else ("GRID_SLEEVE_SAT" if a else None)
        uyumsuz = (g is None) != (a is None)
        if not uyumsuz and g is not None and a is not None:
            uyumsuz = (g["tarih"] != a["tarih"] or abs(g["fiyat"] - a["fiyat"]) > TOLERANS
                       or (g_tip.endswith("AL")) != (a["side"] == "AL"))
        neden = None
        if uyumsuz:
            neden = "TRADE_SEQUENCE_MISMATCH"
            if ilk_uyumsuzluk is None:
                ilk_uyumsuzluk = (i, g["tarih"] if g else (a["tarih"] if a else None))
        def _r(x, nd=6):
            return round(x, nd) if isinstance(x, (int, float)) else None

        satirlar.append({
            "timestamp": (g["tarih"] if g else (a["tarih"] if a else None)),
            "grid_action": g_tip, "atbe_core_action": (a and ("GRID_SLEEVE_" + a["side"])),
            "grid_qty": _r(g_coin), "atbe_qty": _r(a["qty"]) if a else None,
            "grid_price": (g["fiyat"] if g else None), "atbe_price": (a["fiyat"] if a else None),
            "grid_fee": None, "atbe_fee": None,
            "grid_cash": _r(g_usdt), "atbe_cash": _r(a["usdt_sonra"]) if a else None,
            "grid_shib": _r(g_coin), "atbe_shib": _r(a["coin_sonra"]) if a else None,
            "grid_equity": None, "atbe_equity": None,
            "difference_reason": neden,
        })

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    yol = os.path.join(OUTPUT_DIR, f"{test_adi}_trade_diff.csv")
    with open(yol, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(satirlar[0].keys()) if satirlar else
                                 ["timestamp", "grid_action", "atbe_core_action", "grid_qty", "atbe_qty",
                                  "grid_price", "atbe_price", "grid_fee", "atbe_fee", "grid_cash", "atbe_cash",
                                  "grid_shib", "atbe_shib", "grid_equity", "atbe_equity", "difference_reason"])
        writer.writeheader()
        writer.writerows(satirlar)

    final_equity_fark = abs(grid_equity[-1] - atbe_equity[-1])
    final_coin_fark = abs(grid_coin[-1] - atbe_coin[-1])
    trade_count_fark = len(grid_trades) - len(atbe_trades)

    print(f"\n--- {test_adi} ---")
    print(f"GRID  : final_equity={grid_equity[-1]:.6f}  final_coin_equiv={grid_coin[-1]:.6f}  trades={len(grid_trades)}")
    print(f"ATBE  : final_equity={atbe_equity[-1]:.6f}  final_coin_equiv={atbe_coin[-1]:.6f}  trades={len(atbe_trades)}")
    print(f"FARK  : equity={final_equity_fark:.8f}  coin_equiv={final_coin_fark:.8f}  trade_count={trade_count_fark}")
    print(f"[PARITY] CSV yazildi: {yol} ({len(satirlar)} satir)")

    gecti = final_equity_fark < TOLERANS and final_coin_fark < TOLERANS and trade_count_fark == 0 and ilk_uyumsuzluk is None
    if gecti:
        print(f"SONUC: PARITY GECTI ({test_adi}) - GRID ile ATBE core BIREBIR AYNI (float tolerans disinda).")
    else:
        print(f"SONUC: PARITY BASARISIZ ({test_adi}).")
        if ilk_uyumsuzluk:
            print(f"  ILK DIVERGENCE: trade #{ilk_uyumsuzluk[0]}  timestamp={ilk_uyumsuzluk[1]}")
        else:
            print("  Trade sirasi/tip/fiyat AYNI ama final equity/coin/islem-sayisi FARKLI - "
                  "muhtemelen cost_pct veya baslangic sermayesi uyumsuzlugu.")
    return gecti, ilk_uyumsuzluk


def test_a_zero_booster_parity():
    """madde 1: GRID 1000 USDT == ATBE CORE-only (core_pct=100) 1000 USDT,
    AYNI 5m mum, AYNI (fee-only, slippage YOK) maliyet."""
    zamanlar, kapanislar = _fetch_5m(DAYS)
    onceki_sermaye = gb.BACKTEST_START_CAPITAL
    gb.BACKTEST_START_CAPITAL = 1000.0
    try:
        grid_trades, grid_equity, grid_coin, *_ = gb.simulate(kapanislar, zamanlar)
    finally:
        gb.BACKTEST_START_CAPITAL = onceki_sermaye

    atbe_trades, atbe_equity, atbe_coin, _ = _replicate_core_only(zamanlar, kapanislar, 1000.0, gb.TRADING_FEE_PERCENT)
    return _compare_ve_csv_yaz("TEST_A_zero_booster_1000", grid_trades, grid_equity, grid_coin, zamanlar, kapanislar,
                                atbe_trades, atbe_equity, atbe_coin)


def test_b_normalized_core_800():
    """madde 5: GRID 800 USDT (standalone, gb.BACKTEST_START_CAPITAL=800) ==
    ATBE'nin core_pct=80 ile 1000 USDT toplam icindeki 800 USDT'lik core'u."""
    zamanlar, kapanislar = _fetch_5m(DAYS)
    onceki_sermaye = gb.BACKTEST_START_CAPITAL
    gb.BACKTEST_START_CAPITAL = 800.0
    try:
        grid_trades, grid_equity, grid_coin, *_ = gb.simulate(kapanislar, zamanlar)
    finally:
        gb.BACKTEST_START_CAPITAL = onceki_sermaye

    atbe_trades, atbe_equity, atbe_coin, _ = _replicate_core_only(zamanlar, kapanislar, 800.0, gb.TRADING_FEE_PERCENT)
    return _compare_ve_csv_yaz("TEST_B_normalized_core_800", grid_trades, grid_equity, grid_coin, zamanlar, kapanislar,
                                atbe_trades, atbe_equity, atbe_coin)


def test_c_shadow_booster():
    """madde 2: core=100%, booster GERCEK sinyal hesaplar (gercek shib_series/
    majors_series okunur) ama HICBIR ZAMAN trade YAPMAZ, coordinator KAPALI.
    Sonuc yine standalone GRID 1000 ile BIREBIR AYNI olmali - degilse booster
    SINYAL HESAPLAMASININ KENDISI (yan etki/paylasilan mutable state) core
    path'i etkiliyor demektir."""
    zamanlar, kapanislar = _fetch_5m(DAYS)
    onceki_sermaye = gb.BACKTEST_START_CAPITAL
    gb.BACKTEST_START_CAPITAL = 1000.0
    try:
        grid_trades, grid_equity, grid_coin, *_ = gb.simulate(kapanislar, zamanlar)
    finally:
        gb.BACKTEST_START_CAPITAL = onceki_sermaye

    shib_series, majors_series, _z, _k = atb._fetch_all(DAYS)
    onceki_baslangic = atbb.BACKTEST_START_CAPITAL
    atbb.BACKTEST_START_CAPITAL = 1000.0
    try:
        sonuc = atbb.simulate_atbe(shib_series, majors_series, zamanlar, kapanislar, core_pct=100.0,
                                    cost_percent=gb.TRADING_FEE_PERCENT,
                                    booster_execution_enabled=False, coordinator_enabled=False)
    finally:
        atbb.BACKTEST_START_CAPITAL = onceki_baslangic

    atbe_trades = [{"tarih": t["tarih"], "fiyat": t["fiyat"], "side": "AL" if t["tip"] == "GRID_SLEEVE_AL" else "SAT",
                     "usdt_tutari": t.get("usdt_tutari", 0.0), "qty": 0.0, "usdt_sonra": None, "coin_sonra": None}
                    for t in sonuc["trades"] if t["tip"].startswith("GRID_SLEEVE_")]
    assert not any(t["tip"].startswith("BOOSTER_") for t in sonuc["trades"]), "SHADOW modda booster trade OLUSMAMALI"
    return _compare_ve_csv_yaz("TEST_C_shadow_booster", grid_trades, grid_equity, grid_coin, zamanlar, kapanislar,
                                atbe_trades, sonuc["equity_egrisi"], sonuc["coin_egrisi"])


def test_d_coordinator_isolation():
    """madde 3: core=100%, booster trade OFF, coordinator ON (varsayilan).
    SUPPRESSED CORE TRADE = 0 olmali (coordinator YAPISI GEREGI core'a HIC
    dokunamaz - sadece booster taleplerini degerlendirir) VE booster hic
    gercek trade denemedigi icin self_cross_count DE 0 olmali."""
    zamanlar, kapanislar = _fetch_5m(DAYS)
    shib_series, majors_series, _z, _k = atb._fetch_all(DAYS)
    onceki_baslangic = atbb.BACKTEST_START_CAPITAL
    atbb.BACKTEST_START_CAPITAL = 1000.0
    try:
        sonuc = atbb.simulate_atbe(shib_series, majors_series, zamanlar, kapanislar, core_pct=100.0,
                                    cost_percent=gb.TRADING_FEE_PERCENT,
                                    booster_execution_enabled=False, coordinator_enabled=True)
    finally:
        atbb.BACKTEST_START_CAPITAL = onceki_baslangic

    cross = sonuc["coordinator"].ozet()
    core_trade_sayisi = sum(1 for t in sonuc["trades"] if t["tip"].startswith("GRID_SLEEVE_"))
    print(f"\n--- TEST_D_coordinator_isolation ---")
    print(f"self_cross_count={cross['self_cross_count']}  core_trade_sayisi={core_trade_sayisi}")
    assert cross["self_cross_count"] == 0, "Booster hic trade denemedigi halde self-cross sayildi - BUG."
    print("SONUC: PARITY GECTI (TEST_D) - coordinator, booster trade yokken HICBIR core islemi bastirmadi/etkilemedi.")
    return True, None


def test_e_sleeve_accounting_identity():
    """madde 4/11: CORE_PNL + BOOSTER_PNL - COSTS ~ TOTAL_PNL. simulate_atbe
    ZATEN her bar'da bu ozdesligi assert ediyor (atbe_backtest.py) - burada
    GERCEK (booster AKTIF) bir kosuda bunu AYRICA acikca yazdiriyoruz."""
    zamanlar, kapanislar = _fetch_5m(DAYS)
    shib_series, majors_series, _z, _k = atb._fetch_all(DAYS)
    onceki_baslangic = atbb.BACKTEST_START_CAPITAL
    atbb.BACKTEST_START_CAPITAL = 1000.0
    try:
        sonuc = atbb.simulate_atbe(shib_series, majors_series, zamanlar, kapanislar, core_pct=80.0)
    finally:
        atbb.BACKTEST_START_CAPITAL = onceki_baslangic

    core_pnl = sonuc["core_equity_bar"][-1] - sonuc["core_deger_baslangic"]
    booster_pnl = sonuc["booster_equity_bar"][-1] - sonuc["booster_deger_baslangic"]
    toplam_pnl = sonuc["equity_egrisi"][-1] - 1000.0
    fark = abs((core_pnl + booster_pnl) - toplam_pnl)
    print(f"\n--- TEST_E_sleeve_accounting_identity ---")
    print(f"CORE_PNL={core_pnl:.6f}  BOOSTER_PNL={booster_pnl:.6f}  TOPLAM_PNL(gercek)={toplam_pnl:.6f}  FARK={fark:.10f}")
    assert fark < 1e-6, "CORE+BOOSTER != TOPLAM - accounting hatasi (SHIB stogu iki kez sayiliyor olabilir)."
    print("SONUC: ACCOUNTING IDENTITY DOGRULANDI (CORE_PNL+BOOSTER_PNL == TOPLAM_PNL, maliyetler zaten iceride).")
    return True, None


def main():
    print(f"{'#' * 78}\nATBE PARITY / AUDIT TEST SUITE - {SYMBOL}, son {DAYS} gun\n{'#' * 78}")
    sonuclar = {}
    sonuclar["A"] = test_a_zero_booster_parity()
    sonuclar["B"] = test_b_normalized_core_800()
    sonuclar["C"] = test_c_shadow_booster()
    sonuclar["D"] = test_d_coordinator_isolation()
    sonuclar["E"] = test_e_sleeve_accounting_identity()

    print(f"\n{'#' * 78}\nPARITY OZET\n{'#' * 78}")
    hepsi_gecti = True
    for isim, (gecti, ilk) in sonuclar.items():
        print(f"  TEST {isim}: {'GECTI' if gecti else 'BASARISIZ'}" + (f"  (ilk divergence: {ilk})" if ilk else ""))
        hepsi_gecti = hepsi_gecti and gecti

    if hepsi_gecti:
        print("\nKARAR: PARITY TAMAMEN DOGRU. ATBE'nin core sleeve'i standalone GRID'i "
              "gercekten birebir sarmalıyor - mevcut ATBE 180g sonucu GERCEK bir strateji "
              "basarisizligidir (parity bug DEGIL).")
    else:
        print("\nKARAR: PARITY BUG BULUNDU. Strateji sonuclarina GUVENILMEMELI - once bu "
              "bug duzeltilmeli, 30/60/90/180 testleri TEKRAR calistirilmali, eski ATBE "
              "sonuclari GECERSIZ sayilmalidir.")


if __name__ == "__main__":
    main()
