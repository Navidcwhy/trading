#!/usr/bin/env bash
# Papperböckerna överlever en ny molncontainer: snapshot till git-branchen claude/trading-desk-state
# (en enda commit som skrivs över, så repot växer inte).
#   bash cloud/state.sh save      ta en konsistent kopia av båda böckerna (VACUUM INTO) och pusha
#   bash cloud/state.sh restore   hämta senaste snapshot, bara för böcker som saknas lokalt
#   bash cloud/state.sh loop      save var 10:e minut (startas av cloud/start.sh)
set -u
cd "$(dirname "$0")/.." || exit 1
BRANCH=claude/trading-desk-state
BOOKS="paper paper-daytrade"

save() {
  local tmp f blob tree commit
  tmp=$(mktemp -d) || return 1
  for d in $BOOKS; do
    [ -f "data/$d/desk.db" ] || continue
    python3 - "data/$d/desk.db" "$tmp/$d.db" <<'PY' || { rm -rf "$tmp"; return 1; }
import sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=30)
con.execute("VACUUM INTO ?", (sys.argv[2],))
PY
    gzip -9 "$tmp/$d.db"
    [ -f "data/$d/tokenized_stocks.json" ] && cp "data/$d/tokenized_stocks.json" "$tmp/$d.tokenized_stocks.json"
  done
  if [ -z "$(ls "$tmp")" ]; then rm -rf "$tmp"; echo "save: no books yet"; return 0; fi
  tree=$(for f in $(ls "$tmp"); do
           blob=$(git hash-object -w "$tmp/$f") && printf '100644 blob %s\t%s\n' "$blob" "$f"
         done | git mktree) || { rm -rf "$tmp"; return 1; }
  commit=$(git -c user.name="desk" -c user.email="desk@localhost" commit-tree "$tree" \
           -m "state $(TZ=Europe/Stockholm date '+%Y-%m-%d %H:%M') boot $(cut -c1-8 /proc/sys/kernel/random/boot_id)") || { rm -rf "$tmp"; return 1; }
  rm -rf "$tmp"
  for i in 1 2 3 4; do
    git push -q -f origin "$commit:refs/heads/$BRANCH" && { echo "save: $(TZ=Europe/Stockholm date '+%H:%M') ok"; return 0; }
    sleep $((2 ** i))
  done
  echo "save: push failed"; return 1
}

restore() {
  local need=0 d
  for d in $BOOKS; do [ -f "data/$d/desk.db" ] || need=1; done
  if [ "$need" = 0 ]; then echo "restore: books present, nothing to do"; return 0; fi
  if ! git fetch -q origin "$BRANCH" 2>/dev/null; then echo "restore: no snapshot yet, books start fresh"; return 0; fi
  for d in $BOOKS; do
    [ -f "data/$d/desk.db" ] && continue
    git cat-file -e "FETCH_HEAD:$d.db.gz" 2>/dev/null || continue
    mkdir -p "data/$d"
    git show "FETCH_HEAD:$d.db.gz" | gunzip > "data/$d/desk.db" || { rm -f "data/$d/desk.db"; return 1; }
    git cat-file -e "FETCH_HEAD:$d.tokenized_stocks.json" 2>/dev/null &&
      git show "FETCH_HEAD:$d.tokenized_stocks.json" > "data/$d/tokenized_stocks.json"
    echo "restore: data/$d/desk.db from '$(git log -1 --format=%s FETCH_HEAD)'"
  done
}

case "${1:-}" in
  save) save ;;
  restore) restore ;;
  loop) mkdir -p logs/cloud; sleep 120
        while true; do save >> logs/cloud/state.log 2>&1; sleep 600; done ;;
  *) echo "usage: $0 save|restore|loop"; exit 2 ;;
esac
