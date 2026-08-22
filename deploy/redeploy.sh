#!/usr/bin/env bash
# Wird von GitHub Actions per SSH auf der VM ausgeführt, um nach einem
# Push die neueste Version zu übernehmen. Kann auch manuell auf der VM
# ausgeführt werden.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${APP_DIR}"

echo "==> Hole neuesten Code"
git fetch origin
git reset --hard origin/main

echo "==> Aktualisiere Python-Abhängigkeiten"
"${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

echo "==> Starte Dienst neu"
sudo systemctl restart padel-booking

echo "==> Fertig."
