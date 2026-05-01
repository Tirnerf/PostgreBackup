#!/usr/bin/env bash
# setup.sh — PostgreSQL Yedekleme Uygulaması Kurulumu
set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="pgbackup"
SERVICE_USER="$(whoami)"
PYTHON="python3"

# ── Kimlik bilgileri (isteğe bağlı: kurulum öncesi ortam değişkeni olarak geçilebilir)
APP_USERNAME="${APP_USERNAME:-admin}"
APP_PASSWORD="${APP_PASSWORD:-}"   # boşsa aşağıda rastgele üretilir
SECRET_KEY="${SECRET_KEY:-}"       # boşsa aşağıda rastgele üretilir

echo "==> PostgreSQL Yedekleme Uygulaması Kurulumu"
echo "    Dizin    : $APP_DIR"
echo "    Kullanıcı: $SERVICE_USER"
echo ""

# 1. Python kontrolü
if ! command -v python3 &>/dev/null; then
  echo "[!] python3 bulunamadı, kuruluyor..."
  sudo apt-get update -qq && sudo apt-get install -y python3 python3-pip python3-venv
fi

# 2. gzip / gunzip kontrolü
if ! command -v gzip &>/dev/null || ! command -v gunzip &>/dev/null; then
  echo "[!] gzip bulunamadı, kuruluyor..."
  sudo apt-get install -y gzip
fi

# 3. sshpass kontrolü (SSH şifreli bağlantı için)
if ! command -v sshpass &>/dev/null; then
  echo "[i] sshpass kuruluyor (SSH şifre desteği için)..."
  sudo apt-get install -y sshpass
fi

# 4. Sanal ortam
echo "==> Python sanal ortamı oluşturuluyor..."
$PYTHON -m venv "$APP_DIR/venv"
source "$APP_DIR/venv/bin/activate"

# 5. Bağımlılıklar
echo "==> Bağımlılıklar yükleniyor..."
pip install --quiet --upgrade pip
pip install --quiet -r "$APP_DIR/requirements.txt"

# 6. Dizinler
mkdir -p "$APP_DIR/data" "$APP_DIR/backups"

# 7. Rastgele güvenli değerler üret (belirtilmemişse)
if [ -z "$APP_PASSWORD" ]; then
  APP_PASSWORD="$(python3 -c 'import secrets, string; print(secrets.token_urlsafe(16))')"
  echo "[i] APP_PASSWORD otomatik üretildi (aşağıda gösterilecek)"
fi
if [ -z "$SECRET_KEY" ]; then
  SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
fi

# 8. systemd servis dosyası
echo "==> systemd servisi oluşturuluyor..."
sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null <<EOF
[Unit]
Description=PostgreSQL Yedekleme Web Uygulaması
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/app.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=APP_USERNAME=${APP_USERNAME}
Environment=APP_PASSWORD=${APP_PASSWORD}
Environment=SECRET_KEY=${SECRET_KEY}

[Install]
WantedBy=multi-user.target
EOF

# 9. Servisi etkinleştir ve başlat
sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}
sudo systemctl restart ${SERVICE_NAME}

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "  Kurulum tamamlandı!"
echo ""
echo "  Web arayüzü : http://$(hostname -I | awk '{print $1}'):5000"
echo ""
echo "  Giriş bilgileri:"
echo "    Kullanıcı adı : ${APP_USERNAME}"
echo "    Şifre         : ${APP_PASSWORD}"
echo ""
echo "  Kimlik bilgilerini değiştirmek için:"
echo "    sudo systemctl edit ${SERVICE_NAME}"
echo "    (APP_USERNAME ve APP_PASSWORD satırlarını güncelleyin)"
echo "    sudo systemctl restart ${SERVICE_NAME}"
echo ""
echo "  Servis komutları:"
echo "    sudo systemctl status  ${SERVICE_NAME}"
echo "    sudo systemctl stop    ${SERVICE_NAME}"
echo "    sudo journalctl -u ${SERVICE_NAME} -f"
echo "╚══════════════════════════════════════════════════════════╝"
