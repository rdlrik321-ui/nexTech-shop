#!/usr/bin/env bash
# Запуск на чистом Ubuntu 22.04/24.04 от root:  bash setup.sh [домен]
# Без домена используется <ip>.sslip.io — бесплатный адрес, для него Caddy сам выпустит HTTPS.
set -euo pipefail

IP=$(curl -4 -s https://ifconfig.me)
DOMAIN="${1:-${IP//./-}.sslip.io}"
APP_DIR=/opt/nextech

apt-get update
apt-get install -y python3-venv git caddy

id nextech &>/dev/null || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin nextech
[ -d "$APP_DIR/.git" ] || git clone https://github.com/rdlrik321-ui/nexTech-shop.git "$APP_DIR"
cd "$APP_DIR/backend"

python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo ">>> Заполни секреты:  nano $APP_DIR/backend/.env  — затем запусти скрипт ещё раз"
  exit 1
fi

chown -R nextech:nextech "$APP_DIR"
chmod 600 .env

cp deploy/nextech.service /etc/systemd/system/nextech.service
printf '%s {\n    reverse_proxy 127.0.0.1:8000\n}\n' "$DOMAIN" > /etc/caddy/Caddyfile

systemctl daemon-reload
systemctl enable --now nextech
systemctl restart nextech caddy

echo "Готово. Адрес бэкенда: https://$DOMAIN"
