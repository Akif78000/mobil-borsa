"""
grid_bot.py'daki salinim (grid) stratejisini gecmis veride test eden arac.

trade_bot.py/backtest.py'deki RSI+EMA trend stratejisinden FARKLI bir
mantik test eder: trend/RSI'ya bakmaz, sadece fiyatin referans noktasindan
% olarak ne kadar hareket ettigine bakar. Amac dolar getirisi degil,
TOKEN ADEDI degisimini olcmek (bkz. ozet ciktisindaki "TOKEN ADEDI DEGISIMI").

Kullanim:
    python3 grid_backtest.py

Not: Grid botu canlida ani (ticker) fiyata gore, saniyeler seviyesinde
tepki verir. Bu backtest ise mum KAPANISLARINI "o anki fiyat" gibi
kullanir - GRID_KLINE_INTERVAL ne kadar kucuk olursa (orn. 5m, 1m),
simulasyon canliya o kadar yakin olur, ama Binance'ten cekilecek veri de
o kadar artar (daha yavas calisir).
"""

import os
import time

from trade_bot import _load_dotenv
from backtest import fetch_history, INTERVAL_MS

_load_dotenv()

SYMBOL = os.environ.get("SYMBOL", "SHIBUSDT")
GRID_KLINE_INTERVAL = os.environ.get("GRID_KLINE_INTERVAL", "5m")
BACKTEST_DAYS = int(os.environ.get("BACKTEST_DAYS", "30"))
BACKTEST_START_CAPITAL = float(os.environ.get("BACKTEST_START_CAPITAL", "1000"))
GRID_STEP_DOWN_PERCENT = float(os.environ.get("GRID_STEP_DOWN_PERCENT", "1"))
GRID_STEP_UP_PERCENT = float(os.environ.get("GRID_STEP_UP_PERCENT", "1"))
GRID_ORDER_PERCENT = float(os.environ.get("GRID_ORDER_PERCENT", "10"))
GRID_MAX_OPEN_LOTS = int(os.environ.get("GRID_MAX_OPEN_LOTS", "8"))
GRID_RESERVE_PERCENT = float(os.environ.get("GRID_RESERVE_PERCENT", "20"))
GRID_LOT_STOP_PERCENT = float(os.environ.get("GRID_LOT_STOP_PERCENT", "15"))
TRADING_FEE_PERCENT = float(os.environ.get("TRADING_FEE_PERCENT", "0.1"))
START_IN_SHIB = os.environ.get("START_IN_SHIB", "false").lower() in ("1", "true", "evet")


def simulate(kapanislar, zamanlar):
    if START_IN_SHIB:
        usdt = 0.0
        coin = BACKTEST_START_CAPITAL / kapanislar[0]
        # Elde tutulan SHIB, satilabilir bir "lot" olarak kaydedilmezse bot hicbir
        # zaman ilk satisi yapamaz (USDT'si olmadigi icin sonra alim da yapamaz) -
        # tamamen hareketsiz kalir. Baslangic pozisyonunu, giris fiyati ilk mumun
        # kapanisi olan normal bir lot gibi ekleyerek grid'in ilk gunden calismasini
        # sagliyoruz.
        open_lots = [{"qty": coin, "entry_price": kapanislar[0], "quote_spent": BACKTEST_START_CAPITAL}]
    else:
        usdt = BACKTEST_START_CAPITAL
        coin = 0.0
        open_lots = []  # {"qty", "entry_price", "quote_spent"}
    baslangic_coin_esdeger = coin + usdt / kapanislar[0]

    referans_fiyat = kapanislar[0]
    trades = []
    equity_egrisi = []
    coin_egrisi = []
    rezerv = BACKTEST_START_CAPITAL * (GRID_RESERVE_PERCENT / 100)

    for i, fiyat in enumerate(kapanislar):
        tarih = time.strftime("%Y-%m-%d %H:%M", time.localtime(zamanlar[i] / 1000))

        al_tetik = fiyat <= referans_fiyat * (1 - GRID_STEP_DOWN_PERCENT / 100)
        harcanacak = usdt * (GRID_ORDER_PERCENT / 100)

        # Cikislar (kar hedefi/stop-loss) her zaman yeni alimdan ONCE kontrol
        # edilir - koruyucu satis, yeni pozisyon acmaktan daha oncelikli olmali.
        satis_yapildi = False
        for lot in sorted(open_lots, key=lambda l: l["entry_price"]):
            hedef = lot["entry_price"] * (1 + GRID_STEP_UP_PERCENT / 100)
            stop_seviyesi = lot["entry_price"] * (1 - GRID_LOT_STOP_PERCENT / 100)
            if fiyat >= hedef or fiyat <= stop_seviyesi:
                brut = lot["qty"] * fiyat
                net = brut * (1 - TRADING_FEE_PERCENT / 100)
                usdt += net
                coin -= lot["qty"]
                kar_yuzde = (fiyat - lot["entry_price"]) / lot["entry_price"] * 100
                sebep = "grid_hedef" if fiyat >= hedef else "stop_loss"
                trades.append({
                    "tip": "SAT", "tarih": tarih, "fiyat": fiyat,
                    "kar_yuzde": kar_yuzde, "sebep": sebep,
                })
                open_lots.remove(lot)
                referans_fiyat = fiyat
                satis_yapildi = True
                break  # bu adimda en fazla bir islem, backtest.py ile ayni sadelik ilkesi

        if not satis_yapildi and al_tetik and len(open_lots) < GRID_MAX_OPEN_LOTS and harcanacak > 0 and (usdt - harcanacak) >= rezerv:
            alim_ucreti = harcanacak * TRADING_FEE_PERCENT / 100
            qty = (harcanacak - alim_ucreti) / fiyat
            usdt -= harcanacak
            coin += qty
            open_lots.append({"qty": qty, "entry_price": fiyat, "quote_spent": harcanacak})
            referans_fiyat = fiyat
            trades.append({"tip": "AL", "tarih": tarih, "fiyat": fiyat, "lot_sayisi": len(open_lots)})

        equity_egrisi.append(usdt + coin * fiyat)
        coin_egrisi.append(coin + usdt / fiyat)

    return trades, equity_egrisi, coin_egrisi, baslangic_coin_esdeger, len(open_lots)


def ozet_yazdir(trades, equity_egrisi, coin_egrisi, baslangic_coin, acik_lot_sayisi, mum_sayisi):
    toplam_deger = equity_egrisi[-1] if equity_egrisi else BACKTEST_START_CAPITAL
    getiri_yuzde = (toplam_deger - BACKTEST_START_CAPITAL) / BACKTEST_START_CAPITAL * 100

    satislar = [t for t in trades if t["tip"] == "SAT"]
    alimlar = [t for t in trades if t["tip"] == "AL"]
    karli = [t for t in satislar if t["kar_yuzde"] > 0]
    kazanma_orani = (len(karli) / len(satislar) * 100) if satislar else 0
    ort_kar = (sum(t["kar_yuzde"] for t in satislar) / len(satislar)) if satislar else 0

    tepe = float("-inf")
    max_dusus = 0.0
    for deger in equity_egrisi:
        tepe = max(tepe, deger)
        if tepe > 0:
            max_dusus = min(max_dusus, (deger - tepe) / tepe * 100)

    bitis_coin = coin_egrisi[-1] if coin_egrisi else baslangic_coin
    token_degisim = (bitis_coin - baslangic_coin) / baslangic_coin * 100 if baslangic_coin else 0

    print()
    print("=" * 56)
    print(f"GRID BACKTEST: {SYMBOL} {GRID_KLINE_INTERVAL} - son {BACKTEST_DAYS} gun ({mum_sayisi} mum)")
    print(f"ADIM: asagi=%{GRID_STEP_DOWN_PERCENT} yukari=%{GRID_STEP_UP_PERCENT} "
          f"siparis=%{GRID_ORDER_PERCENT} maks_lot={GRID_MAX_OPEN_LOTS} rezerv=%{GRID_RESERVE_PERCENT}")
    print("=" * 56)
    print(f"Baslangic sermaye      : {BACKTEST_START_CAPITAL:,.2f} USDT")
    print(f"Bitis degeri            : {toplam_deger:,.2f} USDT")
    print(f"Strateji getirisi       : {getiri_yuzde:+.2f}%")
    print(f"Toplam islem            : {len(trades)}  ({len(alimlar)} alim, {len(satislar)} satis)")
    print(f"Kapanista acik kalan lot: {acik_lot_sayisi}")
    print(f"Karli islem orani       : %{kazanma_orani:.1f}")
    print(f"Ortalama islem kari     : {ort_kar:+.2f}%")
    print(f"En buyuk gerileme (DD)  : {max_dusus:.2f}%")
    print("-" * 56)
    print(f"Baslangic token esdegeri: {baslangic_coin:,.0f} {SYMBOL.replace('USDT', '')}")
    print(f"Bitis token esdegeri    : {bitis_coin:,.0f} {SYMBOL.replace('USDT', '')}")
    print(f"TOKEN ADEDI DEGISIMI    : {token_degisim:+.2f}%  (sadece tutsaydiniz: %0,00)")
    print("=" * 56)

    if token_degisim > 0:
        print("Grid stratejisi, sadece tutmaya kiyasla DAHA FAZLA token biriktirdi.")
    else:
        print("Grid stratejisi, sadece tutmaya kiyasla token adedini ARTIRAMADI.")
    print("(Not: gecmis performans gelecegi garanti etmez, tek donem yeterli kanit degildir.)")

    if trades:
        csv_path = "grid_backtest_trades.csv"
        with open(csv_path, "w") as f:
            f.write("tip,tarih,fiyat,kar_yuzde,sebep\n")
            for t in trades:
                f.write(f"{t['tip']},{t['tarih']},{t['fiyat']},{t.get('kar_yuzde', '')},{t.get('sebep', '')}\n")
        print(f"\nIslem detaylari kaydedildi: {csv_path}")


def main():
    print(f"Gecmis veri cekiliyor: {SYMBOL} {GRID_KLINE_INTERVAL} son {BACKTEST_DAYS} gun...")
    candles = fetch_history(SYMBOL, GRID_KLINE_INTERVAL, BACKTEST_DAYS)
    kapanislar = [float(c[4]) for c in candles]
    zamanlar = [c[0] for c in candles]
    trades, equity_egrisi, coin_egrisi, baslangic_coin, acik_lot_sayisi = simulate(kapanislar, zamanlar)
    ozet_yazdir(trades, equity_egrisi, coin_egrisi, baslangic_coin, acik_lot_sayisi, len(candles))


if __name__ == "__main__":
    main()
