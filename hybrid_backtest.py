"""
HYBRID sistem backtest'i: GRID (kanitlanmis %1 salinim) + HYBRID TREND
ENGINE + PORTFOLIO MANAGER birlikte. grid_bot.py / grid_backtest.py /
trend_engine_backtest.py'ye HIC dokunmaz - hepsi ayri, degismeden calisir;
bu script onlari IMPORT EDIP ayni pencerede yan yana raporlar.

MIMARI (kullanicinin istedigi "grid sadece uygulayici, o an rejime gore
YATAY'da grid'in kendi isini yapmasi" gereksinimini karsilamak icin):

  Toplam sermaye IKI bagimsiz alt-hesaba (sleeve) bolunur:

    GRID SLEEVE (HYBRID_GRID_SLEEVE_PERCENT, varsayilan %40)
      - grid_backtest.py'nin BIREBIR AYNI %1 salinim mantigi, kendi ic
        muhasebesiyle. REJIMDEN BAGIMSIZ HER BARDA calisir - "YATAY'da
        mevcut grid sistemi aktif olsun" istegi boylece HER ZAMAN, ozellikle
        YATAY'da anlamli sekilde karsilanir (cunku grid zaten yatay
        piyasada en iyi calisan mekanizma - bkz. SYSTEM_OVERVIEW.md).

    TREND SLEEVE (kalan yuzde)
      - hybrid_engine.classify() + portfolio_manager.hybrid_target_
        allocation() tarafindan yonetilir. YATAY rejiminde bu sleeve'in
        hedefi DEGISTIRILMEZ (portfolio_manager None doner) - yani trend
        sleeve YATAY'da sessiz kalir, karar grid sleeve'e kalir. Diger
        rejimlerde (GUCLU_YUKSELIS...DAGITIM) kademeli olarak hedefe dogru
        yeniden dengelenir.

  Boylece: yatay piyasada agirlik fiilen grid mantigina (sleeve + trend
  sleeve'in "degismeme" hali) kayar; guclu trendlerde ise trend sleeve
  yonlu pozisyon alarak gridin "trendi kacirma" zaafini telafi etmeye
  calisir. Iki sleeve'in ORANI (HYBRID_GRID_SLEEVE_PERCENT) sabit bir
  gercek DEGILDIR - farkli degerlerle (orn. %20/%40/%60) backtest edip
  risk/getiri dengesini gozlemleyin.

Kullanim:
    python3 hybrid_backtest.py

Rapor: GRID / EMA / SUPERTREND / KAMA / HYBRID sistemlerini AYNI
BACKTEST_DAYS penceresinde tek tabloda karsilastirir - "SHIB icin bu
sistem sadece grid kullanmaya gore gercekten daha iyi mi?" sorusuna
somut sayilarla cevap arar. 30/90/180 gun icin AYRI AYRI calistirin
(diger tum backtest araclariyla ayni kural).
"""

import os
import time

from backtest import fetch_history, INTERVAL_MS
from trade_bot import _load_dotenv
import hybrid_engine as he
import portfolio_manager as pm
import grid_backtest as gb
import trend_engine_backtest as teb
import trend_engine as te

_load_dotenv()

SYMBOL = os.environ.get("SYMBOL", "SHIBUSDT")
MAJOR_SYMBOLS = [s.strip() for s in os.environ.get("ENGINE_MAJOR_SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT").split(",") if s.strip()]
BACKTEST_DAYS = int(os.environ.get("BACKTEST_DAYS", "30"))
BACKTEST_START_CAPITAL = float(os.environ.get("BACKTEST_START_CAPITAL", "1000"))
TRADING_FEE_PERCENT = float(os.environ.get("TRADING_FEE_PERCENT", "0.1"))
SLIPPAGE_PERCENT = float(os.environ.get("SLIPPAGE_PERCENT", "0.05"))
START_IN_SHIB = os.environ.get("START_IN_SHIB", "false").lower() in ("1", "true", "evet")

BASE_INTERVAL = "15m"
TIMEFRAMES = he.TIMEFRAMES  # 3m,5m,15m,30m,1h,4h,1d

HYBRID_GRID_SLEEVE_PERCENT = float(os.environ.get("HYBRID_GRID_SLEEVE_PERCENT", "40"))
HYBRID_REBALANCE_MINUTES = int(os.environ.get("HYBRID_REBALANCE_MINUTES", "60"))
HYBRID_MAX_STEP_PERCENT = float(os.environ.get("HYBRID_MAX_STEP_PERCENT", "10"))
HYBRID_MIN_REBALANCE_DELTA = float(os.environ.get("HYBRID_MIN_REBALANCE_DELTA", "5"))
# HYSTERESIS: ham rejim tek bir kontrolde degisse bile, PORTFOLIO MANAGER'a
# yansimasi icin AYNI ham rejimin ust uste kac rebalance kontrolu boyunca
# tekrarlanmasi gerekir (flip-flop'u ve gereksiz al-sat'i azaltir).
HYBRID_HYSTERESIS_BARS = int(os.environ.get("HYBRID_HYSTERESIS_BARS", "2"))

BIG_MOVE_THRESHOLD_PERCENT = float(os.environ.get("ENGINE_BIG_MOVE_THRESHOLD", "5"))
BIG_MOVE_WINDOW_HOURS = float(os.environ.get("ENGINE_BIG_MOVE_WINDOW_HOURS", "24"))

WARMUP_DAYS = {"3m": 2, "5m": 2, "15m": 2, "30m": 3, "1h": 6, "4h": 16, "1d": 60}


def _fetch_hybrid_series(symbol, interval, days, compute_nw=False, compute_wt=False):
    fetch_days = days + WARMUP_DAYS.get(interval, 6)
    candles = fetch_history(symbol, interval, fetch_days)
    open_times = [c[0] for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    closes = [float(c[4]) for c in candles]
    volumes = [float(c[5]) for c in candles]
    return he.HybridSeries(open_times, highs, lows, closes, volumes, INTERVAL_MS[interval],
                            compute_nw=compute_nw, compute_wt=compute_wt)


def _fetch_hybrid_all(days):
    print(f"[HYBRID] Gecmis veri cekiliyor: {SYMBOL} (3m/5m/15m/30m/1h/4h/1d) + "
          f"{', '.join(MAJOR_SYMBOLS)} (1h/4h/1d), simulasyon penceresi son {days} gun...")
    shib_series = {
        tf: _fetch_hybrid_series(SYMBOL, tf, days, compute_nw=(tf == "1h"), compute_wt=(tf == "1h"))
        for tf in TIMEFRAMES
    }
    majors_series = {}
    for sym in MAJOR_SYMBOLS:
        majors_series[sym] = {tf: _fetch_hybrid_series(sym, tf, days) for tf in ("1h", "4h", "1d")}

    base_full = shib_series[BASE_INTERVAL]
    kesim_zamani = base_full.open_times[-1] - days * 86_400_000
    kesim_idx = 0
    while kesim_idx < len(base_full.open_times) and base_full.open_times[kesim_idx] < kesim_zamani:
        kesim_idx += 1
    zamanlar = base_full.open_times[kesim_idx:]
    kapanislar = base_full.closes[kesim_idx:]
    return shib_series, majors_series, zamanlar, kapanislar


def _grid_sleeve_step(state, fiyat, tarih, trades, cost_pct):
    """grid_backtest.py'nin ana %1 salinim mantiginin BIREBIR AYNI kurallari,
    ayri bir alt-hesap (sleeve) uzerinde - rejimden BAGIMSIZ, her bar calisir."""
    satis_yapildi = False
    for lot in sorted(state["open_lots"], key=lambda l: l["entry_price"]):
        hedef = lot["entry_price"] * (1 + gb.GRID_STEP_UP_PERCENT / 100)
        stop_seviyesi = lot["entry_price"] * (1 - gb.GRID_LOT_STOP_PERCENT / 100)
        if fiyat >= hedef or fiyat <= stop_seviyesi:
            brut = lot["qty"] * fiyat
            net = brut * (1 - cost_pct / 100)
            state["usdt"] += net
            state["coin"] -= lot["qty"]
            state["open_lots"].remove(lot)
            state["reference_price"] = fiyat
            trades.append({"tip": "GRID_SLEEVE_SAT", "tarih": tarih, "fiyat": fiyat})
            satis_yapildi = True
            break
    al_tetik = fiyat <= state["reference_price"] * (1 - gb.GRID_STEP_DOWN_PERCENT / 100)
    harcanacak = state["usdt"] * (gb.GRID_ORDER_PERCENT / 100)
    rezerv = state["baslangic_deger"] * (gb.GRID_RESERVE_PERCENT / 100)
    if (not satis_yapildi and al_tetik and len(state["open_lots"]) < gb.GRID_MAX_OPEN_LOTS
            and harcanacak > 0 and (state["usdt"] - harcanacak) >= rezerv):
        maliyet = harcanacak * cost_pct / 100
        qty = (harcanacak - maliyet) / fiyat
        state["usdt"] -= harcanacak
        state["coin"] += qty
        state["open_lots"].append({"qty": qty, "entry_price": fiyat})
        state["reference_price"] = fiyat
        trades.append({"tip": "GRID_SLEEVE_AL", "tarih": tarih, "fiyat": fiyat})


def simulate(shib_series, majors_series, zamanlar, kapanislar, cost_percent=None):
    cost_pct = (TRADING_FEE_PERCENT + SLIPPAGE_PERCENT) if cost_percent is None else cost_percent
    grid_deger = BACKTEST_START_CAPITAL * (HYBRID_GRID_SLEEVE_PERCENT / 100)
    trend_deger = BACKTEST_START_CAPITAL - grid_deger

    if START_IN_SHIB:
        ilk_qty = grid_deger / kapanislar[0]
        grid_state = {"usdt": 0.0, "coin": ilk_qty,
                      "open_lots": [{"qty": ilk_qty, "entry_price": kapanislar[0]}],
                      "reference_price": kapanislar[0], "baslangic_deger": grid_deger}
        trend_usdt, trend_coin = 0.0, trend_deger / kapanislar[0]
        trend_target = 100.0
    else:
        grid_state = {"usdt": grid_deger, "coin": 0.0, "open_lots": [],
                      "reference_price": kapanislar[0], "baslangic_deger": grid_deger}
        trend_usdt, trend_coin = trend_deger, 0.0
        trend_target = 0.0

    baslangic_coin_esdeger = (grid_state["coin"] + trend_coin) + (grid_state["usdt"] + trend_usdt) / kapanislar[0]

    base_interval_ms = INTERVAL_MS[BASE_INTERVAL]
    steps_per_rebalance = max(1, round(HYBRID_REBALANCE_MINUTES * 60_000 / base_interval_ms))

    trades = []
    equity_egrisi = []
    coin_egrisi = []
    shib_pct_gecmisi = []
    regime_gecmisi = []
    prev_score = 0.0
    # HYSTERESIS durumu: ham rejim ayni kalmadan PORTFOLIO MANAGER'a
    # yansimaz - flip-flop / gereksiz al-sat azaltilir (kullanicinin acik
    # istegi). `confirmed_regime` HER ZAMAN en az HYBRID_HYSTERESIS_BARS
    # rebalance kontrolu boyunca ayni cikan bir rejimdir.
    candidate_regime, candidate_count = None, 0
    confirmed_regime, confirmed_score = "YATAY", 0.0

    for i, fiyat in enumerate(kapanislar):
        tarih = time.strftime("%Y-%m-%d %H:%M", time.localtime(zamanlar[i] / 1000))
        karar_zamani = zamanlar[i] + base_interval_ms

        _grid_sleeve_step(grid_state, fiyat, tarih, trades, cost_pct)

        if i % steps_per_rebalance == 0:
            sonuc = he.classify(shib_series, majors_series, karar_zamani, prev_score=prev_score)
            prev_score = sonuc["score"]

            if sonuc["regime"] == candidate_regime:
                candidate_count += 1
            else:
                candidate_regime, candidate_count = sonuc["regime"], 1
            if candidate_count >= HYBRID_HYSTERESIS_BARS:
                confirmed_regime, confirmed_score = candidate_regime, sonuc["score"]
            # candidate_count HENUZ esigi gecmediyse confirmed_regime bir
            # ONCEKI dogrulanmis rejimde KALIR - bu hysteresis'in ozudur.

            hedef_raw = pm.hybrid_target_allocation(confirmed_regime, abs(confirmed_score), confirmed_score, trend_target)
            if hedef_raw is not None:
                if confirmed_regime == "TOPARLANMA":
                    trend_target = hedef_raw
                else:
                    trend_target = pm.smooth_target(trend_target, hedef_raw, HYBRID_MAX_STEP_PERCENT)
            # hedef_raw None (YATAY) -> trend_target degismez, grid sleeve'e birakilir

            trend_deger_simdi = trend_usdt + trend_coin * fiyat
            simdiki_pct = (trend_coin * fiyat / trend_deger_simdi * 100) if trend_deger_simdi > 0 else 0.0
            fark = trend_target - simdiki_pct
            if abs(fark) >= HYBRID_MIN_REBALANCE_DELTA and trend_deger_simdi > 0:
                hedef_shib_deger = trend_deger_simdi * (trend_target / 100)
                simdiki_shib_deger = trend_coin * fiyat
                delta_deger = hedef_shib_deger - simdiki_shib_deger
                if delta_deger > 0:
                    harcanacak = min(delta_deger, trend_usdt)
                    if harcanacak > 0:
                        maliyet = harcanacak * cost_pct / 100
                        qty = (harcanacak - maliyet) / fiyat
                        trend_usdt -= harcanacak
                        trend_coin += qty
                        trades.append({"tip": "TREND_SLEEVE_AL", "tarih": tarih, "fiyat": fiyat, "regime": confirmed_regime})
                else:
                    satilacak_qty = min(trend_coin, -delta_deger / fiyat)
                    if satilacak_qty > 0:
                        brut = satilacak_qty * fiyat
                        net = brut * (1 - cost_pct / 100)
                        trend_usdt += net
                        trend_coin -= satilacak_qty
                        trades.append({"tip": "TREND_SLEEVE_SAT", "tarih": tarih, "fiyat": fiyat, "regime": confirmed_regime})

            regime_gecmisi.append((i, karar_zamani, confirmed_regime, confirmed_score, trend_target))

        toplam_usdt = grid_state["usdt"] + trend_usdt
        toplam_coin = grid_state["coin"] + trend_coin
        toplam_deger = toplam_usdt + toplam_coin * fiyat
        actual_pct = (toplam_coin * fiyat / toplam_deger * 100) if toplam_deger > 0 else 0.0
        equity_egrisi.append(toplam_deger)
        coin_egrisi.append(toplam_coin + toplam_usdt / fiyat)
        shib_pct_gecmisi.append(actual_pct)

    return {
        "trades": trades, "equity_egrisi": equity_egrisi, "coin_egrisi": coin_egrisi,
        "baslangic_coin": baslangic_coin_esdeger, "shib_pct_gecmisi": shib_pct_gecmisi,
        "regime_gecmisi": regime_gecmisi,
    }


def _ozet(etiket, sonuc, sonuc_brut, zamanlar, kapanislar):
    equity_egrisi = sonuc["equity_egrisi"]
    coin_egrisi = sonuc["coin_egrisi"]
    baslangic_coin = sonuc["baslangic_coin"]
    toplam_deger = equity_egrisi[-1] if equity_egrisi else BACKTEST_START_CAPITAL
    getiri = (toplam_deger - BACKTEST_START_CAPITAL) / BACKTEST_START_CAPITAL * 100
    brut_deger = sonuc_brut["equity_egrisi"][-1] if sonuc_brut["equity_egrisi"] else BACKTEST_START_CAPITAL
    brut_getiri = (brut_deger - BACKTEST_START_CAPITAL) / BACKTEST_START_CAPITAL * 100
    bitis_coin = coin_egrisi[-1] if coin_egrisi else baslangic_coin
    token_degisim = (bitis_coin - baslangic_coin) / baslangic_coin * 100 if baslangic_coin else 0
    max_dusus = teb._max_drawdown(equity_egrisi)
    metrikler = teb._trend_metrics(zamanlar, kapanislar, sonuc["shib_pct_gecmisi"], sonuc["regime_gecmisi"])

    print(f"\n--- {etiket} ---")
    print(f"Bitis degeri (NET)      : {toplam_deger:,.2f} USDT  (NET {getiri:+.2f}%, BRUT {brut_getiri:+.2f}%, "
          f"maliyet {brut_getiri - getiri:.2f} puan)")
    print(f"TOKEN ADEDI DEGISIMI    : {token_degisim:+.2f}%")
    print(f"En buyuk gerileme (DD)  : {max_dusus:.2f}%")
    print(f"Toplam islem            : {len(sonuc['trades'])}")
    yakalama = metrikler["trend_yakalama_orani"]
    print(f"Trend Yakalama Orani    : {yakalama:.1f}%" if yakalama is not None else "Trend Yakalama Orani    : (buyuk hareket yok)")
    print(f"Kacirilan Guclu Trend   : {metrikler['kacirilan_guclu_trend']} / {metrikler['buyuk_hareket_sayisi']} "
          f"(kacirilan yukselis: {metrikler['kacirilan_yukselis']}, onlenemeyen dusus: {metrikler['onlenemeyen_dusus']})")
    print(f"Dogru Rejimle Yakalanan : {metrikler['dogru_rejimle_yakalanan']} / {metrikler['buyuk_hareket_sayisi']}")
    gecikme = metrikler["tespit_gecikmesi_saat"]
    print(f"Trend Tespit Gecikmesi  : {gecikme:.1f} saat" if gecikme is not None else "Trend Tespit Gecikmesi  : (olcum yok)")
    fp = metrikler["yanlis_pozitif_orani"]
    print(f"Yanlis Pozitif Orani    : %{fp:.1f}" if fp is not None else "Yanlis Pozitif Orani    : (GUCLU_ sinyali yok)")
    gy = metrikler["ort_shib_pct_guclu_yukselis"]
    print(f"Guclu YUKSELIS ort SHIB%: %{gy:.1f}" if gy is not None else "Guclu YUKSELIS ort SHIB%: (girilmedi)")
    gd = metrikler["ort_usdt_pct_guclu_dusus"]
    print(f"Guclu DUSUS ort USDT%   : %{gd:.1f}" if gd is not None else "Guclu DUSUS ort USDT%   : (girilmedi)")
    rejim_sayilari = {}
    for _idx, _ts, regime, _score, _target in sonuc["regime_gecmisi"]:
        rejim_sayilari[regime] = rejim_sayilari.get(regime, 0) + 1
    print(f"Rejim dagilimi (kontrol): {rejim_sayilari}")

    return {
        "sistem": etiket, "net_getiri": getiri, "brut_getiri": brut_getiri,
        "token_degisim": token_degisim, "max_dusus": max_dusus, "islem": len(sonuc["trades"]),
        **metrikler,
    }


def _grid_only_ozet(days):
    print(f"[GRID] Gecmis veri cekiliyor: {SYMBOL} {gb.GRID_KLINE_INTERVAL} son {days} gun...")
    candles = gb.fetch_history(SYMBOL, gb.GRID_KLINE_INTERVAL, days)
    kapanislar = [float(c[4]) for c in candles]
    zamanlar = [c[0] for c in candles]
    trades, equity_egrisi, coin_egrisi, baslangic_coin, *_ = gb.simulate(kapanislar, zamanlar)
    toplam_deger = equity_egrisi[-1] if equity_egrisi else gb.BACKTEST_START_CAPITAL
    getiri = (toplam_deger - gb.BACKTEST_START_CAPITAL) / gb.BACKTEST_START_CAPITAL * 100
    bitis_coin = coin_egrisi[-1] if coin_egrisi else baslangic_coin
    token_degisim = (bitis_coin - baslangic_coin) / baslangic_coin * 100 if baslangic_coin else 0
    max_dusus = teb._max_drawdown(equity_egrisi)
    return {
        "sistem": "GRID (klasik, tek basina)", "net_getiri": getiri, "brut_getiri": None,
        "token_degisim": token_degisim, "max_dusus": max_dusus, "islem": len(trades),
        "trend_yakalama_orani": None, "kacirilan_guclu_trend": None, "dogru_rejimle_yakalanan": None,
        "tespit_gecikmesi_saat": None, "yanlis_pozitif_orani": None,
        "ort_shib_pct_guclu_yukselis": None, "ort_usdt_pct_guclu_dusus": None,
    }


def _detector_ozet(days, detector):
    shib_series, majors_series, zamanlar, kapanislar = teb._fetch_all(days, detector)
    sonuc = teb.simulate(shib_series, majors_series, zamanlar, kapanislar)
    toplam_deger = sonuc["equity_egrisi"][-1] if sonuc["equity_egrisi"] else BACKTEST_START_CAPITAL
    getiri = (toplam_deger - BACKTEST_START_CAPITAL) / BACKTEST_START_CAPITAL * 100
    bitis_coin = sonuc["coin_egrisi"][-1] if sonuc["coin_egrisi"] else sonuc["baslangic_coin"]
    token_degisim = (bitis_coin - sonuc["baslangic_coin"]) / sonuc["baslangic_coin"] * 100 if sonuc["baslangic_coin"] else 0
    max_dusus = teb._max_drawdown(sonuc["equity_egrisi"])
    metrikler = teb._trend_metrics(zamanlar, kapanislar, sonuc["shib_pct_gecmisi"], sonuc["regime_gecmisi"])
    return {
        "sistem": f"DEDEKTOR: {detector}", "net_getiri": getiri, "brut_getiri": None,
        "token_degisim": token_degisim, "max_dusus": max_dusus, "islem": len(sonuc["trades"]),
        **metrikler,
    }


def _master_tablo(satirlar):
    print(f"\n{'=' * 100}")
    print("MASTER KARSILASTIRMA (ayni pencere, ayni baslangic sermayesi)")
    print(f"{'Sistem':<28} {'NET%':>8} {'Token%':>8} {'DD%':>8} {'Islem':>6} {'Yakala%':>8} {'Gecikme(sa)':>11} {'YanlisPoz%':>10}")
    print("-" * 100)
    for s in satirlar:
        def fmt(v, suffix=""):
            return f"{v:.1f}{suffix}" if v is not None else "n/a"
        print(f"{s['sistem']:<28} {fmt(s['net_getiri']):>8} {fmt(s['token_degisim']):>8} {fmt(s['max_dusus']):>8} "
              f"{s['islem']:>6} {fmt(s.get('trend_yakalama_orani')):>8} {fmt(s.get('tespit_gecikmesi_saat')):>11} "
              f"{fmt(s.get('yanlis_pozitif_orani')):>10}")
    print("=" * 100)


def run_for_days(days):
    print(f"\n{'#' * 62}\nHYBRID SISTEM BACKTEST: {SYMBOL} - son {days} gun\n{'#' * 62}")
    print(f"Grid sleeve: %{HYBRID_GRID_SLEEVE_PERCENT} sermaye, kanitlanmis %1 grid (rejimden bagimsiz her bar)")
    print(f"Trend sleeve: %{100 - HYBRID_GRID_SLEEVE_PERCENT} sermaye, HYBRID motoru yonetir (YATAY'da dokunmaz)")
    print(f"Komisyon: %{TRADING_FEE_PERCENT}  Kayma (slippage): %{SLIPPAGE_PERCENT}  "
          f"Hysteresis: {HYBRID_HYSTERESIS_BARS} ardisik kontrol  Esikler: guclu>=%{he.HYBRID_ESIK_GUCLU} yon>=%{he.HYBRID_ESIK_YON}")

    satirlar = []
    satirlar.append(_grid_only_ozet(days))
    for detector in te.DETECTORS:
        satirlar.append(_detector_ozet(days, detector))

    shib_series, majors_series, zamanlar, kapanislar = _fetch_hybrid_all(days)
    sonuc = simulate(shib_series, majors_series, zamanlar, kapanislar)
    sonuc_brut = simulate(shib_series, majors_series, zamanlar, kapanislar, cost_percent=0)
    hybrid_ozet = _ozet(f"HYBRID (grid sleeve %{HYBRID_GRID_SLEEVE_PERCENT} + trend sleeve)", sonuc, sonuc_brut, zamanlar, kapanislar)
    satirlar.append(hybrid_ozet)

    _master_tablo(satirlar)

    en_iyi_token = max(satirlar, key=lambda s: s["token_degisim"])
    en_iyi_dd = min(satirlar, key=lambda s: s["max_dusus"])
    en_iyi_net = max((s for s in satirlar if s["net_getiri"] is not None), key=lambda s: s["net_getiri"])
    print(f"\n--- {days} GUN OZET SORULARI ---")
    print(f"En yuksek NET USDT getirisi : {en_iyi_net['sistem']} ({en_iyi_net['net_getiri']:+.2f}%)")
    print(f"En fazla token biriktiren   : {en_iyi_token['sistem']} ({en_iyi_token['token_degisim']:+.2f}%)")
    print(f"En dusuk drawdown           : {en_iyi_dd['sistem']} ({en_iyi_dd['max_dusus']:.2f}%)")
    yakalayanlar = [s for s in satirlar if s.get("trend_yakalama_orani") is not None]
    if yakalayanlar:
        en_iyi_yakalama = max(yakalayanlar, key=lambda s: s["trend_yakalama_orani"])
        print(f"Buyuk yukselisi en erken/iyi yakalayan: {en_iyi_yakalama['sistem']} "
              f"(yakalama %{en_iyi_yakalama['trend_yakalama_orani']:.1f})")
    print("\nONEMLI: Bu TEK bir donemin sonucu. HYBRID'in tutarli olup olmadigini, tek donemlik")
    print("sansa mi yoksa gercek bir kenar mi oldugunu anlamak icin BU KOMUTU 30/90/180 gun icin")
    print("AYRI AYRI calistirip uc sonucu birlikte karsilastirmadan KESIN karar vermeyin.")
    print("(Not: gecmis performans gelecegi garanti etmez.)")
    return satirlar


def main():
    """Kullanicinin acik istegi: 30/90/180 gunu TEK KOMUTLA, otomatik
    sirayla calistir (ayri ayri elle tetiklemeye gerek kalmasin), sonda
    HYBRID'in UC PENCEREDE TUTARLI olup olmadigini ozetleyen bir tablo
    goster - "basari tek doneme mi ait" sorusuna dogrudan cevap.
    HYBRID_BACKTEST_DAYS_LIST ile ozellestirilebilir (varsayilan 30,90,180);
    BACKTEST_DAYS gecerli DEGILDIR (kasitli - bu script her zaman ucunu
    birden kosar)."""
    gun_listesi = [int(g.strip()) for g in os.environ.get("HYBRID_BACKTEST_DAYS_LIST", "30,90,180").split(",") if g.strip()]
    tum_sonuclar = {}
    for gun in gun_listesi:
        tum_sonuclar[gun] = run_for_days(gun)

    if len(tum_sonuclar) > 1:
        print(f"\n{'#' * 62}\nHYBRID TUTARLILIK OZETI (uc pencerede ayni mi?)\n{'#' * 62}")
        print(f"{'Gun':>6} {'NET%':>8} {'Token%':>8} {'DD%':>8} {'Yakala%':>8} {'YanlisPoz%':>10}")
        hybrid_satirlari = []
        for gun, satirlar in tum_sonuclar.items():
            hybrid_row = next(s for s in satirlar if s["sistem"].startswith("HYBRID"))
            hybrid_satirlari.append((gun, hybrid_row))
            def fmt(v):
                return f"{v:.1f}" if v is not None else "n/a"
            print(f"{gun:>6} {fmt(hybrid_row['net_getiri']):>8} {fmt(hybrid_row['token_degisim']):>8} "
                  f"{fmt(hybrid_row['max_dusus']):>8} {fmt(hybrid_row.get('trend_yakalama_orani')):>8} "
                  f"{fmt(hybrid_row.get('yanlis_pozitif_orani')):>10}")
        tokenler = [r["token_degisim"] for _g, r in hybrid_satirlari]
        tumu_pozitif = all(t > 0 for t in tokenler)
        tumu_negatif = all(t < 0 for t in tokenler)
        print()
        if tumu_pozitif:
            print("HYBRID uc pencerede de token biriktirdi (tutarli pozitif sinyal).")
        elif tumu_negatif:
            print("HYBRID uc pencerede de token KAYBETTI (tutarli negatif sinyal - is birakma).")
        else:
            print("HYBRID pencereler arasinda YON DEGISTIRIYOR (bazen pozitif, bazen negatif) - bu,")
            print("sonuclarin tek bir donemin sansina bagli olabilecegini, asiri-uydurma (overfitting)")
            print("riskinin goz ardi edilmemesi gerektigini gosterir. Canliya almadan once temkinli olun.")


if __name__ == "__main__":
    main()
