#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
SERVICE_FILE="${SYSTEMD_USER_DIR}/samad-monitor.service"

if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "❌ محیط مجازی (${PYTHON_BIN}) یافت نشد! ابتدا محیط مجازی .venv را بسازید."
    exit 1
fi

mkdir -p "${SYSTEMD_USER_DIR}"

cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Samad Food Monitor - Shahid Beheshti University Weekly Menu Watcher
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_DIR}
ExecStart=${PYTHON_BIN} -m samad_monitor run
Restart=always
RestartSec=15
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now samad-monitor.service

echo "✅ سرویس کاربری samad-monitor.service فعال و اجرا شد."
echo "   مشاهده وضعیت : systemctl --user status samad-monitor.service"
echo "   مشاهده لاگ‌ها : journalctl --user -u samad-monitor.service -f"
