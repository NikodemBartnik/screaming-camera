#!/usr/bin/env bash
# Setup for Qualcomm Dragonwing IQ-9075 EVK running Ubuntu 24.04 (also fine on any arm64/amd64 Ubuntu).
# Run from the repo root:  bash scripts/setup_iq9075.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== system packages"
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-dev build-essential \
    libportaudio2 libsndfile1 ffmpeg \
    bluez pulseaudio-utils pipewire-audio \
    docker.io docker-compose-v2 curl

echo "== python venv"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[piper,sapi,dev]"

echo "== docker (eufy bridge)"
sudo usermod -aG docker "$USER" || true
if [ ! -f .env ]; then cp .env.example .env; echo ">> edit .env with your Eufy guest account, then: docker compose up -d"; fi

echo "== GenieX (Qualcomm on-device runtime, OpenAI-compatible server on :18181)"
if ! command -v geniex >/dev/null 2>&1; then
  cat <<'EOF'
>> GenieX is not installed. Follow https://geniex.aihub.qualcomm.com/ (developer preview), then:
     geniex pull google/gemma-4-E4B-it-qat-q4_0-gguf     # or the model name shown by the Qualcomm team
     geniex serve                                         # listens on http://127.0.0.1:18181
   Fallback without NPU: build llama.cpp (CPU/OpenCL) and run
     llama-server -m gemma-4-E4B-it-Q4_0.gguf --mmproj mmproj-gemma-4-E4B-it.gguf --port 18181 -ngl 99
EOF
fi

echo "== config"
[ -f config.yaml ] || cp config.example.yaml config.yaml
sed -i 's#endpoint: http://127.0.0.1:11434/v1#endpoint: http://127.0.0.1:18181/v1#; s#name: gemma4:e4b#name: gemma-4-E4B-it#' config.yaml || true

echo "== systemd service"
SERVICE=/etc/systemd/system/screaming-camera.service
sudo tee "$SERVICE" >/dev/null <<EOF
[Unit]
Description=Screaming Camera
After=network-online.target docker.service
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$(pwd)
Environment=PULSE_SERVER=unix:/run/user/$(id -u)/pulse/native
Environment=XDG_RUNTIME_DIR=/run/user/$(id -u)
ExecStart=$(pwd)/.venv/bin/python -m screaming_camera --config config.yaml
Restart=always
RestartSec=5
TimeoutStopSec=15
KillMode=mixed

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable screaming-camera
echo ">> start with: sudo systemctl start screaming-camera   (logs: journalctl -fu screaming-camera)"
echo ">> panel: http://$(hostname -I | awk '{print $1}'):8080"

cat <<'EOF'

== Bluetooth speaker (optional)
  bluetoothctl
    power on ; agent on ; scan on      -> note the MAC
    pair XX:XX:XX:XX:XX:XX ; trust XX:XX:XX:XX:XX:XX ; connect XX:XX:XX:XX:XX:XX
  Then in the panel: Speakers -> local_audio -> device name contains e.g. "JBL", keep_alive on.
EOF
