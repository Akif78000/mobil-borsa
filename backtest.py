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
import urllib.error
import urllib.parse
import urllib.request

from trade_bot import rsi_hesapla, _load_dotenv

_load_dotenv()

SYMBOL = os.environ.get("SYMBOL", "SHIBUSDT")
KLINE_INTERVAL = os.environ.get("KLINE_INTERVAL", "15m")
TRADE_PERCENT = float(os.environ.get("TRADE_PERCENT", "33"))
RSI_BUY_THRESHOLD = float(os.environ.get("RSI_BUY_THRESHOLD", "30"))
RSI_SELL_THRESHOLD = float(os.environ.get("RSI_SELL_THRESHOLD", "70"))
EMA_TREND_SHORT = int(os.environ.get("EMA_TREND_SHORT", "50"))
EMA_TREND_LONG = int(os.environ.get("EMA_TREND_LONG", "200"))
BACKTEST_DAYS = int(os.environ.get("BACKTEST_DAYS", "30"))
BACKTEST_START_CAPITAL = float(os.environ.get("BACKTEST_START_CAPITAL", "1000"))
# true: simulasyon USDT yerine SHIB ile baslar (zaten elinde SHIB tutan, dolar
# degerini degil TOKEN ADEDINI artirmak isteyen kullanicilar icin). Bu durumda
# BACKTEST_START_CAPITAL, o SHIB'in baslangictaki USDT karsiligi (deger) olarak
# yorumlanir; ilk mumun fiyatiyla token adedine cevrilir.
START_IN_SHIB = os.environ.get("START_IN_SHIB", "false").lower() in ("1", "true", "evet")
STOP_LOSS_PERCENT = float(os.environ.get("STOP_LOSS_PERCENT", "3"))
TAKE_PROFIT_PERCENT = float(os.environ.get("TAKE_PROFIT_PERCENT", "6"))
TRADING_FEE_PERCENT = float(os.environ.get("TRADING_FEE_PERCENT", "0.1"))
EMA_SLOPE_LOOKBACK = int(os.environ.get("EMA_SLOPE_LOOKBACK", "5"))
ATR_PERIOD = int(os.environ.get("ATR_PERIOD", "14"))
ATR_STOP_MULTIPLIER = float(os.environ.get("ATR_STOP_MULTIPLIER", "2.0"))
ATR_TAKE_PROFIT_MULTIPLIER = float(os.environ.get("ATR_TAKE_PROFIT_MULTIPLIER", "3.0"))
VOLUME_PERIOD = int(os.environ.get("VOLUME_PERIOD", "20"))
VOLUME_MULTIPLIER = float(os.environ.get("VOLUME_MULTIPLIER", "1.20"))

MAINNET_BASE = "https://api.binance.com"
_CTX = ssl.create_default_context()

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}


# --- Veri cekme dayanikliligi -----------------------------------------
# Uzun BACKTEST_DAYS'te (orn. hybrid_backtest.py'nin 180 gunluk kosumu)
# yuzlerce sayfalama istegi var; Binance ara sira yavas/timeout verebiliyor
# veya rate-limit (429) donebiliyor. Asagidaki katman SADECE bunun icin -
# hicbir gosterge/strateji/sinyal mantigina dokunmaz, sadece AYNI veriyi
# daha guvenilir sekilde ceker.
FETCH_TIMEOUT_SECONDS = float(os.environ.get("FETCH_TIMEOUT_SECONDS", "30"))
FETCH_MAX_RETRIES = int(os.environ.get("FETCH_MAX_RETRIES", "5"))
FETCH_RETRY_DELAYS = [2, 4, 8, 16, 30]  # sirayla kullanilir, bittiginde son deger tekrarlanir
FETCH_RATE_LIMIT_SECONDS = float(os.environ.get("FETCH_RATE_LIMIT_SECONDS", "0.15"))

# Ayni Python sureci icinde (orn. hybrid_backtest.py'nin TEK CALISTIRMASINDA
# GRID + EMA + SUPERTREND + KAMA + HYBRID) ayni symbol+interval+gun penceresi
# BIRDEN FAZLA kez istenebiliyor (orn. uc dedektor de SHIB+majors'in ayni
# 1sa/4sa/1gun verisini ayri ayri cekiyordu). Bellek-ici (surec omrunce
# gecerli, diske yazilmaz) basit bir onbellek bunu tekrar tekrar Binance'ten
# indirmeyi onler - veri ICERIGINI DEGISTIRMEZ, sadece tekrar kullanir.
_KLINES_CACHE = {}


def _gecikme(deneme_no):
    idx = min(deneme_no - 1, len(FETCH_RETRY_DELAYS) - 1)
    return FETCH_RETRY_DELAYS[idx]


def _fetch_klines_batch(symbol, interval, url, max_retries=None):
    """Tek bir sayfalama istegini guvenli sekilde ceker:
      - timeout=FETCH_TIMEOUT_SECONDS, en fazla max_retries deneme.
      - TimeoutError / URLError / gecici OSError -> ustel gecikmeyle (2,4,8,
        16,30sn) tekrar dener.
      - HTTP 429/418 (rate limit) veya 5xx (Binance tarafi gecici sorun) ->
        ayni sekilde tekrar dener; 429/418'de sunucunun Retry-After basligi
        varsa ONU kullanir (yoksa normal gecikme takvimine duser).
      - TUM denemeler tukenirse: sessizce devam ETMEZ, hangi symbol/interval
        icin basarisiz oldugunu acikca yazip HATAYI YUKARI FIRLATIR - cagiran
        (fetch_history) bunu yakalayip backtest'i DURDURUR (eksik/hayali veri
        ile sonuc uretilmez)."""
    max_retries = max_retries or FETCH_MAX_RETRIES
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for deneme in range(1, max_retries + 1):
        print(f"[DATA] {symbol} {interval} deneniyor {deneme}/{max_retries}...")
        try:
            with urllib.request.urlopen(req, context=_CTX, timeout=FETCH_TIMEOUT_SECONDS) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            son_deneme = deneme == max_retries
            if e.code in (429, 418) or 500 <= e.code < 600:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                bekleme = float(retry_after) if retry_after else _gecikme(deneme)
                sebep = f"HTTP {e.code}" + (" (rate limit)" if e.code in (429, 418) else " (Binance tarafi gecici hata)")
                if son_deneme:
                    print(f"[DATA] HATA: {symbol} {interval} - {sebep} - tum {max_retries} deneme tukendi.")
                    raise
                print(f"[DATA] {sebep} - {bekleme:.0f} saniye sonra tekrar denenecek")
                time.sleep(bekleme)
                continue
            print(f"[DATA] HATA: {symbol} {interval} - HTTP {e.code} (tekrar denenmeyecek turden bir hata) - {e}")
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            son_deneme = deneme == max_retries
            if son_deneme:
                print(f"[DATA] HATA: {symbol} {interval} - {e} - tum {max_retries} deneme tukendi.")
                raise
            bekleme = _gecikme(deneme)
            print(f"[DATA] timeout/baglanti hatasi ({e}) - {bekleme} saniye sonra tekrar denenecek")
            time.sleep(bekleme)


def fetch_history(symbol, interval, days):
    if interval not in INTERVAL_MS:
        raise SystemExit(f"Desteklenmeyen KLINE_INTERVAL: {interval}")

    cache_key = (symbol, interval, days)
    if cache_key in _KLINES_CACHE:
        cached = _KLINES_CACHE[cache_key]
        print(f"[DATA] {symbol} {interval} (son {days} gun) onbellekten kullanildi: {len(cached)} mum")
        return cached

    end_time = int(time.time() * 1000)
    start_time = end_time - days * 86_400_000
    candles = []
    cursor = end_time
    try:
        while cursor > start_time:
            params = {"symbol": symbol, "interval": interval, "limit": 1000, "endTime": cursor}
            url = f"{MAINNET_BASE}/api/v3/klines?{urllib.parse.urlencode(params)}"
            batch = _fetch_klines_batch(symbol, interval, url)
            if not batch:
                break
            candles = batch + candles
            first_open_time = batch[0][0]
            if first_open_time <= start_time:
                break
            cursor = first_open_time - 1
            time.sleep(FETCH_RATE_LIMIT_SECONDS)
    except Exception:
        # Kismi/eksik veriyle SESSIZCE devam ETMEK yerine (bu yanlis backtest
        # sonucu uretir) burada durup hatayi cagirana iletiyoruz. Ne kadarlik
        # veri toplanabildigi bilgi amacli yazilir, KULLANILMAZ.
        print(f"[DATA] {symbol} {interval}: sadece {len(candles)} mum toplanabilmisti, "
              f"veri cekme BASARISIZ oldu - backtest durduruluyor.")
        raise

    sonuc = [c for c in candles if c[0] >= start_time]
    print(f"[DATA] {symbol} {interval} tamamlandi: {len(sonuc)} mum")
    _KLINES_CACHE[cache_key] = sonuc
    return sonuc


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
    min_gerekli = EMA_TREND_LONG + 15
    if len(kapanislar) < min_gerekli:
        raise SystemExit(
            f"Yetersiz veri: {len(kapanislar)} mum var, en az {min_gerekli} lazim. "
            f"BACKTEST_DAYS degerini artirin."
        )

    ema_kisa_serisi = compute_ema_series(kapanislar, EMA_TREND_SHORT)
    ema_uzun_serisi = compute_ema_series(kapanislar, EMA_TREND_LONG)

    if START_IN_SHIB:
        # Zaten SHIB tutan kullanici: baslangicta USDT degil, dogrudan SHIB
        # ile basla. in_position=True, ilk cikis sinyali (RSI/trend) gelene
        # kadar stop/kar_al henuz yok (ilk "yeniden alim" ile olusacak).
        ilk_fiyat_t0 = kapanislar[min_gerekli]
        usdt = 0.0
        coin = BACKTEST_START_CAPITAL / ilk_fiyat_t0
        in_position = True
        giris_fiyati = ilk_fiyat_t0
    else:
        usdt = BACKTEST_START_CAPITAL
        coin = 0.0
        in_position = False
        giris_fiyati = None
    baslangic_coin = coin
    stop_fiyati = None
    kar_al_fiyati = None
    trades = []
    equity_egrisi = []
    coin_egrisi = []

    for i in range(min_gerekli, len(kapanislar)):
        fiyat = kapanislar[i]
        # rsi_hesapla sonucu sadece son 15 kapanisa bagli (fonksiyonun kendi
        # ic mantigi geregi), o yuzden kucuk bir pencere yeterli ve hizli.
        rsi = rsi_hesapla(kapanislar[max(0, i - 59): i + 1])
        ek, eu = ema_kisa_serisi[i], ema_uzun_serisi[i]
        trend_yukari = None if ek is None or eu is None else ek > eu
        onceki_ema = ema_kisa_serisi[i - EMA_SLOPE_LOOKBACK] if i >= EMA_SLOPE_LOOKBACK else None
        ema_egimi_yukari = ek is not None and onceki_ema is not None and ek > onceki_ema
        hacim_ort = sum(hacimler[i - VOLUME_PERIOD:i]) / VOLUME_PERIOD
        hacim_onayi = hacimler[i] >= hacim_ort * VOLUME_MULTIPLIER
        true_ranges = []
        for j in range(i - ATR_PERIOD + 1, i + 1):
            true_ranges.append(max(yuksekler[j] - dusukler[j],
                                   abs(yuksekler[j] - kapanislar[j - 1]),
                                   abs(dusukler[j] - kapanislar[j - 1])))
        atr = sum(true_ranges) / ATR_PERIOD
        tarih = time.strftime("%Y-%m-%d %H:%M", time.localtime(zamanlar[i] / 1000))

        if (not in_position and rsi < RSI_BUY_THRESHOLD and trend_yukari is True
                and ema_egimi_yukari and hacim_onayi):
            harcanacak = usdt * (TRADE_PERCENT / 100)
            if harcanacak > 0:
                alim_ucreti = harcanacak * TRADING_FEE_PERCENT / 100
                coin += (harcanacak - alim_ucreti) / fiyat
                usdt -= harcanacak
                in_position = True
                giris_fiyati = fiyat
                stop_fiyati = fiyat - atr * ATR_STOP_MULTIPLIER
                kar_al_fiyati = fiyat + atr * ATR_TAKE_PROFIT_MULTIPLIER
                trades.append({"tip": "AL", "tarih": tarih, "fiyat": fiyat})

        elif in_position and (
            rsi > RSI_SELL_THRESHOLD
            or trend_yukari is False
            or (stop_fiyati is not None and fiyat <= stop_fiyati)
            or (kar_al_fiyati is not None and fiyat >= kar_al_fiyati)
        ):
            satilacak_coin = coin
            if satilacak_coin > 0:
                brut_tutar = satilacak_coin * fiyat
                usdt += brut_tutar * (1 - TRADING_FEE_PERCENT / 100)
                coin -= satilacak_coin
                kar_yuzde = (fiyat - giris_fiyati) / giris_fiyati * 100
                if stop_fiyati is not None and fiyat <= stop_fiyati:
                    sebep = "stop_loss"
                elif kar_al_fiyati is not None and fiyat >= kar_al_fiyati:
                    sebep = "kar_al"
                elif rsi > RSI_SELL_THRESHOLD:
                    sebep = "asiri_alim"
                else:
                    sebep = "trend_bozuldu"
                trades.append(
                    {"tip": "SAT", "tarih": tarih, "fiyat": fiyat, "kar_yuzde": kar_yuzde, "sebep": sebep}
                )
            in_position = False
            stop_fiyati = None
            kar_al_fiyati = None

        equity_egrisi.append(usdt + coin * fiyat)
        coin_egrisi.append(coin + usdt / fiyat)  # o anki fiyattan "tum varlik SHIB olsa kac token" esdegeri

    return trades, equity_egrisi, coin_egrisi, baslangic_coin, kapanislar[min_gerekli]


def ozet_yazdir(trades, equity_egrisi, coin_egrisi, baslangic_coin, ilk_fiyat, son_fiyat, mum_sayisi):
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
    print(f"Ortalama islem kari    : {ort_kar:+.2f}%")
    print(f"En buyuk gerileme (DD) : {max_dusus:.2f}%")

    if START_IN_SHIB and coin_egrisi:
        bitis_coin = coin_egrisi[-1]
        token_degisim = (bitis_coin - baslangic_coin) / baslangic_coin * 100
        print("-" * 56)
        print(f"Baslangic token miktari: {baslangic_coin:,.0f} {SYMBOL.replace('USDT','')}")
        print(f"Bitis token miktari    : {bitis_coin:,.0f} {SYMBOL.replace('USDT','')}")
        print(f"TOKEN ADEDI DEGISIMI   : {token_degisim:+.2f}%  (sadece tutsaydiniz: %0,00)")

    print("=" * 56)

    if START_IN_SHIB:
        if coin_egrisi and coin_egrisi[-1] > baslangic_coin:
            print("Strateji, sadece tutmaya kiyasla DAHA FAZLA token biriktirdi.")
        else:
            print("Strateji, sadece tutmaya kiyasla token adedini ARTIRAMADI (ayni kaldi/azaldi).")
    elif getiri_yuzde > al_ve_tut_getiri:
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
    trades, equity_egrisi, coin_egrisi, baslangic_coin, ilk_fiyat = simulate(
        kapanislar, yuksekler, dusukler, hacimler, zamanlar
    )
    ozet_yazdir(trades, equity_egrisi, coin_egrisi, baslangic_coin, ilk_fiyat, kapanislar[-1], len(candles))


if __name__ == "__main__":
    main()
