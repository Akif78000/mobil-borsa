#!/data/data/com.termux/files/usr/bin/bash
# Android + Termux icin tek komutla trade_bot.py baslatma yardimcisi.
#
# Kullanim (Termux icinde, repo klasorunde):
#   bash run_termux.sh
#
# Ilk calistirmada gerekli paketleri kurar, .env yoksa .env.example'dan
# olusturup duzenlemeniz icin durur. Telefon uykuya gecince islemin
# olmemesi icin termux-wake-lock alir.

set -e

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 kuruluyor..."
    pkg update -y && pkg install -y python
fi

if [ ! -f .env ]; then
    cp .env.example .env
    echo "'.env' dosyasi olusturuldu. Once BINANCE / TELEGRAM bilgilerinizi girin:"
    echo "  nano .env"
    exit 0
fi

if command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock
    echo "termux-wake-lock aktif (telefon uyusa da bot calismaya devam eder)."
else
    echo "Not: termux-api kurulu degil, 'pkg install termux-api' ile kurup"
    echo "Termux:API uygulamasini da indirirseniz wake-lock kullanilabilir."
fi

python3 trade_bot.py
