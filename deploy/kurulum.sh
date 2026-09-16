#!/usr/bin/env bash
# Sunucuda (GCP VM vb.) tek seferlik kurulum scripti.
# Kullanım: repo klonlandıktan sonra, repo klasörünün içinden çalıştır:
#   bash deploy/kurulum.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CALISTIRAN_KULLANICI="$(whoami)"

echo "== 1/6: Sistem paketleri kontrol ediliyor =="
if ! command -v python3 >/dev/null 2>&1 || ! python3 -m venv --help >/dev/null 2>&1; then
    sudo apt update
    sudo apt install -y python3-venv python3-pip
fi

echo "== 2/6: Sanal ortam (venv) kuruluyor =="
cd "$REPO_DIR"
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt

echo "== 3/6: .env dosyası hazırlanıyor =="
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "UYARI: .env dosyası .env.example'dan kopyalandı, henüz DOLDURULMADI."
    echo "       Canlı işlem için BINANCE_API_KEY/SECRET, ONAY_GERCEK_PARA ve APP_SIFRE'yi"
    echo "       doldurmadan önce servisler güvenli (DRY_RUN=true) modda çalışacak."
else
    echo ".env zaten mevcut, dokunulmadı."
fi

echo "== 4/6: systemd servisleri kuruluyor =="
for SERVICE in trading-bot streamlit-panel; do
    sed \
        -e "s|CHANGE_ME_kullanici_adi|${CALISTIRAN_KULLANICI}|g" \
        -e "s|/home/${CALISTIRAN_KULLANICI}/mobil-borsa|${REPO_DIR}|g" \
        "deploy/${SERVICE}.service" | sudo tee "/etc/systemd/system/${SERVICE}.service" > /dev/null
done

echo "== 5/6: Firewall (ufw) - 8501 portu açılıyor (varsa) =="
if command -v ufw >/dev/null 2>&1; then
    sudo ufw allow 8501/tcp || true
fi

echo "== 6/6: Servisler başlatılıyor =="
sudo systemctl daemon-reload
sudo systemctl enable --now trading-bot streamlit-panel

echo ""
echo "KURULUM TAMAMLANDI."
echo "Panel: http://$(curl -s -4 ifconfig.me 2>/dev/null || echo SUNUCU_IP):8501"
echo ""
echo "Durum kontrolü:"
echo "  sudo systemctl status trading-bot"
echo "  sudo journalctl -u trading-bot -f"
echo ""
echo "ONEMLI: .env dosyasi henuz duzenlenmediyse bot SANAL (dry-run) modda calisiyor,"
echo "        gercek para kullanmiyor. Canliya gecmek icin .env'i doldurup:"
echo "  sudo systemctl restart trading-bot streamlit-panel"
