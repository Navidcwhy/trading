#!/usr/bin/env bash
# Vakthund för molndeskarna. Körs som bakgrundsjobb i Claude-sessionen:
#   bash cloud/watchdog.sh [MINUTER]      (standard 110)
# Kontrollerar varje minut att båda deskarna och snapshot-loopen kör, och kör cloud/start.sh om
# något har fallit. Ett pågående bakgrundsjobb håller sessionen aktiv, så att maskinen inte pausas.
# Jobbet avslutas efter MINUTER: då väcks sessionen och startar ett nytt.
cd "$(dirname "$0")/.." || exit 1
export TZ=Europe/Stockholm
mkdir -p logs/cloud
echo $$ > logs/cloud/watchdog.pid
end=$(( $(date +%s) + ${1:-110} * 60 ))
alive() { [ -f "logs/cloud/$1.pid" ] && ps -o stat= -p "$(cat "logs/cloud/$1.pid")" 2>/dev/null | grep -qv Z; }
restarts=0
while [ "$(date +%s)" -lt "$end" ]; do
  if ! alive main || ! alive daytrade || ! alive state; then
    echo "$(date +%H:%M) något låg nere, kör start.sh"
    bash cloud/start.sh
    restarts=$((restarts + 1))
  fi
  sleep 60
done
echo "$(date +%H:%M) vakthunden klar efter ${1:-110} min, $restarts omstarter"
bash cloud/health.sh
