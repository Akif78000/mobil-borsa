# mobil-borsa — Sistem Özeti (Codex / başka bir AI aracına devir için)

Bu dosya, bu projede şu ana kadar yapılanların tam özetidir. Başka bir AI
aracına (Codex, ChatGPT, vb.) bu projeyi devrederken bu dosyayı verin —
mimariyi, dosyaları, strateji mantığını ve güvenlik kurallarını tek yerden
anlayabilsin.

## Amaç

Binance'te SHIB/USDT (veya `.env` ile herhangi bir sembol) için RSI + EMA
trend filtresine dayalı, %33 (yapılandırılabilir) pozisyon boyutlandırmalı
bir trade botu. Üç güvenli çalışma modu var: sadece simülasyon (varsayılan),
testnet (sahte para), ve gerçek para (çift onaylı). Kullanıcı botu kendi
Android telefonunda Termux üzerinden çalıştırıyor.

## Dosya haritası

| Dosya | Ne işe yarar |
|---|---|
| `main.py` | Streamlit tabanlı ayrı bir analiz uygulaması (BIST/kripto RSI tarayıcı). Trade botundan bağımsız, orijinal proje. |
| `trade_bot.py` | Ana trade botu. Sürekli döngüde Binance'ten fiyat çeker, RSI+EMA sinyaline göre AL/SAT kararı verir, moda göre gerçek emir gönderir veya simüle eder, Telegram'a bildirim yollar. |
| `backtest.py` | `trade_bot.py` ile **birebir aynı** sinyal mantığını geçmiş veri üzerinde bar bar oynatıp performansı ölçen araç (getiri, kazanma oranı, max drawdown, al-ve-tut karşılaştırması). |
| `.env.example` | Tüm yapılandırılabilir ayarların şablonu. Kullanıcı bunu `.env` olarak kopyalayıp dolduruyor. `.env` asla repoya girmiyor (`.gitignore`'da). |
| `run_termux.sh` | Android/Termux için tek komutluk başlatıcı: paketleri kurar, `.env` yoksa oluşturur, `termux-wake-lock` alır, botu başlatır. |
| `run_windows.bat` | Windows PC için çift-tıkla başlatıcı: Python kurulu mu kontrol eder, `.env` yoksa oluşturup Not Defteri'nde açar, bot/backtest/`.env` düzenleme seçenekli bir menü sunar. |
| `.gitignore` | `__pycache__/`, `.env`, `trade_bot_state.json`, `backtest_trades.csv` hariç tutulmuş — hiçbiri repoya girmemeli (API anahtarı/secret sızıntısı riski). |

## Strateji mantığı (trade_bot.py ve backtest.py'de ortak)

**Göstergeler:**
- `rsi_hesapla(fiyat_listesi)`: Klasik 14 periyotluk RSI. **Not:** Fonksiyonun
  sonucu sadece verilen listenin **son 15 elemanına** bağlıdır (kod böyle
  yazılmış, kasıtlı bir optimizasyon fırsatı sağlıyor).
- `ema_hesapla(fiyat_listesi, periyot)` (`trade_bot.py`) / `compute_ema_series`
  (`backtest.py`, aynı formülün O(n) toplu hesaplanan hali): Basit EMA, seed
  = ilk `periyot` elemanın ortalaması.
- Trend filtresi: `EMA(kısa=50) > EMA(uzun=200)` ise "yükseliş trendi".

**Karar kuralları (2. iterasyon — ATR stop/hedef, hacim ve eğim filtresi eklendi):**
- **AL**: hepsi birden sağlanmalı:
  1. `RSI < RSI_BUY_THRESHOLD (30)`
  2. `EMA(kısa=50) > EMA(uzun=200)` (yükseliş trendi)
  3. Uzun EMA (200), `EMA_SLOPE_LOOKBACK` mum önceki değerinden **hâlâ yüksek**
     (trend gerçekten güçleniyor mu — kısa EMA değil uzun EMA kullanılıyor,
     çünkü kısa EMA zaten RSI dip'i sırasında düşer, bunu şart koşmak RSI
     mantığıyla çelişir)
  4. Hacim teyidi: güncel mum hacmi, son `VOLUME_PERIOD` mumun ortalamasının
     `VOLUME_MULTIPLIER` katından fazla
  5. ATR hesaplanabiliyor (yeterli veri var)
  6. Pozisyonda değilken
  
  Bakiyenin `%TRADE_PERCENT`'i (varsayılan 33) ile alım yapılır. Giriş anında
  `stop_price = giriş − ATR_STOP_MULTIPLIER×ATR` ve
  `take_profit_price = giriş + ATR_TP_MULTIPLIER×ATR` hesaplanıp state'e
  kaydedilir (sabit %'lik stop yerine, o anki oynaklığa göre ölçekli).

- **SAT**: pozisyondayken şunlardan biri yeterli:
  1. Fiyat `stop_price`'ın altına düştü (stop-loss)
  2. Fiyat `take_profit_price`'ın üstüne çıktı (kâr hedefi)
  3. `RSI > RSI_SELL_THRESHOLD (70)` (aşırı alım/kâr realizasyonu)
  4. Trend aşağı döndü (koruyucu çıkış)
  
  Elde tutulan coin'in `%TRADE_PERCENT`'i satılır.

- **Önemli tasarım detayı:** Satış sadece kısmi (%33) olsa da, bot
  `in_position` bayrağını satıştan sonra **koşulsuz** `False` yapıyor — yani
  gerçekte pozisyonun bir kısmı elde kalabilir, ama bot "pozisyon kapandı"
  sayıp yeni bir AL sinyalini tekrar değerlendirmeye başlıyor. Bu basit bir
  basitleştirme; backtest.py bunu bilerek birebir aynı şekilde taklit
  ediyor ki sonuçlar canlı botla tutarlı olsun.

- **backtest.py'de intrabar stop/hedef kontrolü:** Sadece kapanışa değil,
  mumun `high`/`low` değerlerine bakarak stop/hedefin mum içinde tetiklenip
  tetiklenmediği kontrol edilir (gerçekçi simülasyon için — sadece kapanışa
  bakmak, mum içi sert hareketleri kaçırıp gecikmeli/optimistik sonuç verirdi).

**Veri çekme:** `get_klines()` varsayılan `KLINE_INTERVAL=15m` mumlar,
`POLL_INTERVAL_SECONDS=60` ile kontrol ediliyor — Binance'in "oluşmakta
olan mum" verisi anlık fiyatı yansıttığı için sinyaller gerçek zamanlıya
yakın güncelleniyor (eskiden 1h mum + 5dk poll'du, neredeyse hiç
değişmiyordu).

## Güvenlik / mod sistemi (`trade_bot.py`)

`MODE` ortam değişkeni ile kontrol edilir:

- `dry_run` (**varsayılan**): Hiçbir Binance emri gönderilmez, API anahtarı
  gerekmez. Sadece ne yapılacağını hesaplayıp Telegram/konsola yazar.
- `testnet`: `testnet.binance.vision` üzerinde sahte parayla gerçek emir.
  Testnet API anahtarı gerekir.
- `live`: **Gerçek parayla** gerçek emir. Açılması için **ikisi birden**
  gerekli: `MODE=live` **ve** `CONFIRM_REAL_MONEY=EVET`. İkisi de yoksa
  `check_mode_guard()` `SystemExit` ile başlamayı reddediyor.

API anahtarları ve Telegram token'ı **sadece** `.env`/ortam değişkeninden
okunur, hiçbir zaman koda/repoya yazılmaz.

## Yapılandırma (`.env.example`)

```
MODE=dry_run
SYMBOL=SHIBUSDT
BASE_ASSET=SHIB
QUOTE_ASSET=USDT
TRADE_PERCENT=33
POLL_INTERVAL_SECONDS=60
KLINE_INTERVAL=15m
RSI_BUY_THRESHOLD=30
RSI_SELL_THRESHOLD=70
EMA_TREND_SHORT=50
EMA_TREND_LONG=200
EMA_SLOPE_LOOKBACK=3
ATR_PERIOD=14
ATR_STOP_MULTIPLIER=1.5
ATR_TP_MULTIPLIER=3.0
VOLUME_PERIOD=20
VOLUME_MULTIPLIER=1.2
BINANCE_API_KEY=
BINANCE_API_SECRET=
CONFIRM_REAL_MONEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
BACKTEST_DAYS=30
BACKTEST_START_CAPITAL=1000
```

## Çalıştırma

**Bot (canlı/simüle döngü):**
```
python3 trade_bot.py
```
Android/Termux'ta kısayolu: `bash run_termux.sh`

**Backtest:**
```
python3 backtest.py
```
Son `BACKTEST_DAYS` günün geçmiş verisini Binance'in public API'sinden
çekip stratejiyi simüle eder, özet ve `backtest_trades.csv` üretir.

## Değişiklik geçmişi: kullanıcı backtest bulgusu → strateji güncellemesi

Kullanıcı, ilk sürümü (sadece RSI+EMA trend filtresi, sabit stop yok) 30/90/180
günlük gerçek Binance verisiyle test etti ve üç dönemde de **zararlı**, iki
dönemde al-tut'tan **kötü** sonuç bulup canlı botu durdurdu. Belirttiği
sorunlar: seyrek/geç sinyal, sabit %stop'un oynaklığa uymaması, hacim/rejim
filtresi eksikliği, stop'un mum kapanışını bekleyip geç kalması. Bunun
üzerine strateji şu şekilde güncellendi (yukarıdaki "Karar kuralları"
bölümüne yansıdı): ATR tabanlı (oynaklığa duyarlı) stop-loss/kâr hedefi,
hacim teyidi, uzun-EMA eğim filtresi, backtest'te intrabar (high/low)
stop kontrolü. **Bu güncelleme sonrası yeni bir 30/90/180 günlük backtest
henüz kullanıcı tarafından koşulmadı** — canlıya dönmeden önce mutlaka
tekrar test edilmeli.

## Bilinen sınırlamalar / dürüst notlar

- Çoklu coin takibi, işlem geçmişi/performans dashboard'u gibi genişletmeler
  henüz **eklenmedi**.
- `in_position` bayrağının kısmi satıştan sonra koşulsuz sıfırlanması,
  gerçek bakiye takibiyle tam örtüşmeyebilir (yukarıda açıklandı).
- Backtest'teki EMA hesaplaması, geçmiş verinin tamamından tek seferde
  seed alınarak sürekli hesaplanıyor; canlı bot her `get_klines()`
  çağrısında son `limit=300` mumluk pencereden yeniden seed alıyor — uzun
  vadede ihmal edilebilir bir fark yaratır ama backtest sonucu ile canlı
  botun EMA'sı milimetrik olarak aynı olmayabilir.
- Canlı bot, stop-loss'u sadece her `POLL_INTERVAL_SECONDS` (varsayılan 60sn)
  kontrolünde fiyatı okuyup değerlendiriyor — tick-bazlı/websocket anlık takip
  yok, iki kontrol arasında olabilecek ani bir fiyat hareketini kaçırabilir.
  Backtest ise mum içi (high/low) kontrolü yaptığı için canlıdan biraz daha
  "iyimser" sonuç verebilir.
- ATR/hacim/eğim parametreleri (`ATR_STOP_MULTIPLIER`, `VOLUME_MULTIPLIER`
  vb.) hiç optimize edilmedi, varsayılan/tipik değerler kullanıldı — walk
  forward test veya parametre taraması henüz yapılmadı.
- Bu proje **yatırım tavsiyesi değildir**; basit teknik göstergelere
  dayanır, yanlış sinyal riski yüksektir. Gerçek paraya geçmeden önce
  backtest + testnet ile uzunca test edilmesi öneriliyor.

## Repo / branch bilgisi

- Repo: `Akif78000/mobil-borsa`
- Aktif geliştirme branch'i: `claude/binance-giro-shiba-trade-xasi2t`
  (henüz `main`'e merge edilmedi / PR açılmadı)
