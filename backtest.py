"""
Gecmis veri uzerinde trade_bot.py'daki RSI+EMA trend stratejisini test eden
backtest araci.

Amac: Gercek paraya (veya testnete) gecmeden once "bu strateji gecmiste ise
yarardi mi?" sorusuna somut sayilarla cevap vermek. Ayni sinyal mantigini
(trade_bot.py ile birebir ayni RSI ve trend filtresi kurallari) gecmis
fiyatlar uzerinde bar bar yeniden oynatir, sanal bir cuzdanla islem yapar.

Kullanim:
    python3 backtest.py

Ayarlar .env dosyasindan (trade_bot.py ile ayni degiskenler) veya ortam
degiskenlerinden okunur; ayrica BACKTEST_DAYS ve BACKTEST_START_CAPITAL
eklenmistir.

ONEMLI: Gecmiste ise yaramis olmasi gelecekte de ise yarayacagi anlamina
gelmez (overfitting riski). Sonuclari tek bir "kesin dogru" olarak degil,
stratejinin genel egilimini gormek icin kullanin.
"""

import json
import os
import ssl
import time
import urllib.parse
import urllib.request

from trade_bot import rsi_hesapla, atr_hesapla, _load_dotenv

_load_dotenv()

SYMBOL = os.environ.get("SYMBOL", "SHIBUSDT")
KLINE_INTERVAL = os.environ.get("KLINE_INTERVAL", "15m")
TRADE_PERCENT = float(os.environ.get("TRADE_PERCENT", "33"))
RSI_BUY_THRESHOLD = float(os.environ.get("RSI_BUY_THRESHOLD", "30"))
RSI_SELL_THRESHOLD = float(os.environ.get("RSI_SELL_THRESHOLD", "70"))
EMA_TREND_SHORT = int(os.environ.get("EMA_TREND_SHORT", "50"))
EMA_TREND_LONG = int(os.environ.get("EMA_TREND_LONG", "200"))
EMA_SLOPE_LOOKBACK = int(os.environ.get("EMA_SLOPE_LOOKBACK", "3"))
ATR_PERIOD = int(os.environ.get("ATR_PERIOD", "14"))
ATR_STOP_MULTIPLIER = float(os.environ.get("ATR_STOP_MULTIPLIER", "1.5"))
ATR_TP_MULTIPLIER = float(os.environ.get("ATR_TP_MULTIPLIER", "3.0"))
VOLUME_PERIOD = int(os.environ.get("VOLUME_PERIOD", "20"))
VOLUME_MULTIPLIER = float(os.environ.get("VOLUME_MULTIPLIER", "1.2"))
BACKTEST_DAYS = int(os.environ.get("BACKTEST_DAYS", "30"))
BACKTEST_START_CAPITAL = float(os.environ.get("BACKTEST_START_CAPITAL", "1000"))

MAINNET_BASE = "https://api.binance.com"
_CTX = ssl.create_default_context()

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}


def fetch_history(symbol, interval, days):
    if interval not in INTERVAL_MS:
        raise SystemExit(f"Desteklenmeyen KLINE_INTERVAL: {interval}")
    end_time = int(time.time() * 1000)
    start_time = end_time - days * 86_400_000
    candles = []
    cursor = end_time
    while cursor > start_time:
        params = {"symbol": symbol, "interval": interval, "limit": 1000, "endTime": cursor}
        url = f"{MAINNET_BASE}/api/v3/klines?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, context=_CTX, timeout=15) as resp:
            batch = json.loads(resp.read().decode())
        if not batch:
            break
        candles = batch + candles
        first_open_time = batch[0][0]
        if first_open_time <= start_time:
            break
        cursor = first_open_time - 1
        time.sleep(0.2)
    return [c for c in candles if c[0] >= start_time]


def compute_ema_series(fiyatlar, periyot):
    """Her indeks icin EMA degeri; yeterli veri olmayan indekslerde None.
    trade_bot.ema_hesapla ile ayni seed/formul, ama tek O(n) geciste tum
    seriyi uretir (backtest dongusunde tekrar tekrar O(n) hesaplamamak icin)."""
    n = len(fiyatlar)
    sonuc = [None] * n
    if n < periyot:
        return sonuc
    k = 2 / (periyot + 1)
    ema = sum(fiyatlar[:periyot]) / periyot
    sonuc[periyot - 1] = ema
    for i in range(periyot, n):
        ema = fiyatlar[i] * k + ema * (1 - k)
        sonuc[i] = ema
    return sonuc


def simulate(kapanislar, yuksekler, dusukler, hacimler, zamanlar):
    min_gerekli = max(EMA_TREND_LONG + EMA_SLOPE_LOOKBACK, ATR_PERIOD, VOLUME_PERIOD) + 15
    if len(kapanislar) < min_gerekli:
        raise SystemExit(
            f"Yetersiz veri: {len(kapanislar)} mum var, en az {min_gerekli} lazim. "
            f"BACKTEST_DAYS degerini artirin."
        )

    ema_kisa_serisi = compute_ema_series(kapanislar, EMA_TREND_SHORT)
    ema_uzun_serisi = compute_ema_series(kapanislar, EMA_TREND_LONG)

    usdt = BACKTEST_START_CAPITAL
    coin = 0.0
    in_position = False
    giris_fiyati = None
    stop_price = None
    tp_price = None
    trades = []
    equity_egrisi = []

    for i in range(min_gerekli, len(kapanislar)):
        fiyat = kapanislar[i]
        yuksek = yuksekler[i]
        dusuk = dusukler[i]
        # rsi_hesapla sonucu sadece son 15 kapanisa bagli (fonksiyonun kendi
        # ic mantigi geregi), o yuzden kucuk bir pencere yeterli ve hizli.
        rsi = rsi_hesapla(kapanislar[max(0, i - 59): i + 1])
        ek, eu = ema_kisa_serisi[i], ema_uzun_serisi[i]
        trend_yukari = None if ek is None or eu is None else ek > eu

        # Egim icin uzun EMA kullanilir (trade_bot.py ile ayni gerekce: kisa
        # EMA dip sirasinda zaten asagi doner, uzun EMA daha yavas tepki verip
        # ana trendin gercekten guclenip guclenmedigini gosterir).
        eu_once = ema_uzun_serisi[i - EMA_SLOPE_LOOKBACK] if i - EMA_SLOPE_LOOKBACK >= 0 else None
        trend_yukseliyor = None if eu is None or eu_once is None else eu > eu_once

        atr_basi = max(0, i - ATR_PERIOD - 1)
        atr = atr_hesapla(yuksekler[atr_basi : i + 1], dusukler[atr_basi : i + 1], kapanislar[atr_basi : i + 1], ATR_PERIOD)

        hacim_basi = max(0, i - VOLUME_PERIOD + 1)
        pencere_hacim = hacimler[hacim_basi : i + 1]
        ortalama_hacim = sum(pencere_hacim) / len(pencere_hacim) if len(pencere_hacim) >= VOLUME_PERIOD else None
        hacim_teyidi = ortalama_hacim is not None and hacimler[i] > ortalama_hacim * VOLUME_MULTIPLIER

        tarih = time.strftime("%Y-%m-%d %H:%M", time.localtime(zamanlar[i] / 1000))

        if in_position:
            # Stop/kar hedefi mum icinde (high/low) tetiklenmis olabilir, sadece
            # kapanisa bakmak gec kalir - gercekci simulasyon icin high/low kontrolu.
            stop_tetiklendi = stop_price is not None and dusuk <= stop_price
            tp_tetiklendi = not stop_tetiklendi and tp_price is not None and yuksek >= tp_price
            if stop_tetiklendi or tp_tetiklendi or rsi > RSI_SELL_THRESHOLD or trend_yukari is False:
                cikis_fiyati = stop_price if stop_tetiklendi else (tp_price if tp_tetiklendi else fiyat)
                satilacak_coin = coin * (TRADE_PERCENT / 100)
                if satilacak_coin > 0:
                    usdt += satilacak_coin * cikis_fiyati
                    coin -= satilacak_coin
                    kar_yuzde = (cikis_fiyati - giris_fiyati) / giris_fiyati * 100
                    if stop_tetiklendi:
                        sebep = "stop_loss"
                    elif tp_tetiklendi:
                        sebep = "kar_hedefi"
                    elif rsi > RSI_SELL_THRESHOLD:
                        sebep = "asiri_alim"
                    else:
                        sebep = "trend_bozuldu"
                    trades.append(
                        {"tip": "SAT", "tarih": tarih, "fiyat": cikis_fiyati, "kar_yuzde": kar_yuzde, "sebep": sebep}
                    )
                # trade_bot.py canli calisirken de sadece TRADE_PERCENT kadar satip
                # pozisyonu "kapali" sayiyor (kismi pozisyon kaliyor) - ayni davranisi
                # birebir yansitiyoruz ki backtest sonucu canli botla tutarli olsun.
                in_position = False
                stop_price = None
                tp_price = None

        elif (
            rsi < RSI_BUY_THRESHOLD
            and trend_yukari is True
            and trend_yukseliyor is True
            and hacim_teyidi
            and atr is not None
        ):
            harcanacak = usdt * (TRADE_PERCENT / 100)
            if harcanacak > 0:
                coin += harcanacak / fiyat
                usdt -= harcanacak
                in_position = True
                giris_fiyati = fiyat
                stop_price = fiyat - ATR_STOP_MULTIPLIER * atr
                tp_price = fiyat + ATR_TP_MULTIPLIER * atr
                trades.append({"tip": "AL", "tarih": tarih, "fiyat": fiyat})

        equity_egrisi.append(usdt + coin * fiyat)

    return trades, equity_egrisi, kapanislar[min_gerekli]


def ozet_yazdir(trades, equity_egrisi, ilk_fiyat, son_fiyat, mum_sayisi):
    toplam_deger = equity_egrisi[-1] if equity_egrisi else BACKTEST_START_CAPITAL
    getiri_yuzde = (toplam_deger - BACKTEST_START_CAPITAL) / BACKTEST_START_CAPITAL * 100

    al_ve_tut_deger = BACKTEST_START_CAPITAL / ilk_fiyat * son_fiyat
    al_ve_tut_getiri = (al_ve_tut_deger - BACKTEST_START_CAPITAL) / BACKTEST_START_CAPITAL * 100

    satislar = [t for t in trades if t["tip"] == "SAT"]
    karli = [t for t in satislar if t["kar_yuzde"] > 0]
    kazanma_orani = (len(karli) / len(satislar) * 100) if satislar else 0
    ort_kar = (sum(t["kar_yuzde"] for t in satislar) / len(satislar)) if satislar else 0

    tepe = float("-inf")
    max_dusus = 0.0
    for deger in equity_egrisi:
        tepe = max(tepe, deger)
        if tepe > 0:
            max_dusus = min(max_dusus, (deger - tepe) / tepe * 100)

    print()
    print("=" * 56)
    print(f"BACKTEST SONUCU: {SYMBOL} {KLINE_INTERVAL} - son {BACKTEST_DAYS} gun ({mum_sayisi} mum)")
    print("=" * 56)
    print(f"Baslangic sermaye     : {BACKTEST_START_CAPITAL:,.2f} USDT")
    print(f"Bitis degeri           : {toplam_deger:,.2f} USDT")
    print(f"Strateji getirisi      : {getiri_yuzde:+.2f}%")
    print(f"Al-ve-tut getirisi     : {al_ve_tut_getiri:+.2f}%  (karsilastirma icin)")
    print(f"Toplam islem           : {len(trades)}  ({len(satislar)} satis)")
    print(f"Karli islem orani      : %{kazanma_orani:.1f}")
    print(f"Ortalama islem karı    : {ort_kar:+.2f}%")
    print(f"En buyuk gerileme (DD) : {max_dusus:.2f}%")
    print("=" * 56)

    if getiri_yuzde > al_ve_tut_getiri:
        print("Strateji, bu donemde sadece alip tutmaktan DAHA IYI sonuc verdi.")
    else:
        print("Strateji, bu donemde sadece alip tutmaktan DAHA KOTU sonuc verdi.")
    print("(Not: gecmis performans gelecegi garanti etmez, tek donem yeterli kanit degildir.)")

    if trades:
        csv_path = "backtest_trades.csv"
        with open(csv_path, "w") as f:
            f.write("tip,tarih,fiyat,kar_yuzde,sebep\n")
            for t in trades:
                f.write(
                    f"{t['tip']},{t['tarih']},{t['fiyat']},{t.get('kar_yuzde', '')},{t.get('sebep', '')}\n"
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
    trades, equity_egrisi, ilk_fiyat = simulate(kapanislar, yuksekler, dusukler, hacimler, zamanlar)
    ozet_yazdir(trades, equity_egrisi, ilk_fiyat, kapanislar[-1], len(candles))


if __name__ == "__main__":
    main()
