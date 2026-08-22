#!/usr/bin/env bash
# Einmaliges Setup auf der Oracle Cloud Free Tier VM.
#
# Nutzung (auf der VM, im geklonten Repo-Verzeichnis ausgeführt):
#   ./deploy/install.sh
#
# Danach: .env im Repo-Root ausfüllen (siehe .env.example), config.yaml
# nach Bedarf anpassen, dann:
#   sudo systemctl start padel-booking
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="$(whoami)"
SERVICE_FILE="/etc/systemd/system/padel-booking.service"

echo "==> App-Verzeichnis: ${APP_DIR}"
echo "==> Dienst läuft als Benutzer: ${APP_USER}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 nicht gefunden. Bitte zuerst installieren (z.B. 'sudo apt-get install -y python3 python3-venv')." >&2
  exit 1
fi

echo "==> Erzeuge virtuelle Umgebung (.venv)"
python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install --upgrade pip
"${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

if [ ! -f "${APP_DIR}/.env" ]; then
  echo "==> Keine .env gefunden - kopiere .env.example nach .env"
  cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
  echo "    WICHTIG: ${APP_DIR}/.env jetzt mit echten Zugangsdaten befüllen!"
fi

echo "==> Installiere systemd-Service nach ${SERVICE_FILE}"
sed \
  -e "s|__APP_DIR__|${APP_DIR}|g" \
  -e "s|__APP_USER__|${APP_USER}|g" \
  "${APP_DIR}/deploy/padel-booking.service" | sudo tee "${SERVICE_FILE}" >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable padel-booking

echo ""
echo "==> Fertig. Nächste Schritte:"
echo "    1. ${APP_DIR}/.env ausfüllen (falls noch nicht geschehen)"
echo "    2. ${APP_DIR}/config.yaml auf den gewünschten Ziel-Slot prüfen"
echo "    3. Test:   ${APP_DIR}/.venv/bin/python ${APP_DIR}/main.py test-login"
echo "    4. Start:  sudo systemctl start padel-booking"
echo "    5. Logs:   journalctl -u padel-booking -f"
