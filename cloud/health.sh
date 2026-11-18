#!/usr/bin/env bash
# Läget för molndeskarna på en skärm.   bash cloud/health.sh
cd "$(dirname "$0")/.." || exit 1
export TZ=Europe/Stockholm
echo "nu $(date '+%Y-%m-%d %H:%M') | container $(cut -c1-8 /proc/sys/kernel/random/boot_id), uppe $(awk '{printf "%.1f h", $1/3600}' /proc/uptime)"
alive() { [ -f "logs/cloud/$1.pid" ] && ps -o stat= -p "$(cat "logs/cloud/$1.pid")" 2>/dev/null | grep -qv Z; }
down=0
for n in main daytrade state; do
  if alive "$n"; then echo "$n: kör"; else echo "$n: NERE"; down=1; fi
done
if alive watchdog; then echo "vakthund: kör"; else echo "vakthund: kör inte (starta som bakgrundsjobb)"; fi
t=$(date -d '-1 hour' +%H:%M:%S)
for p in "main:logs/console.log" "daytrade:logs/daytrade/console.log"; do
  n=${p%%:*}; f=${p#*:}
  [ -f "$f" ] || continue
  echo "--- $n"
  grep -a "cycle .* done" "$f" | tail -n 1 | cut -c1-150
  grep -a "TRADE" "$f" | tail -n 3 | cut -c1-170
  echo "cykler senaste timmen: $(awk -v t="$t" '$1 >= t' "$f" | grep -a -c 'cycle .* done') av ~12 |" \
       "omstarter i dag: $(grep -a -c "$(date '+%a %b %e').* desk start =" "$f") |" \
       "fel senaste timmen: $(awk -v t="$t" '$1 >= t' "$f" | grep -a -c -E 'Traceback|ERROR|desk exited')"
done
python3 -m desk compare 2>&1 | sed -n '3,16p'
echo "senaste snapshot: $(grep -a "^save:" logs/cloud/state.log 2>/dev/null | tail -n 1)"
exit $down
