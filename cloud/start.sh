#!/usr/bin/env bash
# Startar båda paper-deskarna i en molnsession. Säker att köra flera gånger: det som redan kör
# lämnas i fred. I en ny container återställs böckerna först från senaste snapshot.
#   bash cloud/start.sh
set -u
cd "$(dirname "$0")/.." || exit 1
export TZ=Europe/Stockholm
mkdir -p logs/cloud

# Molnmaskinen: api.binance.com och fapi.binance.com svarar 451 härifrån (geospärr).
# Spot-data via Binances spegling, perps och guld/silver av. Inga hemligheter i .env här.
[ -f .env ] || touch .env
grep -q '^DESK_BINANCE_SPOT_DATA=' .env || echo 'DESK_BINANCE_SPOT_DATA=https://data-api.binance.vision' >> .env
grep -q '^DESK_PERPS_OFF=' .env || echo 'DESK_PERPS_OFF=1' >> .env

bash cloud/state.sh restore

launch() {  # name, command...
  local name=$1 pidf="logs/cloud/$1.pid"; shift
  if [ -f "$pidf" ] && ps -o stat= -p "$(cat "$pidf")" 2>/dev/null | grep -qv Z; then
    echo "$name: already running (pid $(cat "$pidf"))"; return
  fi
  setsid nohup "$@" > /dev/null 2>&1 < /dev/null &
  echo $! > "$pidf"
  echo "$name: started (pid $!)"
}

launch main     bash run_desk.sh main
launch daytrade bash run_desk.sh daytrade
launch state    bash cloud/state.sh loop
