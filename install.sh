#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ "$(pwd)" != /opt/cryptolove ]]; then
  echo 'Copia el contenido de cryptolove-server a /opt/cryptolove y ejecuta allí: bash install.sh'
  exit 1
fi
if [[ $EUID -ne 0 ]]; then echo 'Ejecuta sudo bash install.sh'; exit 1; fi
apt-get update
apt-get install -y python3 python3-venv caddy
if ! id cryptolove >/dev/null 2>&1; then useradd --system --home /opt/cryptolove --shell /usr/sbin/nologin cryptolove; fi
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
if [[ ! -f .env ]]; then
  cp .env.example .env
  .venv/bin/python - <<'PY'
from pathlib import Path
from cryptography.fernet import Fernet
import secrets
p=Path('.env')
s=p.read_text().replace('APP_SECRET=\n','APP_SECRET='+secrets.token_urlsafe(48)+'\n').replace('ENCRYPTION_KEY=\n','ENCRYPTION_KEY='+Fernet.generate_key().decode()+'\n')
p.write_text(s)
PY
fi
mkdir -p data
chown -R cryptolove:cryptolove /opt/cryptolove
chmod 700 data
chmod 600 .env
cat > /etc/systemd/system/cryptolove-api.service <<'UNIT'
[Unit]
Description=CryptoLove private API
After=network-online.target
Wants=network-online.target
[Service]
User=cryptolove
Group=cryptolove
WorkingDirectory=/opt/cryptolove
EnvironmentFile=/opt/cryptolove/.env
ExecStart=/opt/cryptolove/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8091 --workers 1 --proxy-headers --forwarded-allow-ips=127.0.0.1 --no-access-log
Restart=always
RestartSec=5
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/opt/cryptolove/data
Environment=PYTHONDONTWRITEBYTECODE=1
PrivateTmp=true
[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/cryptolove-worker.service <<'UNIT'
[Unit]
Description=CryptoLove mail, subscribers and public account reporting
After=network-online.target
Wants=network-online.target
[Service]
User=cryptolove
Group=cryptolove
WorkingDirectory=/opt/cryptolove
EnvironmentFile=/opt/cryptolove/.env
ExecStart=/opt/cryptolove/.venv/bin/python worker.py
Restart=always
RestartSec=10
KillMode=mixed
TimeoutStopSec=45
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/opt/cryptolove/data
Environment=PYTHONDONTWRITEBYTECODE=1
PrivateTmp=true
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
echo 'Instalación terminada. Edita .env, configura Caddy y permisos del motor; sigue PASO_A_PASO.md.'
echo 'No se han iniciado servicios ni operaciones.'
