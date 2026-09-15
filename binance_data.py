"""Binance genel (public) piyasa verisi ve teknik gösterge yardımcıları.

Bu modüldeki fonksiyonlar kimlik doğrulama gerektirmez; hem Streamlit
panelinde (main.py) hem de otomatik işlem botunda (trading_bot.py)
ortak kullanılır.
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Tuple

import pandas as pd

BINANCE_BASE_URL = "https://api.binance.com"
RSI_PERIYODU = 14
RSI_ASIRI_ALIM = 70
RSI_ASIRI_SATIM = 30

# Piyasa değeri büyük, likiditesi yüksek, köklü coinler - meme/mikro-cap
# coinlere göre ani ve aşırı oynaklık riski daha düşük.
POPULER_COINLER = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT", "LTCUSDT",
]


def _hata_mesaji_ayikla(govde: bytes) -> str:
    try:
        return json.loads(govde.decode()).get("msg", "geçersiz istek")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "geçersiz istek"


def binance_klines_getir(sembol: str, gun_sayisi: int, interval: str = "1d") -> pd.DataFrame:
    """Binance'ın herkese açık klines uç noktası; API anahtarı gerekmez."""
    parametreler = urllib.parse.urlencode(
        {"symbol": sembol, "interval": interval, "limit": gun_sayisi}
    )
    istek = urllib.request.Request(
        f"{BINANCE_BASE_URL}/api/v3/klines?{parametreler}",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    try:
        with urllib.request.urlopen(istek, timeout=10) as yanit:
            veri_json = json.loads(yanit.read().decode())
    except urllib.error.HTTPError as e:
        # Binance geçersiz sembol/parametre hatalarını 4xx durum koduyla
        # döndürür; urllib bunu istisna olarak fırlatır (normal yanıt değil).
        raise ValueError(f"Binance hatası: {_hata_mesaji_ayikla(e.read())}") from e
    except urllib.error.URLError as e:
        raise ValueError(f"Binance'a bağlanılamadı: {e.reason}") from e

    if isinstance(veri_json, dict):
        raise ValueError(f"Binance hatası: {veri_json.get('msg', 'geçersiz sembol')}")
    if not veri_json:
        raise ValueError("Bu sembol için veri bulunamadı.")

    kolonlar = [
        "Open time", "Open", "High", "Low", "Close", "Volume",
        "Close time", "Quote volume", "Trades", "Taker buy base",
        "Taker buy quote", "Ignore",
    ]
    veri = pd.DataFrame(veri_json, columns=kolonlar)
    veri["Date"] = pd.to_datetime(veri["Open time"], unit="ms")
    veri = veri.set_index("Date")
    for kolon in ["Open", "High", "Low", "Close", "Volume"]:
        veri[kolon] = veri[kolon].astype(float)
    return veri[["Open", "High", "Low", "Close", "Volume"]]


def rsi_hesapla(kapanislar: pd.Series, periyot: int = RSI_PERIYODU) -> pd.Series:
    """Wilder'in üstel düzeltmeli RSI yöntemiyle hesaplama (standart tanıma uygun)."""
    fark = kapanislar.diff()
    kazanc = fark.clip(lower=0)
    kayip = -fark.clip(upper=0)
    ort_kazanc = kazanc.ewm(alpha=1 / periyot, min_periods=periyot, adjust=False).mean()
    ort_kayip = kayip.ewm(alpha=1 / periyot, min_periods=periyot, adjust=False).mean()
    rs = ort_kazanc / ort_kayip.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    # Kayıp sıfırsa (ör. kesintisiz yükseliş) bölme NaN üretir; doğru RSI
    # değeri o durumda 100'dür (hiç düşüş yok), tamamen düz seyirde ise 50.
    rsi = rsi.mask((ort_kayip == 0) & (ort_kazanc > 0), 100.0)
    rsi = rsi.mask((ort_kayip == 0) & (ort_kazanc == 0), 50.0)
    return rsi.fillna(50)


def sinyal_uret(rsi_degeri: float) -> Tuple[str, str]:
    """Tüm uygulamalarda tek ve tutarlı eşik mantığını kullanan sinyal üretici."""
    if rsi_degeri is None or pd.isna(rsi_degeri):
        return "⚪ [BEKLE]", "info"
    if rsi_degeri > RSI_ASIRI_ALIM:
        return "🔴 [SAT]", "warning"
    if rsi_degeri < RSI_ASIRI_SATIM:
        return "🟢 [AL]", "success"
    return "⚪ [BEKLE]", "info"


def gosterge_ekle(veri: pd.DataFrame) -> pd.DataFrame:
    """RSI ve trend (SMA20/50) kolonlarını ekleyip döndürür."""
    veri = veri.copy()
    veri["RSI"] = rsi_hesapla(veri["Close"])
    veri["SMA20"] = veri["Close"].rolling(20, min_periods=1).mean()
    veri["SMA50"] = veri["Close"].rolling(50, min_periods=1).mean()
    return veri


def format_fiyat(fiyat: float) -> str:
    if fiyat is None or pd.isna(fiyat):
        return "0.00"
    if fiyat < 0.01:
        return f"{fiyat:.8f}"
    if fiyat < 1:
        return f"{fiyat:.4f}"
    return f"{fiyat:.2f}"
