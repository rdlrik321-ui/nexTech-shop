#!/usr/bin/env bash
# Обновление бэкенда после изменений в репозитории:  bash /opt/nextech/backend/deploy/update.sh
set -euo pipefail
cd /opt/nextech
git pull
backend/.venv/bin/pip install -q -r backend/requirements.txt
chown -R nextech:nextech /opt/nextech
systemctl restart nextech
