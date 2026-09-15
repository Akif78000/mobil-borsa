import json
import re
import urllib.parse
import urllib.request
from typing import Tuple

import altair as alt
import pandas as pd
import streamlit as st
import yfinance as yf

st.set_page_config(page_title="Borsa Tarayıcı", page_icon="📊", layout="centered")

RSI_PERIYODU = 14
RSI_ASIRI_ALIM = 70
RSI_ASIRI_SATIM = 30
TICKER_DESENI = re.compile(r"^[A-Z0-9.]{1,15}$")
HIZLI_SECIMLER = ["SHIBUSDT", "BTCUSDT", "ETHUSDT", "THYAO.IS"]
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
BINANCE_GUN_LIMITI = {"1mo": 30, "3mo": 90, "6mo": 180, "1y": 365}

st.title("📊 Yapay Zeka Destekli Borsa Tarayıcı")
st.caption(
    "Kripto için Binance sembolü girin (Örn: SHIBUSDT, BTCUSDT). "
    "BIST hisseleri için sonuna .IS ekleyin (Örn: THYAO.IS)."
)


def format_fiyat(fiyat: float) -> str:
    if fiyat is None or pd.isna(fiyat):
        return "0.00"
    if fiyat < 0.01:
        return f"{fiyat:.8f}"
    if fiyat < 1:
        return f"{fiyat:.4f}"
    return f"{fiyat:.2f}"


def rsi_hesapla(kapanislar: pd.Series, periyot: int = RSI_PERIYODU) -> pd.Series:
    """Wilder'in üstel düzeltmeli RSI yöntemiyle hesaplama (standart tanıma uygun)."""
    fark = kapanislar.diff()
    kazanc = fark.clip(lower=0)
    kayip = -fark.clip(upper=0)
    ort_kazanc = kazanc.ewm(alpha=1 / periyot, min_periods=periyot, adjust=False).mean()
    ort_kayip = kayip.ewm(alpha=1 / periyot, min_periods=periyot, adjust=False).mean()
    rs = ort_kazanc / ort_kayip.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)


def sinyal_uret(rsi_degeri: float) -> Tuple[str, str]:
    """Tüm arayüzde tek ve tutarlı eşik mantığını kullanan sinyal üretici."""
    if rsi_degeri is None or pd.isna(rsi_degeri):
        return "⚪ [BEKLE]", "info"
    if rsi_degeri > RSI_ASIRI_ALIM:
        return "🔴 [SAT]", "warning"
    if rsi_degeri < RSI_ASIRI_SATIM:
        return "🟢 [AL]", "success"
    return "⚪ [BEKLE]", "info"


def _binance_klines_getir(sembol: str, gun_sayisi: int) -> pd.DataFrame:
    """Binance genel (public) piyasa verisi uç noktası; API anahtarı gerekmez."""
    parametreler = urllib.parse.urlencode(
        {"symbol": sembol, "interval": "1d", "limit": gun_sayisi}
    )
    istek = urllib.request.Request(
        f"{BINANCE_KLINES_URL}?{parametreler}", headers={"User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(istek, timeout=10) as yanit:
        veri_json = json.loads(yanit.read().decode())

    if isinstance(veri_json, dict):
        raise ValueError(f"Binance hatası: {veri_json.get('msg', 'geçersiz sembol')}")
    if not veri_json:
        raise ValueError("Bu sembol için Binance'ta veri bulunamadı.")

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


@st.cache_data(ttl=300, show_spinner="Veri çekiliyor...")
def veri_getir(ticker: str, periyot: str) -> pd.DataFrame:
    if ticker.endswith(".IS"):
        veri = yf.Ticker(ticker).history(period=periyot, interval="1d", auto_adjust=True)
    else:
        gun_sayisi = BINANCE_GUN_LIMITI[periyot]
        veri = _binance_klines_getir(ticker, gun_sayisi)

    if veri.empty:
        raise ValueError("Bu kod için veri bulunamadı. Kodun doğruluğunu kontrol edin.")
    return veri


def basit_rsi_backtest(veri: pd.DataFrame) -> dict:
    """RSI eşik geçişlerine dayalı basit AL/SAT stratejisinin geçmiş test simülasyonu."""
    islem_getirileri = []
    pozisyonda = False
    giris_fiyati = 0.0

    for fiyat, rsi_degeri in zip(veri["Close"], veri["RSI"]):
        if pd.isna(rsi_degeri):
            continue
        if not pozisyonda and rsi_degeri < RSI_ASIRI_SATIM:
            pozisyonda = True
            giris_fiyati = fiyat
        elif pozisyonda and rsi_degeri > RSI_ASIRI_ALIM:
            islem_getirileri.append((fiyat - giris_fiyati) / giris_fiyati * 100)
            pozisyonda = False

    acik_pozisyon_getirisi = None
    if pozisyonda:
        acik_pozisyon_getirisi = (veri["Close"].iloc[-1] - giris_fiyati) / giris_fiyati * 100

    al_tut_getirisi = (veri["Close"].iloc[-1] - veri["Close"].iloc[0]) / veri["Close"].iloc[0] * 100
    kazanan_islem = sum(1 for getiri in islem_getirileri if getiri > 0)

    return {
        "islem_sayisi": len(islem_getirileri),
        "kazanan_islem": kazanan_islem,
        "toplam_strateji_getirisi": sum(islem_getirileri),
        "acik_pozisyon_getirisi": acik_pozisyon_getirisi,
        "al_tut_getirisi": al_tut_getirisi,
    }


if "ticker_input" not in st.session_state:
    st.session_state["ticker_input"] = "SHIBUSDT"

st.write("**Hızlı Seçim:**")
hizli_secim_kolonlari = st.columns(len(HIZLI_SECIMLER))
for kolon, sembol in zip(hizli_secim_kolonlari, HIZLI_SECIMLER):
    if kolon.button(sembol):
        st.session_state["ticker_input"] = sembol

ticker = st.text_input("Varlık Kodu Girin:", key="ticker_input").strip().upper()
periyot = st.selectbox("Zaman Aralığı", ["1mo", "3mo", "6mo", "1y"], index=1)

if st.button("ANALİZ ET", type="primary"):
    if not TICKER_DESENI.match(ticker):
        st.error("Geçersiz varlık kodu. Sadece harf, rakam ve nokta kullanın (Örn: SHIBUSDT).")
    else:
        try:
            veri = veri_getir(ticker, periyot)
        except ValueError as e:
            st.error(str(e))
        except Exception as e:
            st.error(
                f"Veri alınırken bir sorun oluştu, lütfen daha sonra tekrar deneyin. "
                f"(Detay: {type(e).__name__})"
            )
        else:
            if len(veri) < RSI_PERIYODU + 5:
                st.error("Yetersiz veri geçmişi! Daha uzun bir zaman aralığı seçin.")
            else:
                veri = veri.copy()
                veri["RSI"] = rsi_hesapla(veri["Close"])
                veri["SMA20"] = veri["Close"].rolling(20, min_periods=1).mean()
                veri["SMA50"] = veri["Close"].rolling(50, min_periods=1).mean()

                son_fiyat = veri["Close"].iloc[-1]
                guncel_rsi = veri["RSI"].iloc[-1]
                trend = "YÜKSELİŞ 📈" if veri["SMA20"].iloc[-1] > veri["SMA50"].iloc[-1] else "DÜŞÜŞ 📉"

                col1, col2, col3 = st.columns(3)
                col1.metric("Son Fiyat", format_fiyat(son_fiyat))
                col2.metric("Güncel RSI", f"{guncel_rsi:.2f}")
                col3.metric("Trend (SMA20/50)", trend)

                _, seviye = sinyal_uret(guncel_rsi)
                mesajlar = {
                    "warning": "⚠️ AŞIRI ALIM BÖLGESİ! Fiyat teknik tepeye yakın, yeni alım riskli olabilir.",
                    "success": "✅ AŞIRI SATIM BÖLGESİ! Fiyat oldukça ucuzlamış, kademeli alım düşünülebilir.",
                    "info": "🔄 NÖTR BÖLGE: Fiyat dengeli bantta gidiyor.",
                }
                getattr(st, seviye)(mesajlar[seviye])

                st.subheader("📈 Fiyat ve Hareketli Ortalamalar")
                st.line_chart(veri[["Close", "SMA20", "SMA50"]])

                st.subheader("📊 RSI Göstergesi")
                idx_adi = veri.index.name or "Date"
                rsi_df = veri.reset_index()[[idx_adi, "RSI"]]
                rsi_df.columns = ["Tarih", "RSI"]
                rsi_cizgi = alt.Chart(rsi_df).mark_line(color="#1f77b4").encode(
                    x="Tarih:T", y=alt.Y("RSI:Q", scale=alt.Scale(domain=[0, 100]))
                )
                asiri_alim_cizgi = alt.Chart(pd.DataFrame({"y": [RSI_ASIRI_ALIM]})).mark_rule(
                    color="red", strokeDash=[4, 4]
                ).encode(y="y")
                asiri_satim_cizgi = alt.Chart(pd.DataFrame({"y": [RSI_ASIRI_SATIM]})).mark_rule(
                    color="green", strokeDash=[4, 4]
                ).encode(y="y")
                st.altair_chart(rsi_cizgi + asiri_alim_cizgi + asiri_satim_cizgi, use_container_width=True)

                st.subheader("📅 Son 10 Günlük Trend ve Sinyaller")
                tablo_verisi = []
                for tarih, satir in veri.tail(10).iterrows():
                    etiket, _ = sinyal_uret(satir["RSI"])
                    tablo_verisi.append(
                        {
                            "Tarih": tarih.strftime("%d/%m/%Y"),
                            "Fiyat": format_fiyat(satir["Close"]),
                            "RSI": f"{satir['RSI']:.1f}",
                            "Sinyal": etiket,
                        }
                    )
                st.table(tablo_verisi)

                st.subheader("🧪 Strateji Geçmiş Testi (Backtest)")
                sonuc = basit_rsi_backtest(veri)
                bcol1, bcol2, bcol3 = st.columns(3)
                bcol1.metric("Toplam İşlem", sonuc["islem_sayisi"])
                kazanma_orani = (
                    f"%{(sonuc['kazanan_islem'] / sonuc['islem_sayisi'] * 100):.0f}"
                    if sonuc["islem_sayisi"]
                    else "—"
                )
                bcol2.metric("Kazanma Oranı", kazanma_orani)
                bcol3.metric("Strateji Toplam Getiri", f"%{sonuc['toplam_strateji_getirisi']:.1f}")
                st.caption(f"Aynı dönemde Al-ve-Tut getirisi: %{sonuc['al_tut_getirisi']:.1f}")
                if sonuc["acik_pozisyon_getirisi"] is not None:
                    st.caption(
                        f"Şu anda simülasyonda açık pozisyon var, güncel getirisi: "
                        f"%{sonuc['acik_pozisyon_getirisi']:.1f}"
                    )
                st.caption(
                    "Bu sonuçlar tamamen geçmiş veriye dayalı bir simülasyondur; "
                    "işlem maliyetlerini/kaymayı içermez ve gelecekteki performansı garanti etmez."
                )

st.markdown("---")
st.caption(
    """
⚠️ **YASAL UYARI:** Bu uygulamada yer alan tüm bilgiler, grafikler, al-sat sinyalleri ve
geçmiş test (backtest) sonuçları tamamen teknik göstergeler (RSI, hareketli ortalama) baz
alınarak otomatik hesaplanmaktadır ve **kesinlikle yatırım danışmanlığı kapsamında değildir.**
Geçmiş performans gelecekteki kazancı garanti etmez; özellikle SHIB gibi yüksek oynaklığa sahip
kripto varlıklarda sermayenizin tamamını kaybedebilirsiniz. Burada yer alan yorumlar
doğrultusunda yapılacak işlemler sonucunda oluşabilecek zararlardan bu yazılım ve geliştiricisi
sorumlu tutulamaz. Güzel kızıma ithafen yapılmıştır. GRA
"""
)
