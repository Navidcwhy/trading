#!/usr/bin/env bash
# Startar båda paper-deskarna i en molnsession. Säker att köra flera gånger: det som redan kör
# lämnas i fred. I en ny container återställs böckerna först från senaste snapshot.
#   bash cloud/start.sh            starta det som inte kör
#   bash cloud/start.sh restart    starta om båda deskarna (efter ändrad .env); böckerna behålls
set -u
cd "$(dirname "$0")/.." || exit 1
export TZ=Europe/Stockholm
mkdir -p logs/cloud

# Molnmaskinen: api.binance.com och fapi.binance.com svarar 451 härifrån (geospärr).
# Spot-data via Binances spegling, perps och guld/silver av.
[ -f .env ] || touch .env
grep -q '^DESK_BINANCE_SPOT_DATA=' .env || echo 'DESK_BINANCE_SPOT_DATA=https://data-api.binance.vision' >> .env
grep -q '^DESK_PERPS_OFF=' .env || echo 'DESK_PERPS_OFF=1' >> .env

# Hemliga nycklar ligger som nätverkshemligheter på molnmiljön: proxyn byter platshållaren
# "proxy-injected" mot det riktiga värdet. .env får aldrig riktiga hemligheter här.
probe() { curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$@"; }
sed -i -e '/^FINNHUB_API_KEY=$/d' -e '/^ALPACA_SECRET_KEY=$/d' .env
if ! grep -q '^FINNHUB_API_KEY=' .env; then
  if [ "$(probe -H 'X-Finnhub-Token: proxy-injected' 'https://finnhub.io/api/v1/stock/market-status?exchange=US')" = 200 ]; then
    echo 'FINNHUB_API_KEY=proxy-injected' >> .env; echo "finnhub: key via proxy"
  else
    echo "finnhub: no working key on this environment - stock news/earnings stay off (fail-closed)"
  fi
fi
AID=$(grep '^ALPACA_KEY_ID=' .env | tail -n 1 | cut -d= -f2)
if ! grep -q '^ALPACA_SECRET_KEY=' .env; then
  if [ -z "$AID" ]; then
    echo "alpaca: no ALPACA_KEY_ID in .env - stocks stay off"
  elif [ "$(probe -H "APCA-API-KEY-ID: $AID" -H 'APCA-API-SECRET-KEY: proxy-injected' https://paper-api.alpaca.markets/v2/clock)" = 200 ]; then
    echo 'ALPACA_SECRET_KEY=proxy-injected' >> .env; echo "alpaca: key ID + secret via proxy OK"
  else
    echo "alpaca: key ID and saved secret are rejected (401) - stocks stay off"
  fi
fi

bash cloud/state.sh restore

if [ "${1:-}" = restart ]; then
  for n in main daytrade; do
    f="logs/cloud/$n.pid"
    [ -f "$f" ] && kill -- -"$(cat "$f")" 2>/dev/null && echo "$n: stopped"
  done
  for i in $(seq 1 30); do pgrep -f "m desk run" > /dev/null || break; sleep 1; done
fi

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
