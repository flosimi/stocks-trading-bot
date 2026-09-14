#!/usr/bin/env bash
#
# Deployment botului Python de trading pe laptopul vechi (Acer / Linux).
# Muta DOAR folderul Python_bot/ pe Acer, apoi ruleaza acest script din el.
#
# Ce face:
#   1. instaleaza dependintele de sistem
#   2. creeaza venv + instaleaza requirements.txt
#   3. verifica/securizeaza bot_config.json
#   4. genereaza serviciul systemd (auto-start la boot, auto-restart la crash)
#   5. activeaza serviciul
#
set -euo pipefail

# ============ editeaza daca e cazul ============
USER_NAME="$(whoami)"
PROJECT_DIR="$HOME/AI_trading_bot"
BOT_DIR="${PROJECT_DIR}/Python_bot"
CAPITAL="200"                       # capital USDT pentru trading real
# ===============================================

echo ">> [1/5] Dependinte de sistem..."
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git

echo ">> [2/5] venv + dependinte Python..."
cd "${BOT_DIR}"
python3 -m venv venv
./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -r requirements.txt -q

echo ">> [3/5] Verific configul..."
if [ ! -f "${BOT_DIR}/bot_config.json" ]; then
  echo "   !! Lipseste bot_config.json"
  echo "      Copiaza-l de pe laptopul principal in ${BOT_DIR}/"
  echo "      (sau seteaza cheile ca env vars in serviciu, mai jos)"
else
  chmod 600 "${BOT_DIR}/bot_config.json"
  echo "   OK, config gasit si securizat (chmod 600)."
fi

echo ">> [4/5] Generez serviciul systemd..."
sudo tee /etc/systemd/system/ai-trading-bot.service >/dev/null <<EOF
[Unit]
Description=AI Trading Bot (Binance + Ollama)
After=network-online.target
Wants=network-online.target
# daca crapa de 5 ori in 5 minute, se opreste (evita loop de crash cu ordine)
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=${USER_NAME}
WorkingDirectory=${BOT_DIR}
ExecStart=${BOT_DIR}/venv/bin/python ai_trading_bot.py --capital ${CAPITAL}
Restart=always
RestartSec=10

# --- alternativa la bot_config.json: cheile ca env vars (decomenteaza) ---
# Environment=BINANCE_API_KEY=...
# Environment=BINANCE_API_SECRET=...
# Environment=OLLAMA_URL=http://localhost:11434

[Install]
WantedBy=multi-user.target
EOF

echo ">> [5/5] Activez serviciul..."
sudo systemctl daemon-reload
sudo systemctl enable ai-trading-bot

echo ""
echo "==================================================="
echo "Gata. Comenzi utile:"
echo "  Pornire:   sudo systemctl start ai-trading-bot"
echo "  Status:    systemctl status ai-trading-bot"
echo "  Loguri:    journalctl -u ai-trading-bot -f"
echo "  Oprire:    sudo systemctl stop ai-trading-bot"
echo "==================================================="
echo ""
echo "NU uita sa dezactivezi sleep-ul la inchiderea capacului:"
echo "  sudo sed -i 's/#HandleLidSwitch=suspend/HandleLidSwitch=ignore/' /etc/systemd/logind.conf"
echo "  sudo systemctl restart systemd-logind"
