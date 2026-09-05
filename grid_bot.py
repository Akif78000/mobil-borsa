"""
SHIB Grid (Salinim) Botu - Token Biriktirme Stratejisi

trade_bot.py'daki RSI/EMA/trend takip eden bottan FARKLI bir mantik:
o bot "trend yukariyken al" der, bu bot trend'e hic bakmaz, sadece fiyatin
son islem noktasindan ne kadar hareket ettigine bakar:

  - Fiyat, son ALIM (veya baslangic) fiyatindan GRID_STEP_DOWN_PERCENT kadar
    dusunce -> yeni bir ALIM lotu acilir, referans fiyat bu yeni (dusuk)
    seviyeye guncellenir (bir sonraki alim daha da asagida tetiklenir).
  - Her acik lot, KENDI giris fiyatindan GRID_STEP_UP_PERCENT kadar
    yukselince ayri ayri SATILIR.

Amac: dolar degerini degil, SHIB TOKEN ADEDINI zamanla artirmak (dusukten
al, yuksekten sat dongusu). Trend takip eden bottan farkli olarak, yatay/
dalgali piyasada daha sik islem yapar; guclu tek yonlu trendlerde (surekli
dusus) GRID_MAX_OPEN_LOTS ve GRID_RESERVE_PERCENT ile korunur ama yine de
kayip riski vardir - "garanti kar" degildir.

GUVENLIK: trade_bot.py ile AYNI MODE sistemini (dry_run/testnet/live),
ayni EMERGENCY_STOP_FILE'i, ayni gunluk zarar limitini ve ayni Binance
siparis/LOT_SIZE altyapisini kullanir (import ederek, tekrar yazmadan).

Botu siz kendi bilgisayarinizda/sunucunuzda kendi API anahtarlarinizla
calistirirsiniz.
"""

import json
import os
import time

from trade_bot import (
    MODE,
    SYMBOL,
    BASE_ASSET,
    QUOTE_ASSET,
    BINANCE_API_KEY,
    BINANCE_API_SECRET,
    EMERGENCY_STOP_FILE,
    MAINNET_BASE,
    _load_dotenv,
    _http,
    get_balance,
    place_order,
    notify,
)

_load_dotenv()

GRID_STEP_DOWN_PERCENT = float(os.environ.get("GRID_STEP_DOWN_PERCENT", "1"))
GRID_STEP_UP_PERCENT = float(os.environ.get("GRID_STEP_UP_PERCENT", "1"))
GRID_ORDER_PERCENT = float(os.environ.get("GRID_ORDER_PERCENT", "10"))
GRID_MAX_OPEN_LOTS = int(os.environ.get("GRID_MAX_OPEN_LOTS", "8"))
GRID_RESERVE_PERCENT = float(os.environ.get("GRID_RESERVE_PERCENT", "20"))
# Bir lot hic hedefine ulasmadan fiyat bu kadar duserse, sonsuza kadar acik
# kalmasin diye zararina kapatilir (once "kayip sermaye kilitlenmesin" riskine
# karsi - grid'in kendi basina hicbir zaman sahip olmadigi tek koruma).
GRID_LOT_STOP_PERCENT = float(os.environ.get("GRID_LOT_STOP_PERCENT", "10"))
GRID_POLL_INTERVAL_SECONDS = int(os.environ.get("GRID_POLL_INTERVAL_SECONDS", "30"))
MAX_DAILY_LOSS_PERCENT = float(os.environ.get("MAX_DAILY_LOSS_PERCENT", "2"))
GRID_STATE_FILE = os.environ.get("GRID_STATE_FILE", "grid_bot_state.json")
CONFIRM_REAL_MONEY = os.environ.get("CONFIRM_REAL_MONEY", "")
# Botu USDT'siz, elde zaten SHIB varken baslatmak icin: ilk calismada mevcut
# BASE_ASSET bakiyesinin bu yuzdesi tek seferlik "baslangic lotu" olarak grid
# havuzuna kaydedilir (fiyat yukselince satilir, dusunce geri alinir). Geri
# kalan yuzde bota hic tanitilmaz, asla satilmaz. 0 = kapali (varsayilan,
# eski davranis - bot sadece USDT ile alim yaparak baslar).
GRID_SEED_SHIB_PERCENT = float(os.environ.get("GRID_SEED_SHIB_PERCENT", "0"))
# Grid'in %1 adimina DOKUNMADAN, sadece YENI ALIM sinyaline saatlik/gunluk
# trend filtresi eklemek icin: son 1 saatlik veya son 24 saatlik degisim bu
# esiklerin ALTINDAYSA (guclu dususteyse), o dongude yeni alim atlanir -
# "dusen bicagi yakalama" riskini azaltir. SATIS (kar hedefi/stop-loss) bu
# filtreden HIC etkilenmez, her zaman aynen calisir. -100 = kapali
# (varsayilan, eski davranis, hicbir zaman engellemez).
GRID_TREND_FILTER_1H_PERCENT = float(os.environ.get("GRID_TREND_FILTER_1H_PERCENT", "-100"))
GRID_TREND_FILTER_24H_PERCENT = float(os.environ.get("GRID_TREND_FILTER_24H_PERCENT", "-100"))
GRID_TREND_CHECK_INTERVAL_SECONDS = int(os.environ.get("GRID_TREND_CHECK_INTERVAL_SECONDS", "300"))
GRID_TREND_FILTER_ENABLED = GRID_TREND_FILTER_1H_PERCENT > -100 or GRID_TREND_FILTER_24H_PERCENT > -100

# "Gunluk/saatlik trendi takip edip o payla islem yap" ozelligi: seed edilen
# SHIB havuzunun (GRID_SEED_SHIB_PERCENT) bu yuzdesi, grid'den TAMAMEN AYRI,
# kendi ic sayaçlarini (qty_shib/qty_usdt) tutan bagimsiz bir alt-havuza
# ayrilir. Bu alt-havuz HESAP TOPLAM bakiyesini degil sadece kendi ic
# sayaçlarini kullanir - ayni surec icinde calistigi icin grid ile
# birbirlerinin parasina asla karismazlar (iki ayri bot calistirmanin
# getirecegi bakiye çakışması riski yok). Basit trend mantigi: hem son 1
# saatlik hem son 24 saatlik degisim ESIGIN USTUNDEYSE al (long), ikisinden
# biri ESIGIN ALTINA duserse sat (flat). 0 = kapali (varsayilan).
GRID_TREND_ALLOCATION_PERCENT = float(os.environ.get("GRID_TREND_ALLOCATION_PERCENT", "0"))
# %0.3/-0.3 varsayilaniyla gercek veride test edildiginde 5dk gurultusune bile
# tepki verip gunde 5-10 kez alim-satim yaparak komisyonla kendini eritti
# (180 gunde alt-havuzun %92'si komisyona gitti). Minimum %1 fark sarti
# konarak asiri islem onlenir - grid'in kendi %1 adimiyla da tutarli.
GRID_TREND_ENTRY_PERCENT = float(os.environ.get("GRID_TREND_ENTRY_PERCENT", "1"))
GRID_TREND_EXIT_PERCENT = float(os.environ.get("GRID_TREND_EXIT_PERCENT", "-1"))
GRID_TREND_POOL_ENABLED = GRID_TREND_ALLOCATION_PERCENT > 0

_trend_cache = {"checked_at": 0.0, "change_1h": 0.0, "change_24h": 0.0}


def get_price():
    data = _http("GET", "/api/v3/ticker/price", {"symbol": SYMBOL}, base=MAINNET_BASE)
    return float(data["price"])


def get_trend_context():
    now = time.time()
    if now - _trend_cache["checked_at"] < GRID_TREND_CHECK_INTERVAL_SECONDS:
        return _trend_cache
    try:
        gunluk = _http("GET", "/api/v3/ticker/24hr", {"symbol": SYMBOL}, base=MAINNET_BASE)
        change_24h = float(gunluk["priceChangePercent"])
        saatlik = _http(
            "GET", "/api/v3/klines",
            {"symbol": SYMBOL, "interval": "1h", "limit": 2},
            base=MAINNET_BASE,
        )
        change_1h = (
            (float(saatlik[-1][4]) / float(saatlik[-2][4]) - 1) * 100
            if len(saatlik) >= 2 else 0.0
        )
        _trend_cache.update(checked_at=now, change_1h=change_1h, change_24h=change_24h)
    except Exception as e:
        notify(f"⚠️ Trend filtresi kontrolu basarisiz, bu turda filtre devre disi: {e}")
        _trend_cache["checked_at"] = now
    return _trend_cache


def load_state():
    if os.path.exists(GRID_STATE_FILE):
        with open(GRID_STATE_FILE) as f:
            return json.load(f)
    return {
        "last_reference_price": None,
        "open_lots": [],
        "day": time.strftime("%Y-%m-%d"),
        "daily_realized_pnl": 0.0,
        "day_start_equity": None,
        "initial_equity": None,
        "trend_pool": None,
    }


def save_state(state):
    with open(GRID_STATE_FILE, "w") as f:
        json.dump(state, f)


def normalize_state(state):
    defaults = {
        "last_reference_price": None,
        "open_lots": [],
        "day": time.strftime("%Y-%m-%d"),
        "daily_realized_pnl": 0.0,
        "day_start_equity": None,
        "initial_equity": None,
        "trend_pool": None,
    }
    for key, value in defaults.items():
        state.setdefault(key, value)
    today = time.strftime("%Y-%m-%d")
    if state["day"] != today:
        state.update(day=today, daily_realized_pnl=0.0, day_start_equity=None)
    return state


def check_mode_guard():
    if MODE not in ("dry_run", "testnet", "live"):
        raise SystemExit(f"Gecersiz MODE: {MODE} (dry_run | testnet | live)")
    if MODE == "live":
        if CONFIRM_REAL_MONEY != "EVET":
            raise SystemExit(
                "MODE=live secildi ama CONFIRM_REAL_MONEY=EVET verilmedi. "
                "Gercek para riskini bilerek onaylamadan bot baslamaz."
            )
        if not (BINANCE_API_KEY and BINANCE_API_SECRET):
            raise SystemExit("MODE=live icin BINANCE_API_KEY / BINANCE_API_SECRET gerekli.")
    if MODE == "testnet" and not (BINANCE_API_KEY and BINANCE_API_SECRET):
        raise SystemExit("MODE=testnet icin testnet API anahtarlari gerekli.")
    if not 0 < GRID_ORDER_PERCENT <= 33:
        raise SystemExit("GRID_ORDER_PERCENT guvenlik nedeniyle 0-33 araliginda olmali.")
    if GRID_MAX_OPEN_LOTS < 1:
        raise SystemExit("GRID_MAX_OPEN_LOTS en az 1 olmali.")
    if not 0 <= GRID_RESERVE_PERCENT < 100:
        raise SystemExit("GRID_RESERVE_PERCENT 0-100 araliginda olmali.")
    if not 0 <= GRID_SEED_SHIB_PERCENT <= 100:
        raise SystemExit("GRID_SEED_SHIB_PERCENT 0-100 araliginda olmali.")
    if GRID_TREND_CHECK_INTERVAL_SECONDS < 1:
        raise SystemExit("GRID_TREND_CHECK_INTERVAL_SECONDS en az 1 olmali.")
    if not 0 <= GRID_TREND_ALLOCATION_PERCENT <= 100:
        raise SystemExit("GRID_TREND_ALLOCATION_PERCENT 0-100 araliginda olmali.")
    if GRID_TREND_ALLOCATION_PERCENT > 0 and GRID_SEED_SHIB_PERCENT <= 0:
        raise SystemExit(
            "GRID_TREND_ALLOCATION_PERCENT kullanmak icin once GRID_SEED_SHIB_PERCENT > 0 olmali "
            "(trend alt-havuzu, seed edilen SHIB payindan ayrilir)."
        )


def run_trend_pool(state, fiyat, zaman, trend):
    """Grid'den bagimsiz, kendi ic sayaçlariyla (qty_shib/qty_usdt) calisan
    trend takip alt-havuzu. Asla get_balance() ile hesap TOPLAM bakiyesini
    okumaz - sadece kendi ic durumunu kullanir, boylece grid'in parasina
    hic karismaz."""
    pool = state["trend_pool"]
    if pool["position"] == "long":
        exit_tetik = (
            trend["change_1h"] <= GRID_TREND_EXIT_PERCENT
            or trend["change_24h"] <= GRID_TREND_EXIT_PERCENT
        )
        if not exit_tetik or pool["qty_shib"] <= 0:
            return
        if MODE == "dry_run":
            notify(
                f"📉 [SIMULE] {zaman} - Trend havuzu SATIS (1sa={trend['change_1h']:+.2f}% "
                f"24sa={trend['change_24h']:+.2f}%): {pool['qty_shib']:,.0f} {BASE_ASSET} -> USDT (simule)"
            )
            pool["qty_usdt"] = pool["qty_shib"] * fiyat
        else:
            order = place_order("SELL", pool["qty_shib"], use_quote_qty=False)
            alinan = float(order.get("cummulativeQuoteQty", pool["qty_shib"] * fiyat))
            notify(
                f"📉 [{MODE.upper()}] Trend havuzu SATIS (1sa={trend['change_1h']:+.2f}% "
                f"24sa={trend['change_24h']:+.2f}%): {order}"
            )
            pool["qty_usdt"] = alinan
        pool["qty_shib"] = 0.0
        pool["position"] = "flat"
    else:
        entry_tetik = (
            trend["change_1h"] >= GRID_TREND_ENTRY_PERCENT
            and trend["change_24h"] >= GRID_TREND_ENTRY_PERCENT
        )
        if not entry_tetik or pool["qty_usdt"] <= 0:
            return
        if MODE == "dry_run":
            notify(
                f"📈 [SIMULE] {zaman} - Trend havuzu ALIM (1sa={trend['change_1h']:+.2f}% "
                f"24sa={trend['change_24h']:+.2f}%): {pool['qty_usdt']:,.2f} {QUOTE_ASSET} -> {BASE_ASSET} (simule)"
            )
            pool["qty_shib"] = pool["qty_usdt"] / fiyat
        else:
            order = place_order("BUY", pool["qty_usdt"], use_quote_qty=True)
            qty = float(order.get("executedQty", 0))
            notify(
                f"📈 [{MODE.upper()}] Trend havuzu ALIM (1sa={trend['change_1h']:+.2f}% "
                f"24sa={trend['change_24h']:+.2f}%): {order}"
            )
            pool["qty_shib"] = qty
        pool["qty_usdt"] = 0.0
        pool["entry_price"] = fiyat
        pool["position"] = "long"


def run_once(state):
    if os.path.exists(EMERGENCY_STOP_FILE):
        raise SystemExit(f"Acil durdurma dosyasi bulundu: {EMERGENCY_STOP_FILE}")

    fiyat = get_price()
    zaman = time.strftime("%d/%m/%Y %H:%M:%S")

    if state["last_reference_price"] is None:
        state["last_reference_price"] = fiyat
        notify(f"{zaman} - Grid baslangic referans fiyati ayarlandi: {fiyat}")
        if MODE != "dry_run" and GRID_SEED_SHIB_PERCENT > 0 and not state["open_lots"] and state.get("trend_pool") is None:
            coin_bakiye = get_balance(BASE_ASSET)
            seed_qty_toplam = coin_bakiye * (GRID_SEED_SHIB_PERCENT / 100)
            trend_qty = seed_qty_toplam * (GRID_TREND_ALLOCATION_PERCENT / 100) if GRID_TREND_POOL_ENABLED else 0.0
            grid_qty = seed_qty_toplam - trend_qty
            if grid_qty > 0:
                state["open_lots"].append({
                    "qty": grid_qty,
                    "entry_price": fiyat,
                    "quote_spent": grid_qty * fiyat,
                })
                notify(
                    f"🌱 Mevcut {BASE_ASSET} bakiyesinin %{GRID_SEED_SHIB_PERCENT:.0f}'i "
                    f"({seed_qty_toplam:,.0f} {BASE_ASSET}) ayrildi; bunun %{GRID_TREND_ALLOCATION_PERCENT:.0f}'i "
                    f"trend havuzuna, kalani ({grid_qty:,.0f} {BASE_ASSET}) grid havuzuna baslangic "
                    f"lotu olarak eklendi (giris fiyati={fiyat}, hedef=%{GRID_STEP_UP_PERCENT} yukarida "
                    f"satis). Ayrilmayan bakiyeye bot hic dokunmaz."
                )
            if trend_qty > 0:
                state["trend_pool"] = {
                    "position": "long",
                    "qty_shib": trend_qty,
                    "qty_usdt": 0.0,
                    "entry_price": fiyat,
                }
                notify(
                    f"📈 Trend alt-havuzu olusturuldu: {trend_qty:,.0f} {BASE_ASSET} (giris fiyati={fiyat}). "
                    f"Hem 1sa hem 24sa degisim >=%{GRID_TREND_ENTRY_PERCENT} ise SHIB'de kalir/alir, "
                    f"ikisinden biri <=%{GRID_TREND_EXIT_PERCENT} olursa USDT'ye satar."
                )
        save_state(state)
        return

    acik_lot_sayisi = len(state["open_lots"])
    gunluk_limit = bool(
        state.get("day_start_equity")
        and state.get("daily_realized_pnl", 0) <= -state["day_start_equity"] * MAX_DAILY_LOSS_PERCENT / 100
    )
    al_tetik = fiyat <= state["last_reference_price"] * (1 - GRID_STEP_DOWN_PERCENT / 100)
    trend_havuzu_aktif = state.get("trend_pool") is not None
    trend = get_trend_context() if (GRID_TREND_FILTER_ENABLED or trend_havuzu_aktif) else None
    trend_blocked = bool(trend) and GRID_TREND_FILTER_ENABLED and (
        (GRID_TREND_FILTER_1H_PERCENT > -100 and trend["change_1h"] <= GRID_TREND_FILTER_1H_PERCENT)
        or (GRID_TREND_FILTER_24H_PERCENT > -100 and trend["change_24h"] <= GRID_TREND_FILTER_24H_PERCENT)
    )
    if trend_havuzu_aktif:
        run_trend_pool(state, fiyat, zaman, trend)

    # Cikislar (kar hedefi / stop-loss) her zaman YENI alimdan ONCE kontrol
    # edilir - koruyucu bir satis, yeni bir pozisyon acmaktan daha oncelikli
    # olmali (aksi halde fiyat hem eski bir lotun stop'unu hem yeni bir grid
    # adimini ayni anda tetiklerse, bot yanlislikla once alim yapip stop-loss'u
    # o dongude hic degerlendirmeyebilir).
    for lot in sorted(state["open_lots"], key=lambda l: l["entry_price"]):
        hedef = lot["entry_price"] * (1 + GRID_STEP_UP_PERCENT / 100)
        stop_seviyesi = lot["entry_price"] * (1 - GRID_LOT_STOP_PERCENT / 100)
        kar_hedefi_tetiklendi = fiyat >= hedef
        stop_tetiklendi = fiyat <= stop_seviyesi
        if not (kar_hedefi_tetiklendi or stop_tetiklendi):
            continue
        sebep = "kar hedefi" if kar_hedefi_tetiklendi else "stop-loss"
        if MODE == "dry_run":
            notify(
                f"🔴 [SIMULE] {zaman} - {SYMBOL} fiyat={fiyat} giris={lot['entry_price']:.10f} "
                f"-> GRID SATIS ({sebep})"
            )
            state["open_lots"].remove(lot)
            state["last_reference_price"] = fiyat
        else:
            bakiye = get_balance(BASE_ASSET)
            satilacak = min(bakiye, lot["qty"] or bakiye)
            if satilacak <= 0:
                notify(f"⚠️ {BASE_ASSET} bakiyesi yetersiz, grid satisi atlandi.")
            else:
                order = place_order("SELL", satilacak, use_quote_qty=False)
                alinan = float(order.get("cummulativeQuoteQty", satilacak * fiyat))
                kar = alinan - (lot.get("quote_spent") or 0)
                state["daily_realized_pnl"] += kar
                notify(
                    f"🔴 [{MODE.upper()}] GRID SATIS ({sebep}, giris={lot['entry_price']:.10f} "
                    f"fiyat={fiyat} kar={kar:+.4f} {QUOTE_ASSET}): {order}"
                )
                state["open_lots"].remove(lot)
                state["last_reference_price"] = fiyat
        save_state(state)
        return  # tek dongude en fazla bir islem - basit ve ongorulebilir tutmak icin

    if al_tetik and acik_lot_sayisi < GRID_MAX_OPEN_LOTS and not gunluk_limit:
        if trend_blocked:
            print(
                f"{zaman} - {SYMBOL} fiyat={fiyat} -> ALIM ATLANDI (trend filtresi: "
                f"1sa={trend['change_1h']:+.2f}% 24sa={trend['change_24h']:+.2f}%)."
            )
            save_state(state)
            return
        if MODE == "dry_run":
            notify(
                f"🟢 [SIMULE] {zaman} - {SYMBOL} fiyat={fiyat} referans={state['last_reference_price']} "
                f"-> GRID ALIM (lot {acik_lot_sayisi + 1}/{GRID_MAX_OPEN_LOTS})"
            )
            state["open_lots"].append({"qty": None, "entry_price": fiyat, "quote_spent": None})
            state["last_reference_price"] = fiyat
            save_state(state)
            return
        else:
            bakiye = get_balance(QUOTE_ASSET)
            if state.get("initial_equity") is None:
                coin_bakiye = get_balance(BASE_ASSET)
                state["initial_equity"] = bakiye + coin_bakiye * fiyat
            rezerv_taban = state["initial_equity"] * (GRID_RESERVE_PERCENT / 100)
            harcanacak = bakiye * (GRID_ORDER_PERCENT / 100)
            if harcanacak <= 0:
                notify(f"⚠️ {QUOTE_ASSET} bakiyesi yetersiz, grid alimi atlandi.")
            elif bakiye - harcanacak < rezerv_taban:
                notify(
                    f"⚠️ Rezerv sinirina yaklasildi ({rezerv_taban:.2f} {QUOTE_ASSET} korunmali), "
                    "grid alimi atlandi."
                )
            else:
                order = place_order("BUY", harcanacak, use_quote_qty=True)
                qty = float(order.get("executedQty", 0))
                quote_spent = float(order.get("cummulativeQuoteQty", harcanacak))
                notify(
                    f"🟢 [{MODE.upper()}] GRID ALIM (lot {acik_lot_sayisi + 1}/{GRID_MAX_OPEN_LOTS}, "
                    f"fiyat={fiyat}): {order}"
                )
                state["open_lots"].append({
                    "qty": qty,
                    "entry_price": (quote_spent / qty) if qty else fiyat,
                    "quote_spent": quote_spent,
                })
                state["last_reference_price"] = fiyat
                if state.get("day_start_equity") is None:
                    state["day_start_equity"] = bakiye
            save_state(state)
            return

    print(
        f"{zaman} - {SYMBOL} fiyat={fiyat} referans={state['last_reference_price']:.10f} "
        f"acik_lot={acik_lot_sayisi}/{GRID_MAX_OPEN_LOTS} -> islem yok."
    )
    save_state(state)


def main():
    check_mode_guard()
    trend_bilgisi = (
        f" TREND_FILTRE(1sa<=%{GRID_TREND_FILTER_1H_PERCENT} veya 24sa<=%{GRID_TREND_FILTER_24H_PERCENT} "
        f"ise ALIM atlanir, her {GRID_TREND_CHECK_INTERVAL_SECONDS}sn kontrol)"
        if GRID_TREND_FILTER_ENABLED else ""
    )
    if GRID_TREND_POOL_ENABLED:
        trend_bilgisi += (
            f" TREND_HAVUZU(seed'in %{GRID_TREND_ALLOCATION_PERCENT} - giris>=%{GRID_TREND_ENTRY_PERCENT} "
            f"cikis<=%{GRID_TREND_EXIT_PERCENT})"
        )
    notify(
        f"🤖 Grid botu basladi. MODE={MODE} SYMBOL={SYMBOL} "
        f"ADIM_ASAGI=%{GRID_STEP_DOWN_PERCENT} ADIM_YUKARI=%{GRID_STEP_UP_PERCENT} "
        f"SIPARIS=%{GRID_ORDER_PERCENT} MAKS_LOT={GRID_MAX_OPEN_LOTS} "
        f"REZERV=%{GRID_RESERVE_PERCENT} POLL={GRID_POLL_INTERVAL_SECONDS}s{trend_bilgisi}"
    )
    state = normalize_state(load_state())
    while True:
        try:
            run_once(state)
        except Exception as e:
            notify(f"⚠️ Hata: {e}")
        time.sleep(GRID_POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
