#!/usr/bin/env bash
# Kör en desk och startar om den automatiskt om processen dör (Linux/macOS, motsvarar run_desk.bat).
#   ./run_desk.sh            = huvuddesken (reglerna i config.json)
#   ./run_desk.sh daytrade   = daytrade-desken (config.daytrade.json)
# Kör båda i bakgrunden:  nohup ./run_desk.sh >/dev/null 2>&1 &  nohup ./run_desk.sh daytrade >/dev/null 2>&1 &
# Loggar: logs/console.log respektive logs/daytrade/console.log
cd "$(dirname "$0")" || exit 1
PROFILE="${1:-main}"
PY="$(command -v python3 || command -v python)"
if [ "$PROFILE" = "main" ]; then LOG=logs/console.log; else LOG="logs/$PROFILE/console.log"; fi
mkdir -p "$(dirname "$LOG")"
echo "===== $(date) $PROFILE desk start =====" >> "$LOG"
while true; do
  "$PY" -u -m desk run --profile "$PROFILE" >> "$LOG" 2>&1
  echo "$(date) desk exited, restarting in 15s" >> "$LOG"
  sleep 15
done
