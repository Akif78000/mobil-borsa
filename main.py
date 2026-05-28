import streamlit as st
import urllib.request
import json
import time
import ssl

st.set_page_config(page_title="Borsa Tarayıcı", page_icon="📊", layout="centered")

st.title("📊 Yapay Zeka Destekli Borsa Tarayıcı")
st.caption("BIST hisseleri için sonuna .IS ekleyin (Örn: THYAO.IS). Kriptolar için: BTC-USD, SHIB-USD")

def format_fiyat(fiyat):
    if fiyat is None: return "0.00"
    if fiyat < 0.01: return f"{fiyat:.8f}"
    elif fiyat < 1: return f"{fiyat:.4f}"
    return f"{fiyat:.2f}"

def rsi_hesapla(fiyat_listesi):
    if len(fiyat_listesi) < 15: return 50
    kayiplar, kazanclar = [], []
    for i in range(1, len(fiyat_listesi)):
        fark = fiyat_listesi[i] - fiyat_listesi[i-1]
        if fark > 0:
            kazanclar.append(fark)
            kayiplar.append(0)
        else:
            kazanclar.append(0)
            kayiplar.append(abs(fark))
    son_14_kazanc = sum(kazanclar[-14:]) / 14
    son_14_kayip = sum(kayiplar[-14:]) / 14
    if son_14_kayip == 0: return 100
    return 100 - (100 / (1 + (son_14_kazanc / son_14_kayip)))

ticker = st.text_input("Varlık Kodu Girin:", value="THYAO.IS").strip().upper()

if st.button("ANALİZ ET", type="primary"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=2mo&interval=1d"
    
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        
        with urllib.request.urlopen(req, context=ctx) as response:
            data = json.loads(response.read().decode())
            
        chart_data = data['chart']['result'][0]
        kapanislar = chart_data['indicators']['quote'][0]['close']
        zamanlar = chart_data['timestamp']
        fiyatlar = [x for x in kapanislar if x is not None]
        
        if len(fiyatlar) < 20:
            st.error("Yetersiz veri geçmişi!")
        else:
            son_fiyat = fiyatlar[-1]
            guncel_rsi = rsi_hesapla(fiyatlar)
            
            col1, col2 = st.columns(2)
            col1.metric("Son Fiyat", format_fiyat(son_fiyat))
            col2.metric("Güncel RSI", f"{guncel_rsi:.2f}")
            
            if guncel_rsi > 70:
                st.warning("⚠️ AŞIRI ALIM BÖLGESİ! Fiyat teknik tepeye yakın. Yeni alım riskli olabilir, SATIŞ kollanabilir.")
            elif guncel_rsi < 30:
                st.success("✅ AŞIRI SATIM BÖLGESİ! Fiyat oldukça ucuzlamış. Kademeli ALIM düşünülebilir.")
            else:
                st.info("🔄 NÖTR BÖLGE: Fiyat dengeli bantta gidiyor. Mevcut pozisyonu koruyup bekleyin.")
                
            st.subheader("📅 Son 10 Günlük Trend ve Sinyaller")
            
            tablo_verisi = []
            for i in range(-10, 0):
                f = fiyatlar[i]
                o_gunkuy_gecmis = fiyatlar[:len(fiyatlar)+i+1] if i != -1 else fiyatlar
                o_gunku_rsi = rsi_hesapla(o_gunkuy_gecmis)
                
                if o_gunku_rsi > 68: sinyal = "🔴 [SAT]"
                elif o_gunku_rsi < 32: sinyal = "🟢 [AL]"
                else: sinyal = "⚪ [BEKLE]"
                
                tarih_str = time.strftime('%d/%m/%Y', time.localtime(zamanlar[len(fiyatlar)+i]))
                tablo_verisi.append({"Tarih": tarih_str, "Fiyat": format_fiyat(f), "Sinyal": sinyal})
                
            st.table(tablo_verisi)
            
    except Exception as e:
        st.error(f"Hata oluştu! Kodun doğruluğunu kontrol edin. (Hata: {e})")
      
