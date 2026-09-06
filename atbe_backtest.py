"""
ATBE (ADAPTIVE TREND BOOSTER ENGINE) BACKTEST - orchestrator.

YON DEGISIKLIGI (kullanicinin acik talimati, ATAE'nin 180g -27.7% NET /
-44.7% DD sonucu uzerine): "ATAE mimarisini PARAMETRE AYARLAYARAK duzeltmeye
calisma... Trend motoruna portfoy uzerinde fazla yetki verilmis" kok
teshisi geregi, BU dosya BASTAN farkli bir mimari kurar:

  CORE (madde 1)   : KANITLANMIS grid_backtest.py mantiginin (hybrid_
                     backtest.py'nin ZATEN kullandigi _grid_sleeve_step
                     BIREBIR AYNI kurallari, DEGISTIRILMEDEN reuse edilir)
                     portfoyun %70-80'ini YONETIR.
  BOOSTER (madde 2): atbe_engine.py'nin 8-durumlu merdiveni SADECE booster
                     sleeve'inin (portfoyun %20-30'u) YUZDESINI yonetir -
                     trend motoru ARTIK toplam portfoy allocation'ini
                     yonetmiyor (kok teshisin DOGRUDAN duzeltmesi).

DOSYA BAGIMSIZLIGI: bu dosya hicbir v1/v2/v3/v4/GRID/ATAE dosyasini
DEGISTIRMEZ - SADECE zaten var olan, degistirilmemis fonksiyonlari CAGIRIR
(hb._grid_sleeve_step, gb sabitleri, atae_backtest._fetch_series/_fetch_all
veri-cekme/lookahead-guvenligi ZATEN dogrulanmis oldugu icin TEKRAR
yazilmadi, teb._max_drawdown/_big_move_events, v3b/atb baseline satirlari
icin DEGISTIRILMEDEN cagrilir).

METODOLOJI (madde 15, oncekiyle AYNI, tekrar dogrulandi):
  - No lookahead (AtaeSeries.index_at, causal NW - degismedi).
  - Closed candles / HTF completed candles only (index_at close_time<=t).
  - Fee+slippage HER gercek turnover'a uygulanir (cost_percent parametresi).
  - GRID/HYBRID/ATAE/ATBE AYNI baslangic sermayesi VE AYNI zaman penceresini
    kullanir (hepsi ayni _fetch_all/_fetch_hybrid_all kesim mantigindan gelir).
  - BRUT/NET AYNI karar dizisini kullanir (cost_percent SADECE miktari
    etkiler, kararlari degil - simulate_atbe iki kez, cost_percent=0/gercek,
    AYNI trace uzerinden cagrilir).
  - 30/60/90/180 AYNI parametre setiyle (gun-ozel dallanma yok).
  - Regime attribution CAUSAL (atbe_diagnostics.py'deki aciklamaya bkz,
    madde 11) - future data SADECE ikincil post-hoc capraz-kontrol icin.

Kullanim:
    python3 atbe_backtest.py                (varsayilan 30,60,90,180 gun)
    ATBE_BACKTEST_DAYS_LIST=180 python3 atbe_backtest.py
"""

import csv
import os
import statistics
import time

from backtest import fetch_history, INTERVAL_MS
from trade_bot import _load_dotenv
import grid_backtest as gb
import hybrid_backtest as hb
import hybrid_v2_backtest as v2b
import hybrid_v3_backtest as v3b
import trend_engine_backtest as teb
import atae_engine as ae
import atae_backtest as atb
import atbe_engine as abe
import atbe_allocation as aa
import atbe_coordinator as ac
import atbe_diagnostics as ad

_load_dotenv()

SYMBOL = os.environ.get("SYMBOL", "SHIBUSDT")
BACKTEST_START_CAPITAL = float(os.environ.get("BACKTEST_START_CAPITAL", "1000"))
TRADING_FEE_PERCENT = float(os.environ.get("TRADING_FEE_PERCENT", "0.1"))
SLIPPAGE_PERCENT = float(os.environ.get("SLIPPAGE_PERCENT", "0.05"))
START_IN_SHIB = os.environ.get("START_IN_SHIB", "false").lower() in ("1", "true", "evet")

BASE_INTERVAL = "15m"
REBALANCE_MINUTES = int(os.environ.get("ATBE_REBALANCE_MINUTES", "60"))

BIG_MOVE_THRESHOLD_PERCENT = float(os.environ.get("ENGINE_BIG_MOVE_THRESHOLD", "5"))
BIG_MOVE_WINDOW_HOURS = float(os.environ.get("ENGINE_BIG_MOVE_WINDOW_HOURS", "24"))

# 3 ADAY: ATBE-A=80/20, ATBE-B=75/25, ATBE-C=70/30 (madde 12) - CORE/BOOSTER
# SPLIT DISINDA hicbir parametre degismez (ayni engine/allocation kurallari).
ATBE_VARIANTS = {
    "ATBE-A (80/20)": 80.0,
    "ATBE-B (75/25)": 75.0,
    "ATBE-C (70/30)": 70.0,
}

ATBE_BACKTEST_DAYS_LIST = [int(g.strip()) for g in os.environ.get("ATBE_BACKTEST_DAYS_LIST", "30,60,90,180").split(",") if g.strip()]
OUTPUT_DIR = os.environ.get("ATBE_OUTPUT_DIR", "atbe_reports")


def simulate_atbe(shib_series, majors_series, zamanlar, kapanislar, core_pct, cost_percent=None,
                   booster_execution_enabled=True, coordinator_enabled=True):
    """CORE (grid, core_pct%) + BOOSTER (atbe_engine, 100-core_pct%) - iki
    AYRI sleeve, HICBIR ortak havuz yok (booster ASLA core'un parasina
    erisemez, core ASLA booster mantigini calistirmaz).

    booster_execution_enabled=False (PARITY/AUDIT testleri icin, atbe_parity_
    test.py'nin 'SHADOW BOOSTER' testi - madde 2): booster STATE/EVIDENCE
    HESAPLANMAYA devam eder (abe.step cagrilir, shib_series/majors_series
    GERCEKTEN okunur) ama trade EXECUTION blogu tamamen atlanir - booster
    parasi/pozisyonu HICBIR ZAMAN degismez. Bu, 'booster sinyal hesaplamasi
    core path'i etkiliyor mu' sorusunu (paylasilan mutable state/yan etki
    olmadigini) izole eder.
    coordinator_enabled=False (madde 3 testi icin): coordinator hic
    cagrilmaz - booster (varsa) her zaman should_execute'un izin verdigi
    gibi calisir, self-cross kontrolu YOK."""
    cost_pct = (TRADING_FEE_PERCENT + SLIPPAGE_PERCENT) if cost_percent is None else cost_percent
    core_deger = BACKTEST_START_CAPITAL * (core_pct / 100)
    booster_deger = BACKTEST_START_CAPITAL - core_deger
    assert core_deger >= 0 and booster_deger >= 0

    if START_IN_SHIB:
        core_qty = core_deger / kapanislar[0]
        grid_state = {"usdt": 0.0, "coin": core_qty,
                      "open_lots": [{"qty": core_qty, "entry_price": kapanislar[0]}],
                      "reference_price": kapanislar[0], "baslangic_deger": core_deger}
        booster_usdt, booster_coin, booster_target = 0.0, booster_deger / kapanislar[0], 100.0
    else:
        grid_state = {"usdt": core_deger, "coin": 0.0, "open_lots": [],
                      "reference_price": kapanislar[0], "baslangic_deger": core_deger}
        booster_usdt, booster_coin, booster_target = booster_deger, 0.0, 0.0

    baslangic_coin = (grid_state["coin"] + booster_coin) + (grid_state["usdt"] + booster_usdt) / kapanislar[0]

    base_interval_ms = INTERVAL_MS[BASE_INTERVAL]
    steps_per_rebalance = max(1, round(REBALANCE_MINUTES * 60_000 / base_interval_ms))

    booster_state = abe.yeni_booster_state()
    turnover_tracker = aa.TurnoverTracker()
    coordinator = ac.GridBoosterCoordinator()
    ticks_since_last_booster_trade = 999

    trades = []
    equity_egrisi, coin_egrisi = [], []
    core_equity_bar, booster_equity_bar = [], []
    ladder_gecmisi, htf_regime_gecmisi = [], []
    core_equity_ticks, booster_equity_ticks = [], []
    tick_gecmisi = []

    for i, fiyat in enumerate(kapanislar):
        tarih = time.strftime("%Y-%m-%d %H:%M", time.localtime(zamanlar[i] / 1000))
        karar_zamani = zamanlar[i] + base_interval_ms

        usdt_once, coin_once = grid_state["usdt"], grid_state["coin"]
        gb_trades_this_bar = []
        _grid_sleeve_step_wrapper(grid_state, fiyat, tarih, gb_trades_this_bar, cost_pct)
        for t in gb_trades_this_bar:
            # hb._grid_sleeve_step (DEGISTIRILMEDEN cagrilan) trade dict'ine
            # notional YAZMAZ (sadece tip/tarih/fiyat) - ADDITIVE (karar
            # mantigina dokunmayan) bir telemetri olarak, usdt/coin
            # bakiyesindeki DELTA'dan notional'i BURADA hesapliyoruz. AL icin
            # usdt'den dusulen (harcanacak, ucret DAHIL) miktar; SAT icin
            # satilan miktarin BRUT (ucret ONCESI) degeri - booster
            # tarafinin usdt_tutari konvansiyonuyla (AL=harcanan, SAT=brut)
            # AYNI, boylece iki sleeve karsilastirilabilir/toplanabilir.
            if t["tip"] == "GRID_SLEEVE_AL":
                notional = usdt_once - grid_state["usdt"]
                side = "AL"
            else:
                satilan_qty = coin_once - grid_state["coin"]
                notional = satilan_qty * fiyat
                side = "SAT"
            usdt_once, coin_once = grid_state["usdt"], grid_state["coin"]
            notional = max(0.0, notional)
            t["usdt_tutari"] = round(notional, 4)
            trades.append(t)
            coordinator.rapor_grid_trade(side, notional)

        if i % steps_per_rebalance == 0:
            sonuc = abe.step(booster_state, shib_series, majors_series, karar_zamani)
            ladder_gecmisi.append((len(ladder_gecmisi), karar_zamani, sonuc["ladder_state"]))
            htf_regime_gecmisi.append((len(htf_regime_gecmisi), karar_zamani, sonuc["htf_regime"]))

            raw_target = aa.booster_target_pct(sonuc["ladder_state"])
            yeni_target = aa.staged_step(booster_target, raw_target)
            is_risk_reduction = yeni_target < booster_target

            booster_deger_simdi = booster_usdt + booster_coin * fiyat
            actual_booster_pct = (booster_coin * fiyat / booster_deger_simdi * 100) if booster_deger_simdi > 0 else 0.0

            # KOK NEDEN DUZELTMESI (kullanicinin parity/audit talebi, madde 3):
            # coordinator ONCEDEN (should_execute'DAN ONCE) HER ciplak gap icin
            # cagriliyordu - deadband/ekonomik-esik/turnover-butcesi yuzunden
            # ZATEN gerceklesmeyecek olan (notional=0'a yakin/ekonomik olmayan)
            # bir "niyet" bile self-cross olarak SAYILIP booster'i gereksiz
            # yere bastiriyordu (gercek olmayan bir celiski olcuyordu). Simdi
            # ONCE should_execute ile GERCEKTEN yapilacak islem belirleniyor,
            # coordinator SADECE bu gercek/ekonomik islem icin, VE notional>0
            # ise devreye giriyor - boylece "booster trade yoksa SUPPRESSED
            # CORE TRADE = 0 olmali" VE "coordinator sadece gercek booster
            # emriyle ekonomik overlap varsa devreye girmeli" sartlari saglanir.
            execute, gap, atlama_sebebi = aa.should_execute(
                actual_booster_pct, yeni_target, ticks_since_last_booster_trade, is_risk_reduction,
                booster_deger_simdi, turnover_tracker, karar_zamani)

            if not booster_execution_enabled:
                execute, atlama_sebebi = False, "SHADOW_MODE"
            elif execute and coordinator_enabled:
                gercek_notional = abs(gap) / 100.0 * booster_deger_simdi
                if gercek_notional > 0:
                    side_istenen = "AL" if yeni_target > actual_booster_pct else "SAT"
                    coordinator_izinli = coordinator.booster_trade_izinli_mi(side_istenen, gercek_notional, karar_zamani, is_risk_reduction)
                    if not coordinator_izinli:
                        execute, atlama_sebebi = False, "COORDINATOR_SUPPRESSED"

            booster_target = yeni_target
            assert 0.0 - 1e-6 <= booster_target <= 100.0 + 1e-6

            trade_side, trade_notional, fee_tutari = None, 0.0, 0.0
            if execute:
                hedef_deger = booster_deger_simdi * (booster_target / 100)
                delta_deger = hedef_deger - booster_coin * fiyat
                if delta_deger > 0:
                    harcanacak = min(delta_deger, booster_usdt)
                    if harcanacak > 0:
                        maliyet = harcanacak * cost_pct / 100
                        booster_usdt -= harcanacak
                        booster_coin += (harcanacak - maliyet) / fiyat
                        trades.append({"tip": "BOOSTER_AL", "tarih": tarih, "ts_ms": karar_zamani, "fiyat": fiyat,
                                       "usdt_tutari": harcanacak, "fee": maliyet, "ladder_state": sonuc["ladder_state"]})
                        trade_side, trade_notional, fee_tutari = "AL", harcanacak, maliyet
                        turnover_tracker.add(karar_zamani, harcanacak)
                        ticks_since_last_booster_trade = 0
                else:
                    satilacak = min(booster_coin, -delta_deger / fiyat)
                    if satilacak > 0:
                        brut = satilacak * fiyat
                        net = brut * (1 - cost_pct / 100)
                        booster_usdt += net
                        booster_coin -= satilacak
                        trades.append({"tip": "BOOSTER_SAT", "tarih": tarih, "ts_ms": karar_zamani, "fiyat": fiyat,
                                       "usdt_tutari": brut, "fee": brut - net, "ladder_state": sonuc["ladder_state"]})
                        trade_side, trade_notional, fee_tutari = "SAT", brut, brut - net
                        turnover_tracker.add(karar_zamani, brut)
                        ticks_since_last_booster_trade = 0
            if not execute:
                ticks_since_last_booster_trade += 1
            assert fee_tutari >= 0 and trade_notional >= 0

            coordinator.tick_sifirla()

            core_deger_simdi = grid_state["usdt"] + grid_state["coin"] * fiyat
            core_equity_ticks.append(core_deger_simdi)
            booster_equity_ticks.append(booster_usdt + booster_coin * fiyat)

            tick_gecmisi.append({
                "timestamp": tarih, "price": fiyat, "ladder_state": sonuc["ladder_state"],
                "bull_evidence": round(sonuc["bull_evidence"], 1), "bear_evidence": round(sonuc["bear_evidence"], 1),
                "htf_regime": sonuc["htf_regime"], "bullish_allowed": sonuc["bullish_allowed"],
                "target_booster_pct": round(booster_target, 2), "actual_booster_pct": round(actual_booster_pct, 2),
                "trade_side": trade_side, "trade_notional": round(trade_notional, 2), "fee": round(fee_tutari, 4),
                "skip_reason": atlama_sebebi, "state_change_reason": sonuc["reason"] if sonuc["changed"] else None,
            })

        toplam_usdt = grid_state["usdt"] + booster_usdt
        toplam_coin = grid_state["coin"] + booster_coin
        toplam_deger = toplam_usdt + toplam_coin * fiyat
        assert toplam_deger >= -1e-6
        core_bar = grid_state["usdt"] + grid_state["coin"] * fiyat
        booster_bar = booster_usdt + booster_coin * fiyat
        # SLEEVE ACCOUNTING IDENTITY (madde 4/11): toplam deger, INSA GEREGI
        # (toplam_usdt/toplam_coin literal TOPLAMLAR oldugu icin) core+booster'a
        # TAM esit olmak ZORUNDA - ayni SHIB stogunun iki sleeve tarafindan
        # cift sayilmasi/karismasi MUMKUN DEGIL (iki ayri degisken, hicbir
        # ortak havuz yok). Float rounding disinda sapma OLURSA gercek bir
        # accounting hatasidir.
        assert abs(toplam_deger - (core_bar + booster_bar)) < 1e-6
        equity_egrisi.append(toplam_deger)
        coin_egrisi.append(toplam_coin + toplam_usdt / fiyat)
        core_equity_bar.append(core_bar)
        booster_equity_bar.append(booster_bar)

    return {
        "trades": trades, "equity_egrisi": equity_egrisi, "coin_egrisi": coin_egrisi,
        "baslangic_coin": baslangic_coin, "core_equity_bar": core_equity_bar, "booster_equity_bar": booster_equity_bar,
        "ladder_gecmisi": ladder_gecmisi, "htf_regime_gecmisi": htf_regime_gecmisi,
        "core_equity_ticks": core_equity_ticks, "booster_equity_ticks": booster_equity_ticks,
        "tick_gecmisi": tick_gecmisi, "coordinator": coordinator,
        "core_deger_baslangic": core_deger, "booster_deger_baslangic": booster_deger,
    }


def _grid_sleeve_step_wrapper(state, fiyat, tarih, trades, cost_pct):
    """hb._grid_sleeve_step'in (grid_backtest.py'nin %1 salinim kurallarinin
    BIREBIR AYNISI) DEGISTIRILMEDEN cagrilmasi - ATBE CORE'un tanimi geregi
    (madde 1: 'kanitlanmis GRID sistemi ana strateji olsun') bu fonksiyonun
    KENDI bir kopyasi YAZILMADI, DOGRUDAN reuse edildi."""
    hb._grid_sleeve_step(state, fiyat, tarih, trades, cost_pct)


def _ozet_atbe(etiket, sonuc, sonuc_brut):
    equity = sonuc["equity_egrisi"]
    toplam_deger = equity[-1] if equity else BACKTEST_START_CAPITAL
    net_getiri = (toplam_deger - BACKTEST_START_CAPITAL) / BACKTEST_START_CAPITAL * 100
    brut_deger = sonuc_brut["equity_egrisi"][-1] if sonuc_brut["equity_egrisi"] else BACKTEST_START_CAPITAL
    brut_getiri = (brut_deger - BACKTEST_START_CAPITAL) / BACKTEST_START_CAPITAL * 100
    bitis_coin = sonuc["coin_egrisi"][-1] if sonuc["coin_egrisi"] else sonuc["baslangic_coin"]
    token_degisim = (bitis_coin - sonuc["baslangic_coin"]) / sonuc["baslangic_coin"] * 100 if sonuc["baslangic_coin"] else 0
    max_dusus = teb._max_drawdown(equity)
    toplam_maliyet = sum(t.get("fee", t.get("usdt_tutari", 0.0) * (TRADING_FEE_PERCENT + SLIPPAGE_PERCENT) / 100) for t in sonuc["trades"])
    turnover = sum(t.get("usdt_tutari", 0.0) for t in sonuc["trades"])
    booster_trades = [t for t in sonuc["trades"] if t["tip"].startswith("BOOSTER_")]
    core_trades = [t for t in sonuc["trades"] if t["tip"].startswith("GRID_SLEEVE_")]
    booster_turnover = sum(t.get("usdt_tutari", 0.0) for t in booster_trades)

    print(f"\n--- {etiket} ---")
    print(f"Bitis degeri (NET) : {toplam_deger:,.2f} USDT (NET {net_getiri:+.2f}%, BRUT {brut_getiri:+.2f}%, "
          f"maliyet {brut_getiri - net_getiri:.2f} puan)")
    print(f"TOKEN DEGISIMI     : {token_degisim:+.2f}%   Max DD: {max_dusus:.2f}%")
    print(f"Islem: {len(sonuc['trades'])} (core={len(core_trades)}, booster={len(booster_trades)})   "
          f"Toplam maliyet(USDT): {toplam_maliyet:.2f}   Turnover(USDT): {turnover:.2f} (booster: {booster_turnover:.2f})")
    cross = sonuc["coordinator"].ozet()
    print(f"Self-cross (coordinator): count={cross['self_cross_count']} suppressed_overlap={cross['self_cross_overlap_usdt']:.2f} USDT")

    return {
        "sistem": etiket, "net_getiri": net_getiri, "brut_getiri": brut_getiri, "maliyet_puan": brut_getiri - net_getiri,
        "token_degisim": token_degisim, "max_dusus": max_dusus, "islem": len(sonuc["trades"]),
        "toplam_maliyet_usdt": toplam_maliyet, "turnover_usdt": turnover, "booster_turnover_usdt": booster_turnover,
        "self_cross_count": cross["self_cross_count"],
    }


def run_for_days(days, tum_fp_satirlari, tum_regime_satirlari):
    print(f"\n{'#' * 78}\nATBE BACKTEST: {SYMBOL} - son {days} gun\n{'#' * 78}")

    grid_row = hb._grid_only_ozet(days)
    grid_row["maliyet_puan"] = None

    shib_series, majors_series, zamanlar, kapanislar = atb._fetch_all(days)

    bh_coin = BACKTEST_START_CAPITAL / kapanislar[0]
    bh_deger = bh_coin * kapanislar[-1]
    bh_row = {"sistem": "BUY_AND_HOLD", "net_getiri": (bh_deger - BACKTEST_START_CAPITAL) / BACKTEST_START_CAPITAL * 100,
              "brut_getiri": None, "maliyet_puan": 0.0, "token_degisim": 0.0,
              "max_dusus": teb._max_drawdown([bh_coin * f for f in kapanislar]), "islem": 0,
              "toplam_maliyet_usdt": 0.0, "turnover_usdt": 0.0, "booster_turnover_usdt": None, "self_cross_count": None}

    hyb_shib, hyb_majors, hyb_zamanlar, hyb_kapanislar = hb._fetch_hybrid_all(days)
    v1_sonuc = hb.simulate(hyb_shib, hyb_majors, hyb_zamanlar, hyb_kapanislar)
    v1_sonuc_brut = hb.simulate(hyb_shib, hyb_majors, hyb_zamanlar, hyb_kapanislar, cost_percent=0)
    v1_row = hb._ozet("HYBRID v1 (baseline)", v1_sonuc, v1_sonuc_brut, hyb_zamanlar, hyb_kapanislar)
    v1_row["maliyet_puan"] = v1_row["brut_getiri"] - v1_row["net_getiri"]
    for k in ("toplam_maliyet_usdt", "turnover_usdt", "booster_turnover_usdt", "self_cross_count"):
        v1_row.setdefault(k, None)

    early_series = v2b._fetch_early_series(days)
    v3c_sonuc = v3b.simulate_v3(hyb_shib, hyb_majors, early_series, hyb_zamanlar, hyb_kapanislar,
                                 v1_sonuc["hysteresis_gecmisi"], use_persistence=True, use_asymmetric_exit=True)
    v3c_sonuc_brut = v3b.simulate_v3(hyb_shib, hyb_majors, early_series, hyb_zamanlar, hyb_kapanislar,
                                      v1_sonuc["hysteresis_gecmisi"], use_persistence=True, use_asymmetric_exit=True, cost_percent=0)
    v3c_row = hb._ozet("HYBRID v3-C (baseline)", v3c_sonuc, v3c_sonuc_brut, hyb_zamanlar, hyb_kapanislar)
    v3c_row["maliyet_puan"] = v3c_row["brut_getiri"] - v3c_row["net_getiri"]
    for k in ("toplam_maliyet_usdt", "turnover_usdt", "booster_turnover_usdt", "self_cross_count"):
        v3c_row.setdefault(k, None)

    atae_c_params = atb.ATAE_VARIANTS["ATAE-C (+asymmetric exit+Recovery/Distribution)"]
    atae_c_sonuc = atb.simulate_atae(shib_series, majors_series, zamanlar, kapanislar, atb.ATAE_PROFILE, **atae_c_params)
    atae_c_sonuc_brut = atb.simulate_atae(shib_series, majors_series, zamanlar, kapanislar, atb.ATAE_PROFILE, cost_percent=0, **atae_c_params)
    atae_c_row = atb._ozet_atae("ATAE-C (baseline)", atae_c_sonuc, atae_c_sonuc_brut, zamanlar, kapanislar, [])
    for k in ("toplam_maliyet_usdt", "self_cross_count"):
        atae_c_row.setdefault(k, None)
    atae_c_row["booster_turnover_usdt"] = atae_c_row.get("turnover_usdt")

    satirlar = [grid_row, bh_row, v1_row, v3c_row, atae_c_row]

    for isim, core_pct in ATBE_VARIANTS.items():
        sonuc = simulate_atbe(shib_series, majors_series, zamanlar, kapanislar, core_pct)
        sonuc_brut = simulate_atbe(shib_series, majors_series, zamanlar, kapanislar, core_pct, cost_percent=0)
        row = _ozet_atbe(isim, sonuc, sonuc_brut)
        satirlar.append(row)

        episodes = ad.build_booster_episodes(sonuc["ladder_gecmisi"], sonuc["booster_equity_ticks"], zamanlar, sonuc["trades"])
        fp = ad.false_positive_damage(episodes)
        print(f"FP_COUNT={fp['fp_count']} FP_COST={fp['fp_cost']:.2f} FP_TURNOVER={fp['fp_turnover']:.2f} "
              f"FP_REALIZED_PNL={fp['fp_realized_pnl']:.2f} FP_MAX_ADVERSE_EXCURSION={fp['fp_max_adverse_excursion']:.2f}")
        tum_fp_satirlari.append({"window_days": days, "system": isim, **fp})

        rejim_dagilim = ad.regime_attribution(zamanlar, sonuc["htf_regime_gecmisi"], sonuc["core_equity_ticks"], sonuc["booster_equity_ticks"])
        for r in rejim_dagilim:
            print(f"  REGIME {r['regime']:<9} tick={r['tick_count']:<5} CORE_PNL={r['core_pnl_usdt']:>9.2f} "
                  f"BOOSTER_PNL={r['booster_pnl_usdt']:>9.2f} TOTAL_PNL={r['total_pnl_usdt']:>9.2f}")
            tum_regime_satirlari.append({"window_days": days, "system": isim, **r})

    _master_tablo(satirlar)

    print(f"\n--- {days} GUN WALK-FORWARD (erken/orta/son 1/3, PARAMETRE DEGISMEDI) ---")
    for isim, core_pct in ATBE_VARIANTS.items():
        wf_sonuc = simulate_atbe(shib_series, majors_series, zamanlar, kapanislar, core_pct)
        parcalar = ad.walk_forward_split(zamanlar, wf_sonuc["equity_egrisi"], n_parca=3)
        parca_str = "  ".join(f"P{p['parca']}:{p['net_getiri']:+.1f}%" for p in parcalar)
        print(f"  {isim:<20} {parca_str}")

    return satirlar


def _master_tablo(satirlar):
    genislik = 150
    print(f"\n{'=' * genislik}\nMASTER KARSILASTIRMA (ayni pencere, ayni baslangic sermayesi)\n{'=' * genislik}")
    print(f"{'Sistem':<28} {'NET%':>8} {'BRUT%':>8} {'maliyet.p':>10} {'Token%':>8} {'DD%':>8} {'Islem':>6} {'Turnover':>10} {'SelfCross':>10}")
    print("-" * genislik)
    for s in satirlar:
        def f(v):
            return f"{v:.2f}" if v is not None else "n/a"
        def fi(v):
            return f"{v}" if v is not None else "n/a"
        print(f"{s['sistem']:<28} {f(s['net_getiri']):>8} {f(s['brut_getiri']):>8} {f(s.get('maliyet_puan')):>10} "
              f"{f(s['token_degisim']):>8} {f(s['max_dusus']):>8} {s['islem']:>6} {f(s.get('turnover_usdt')):>10} {fi(s.get('self_cross_count')):>10}")
    print("=" * genislik)


def _acceptance_karar(sonuclar_180):
    """madde 13/18 - ACCEPTANCE kriterlerini AYNEN uygular, 'kararsiz'
    birakmaz. sonuclar_180: {sistem_adi: row} sozlugu (180 gunluk)."""
    print(f"\n{'#' * 78}\nACCEPTANCE / KARAR (madde 13, 18)\n{'#' * 78}")
    grid = sonuclar_180.get("GRID")
    atae_c = sonuclar_180.get("ATAE-C (baseline)")
    en_iyi_isim, en_iyi = None, None
    for isim in ATBE_VARIANTS:
        row = sonuclar_180.get(isim)
        if row is None:
            continue
        if en_iyi is None or (row["net_getiri"] or -1e9) > (en_iyi["net_getiri"] or -1e9):
            en_iyi_isim, en_iyi = isim, row

    if en_iyi is None or grid is None:
        print("YETERSIZ VERI - 180 gunluk sonuc bulunamadi, karar VERILEMEZ (bu bir 'basarili' sayilmaz).")
        return

    net_pozitif = en_iyi["net_getiri"] > 0
    grid_gecti = en_iyi["net_getiri"] > grid["net_getiri"]
    dd_kabul = en_iyi["max_dusus"] > -15.0
    dd_eski_atae_gibi_degil = en_iyi["max_dusus"] > -35.0
    turnover_dusuk = (atae_c is not None and atae_c.get("turnover_usdt") and en_iyi.get("turnover_usdt") is not None
                       and en_iyi["turnover_usdt"] <= atae_c["turnover_usdt"] * 0.5)

    print(f"En iyi ATBE varyanti (180g NET'e gore): {en_iyi_isim}")
    print(f"  180g NET > 0                         : {'EVET' if net_pozitif else 'HAYIR'} ({en_iyi['net_getiri']:+.2f}%)")
    print(f"  180g NET > GRID NET                  : {'EVET' if grid_gecti else 'HAYIR'} (GRID {grid['net_getiri']:+.2f}%)")
    print(f"  180g DD < %15 (ideal)                : {'EVET' if dd_kabul else 'HAYIR'} ({en_iyi['max_dusus']:.2f}%)")
    print(f"  180g DD eski ATAE bandina (~-40%) YAKLASMADI: {'EVET' if dd_eski_atae_gibi_degil else 'HAYIR'}")
    if atae_c is not None and atae_c.get("turnover_usdt"):
        print(f"  Turnover, ATAE-C'den >=%50 dusuk     : {'EVET' if turnover_dusuk else 'HAYIR'} "
              f"(ATBE {en_iyi.get('turnover_usdt')} vs ATAE-C {atae_c.get('turnover_usdt')})")

    if net_pozitif and grid_gecti and dd_eski_atae_gibi_degil:
        print("\nKARAR: HYBRID/ATBE YENI SURUM ADAY OLABILIR (madde 13 asgari sartlari saglaniyor).")
        print("Live-ready entegrasyon kodu (varsayilan KAPALI, gercek emir GONDERMEZ) hazirlanacak.")
    else:
        print("\nKARAR: GRID KALSIN. ATBE bu 180 gunluk pencerede GRID'i gecmedi veya risk kriterlerini saglamadi.")
        print("Bu bir basarisiz arastirma sonucu olarak raporlanmistir - threshold makyaji yapilmamistir.")


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    tum_sonuclar = {}
    tum_fp_satirlari, tum_regime_satirlari = [], []
    for gun in ATBE_BACKTEST_DAYS_LIST:
        satirlar = run_for_days(gun, tum_fp_satirlari, tum_regime_satirlari)
        tum_sonuclar[gun] = {s["sistem"]: s for s in satirlar}

    for isim, satirlar_list in (("false_positive_damage.csv", tum_fp_satirlari), ("regime_attribution.csv", tum_regime_satirlari)):
        if satirlar_list:
            yol = os.path.join(OUTPUT_DIR, isim)
            with open(yol, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(satirlar_list[0].keys()))
                writer.writeheader()
                writer.writerows(satirlar_list)
            print(f"[ATBE] Yazildi: {yol} ({len(satirlar_list)} satir)")

    if len(tum_sonuclar) > 1:
        print(f"\n{'#' * 78}\nCAPRAZ-PENCERE NET GETIRI TABLOSU\n{'#' * 78}")
        sistemler = ["GRID", "BUY_AND_HOLD", "HYBRID v1 (baseline)", "HYBRID v3-C (baseline)", "ATAE-C (baseline)"] + list(ATBE_VARIANTS.keys())
        baslik = f"{'SISTEM':<28}" + "".join(f"{str(g) + 'g NET%':>12}" for g in ATBE_BACKTEST_DAYS_LIST)
        print(baslik)
        print("-" * len(baslik))
        for isim in sistemler:
            satir = f"{isim:<28}"
            for gun in ATBE_BACKTEST_DAYS_LIST:
                v = tum_sonuclar[gun].get(isim, {}).get("net_getiri")
                satir += f"{(f'{v:+.1f}' if v is not None else 'n/a'):>12}"
            print(satir)

    if 180 in tum_sonuclar:
        _acceptance_karar(tum_sonuclar[180])
    else:
        print("\n180 gunluk pencere calistirilmadi - ACCEPTANCE karari verilemez (madde 18 icin 180g ZORUNLU).")


if __name__ == "__main__":
    main()
