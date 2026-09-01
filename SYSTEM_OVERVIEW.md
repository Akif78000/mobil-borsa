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
| `AUDIT_REPORT.md` | Kullanıcının bağımsız bir AI aracıyla (Codex) yaptırdığı güvenlik/mantık denetiminin özeti — hangi düzeltmeler yapıldı, hangi backtest sonucu alındı. |
| `grid_bot.py` | **Farklı strateji**: RSI/trend'e bakmayan salınım (grid) botu. Referans fiyattan %X düşünce AL, o lot'un giriş fiyatından %Y yükselince o lot'u SAT. Amaç dolar değil, **token adedi** biriktirmek. `trade_bot.py`'den mod sistemi/acil durdurma/sipariş altyapısını import ederek kullanır. |
| `grid_backtest.py` | `grid_bot.py` ile aynı grid mantığını geçmiş veride test eder, "TOKEN ADEDİ DEĞİŞİMİ" metriğini raporlar. |
| `adaptive_bot.py` | **Hibrit bot**: Kaufman Verimlilik Oranı ile piyasanın TREND mi RANGE mi olduğuna kendisi karar verip `trade_bot.py` (TREND) veya `grid_bot.py` (RANGE) mantığıyla giriş yapar. Açık pozisyonlar hangi mantıkla açıldıysa o mantıkla kapanır. `trade_bot.py`'den tüm ortak altyapıyı (mod sistemi, sipariş, acil durdurma) import eder. |
| `adaptive_backtest.py` | SAF-TREND / SAF-GRID / HİBRİT üç stratejiyi **aynı** geçmiş veri üzerinde aynı anda çalıştırıp yan yana karşılaştırır — "hangisi tutarlı" sorusuna somut cevap verir. |
| `.gitignore` | `__pycache__/`, `.env`, `*_state.json`, `*_trades.csv`, `STOP_BOT` hariç tutulmuş — hiçbiri repoya girmemeli (API anahtarı/secret sızıntısı veya çalışma zamanı dosyası). |

## Strateji mantığı (trade_bot.py ve backtest.py'de ortak)

**Göstergeler:**
- `rsi_hesapla(fiyat_listesi)`: Klasik 14 periyotluk RSI. **Not:** Fonksiyonun
  sonucu sadece verilen listenin **son 15 elemanına** bağlıdır (kod böyle
  yazılmış, kasıtlı bir optimizasyon fırsatı sağlıyor).
- `ema_hesapla(fiyat_listesi, periyot)` (`trade_bot.py`) / `compute_ema_series`
  (`backtest.py`, aynı formülün O(n) toplu hesaplanan hali): Basit EMA, seed
  = ilk `periyot` elemanın ortalaması.
- Trend filtresi: `EMA(kısa=50) > EMA(uzun=200)` ise "yükseliş trendi".

**Karar kuralları (3. iterasyon — kullanıcının bağımsız Codex denetimiyle güncellendi):**
- **AL**: hepsi birden sağlanmalı:
  1. `RSI < RSI_BUY_THRESHOLD (30)`
  2. `EMA(kısa=50) > EMA(uzun=200)` (yükseliş trendi)
  3. **Kısa EMA (50)**, `EMA_SLOPE_LOOKBACK` (varsayılan 5) mum önceki
     değerinden hâlâ yüksek (`ema_egimi_yukari`). **Not:** Bu, keskin/hızlı
     V-dip'lerde RSI<30 ile aynı anda sağlanamayabilir (kısa EMA henüz
     toparlanmamışken RSI hâlâ düşük olabilir) — test ederken gördüğüm bir
     davranış: bot sert dip'lerin dibini değil, güçlü bir trend içindeki
     **sığ/yavaş geri çekilmeleri** yakalıyor. Bu kasıtlı bir tasarım gibi
     görünüyor (yanlış sinyali azaltmak için), ama "kaçırılan giriş" riskini
     de artırıyor — parametre ayarlarken bunu göz önünde bulundurun.
  4. Hacim teyidi: güncel mum hacmi, **önceki** `VOLUME_PERIOD` mumun
     ortalamasının (kendisi hariç) `VOLUME_MULTIPLIER` katından fazla
  5. Pozisyonda değilken
  6. Günlük gerçekleşmiş zarar, `MAX_DAILY_LOSS_PERCENT`'i aşmamış (aşağıda)
  
  Bakiyenin `%TRADE_PERCENT`'i (varsayılan 33, **kod seviyesinde en fazla
  %33 ile sınırlı**) ile alım yapılır. ATR hesaplanabiliyorsa
  `stop_price = giriş − ATR_STOP_MULTIPLIER×ATR` ve
  `take_profit_price = giriş + ATR_TAKE_PROFIT_MULTIPLIER×ATR`; ATR
  hesaplanamıyorsa **`STOP_LOSS_PERCENT`/`TAKE_PROFIT_PERCENT`** (sabit %)
  yedek olarak kullanılır.

- **SAT**: pozisyondayken şunlardan biri yeterli:
  1. Fiyat stop seviyesinin altına düştü (stop-loss)
  2. Fiyat kâr hedefinin üstüne çıktı (kâr-al)
  3. `RSI > RSI_SELL_THRESHOLD (70)` (aşırı alım/kâr realizasyonu)
  4. Trend aşağı döndü (koruyucu çıkış)
  
  **Düzeltildi:** Artık kısmi değil, bot'un kendi kaydettiği pozisyon
  miktarının **tamamı** satılır (`state["position_qty"]`, hesaptaki mevcut
  bakiyeyle sınırlanarak) — önceki sürümdeki "kısmi satış sonrası
  `in_position=False` ama gerçekte coin elde kalıyor" tutarsızlığı giderildi.

- **Sadece kapanmış mumlar:** `get_candles()` Binance'ten gelen son (henüz
  kapanmamış/oluşmakta olan) mumu **atar**. Ayrıca her kapanmış mum için
  **sadece bir kez** karar verilir (`state["last_candle_close"]` ile aynı
  mumda tekrar emir engellenir) — repaint/çift emir riskini azaltır.

- **Günlük zarar limiti:** `state["daily_realized_pnl"]`, günün başındaki
  bakiyenin (`day_start_quote`) `%MAX_DAILY_LOSS_PERCENT`'ini aşacak kadar
  negatifse, o gün için yeni ALIM yapılmaz (SAT/stop hâlâ çalışır). Gün
  değişince otomatik sıfırlanır.

- **Acil durdurma:** Çalışma dizininde `EMERGENCY_STOP_FILE` (varsayılan
  `STOP_BOT`) adında bir dosya varsa, bot bir sonraki döngüde `SystemExit`
  ile durur. `run_windows.bat` menüsüne bu dosyayı oluşturan bir seçenek
  eklenmeli/eklendi (bkz. ilgili script).

- **LOT_SIZE yuvarlama:** Gerçek SAT emri öncesi Binance'in `exchangeInfo`
  filtrelerinden (`LOT_SIZE`) adım büyüklüğü alınıp miktar aşağı yuvarlanır
  (`floor_to_step`) — yuvarlanmamış miktar Binance tarafından reddedilebilir.

- **backtest.py'de işlem ücreti:** Her AL/SAT işleminde `TRADING_FEE_PERCENT`
  (varsayılan %0,1) kesiliyor — gerçekçi getiri için önemli, önceki sürümde
  yoktu.

**Veri çekme:** `KLINE_INTERVAL=15m` mumlar, `POLL_INTERVAL_SECONDS=60` ile
kontrol ediliyor. **Not:** Canlı bot her `POLL_INTERVAL_SECONDS`'de bir fiyatı
okuyup değerlendiriyor — tick/websocket bazlı anlık takip yok, iki kontrol
arasındaki ani bir hareketi (özellikle stop-loss'u) kaçırabilir.

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
STOP_LOSS_PERCENT=3
TAKE_PROFIT_PERCENT=6
MAX_DAILY_LOSS_PERCENT=2
EMERGENCY_STOP_FILE=STOP_BOT
POLL_INTERVAL_SECONDS=60
KLINE_INTERVAL=15m
RSI_BUY_THRESHOLD=30
RSI_SELL_THRESHOLD=70
EMA_TREND_SHORT=50
EMA_TREND_LONG=200
EMA_SLOPE_LOOKBACK=5
ATR_PERIOD=14
ATR_STOP_MULTIPLIER=2.0
ATR_TAKE_PROFIT_MULTIPLIER=3.0
VOLUME_PERIOD=20
VOLUME_MULTIPLIER=1.20
BINANCE_API_KEY=
BINANCE_API_SECRET=
CONFIRM_REAL_MONEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
BACKTEST_DAYS=30
BACKTEST_START_CAPITAL=1000
TRADING_FEE_PERCENT=0.1
```

**Önemli — sızan anahtar geçmişi:** Bu proje sırasında kullanıcı bir ara
gerçek `BINANCE_API_KEY`/`BINANCE_API_SECRET` değerlerini içeren bir `.env`
dosyasını sohbete yükledi. O anahtarlar repoya **hiç yazılmadı**, ama
kullanıcıya Binance'te o anahtarı iptal edip yenisini oluşturması söylendi.
Codex/bir sonraki oturum bu konuyu tekrar gündeme getirmemeli; sadece bilgi
amaçlı not düşülüyor.

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

## Değişiklik geçmişi

**2. iterasyon:** Kullanıcı ilk sürümü (sadece RSI+EMA trend filtresi, sabit
stop yok) 30/90/180 günlük gerçek Binance verisiyle test etti, üç dönemde de
zararlı/al-tut'tan kötü sonuç buldu. ATR tabanlı stop/hedef, hacim teyidi,
EMA eğim filtresi eklendi (bu sürümde uzun EMA üzerinden).

**3. iterasyon (mevcut):** Kullanıcı projeyi ayrı bir AI aracına (Codex) veya
oturuma götürüp bağımsız bir güvenlik/mantık denetimi yaptırdı
(`AUDIT_REPORT.md`), sonucu buraya (bu repoya) geri getirdi. Codex'in
düzeltmeleri: sadece kapanmış mumlarla sinyal + aynı mumda tekrar işlem
engeli, kısmi-satış tutarsızlığının giderilmesi (artık tam pozisyon
kapatılıyor), LOT_SIZE yuvarlama, günlük zarar limiti, acil durdurma dosyası,
backtest'e işlem ücreti eklenmesi, `TRADE_PERCENT`'in kod seviyesinde %33 ile
sınırlanması. Bu iterasyonun kendi backtest sonucu (`AUDIT_REPORT.md`):
SHIBUSDT 15dk, son 30 gün, 26 işlem, strateji **-%0,60**, al-tut **+%1,60** —
**hâlâ canlıya geçmek için yeterli değil**, `MODE=dry_run` ile teslim edildi.

**Kullanıcının hedefi:** "Az da olsa sürekli kâr" — yani yüksek kazanma
oranı + düşük varyans önemli, tek seferlik büyük getiri değil. Backtest
değerlendirirken toplam getiriye ek olarak **kazanma oranına** ve **işlem
başına ortalama kâra** bakılmalı.

**4. iterasyon:** Kullanıcı hedefini netleştirdi — kayıp telafisi değil
(3 yıldır SHIB tutuyor, $15k'dan $2k'ya düşmüş ama uzun vadeli tutmaya
devam edecek), **dolar değil TOKEN ADEDİ** artırmak istiyor. Bunun üzerine:

1. `backtest.py`'ye `START_IN_SHIB=true` modu eklendi (USDT yerine SHIB ile
   başlar, "token adedi değişimi" raporlar). **Gerçek SHIBUSDT 30 günlük
   veriyle test edildi: token adedi %4,67 AZALDI** (RSI+EMA trend botu,
   token biriktirme hedefi için uygun değil — sattıktan sonra fiyat
   beklenen geri çekilmeyi yapmadan yükselirse, geri alım daha yüksek
   fiyattan oluyor, dolar kârı olsa bile token kaybı).
2. Bunun üzerine **tamamen farklı bir strateji tipi** eklendi:
   `grid_bot.py` + `grid_backtest.py` — RSI/trend'e hiç bakmayan, sadece
   referans fiyattan %X düşünce AL / o lot'un girişinden %Y yükselince SAT
   mantığıyla çalışan salınım (grid) botu. `trade_bot.py`'nin mod
   sistemini, acil durdurmasını, sipariş/LOT_SIZE altyapısını import ederek
   kullanır (kod tekrarı yok).

**Grid stratejisinin doğrulanmış davranışı (sentetik veriyle test edildi,
gerçek veriyle henüz değil):**
- Doğru çalıştığı doğrulandı: **gerçekten net-sıfır-trendli (net trendsiz,
  başladığı fiyata dönen) bir piyasada token adedini artırıyor**
  (test: +%2,80, matematiksel olarak beklenen değerle birebir örtüşüyor).
- **Önemli/genel matematiksel bulgu:** Fiyatta herhangi bir kalıcı net
  yön (yukarı VEYA aşağı) varsa, "token-eşdeğeri-şu anki fiyattan" metriği
  düşer — bu grid'e özgü bir kusur değil, **her "yüksekten sat, düşükten
  al" yaklaşımının doğasında var**: satıştan sonra nakitte beklerken fiyat
  yükselmeye devam ederse, geri alım daha yüksek fiyattan olur. Sentetik
  güçlü-yükseliş-trendinde test edildiğinde token adedi düştü (-%71, -%15
  gibi aşırı senaryolar dahil — bunlar uç örnekler, gerçekçi değil ama
  mekanizmayı göstermek için kullanıldı).
- **Sonuç:** Grid botu, SHIB gerçekten yatay/dalgalı seyrederse işe
  yarayabilir; güçlü tek yönlü bir trend varsa (yukarı da olsa) basit
  tutmaktan token cinsinden geride kalır.

**Gerçek SHIBUSDT verisiyle grid testi (kullanıcı çalıştırdı):**
- İlk denemede (`GRID_STEP=%3`, 30 gün) **0 işlem** çıktı — gerçek bir bug
  bulundu: `START_IN_SHIB=true` modunda başlangıç SHIB'i satılabilir bir
  "lot" olarak kaydedilmiyordu, bu yüzden bot hiç satış yapamıyor, dolayısıyla
  hiç USDT'si olmuyor, dolayısıyla hiç alım da yapamıyordu — tamamen
  hareketsiz kalıyordu. **Düzeltildi**: başlangıç pozisyonu artık normal bir
  lot gibi kaydediliyor.
- Düzeltme sonrası, adım daraltıldıkça (%3→%1,5→%1) hem işlem sayısı arttı
  hem token kaybı küçüldü: 30 gün %1 adımla **-%1,22**, ama **90 günde
  +%12,55, 180 günde +%16,91** — token adedi gerçekten artmış. Trend botunun
  aynı 30 günlük sonucundan (-%4,67) çok daha iyi.
- **Önemli çekince:** "%100 kazanma oranı" yanıltıcı olabilir — grid'in
  doğası geregi kayıp bir lot hiç satılmıyor, açık kalıyor (180 günlük
  testte 7 açık lot vardı). Token hesabı bunları o anki fiyattan dahil
  ediyor (dürüst), ama gerçek risk şu: **grid lotlarında stop-loss yok**,
  fiyat bir daha o lotun +%1 hedefine hiç ulaşmazsa sermaye süresiz
  kilitli kalır. Kullanıcının SHIB'te yaşadığı büyük düşüş geçmişi
  düşünülürse bu ciddi bir risk.

**5. iterasyon:** Kullanıcı "hangisi tutarlı, bot kendini rejime göre mi
yenilesin" diye sordu — bunun üzerine **hibrit (rejim-uyarlamalı) bot**
eklendi: `adaptive_bot.py` + `adaptive_backtest.py`. Kaufman Verimlilik
Oranı (`verimlilik_orani()`, `trade_bot.py`'ye eklendi — son
`REGIME_LOOKBACK` mumda net hareket/toplam zigzag oranı, 0-1 arası, 1'e
yakın=trend, 0'a yakın=yatay) ile piyasa rejimini ölçüp TREND rejiminde
`trade_bot.py` mantığıyla, RANGE rejiminde `grid_bot.py` mantığıyla giriş
yapıyor. Açık pozisyonlar hangi mantıkla açıldıysa o mantıkla yönetiliyor
(rejim ortasında değişse bile pozisyon aniden terk edilmiyor).
`adaptive_backtest.py`, SAF-TREND/SAF-GRID/HİBRİT'i **aynı veri üzerinde
aynı anda** çalıştırıp yan yana karşılaştırıyor. Sentetik veriyle
doğrulandı (rejim algılama doğru çalışıyor, hibrit trend fazında saf-trend
ile birebir aynı kararları veriyor) — **gerçek SHIBUSDT verisiyle henüz
test edilmedi**, bir sonraki adım bu.

## Bilinen sınırlamalar / dürüst notlar

- Çoklu coin takibi, işlem geçmişi/performans dashboard'u gibi genişletmeler
  henüz **eklenmedi**.
- **EMA eğim filtresi davranışı** (yukarıda "Karar kuralları"nda detaylı):
  kısa EMA'nın yükseliyor olması şartı, keskin/hızlı dip'lerde RSI<30 ile
  aynı anda sağlanamayabilir — sentetik testlerde net gördüm. Bu, botun
  agresif "düşen bıçağı yakalama" yapmasını engelliyor (iyi), ama bazı
  gerçek dip fırsatlarını da kaçırabilir (potansiyel dezavantaj). Gerçek
  piyasa verisiyle ne sıklıkla tetiklendiği `backtest.py` ile ölçülmeli.
- Backtest'teki EMA hesaplaması, geçmiş verinin tamamından tek seferde
  seed alınarak sürekli hesaplanıyor; canlı bot her `get_candles()`
  çağrısında son `limit=300` mumluk pencereden yeniden seed alıyor — uzun
  vadede ihmal edilebilir bir fark yaratır ama backtest sonucu ile canlı
  botun EMA'sı milimetrik olarak aynı olmayabilir.
- Canlı bot, stop-loss'u sadece her `POLL_INTERVAL_SECONDS` (varsayılan 60sn)
  kontrolünde fiyatı okuyup değerlendiriyor — tick-bazlı/websocket anlık takip
  yok, iki kontrol arasında olabilecek ani bir fiyat hareketini kaçırabilir.
  Backtest ise ATR/RSI/hacim hesaplarını kapanışa göre yapıyor (stop/kâr
  hedefi kontrolü mum-kapanışı bazlı, intrabar high/low kontrolü yok) —
  canlı botla birebir aynı fill mantığı, ama ikisi de gerçek intrabar
  hareketleri kaçırabilir.
- ATR/hacim/eğim parametreleri (`ATR_STOP_MULTIPLIER`, `VOLUME_MULTIPLIER`
  vb.) hiç optimize edilmedi, varsayılan/tipik değerler kullanıldı — walk
  forward test veya parametre taraması henüz yapılmadı.
- `get_klines()` fonksiyonu artık `run_once()` tarafından kullanılmıyor
  (yerine `get_candles()` geçti) ama dosyada duruyor — ölü kod, zararsız.
- Bu proje **yatırım tavsiyesi değildir**; basit teknik göstergelere
  dayanır, yanlış sinyal riski yüksektir. Gerçek paraya geçmeden önce
  backtest + testnet ile uzunca test edilmesi öneriliyor.

## Repo / branch bilgisi

- Repo: `Akif78000/mobil-borsa`
- Aktif geliştirme branch'i: `claude/binance-giro-shiba-trade-xasi2t`
  (henüz `main`'e merge edilmedi / PR açılmadı)
