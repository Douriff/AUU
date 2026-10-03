#!/usr/bin/env bash
# Box-side daily scheduler for auu_backup.py (the box has no cron). Single instance (flock).
#   auu_backup.sh start | status | now | run
# Daily at AUU_BACKUP_AT_BJ (default 09:30 Beijing: after the 08:00 rebalance and 08:30 digest,
# outside the 07:30-08:45 no-touch window). A missed day (box down) runs at the next check.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
PY=${AUU_BACKUP_PY:-/workspace/.srvvenv/bin/python}
LOCK=/tmp/auu_backup.lock
LOG=${AUU_BACKUP_LOG:-/workspace/backups/auu/backup.log}
AT=${AUU_BACKUP_AT_BJ:-09:30}
STAMP=/workspace/backups/auu/.last_ok_day
mkdir -p "$(dirname "$LOG")"
now() { "$PY" "$HERE/auu_backup.py" backup >>"$LOG" 2>&1; }
loop() {
  exec 9>"$LOCK"; flock -n 9 || { echo "backup scheduler already running"; exit 0; }
  echo "$(TZ=Asia/Shanghai date '+%F %T') scheduler started pid $$ (daily $AT BJ)" >>"$LOG"
  while true; do
    day=$(TZ=Asia/Shanghai date +%F); hm=$(TZ=Asia/Shanghai date +%H:%M)
    if [[ "$hm" > "$AT" || "$hm" == "$AT" ]] && [[ "$(cat "$STAMP" 2>/dev/null)" != "$day" ]]; then
      if now 9>&-; then echo "$day" > "$STAMP"; else echo "$(TZ=Asia/Shanghai date '+%F %T') backup FAILED (retry in 30 min)" >>"$LOG"; sleep 1740; fi
    fi
    # rotate in place (same inode: a concurrent `now` keeps appending to the live file)
    if [ "$(wc -l < "$LOG" 2>/dev/null || echo 0)" -gt 3000 ]; then tail -n 2000 "$LOG" > "$LOG.tmp" && cat "$LOG.tmp" > "$LOG"; rm -f "$LOG.tmp"; fi
    sleep 60
  done
}
case "${1:-status}" in
  start) setsid nohup "$0" run >/dev/null 2>&1 </dev/null & sleep 1; "$0" status ;;
  run) loop ;;
  now) now; rc=$?; tail -n 3 "$LOG"; exit $rc ;;
  status) if flock -n "$LOCK" true 2>/dev/null; then echo "backup scheduler NOT running"; exit 1; else echo "backup scheduler running"; fi ;;
  *) echo "usage: $0 start|status|now|run" >&2; exit 2 ;;
esac
