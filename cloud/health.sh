#!/usr/bin/env bash
# Läget för molndeskarna på en skärm.   bash cloud/health.sh
cd "$(dirname "$0")/.." || exit 1
export TZ=Europe/Stockholm
echo "nu $(date '+%Y-%m-%d %H:%M') | container $(cut -c1-8 /proc/sys/kernel/random/boot_id), uppe $(awk '{printf "%.1f h", $1/3600}' /proc/uptime)"
down=0
for n in main daytrade state; do
  f="logs/cloud/$n.pid"
  if [ -f "$f" ] && ps -o stat= -p "$(cat "$f")" 2>/dev/null | grep -qv Z; then echo "$n: kör"; else echo "$n: NERE"; down=1; fi
done
for p in "main:logs/console.log" "daytrade:logs/daytrade/console.log"; do
  n=${p%%:*}; f=${p#*:}
  [ -f "$f" ] || continue
  echo "--- $n"
  grep -a "cycle .* done" "$f" | tail -n 1 | cut -c1-150
  grep -a -E "TRADE|time exit|take profit|stop hit|trail" "$f" | tail -n 3 | cut -c1-170
  echo "fel senaste timmen: $(awk -v t="$(date -d '-1 hour' +%H:%M:%S)" '$1 >= t' "$f" | grep -a -c -E 'Traceback|ERROR|desk exited')"
done
python3 -m desk compare 2>&1 | sed -n '3,16p'
echo "senaste snapshot: $(tail -n 1 logs/cloud/state.log 2>/dev/null || echo 'ingen än')"
exit $down
