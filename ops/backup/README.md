# Offsite ledger backup (P1-7)

Pulled **from the box** (`/workspace/backups/auu/`) with the box's existing SSH key; nothing is installed or stored on
the server (no credentials, no cron there). Only a temp dir `/tmp/auu-bk-*` (mode 700, owner `auu`) exists on the
server for the seconds of a run and is removed afterwards.

- Files: `mainstream_strategy.sqlite`, `shadow_s3.sqlite`, `mainstream_paper.sqlite` (required), `alerts.sqlite`,
  `recon.sqlite`, `auth_log.sqlite` (when present), `mainstream.sqlite` (market data, ~3 MB gz). Not copied:
  `users.json`, `/etc/auu/auu.env`, any key.
- Method: on the server, as `auu`, `sqlite3.Connection.backup()` (online backup API, consistent under WAL while the
  API runs) → `PRAGMA integrity_check` → SHA-256 → SFTP to the box → hash re-checked → gzip → `MANIFEST.json`
  (hashes, sizes, per-table row counts, server git rev) + `SHA256SUMS`.
- Every run ends with a restore drill (`RESTORE_TEST.json`): hashes → decompress to a temp dir → integrity_check →
  row counts equal the manifest → the app's own `StrategyLedger` opens the restored ledger (runs, last day, NAV, fills).
- Retention: 30 days (`AUU_BACKUP_KEEP_DAYS`); the newest snapshot is never pruned.
- Schedule: the box has no cron, so `auu_backup.sh` is a small flock'd scheduler: daily at 09:30 Beijing
  (`AUU_BACKUP_AT_BJ`; after the 08:00 rebalance, outside 07:30–08:45), retry every 30 min on failure; the box
  watchdog restarts it if it is not running.

```
cp ops/backup/auu_backup.{py,sh} /workspace/bin/ && /workspace/bin/auu_backup.sh start   # install / start
/workspace/bin/auu_backup.sh now                                                         # one backup now
/workspace/.srvvenv/bin/python /workspace/bin/auu_backup.py list | verify [DIR] | restore-test [DIR]
```

Manual restore of one file (server, API stopped, outside 07:30–08:45, keep the current file first):
`gunzip -c mainstream_strategy.sqlite.gz > /tmp/x.sqlite`, check `sha256sum` against `SHA256SUMS`, copy to
`/var/lib/auu/data/`, `chown auu:auu`, remove stale `-wal`/`-shm` of that file, start the API, check GUARD OK.
