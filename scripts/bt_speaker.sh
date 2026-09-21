#!/usr/bin/env bash
# Pair / connect a Bluetooth speaker on Ubuntu (PipeWire) and make it the default output.
#   bash scripts/bt_speaker.sh scan                 # list nearby devices (put the speaker in pairing mode first)
#   bash scripts/bt_speaker.sh pair "JBL Flip"      # pair + trust + connect by (part of) name, or by MAC
#   bash scripts/bt_speaker.sh connect              # reconnect the remembered speaker, set as default sink
#   bash scripts/bt_speaker.sh test                 # play a short tone through the default sink
#   bash scripts/bt_speaker.sh status
set -euo pipefail
STATE_FILE="$HOME/.config/screaming-camera-bt-speaker"
mkdir -p "$(dirname "$STATE_FILE")"

bt() { bluetoothctl -- "$@"; }

find_mac() {  # by MAC or name substring among known/scanned devices
  local q="$1"
  if [[ "$q" =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]]; then echo "$q"; return; fi
  bt devices | grep -i -- "$q" | head -1 | awk '{print $2}'
}

set_default_sink() {
  local sink
  for _ in $(seq 1 15); do
    sink=$(pactl list short sinks | awk '/bluez_output|bluez_sink/{print $2}' | head -1)
    [ -n "$sink" ] && break
    sleep 1
  done
  if [ -z "$sink" ]; then echo "!! no Bluetooth sink appeared in PipeWire (is the speaker connected?)"; return 1; fi
  pactl set-default-sink "$sink"
  pactl set-sink-volume "$sink" 100%
  echo ">> default sink: $sink"
}

case "${1:-status}" in
  scan)
    echo ">> scanning 12 s - the speaker must be in pairing mode"
    bt power on >/dev/null
    bt agent NoInputNoOutput >/dev/null || true
    timeout 12 bluetoothctl --timeout 12 scan on >/dev/null 2>&1 || true
    bt devices
    ;;
  pair)
    q="${2:?name or MAC}"
    bt power on >/dev/null
    bt agent NoInputNoOutput >/dev/null || true
    bt default-agent >/dev/null || true
    mac=$(find_mac "$q")
    if [ -z "$mac" ]; then
      echo ">> not known yet, scanning 15 s..."
      timeout 15 bluetoothctl --timeout 15 scan on >/dev/null 2>&1 || true
      mac=$(find_mac "$q")
    fi
    [ -n "$mac" ] || { echo "!! device matching '$q' not found - is it in pairing mode?"; exit 1; }
    echo ">> pairing $mac"
    bt pair "$mac" || true
    bt trust "$mac"
    bt connect "$mac"
    echo "$mac" > "$STATE_FILE"
    set_default_sink
    ;;
  connect)
    mac=$(cat "$STATE_FILE" 2>/dev/null || true)
    [ -n "$mac" ] || { echo "!! no remembered speaker - run: $0 pair <name>"; exit 1; }
    bt power on >/dev/null
    bt connect "$mac" || true
    set_default_sink
    ;;
  test)
    sink=$(pactl get-default-sink)
    echo ">> playing a 1.5 s tone on $sink"
    python3 - <<'EOF'
import math, struct, subprocess, sys
rate = 16000
pcm = b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * (880 if (i // 4000) % 2 == 0 else 660) * i / rate))) for i in range(int(rate * 1.5)))
subprocess.run(["pacat", "--raw", "--format=s16le", "--rate=16000", "--channels=1"], input=pcm, check=True)
EOF
    ;;
  status)
    echo "--- controller"; bt show | grep -E "Powered|Name"
    echo "--- paired"; bt paired-devices 2>/dev/null || bt devices Paired
    echo "--- connected"; bt devices Connected 2>/dev/null || true
    echo "--- sinks"; pactl list short sinks; echo "default: $(pactl get-default-sink)"
    ;;
  *) echo "usage: $0 scan | pair <name|MAC> | connect | test | status"; exit 1 ;;
esac
