"""Binance üzerinde RSI + trend stratejisiyle otomatik AL/SAT işlemi yapan bot.

ÖNEMLİ - ÇALIŞTIRMADAN ÖNCE OKU:
    Bu bot varsayılan olarak GERÇEK PARA KULLANMAZ. İki ayrı güvenlik
    katmanı vardır ve ikisi de aşılmadan gerçek emir gönderilmez:

      1) DRY_RUN=true (varsayılan): Hiçbir borsa emri gönderilmez.
         Gerçek piyasa fiyatlarıyla, yerel bir sanal bakiye üzerinden
         (varsayılan 1000 USDT) kağıt üzerinde (paper trading) işlem
         simüle edilir. API anahtarı gerekmez.

      2) DRY_RUN=false yapıldığında bile, varsayılan olarak Binance
         SPOT TESTNET (testnet.binance.vision) kullanılır — burada
         "gerçek" sanılan ama değersiz test parası vardır. Gerçek
         mainnet'te (api.binance.com) gerçek parayla işlem açması için
         AŞAĞIDAKİLERİN HEPSİ birden sağlanmalı:
           DRY_RUN=false
           USE_TESTNET=false
           ONAY_GERCEK_PARA=EVET_RISKI_ANLADIM_VE_ONAYLIYORUM

    Bu üçlü kilit olmadan bot mainnet'te gerçek emir GÖNDEREMEZ.

Ortam değişkenleri (hepsi opsiyonel, varsayılanlar güvenli taraftadır):
    BINANCE_API_KEY, BINANCE_API_SECRET   Testnet veya mainnet API anahtarı
                                            (DRY_RUN=true iken gerekmez)
    DRY_RUN                                "true" (varsayılan) / "false"
    USE_TESTNET                            "true" (varsayılan) / "false"
    ONAY_GERCEK_PARA                       Mainnet+canlı mod için zorunlu ibare
    WATCHLIST                              Virgülle ayrılmış semboller
                                            (varsayılan: POPULER_COINLER)
    TRADE_AMOUNT_USDT                      İşlem başına USDT miktarı (varsayılan 10)
    MAX_POSITIONS                          Eşzamanlı en fazla açık pozisyon (varsayılan 3)
    TRADE_SERMAYE_ORANI                    Toplam bakiyenin en fazla ne kadarı
                                            pozisyonlarda olabilir, 0-1 arası
                                            (varsayılan 1.0 = sınırsız; 0.5 =
                                            bakiyenin yarısı hep nakit kalır)
    STOP_LOSS_YUZDE                        Zarar-durdur yüzdesi (varsayılan 5)
    TAKE_PROFIT_YUZDE                      Kâr-al yüzdesi (varsayılan 8)
    MAX_GUNLUK_ZARAR_YUZDE                 Günlük zarar tavanı (varsayılan 5)
    TARAMA_ARALIGI_SANIYE                  Döngü periyodu (varsayılan 900 = 15 dk)

Kullanım:
    python trading_bot.py            # sürekli döngü
    python trading_bot.py --once     # tek tur çalışıp çıkar (test/cron için)

UYARI: Bu araç yatırım danışmanlığı değildir; geçmiş veriye dayalı basit bir
teknik strateji uygular ve kâr garantisi vermez. Mainnet'te gerçek parayla
çalıştırmadan önce mutlaka uzun süre testnet/dry-run modunda izleyin.
"""
import argparse
import hashlib
import hmac
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Optional

from binance_data import (
    POPULER_COINLER,
    RSI_ASIRI_ALIM,
    RSI_ASIRI_SATIM,
    binance_klines_getir,
    gosterge_ekle,
)

DURUM_DOSYASI = Path("trading_bot_state.json")
MAINNET_URL = "https://api.binance.com"
TESTNET_URL = "https://testnet.binance.vision"
ONAY_IBARESI = "EVET_RISKI_ANLADIM_VE_ONAYLIYORUM"
SANAL_BASLANGIC_BAKIYE = 1000.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("trading_bot.log")],
)
log = logging.getLogger("trading_bot")


def _env_bool(ad: str, varsayilan: bool) -> bool:
    deger = os.environ.get(ad)
    if deger is None:
        return varsayilan
    return deger.strip().lower() in ("1", "true", "evet", "yes")


def _env_float(ad: str, varsayilan: float) -> float:
    try:
        return float(os.environ.get(ad, varsayilan))
    except ValueError:
        return varsayilan


def _env_int(ad: str, varsayilan: int) -> int:
    try:
        return int(os.environ.get(ad, varsayilan))
    except ValueError:
        return varsayilan


@dataclass
class BotAyarlari:
    dry_run: bool = field(default_factory=lambda: _env_bool("DRY_RUN", True))
    use_testnet: bool = field(default_factory=lambda: _env_bool("USE_TESTNET", True))
    api_key: str = field(default_factory=lambda: os.environ.get("BINANCE_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.environ.get("BINANCE_API_SECRET", ""))
    watchlist: list = field(
        default_factory=lambda: [
            s.strip().upper()
            for s in os.environ.get("WATCHLIST", ",".join(POPULER_COINLER)).split(",")
            if s.strip()
        ]
    )
    trade_amount_usdt: float = field(default_factory=lambda: _env_float("TRADE_AMOUNT_USDT", 10))
    max_positions: int = field(default_factory=lambda: _env_int("MAX_POSITIONS", 3))
    sermaye_orani: float = field(
        default_factory=lambda: min(max(_env_float("TRADE_SERMAYE_ORANI", 1.0), 0.0), 1.0)
    )
    stop_loss_yuzde: float = field(default_factory=lambda: _env_float("STOP_LOSS_YUZDE", 5))
    take_profit_yuzde: float = field(default_factory=lambda: _env_float("TAKE_PROFIT_YUZDE", 8))
    max_gunluk_zarar_yuzde: float = field(
        default_factory=lambda: _env_float("MAX_GUNLUK_ZARAR_YUZDE", 5)
    )
    tarama_araligi_saniye: int = field(
        default_factory=lambda: _env_int("TARAMA_ARALIGI_SANIYE", 900)
    )

    @property
    def canli_mod(self) -> bool:
        return not self.dry_run

    @property
    def base_url(self) -> str:
        return TESTNET_URL if self.use_testnet else MAINNET_URL

    def dogrula(self) -> None:
        if self.dry_run:
            return  # dry-run her zaman güvenli, ek onay gerekmez
        if not self.use_testnet:
            onay = os.environ.get("ONAY_GERCEK_PARA", "")
            if onay != ONAY_IBARESI:
                log.error(
                    "GERÇEK PARA MODU ENGELLENDİ: Mainnet'te DRY_RUN=false ile çalışmak için "
                    "ONAY_GERCEK_PARA ortam değişkenini tam olarak '%s' yapman gerekiyor.",
                    ONAY_IBARESI,
                )
                sys.exit(1)
        if not self.api_key or not self.api_secret:
            log.error("Canlı/testnet mod için BINANCE_API_KEY ve BINANCE_API_SECRET gerekli.")
            sys.exit(1)


class BinanceIstemci:
    """Kimlik doğrulamalı Binance REST çağrıları (yalnızca dry_run=False iken kullanılır)."""

    def __init__(self, ayarlar: BotAyarlari):
        self.ayarlar = ayarlar
        self._sembol_bilgi_onbellek: dict = {}

    def _imzali_istek(self, yol: str, parametreler: dict, metod: str = "GET") -> dict:
        parametreler = dict(parametreler)
        parametreler["timestamp"] = int(time.time() * 1000)
        parametreler["recvWindow"] = 5000
        sorgu = urllib.parse.urlencode(parametreler)
        imza = hmac.new(
            self.ayarlar.api_secret.encode(), sorgu.encode(), hashlib.sha256
        ).hexdigest()
        url = f"{self.ayarlar.base_url}{yol}?{sorgu}&signature={imza}"
        istek = urllib.request.Request(
            url,
            method=metod,
            headers={"X-MBX-APIKEY": self.ayarlar.api_key, "User-Agent": "Mozilla/5.0"},
        )
        try:
            with urllib.request.urlopen(istek, timeout=10) as yanit:
                return json.loads(yanit.read().decode())
        except urllib.error.HTTPError as e:
            hata_govde = e.read().decode()
            raise RuntimeError(f"Binance API hatası ({e.code}): {hata_govde}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Binance'a bağlanılamadı: {e.reason}") from e

    def sembol_bilgisi(self, sembol: str) -> dict:
        if sembol in self._sembol_bilgi_onbellek:
            return self._sembol_bilgi_onbellek[sembol]
        parametreler = urllib.parse.urlencode({"symbol": sembol})
        istek = urllib.request.Request(
            f"{self.ayarlar.base_url}/api/v3/exchangeInfo?{parametreler}",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        try:
            with urllib.request.urlopen(istek, timeout=10) as yanit:
                veri = json.loads(yanit.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Binance API hatası ({e.code}): {e.read().decode()}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Binance'a bağlanılamadı: {e.reason}") from e

        semboller = veri.get("symbols") or []
        if not semboller:
            raise ValueError(f"{sembol} için borsa bilgisi bulunamadı.")
        bilgi = semboller[0]
        self._sembol_bilgi_onbellek[sembol] = bilgi
        return bilgi

    def adim_buyuklugu(self, sembol: str) -> Decimal:
        bilgi = self.sembol_bilgisi(sembol)
        for f in bilgi["filters"]:
            if f["filterType"] == "LOT_SIZE":
                return Decimal(f["stepSize"])
        return Decimal("0.00000001")

    def serbest_bakiye(self, varlik: str) -> float:
        hesap = self._imzali_istek("/api/v3/account", {})
        for b in hesap["balances"]:
            if b["asset"] == varlik:
                return float(b["free"])
        return 0.0

    def piyasa_emri_ver(self, sembol: str, yon: str, **kwargs) -> dict:
        """yon: 'BUY' veya 'SELL'. kwargs: quoteOrderQty ya da quantity."""
        parametreler = {"symbol": sembol, "side": yon, "type": "MARKET", **kwargs}
        return self._imzali_istek("/api/v3/order", parametreler, metod="POST")


def _miktar_asagi_yuvarla(miktar: float, adim: Decimal) -> float:
    d = Decimal(str(miktar))
    return float((d // adim) * adim)


class TradingBot:
    def __init__(self, ayarlar: BotAyarlari):
        self.ayarlar = ayarlar
        self.istemci: Optional[BinanceIstemci] = None if ayarlar.dry_run else BinanceIstemci(ayarlar)
        self.durum = self._durum_yukle()

    def _durum_yukle(self) -> dict:
        if DURUM_DOSYASI.exists():
            try:
                return json.loads(DURUM_DOSYASI.read_text())
            except json.JSONDecodeError:
                log.warning("Durum dosyası bozuk görünüyor, sıfırdan başlatılıyor.")
        return {
            "bakiye_usdt": SANAL_BASLANGIC_BAKIYE,
            "pozisyonlar": {},
            "gun": None,
            "gun_baslangic_bakiye": SANAL_BASLANGIC_BAKIYE,
            "gun_gerceklesen_pnl_yuzde": 0.0,
            "son_islemler": [],
            "canli_mod": self.ayarlar.canli_mod,
        }

    def _durum_kaydet(self) -> None:
        self.durum["canli_mod"] = self.ayarlar.canli_mod
        self.durum["son_guncelleme"] = datetime.now(timezone.utc).isoformat()
        DURUM_DOSYASI.write_text(json.dumps(self.durum, indent=2, ensure_ascii=False))

    def _gunu_kontrol_et(self) -> None:
        bugun = datetime.now(timezone.utc).date().isoformat()
        if self.durum.get("gun") != bugun:
            self.durum["gun"] = bugun
            self.durum["gun_baslangic_bakiye"] = self._toplam_deger_tahmini()
            self.durum["gun_gerceklesen_pnl_yuzde"] = 0.0
            log.info("Yeni gün başladı (%s), günlük zarar sayaçı sıfırlandı.", bugun)

    def _toplam_deger_tahmini(self) -> float:
        # Basitleştirme: açık pozisyonların giriş değerini nakit gibi sayar.
        pozisyon_degeri = sum(
            p["miktar"] * p["giris_fiyati"] for p in self.durum["pozisyonlar"].values()
        )
        if self.ayarlar.dry_run:
            nakit = self.durum["bakiye_usdt"]
        else:
            try:
                nakit = self.istemci.serbest_bakiye("USDT")
            except Exception:
                log.exception("Gerçek USDT bakiyesi alınamadı, son bilinen değer kullanılıyor.")
                nakit = self.durum.get("bakiye_usdt", 0.0)
        return nakit + pozisyon_degeri

    def _yeni_pozisyon_sermaye_izni_var_mi(self) -> bool:
        """Toplam sermayenin en fazla sermaye_orani kadarı pozisyonlarda olabilir."""
        if self.ayarlar.sermaye_orani >= 1.0:
            return True
        toplam = self._toplam_deger_tahmini()
        acik_pozisyon_degeri = sum(
            p["miktar"] * p["giris_fiyati"] for p in self.durum["pozisyonlar"].values()
        )
        kullanilabilir_tavan = toplam * self.ayarlar.sermaye_orani
        return acik_pozisyon_degeri + self.ayarlar.trade_amount_usdt <= kullanilabilir_tavan

    def _gunluk_zarar_asildi_mi(self) -> bool:
        baslangic = self.durum.get("gun_baslangic_bakiye", SANAL_BASLANGIC_BAKIYE)
        if baslangic <= 0:
            return False
        zarar_yuzde = -self.durum.get("gun_gerceklesen_pnl_yuzde", 0.0)
        return zarar_yuzde >= self.ayarlar.max_gunluk_zarar_yuzde

    def _islem_kaydet(self, sembol: str, yon: str, fiyat: float, getiri_yuzde: Optional[float] = None) -> None:
        kayit = {
            "zaman": datetime.now(timezone.utc).isoformat(),
            "sembol": sembol,
            "yon": yon,
            "fiyat": fiyat,
        }
        if getiri_yuzde is not None:
            kayit["getiri_yuzde"] = round(getiri_yuzde, 2)
        self.durum.setdefault("son_islemler", []).append(kayit)
        self.durum["son_islemler"] = self.durum["son_islemler"][-50:]

    def _pozisyon_ac(self, sembol: str, fiyat: float) -> None:
        tutar = self.ayarlar.trade_amount_usdt
        if self.ayarlar.dry_run:
            if self.durum["bakiye_usdt"] < tutar:
                log.info("[SANAL] %s için yetersiz sanal bakiye, atlanıyor.", sembol)
                return
            miktar = tutar / fiyat
            self.durum["bakiye_usdt"] -= tutar
            self.durum["pozisyonlar"][sembol] = {
                "miktar": miktar,
                "giris_fiyati": fiyat,
                "giris_zamani": datetime.now(timezone.utc).isoformat(),
            }
            log.info("[SANAL] AL %s: %.2f USDT @ %.8f", sembol, tutar, fiyat)
        else:
            sonuc = self.istemci.piyasa_emri_ver(sembol, "BUY", quoteOrderQty=round(tutar, 2))
            gerceklesen_miktar = float(sonuc.get("executedQty", 0))
            gerceklesen_tutar = float(sonuc.get("cummulativeQuoteQty", tutar))
            gercek_fiyat = gerceklesen_tutar / gerceklesen_miktar if gerceklesen_miktar else fiyat
            self.durum["pozisyonlar"][sembol] = {
                "miktar": gerceklesen_miktar,
                "giris_fiyati": gercek_fiyat,
                "giris_zamani": datetime.now(timezone.utc).isoformat(),
            }
            log.info("[CANLI] AL %s: %.8f adet @ %.8f", sembol, gerceklesen_miktar, gercek_fiyat)
        self._islem_kaydet(sembol, "AL", fiyat)

    def _pozisyon_kapat(self, sembol: str, fiyat: float, sebep: str) -> None:
        pozisyon = self.durum["pozisyonlar"].pop(sembol)
        getiri_yuzde = (fiyat - pozisyon["giris_fiyati"]) / pozisyon["giris_fiyati"] * 100

        if self.ayarlar.dry_run:
            satis_tutari = pozisyon["miktar"] * fiyat
            self.durum["bakiye_usdt"] += satis_tutari
            log.info(
                "[SANAL] SAT %s (%s): getiri %%%.2f, yeni bakiye %.2f USDT",
                sembol, sebep, getiri_yuzde, self.durum["bakiye_usdt"],
            )
        else:
            varlik = sembol[:-4] if sembol.endswith("USDT") else sembol
            adim = self.istemci.adim_buyuklugu(sembol)
            elde_mevcut = self.istemci.serbest_bakiye(varlik)
            satilacak_miktar = _miktar_asagi_yuvarla(min(pozisyon["miktar"], elde_mevcut), adim)
            if satilacak_miktar <= 0:
                log.warning("[CANLI] %s için satılacak yeterli bakiye yok, pozisyon kaydı temizlendi.", sembol)
            else:
                sonuc = self.istemci.piyasa_emri_ver(sembol, "SELL", quantity=satilacak_miktar)
                log.info("[CANLI] SAT %s (%s): %s", sembol, sebep, sonuc)

        baslangic_toplam = self.durum.get("gun_baslangic_bakiye", SANAL_BASLANGIC_BAKIYE)
        if baslangic_toplam > 0:
            etki_yuzde = (pozisyon["miktar"] * fiyat - pozisyon["miktar"] * pozisyon["giris_fiyati"]) / baslangic_toplam * 100
            self.durum["gun_gerceklesen_pnl_yuzde"] = self.durum.get("gun_gerceklesen_pnl_yuzde", 0.0) + etki_yuzde

        self._islem_kaydet(sembol, f"SAT ({sebep})", fiyat, getiri_yuzde)

    def _sembol_degerlendir(self, sembol: str) -> None:
        try:
            veri = binance_klines_getir(sembol, gun_sayisi=100)
        except Exception as e:
            log.warning("%s için veri alınamadı: %s", sembol, e)
            return

        if len(veri) < 20:
            return

        veri = gosterge_ekle(veri)
        guncel_fiyat = veri["Close"].iloc[-1]
        guncel_rsi = veri["RSI"].iloc[-1]
        yukselis_trendi = veri["SMA20"].iloc[-1] > veri["SMA50"].iloc[-1]

        acik_pozisyon = self.durum["pozisyonlar"].get(sembol)

        if acik_pozisyon:
            giris = acik_pozisyon["giris_fiyati"]
            degisim_yuzde = (guncel_fiyat - giris) / giris * 100
            if degisim_yuzde <= -self.ayarlar.stop_loss_yuzde:
                self._pozisyon_kapat(sembol, guncel_fiyat, "stop-loss")
            elif degisim_yuzde >= self.ayarlar.take_profit_yuzde:
                self._pozisyon_kapat(sembol, guncel_fiyat, "take-profit")
            elif guncel_rsi > RSI_ASIRI_ALIM:
                self._pozisyon_kapat(sembol, guncel_fiyat, "RSI aşırı alım")
            return

        if self._gunluk_zarar_asildi_mi():
            return
        if len(self.durum["pozisyonlar"]) >= self.ayarlar.max_positions:
            return
        if not self._yeni_pozisyon_sermaye_izni_var_mi():
            log.info(
                "%s için sinyal var ama sermaye tavanı (%%%.0f) doldu, atlanıyor.",
                sembol, self.ayarlar.sermaye_orani * 100,
            )
            return
        if guncel_rsi < RSI_ASIRI_SATIM and yukselis_trendi:
            self._pozisyon_ac(sembol, guncel_fiyat)

    def tek_tur_calistir(self) -> None:
        self._gunu_kontrol_et()
        if self._gunluk_zarar_asildi_mi():
            log.warning(
                "Günlük zarar tavanına (%%%.1f) ulaşıldı, bugün yeni pozisyon açılmayacak.",
                self.ayarlar.max_gunluk_zarar_yuzde,
            )
        for sembol in self.ayarlar.watchlist:
            try:
                self._sembol_degerlendir(sembol)
            except Exception:
                log.exception("%s işlenirken beklenmeyen hata oluştu, bu tur için atlanıyor.", sembol)
        self._durum_kaydet()

    def calistir(self, tek_seferlik: bool = False) -> None:
        if self.ayarlar.canli_mod:
            emir_hedefi = "MAINNET (gerçek para)" if not self.ayarlar.use_testnet else "TESTNET (sahte para)"
        else:
            emir_hedefi = "yok, sadece sanal bakiye simülasyonu"
        log.info(
            "Bot başlatıldı. Mod: %s | Emir hedefi: %s | Fiyat verisi: mainnet (public) | "
            "Watchlist: %s | İşlem tutarı: %.2f USDT | Sermaye tavanı: %%%.0f",
            "CANLI" if self.ayarlar.canli_mod else "SANAL (dry-run)",
            emir_hedefi,
            ", ".join(self.ayarlar.watchlist),
            self.ayarlar.trade_amount_usdt,
            self.ayarlar.sermaye_orani * 100,
        )
        while True:
            try:
                self.tek_tur_calistir()
            except Exception:
                log.exception("Tur sırasında beklenmeyen hata oluştu, döngü devam ediyor.")
            if tek_seferlik:
                break
            time.sleep(self.ayarlar.tarama_araligi_saniye)


def main() -> None:
    ayrıştırıcı = argparse.ArgumentParser(description=__doc__)
    ayrıştırıcı.add_argument("--once", action="store_true", help="Tek tur çalışıp çık")
    args = ayrıştırıcı.parse_args()

    ayarlar = BotAyarlari()
    ayarlar.dogrula()
    bot = TradingBot(ayarlar)
    bot.calistir(tek_seferlik=args.once)


if __name__ == "__main__":
    main()
