#!/bin/bash
# Instala o bot num servidor Ubuntu e deixa-o a correr 24/7 (arranca sozinho se o servidor reiniciar).
# Uso, dentro da pasta nba-alertas:  bash instalar.sh
set -e
PASTA="$(cd "$(dirname "$0")" && pwd)"

read -rsp "Cola o token do bot (não aparece no ecrã) e carrega Enter: " TOKEN; echo
[ -z "$TOKEN" ] && { echo "Token vazio."; exit 1; }

echo ">> A instalar Python..."
sudo apt-get update -y -qq
sudo apt-get install -y -qq python3-venv

echo ">> A instalar dependências..."
python3 -m venv "$PASTA/.venv"
"$PASTA/.venv/bin/pip" install -q -r "$PASTA/requirements.txt"

echo ">> A criar o serviço..."
sudo tee /etc/systemd/system/nba-alertas.service >/dev/null <<EOF
[Unit]
Description=Alertas NBA (bot do Telegram)
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$PASTA
Environment=TELEGRAM_TOKEN=$TOKEN
ExecStart=$PASTA/.venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
sudo chmod 600 /etc/systemd/system/nba-alertas.service
sudo systemctl daemon-reload
sudo systemctl enable --now nba-alertas
sleep 3
sudo systemctl status nba-alertas --no-pager | head -n 5
echo
echo "✅ Pronto. Ver registos:   sudo journalctl -u nba-alertas -f"
echo "   Reiniciar o bot:        sudo systemctl restart nba-alertas"
