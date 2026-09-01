# Guvenlik ve test raporu — 2026-08-31

## Yapilan duzeltmeler

- Islem butcesi kod seviyesinde en fazla %33 ile sinirlandi.
- Yalnizca kapanmis mumlarla sinyal uretilmesi ve ayni mumda tekrar islem
  engeli eklendi.
- %3 stop-loss, %6 kar-al ve %2 gunluk gerceklesmis zarar limiti eklendi.
- Kismi satis/kapali pozisyon tutarsizligi giderildi; bot yalnizca kendisinin
  kaydettigi pozisyon miktarini tamamen kapatir.
- Binance LOT_SIZE adim yuvarlamasi eklendi.
- `STOP_BOT` acil durdurma dosyasi ve Windows menu secenegi eklendi.
- Backtest'e alis/satis ucreti (varsayilan %0,1), stop-loss ve kar-al eklendi.
- Windows UTF-8 konsol uyumlulugu duzeltildi.

## Dogrulama

- Python derleme kontrolu: basarili.
- Temel miktar yuvarlama/risk kontrolu: basarili.
- SHIBUSDT, 15 dakika, son 30 gun backtest: 2880 mum, 26 islem.
- Baslangic: 1000 USDT; bitis: 994,04 USDT.
- Strateji: -%0,60; al-ve-tut: +%1,60; maksimum gerileme: -%2,15.

## Karar

Bu sonuc canli isleme gecmek icin yeterli degildir. Paket `MODE=dry_run`
varsayilaniyla teslim edilir. Testnet ve canli emir yollari gercek borsa
hesabinda bu denetim sirasinda calistirilmamistir.
