# HK standby sync + stage scripts (reference copies)

These two scripts live on the servers (`/usr/local/sbin/`), outside the app checkout. They are kept here so the sync
list is reviewable and tested. **Installing them is a server change and is not done by merging this PR.**

| Script | Where | What changed |
|---|---|---|
| `auu-sync-export` | Tokyo (forced command of `auusync`) and HK (used when exporting back to Tokyo) | `DBS` += `price_alerts.sqlite`, `chart_drawings.sqlite`, `airdrops.sqlite`; those three are read from the staged copies (they are 0600) |
| `auu-points-stage` | Tokyo (`auu-points-stage.timer`, user `auu`) | stages `points`, `price_alerts`, `chart_drawings`, `airdrops` (was only `points`); one failing file no longer blocks the others |

Not synced, by design: `exchange.sqlite` (users' encrypted exchange API credentials stay on the primary) and every key / env
file. `users.json` is synced as before.

Also needed on the box (not in this repo): add the same three names to `LEDGER_FILES` in `/workspace/bin/auu_failover.py`, so a
promote of the HK node installs them (missing files in a snapshot are skipped, so the order of rollout does not matter), and
copy `ops/backup/auu_backup.py` to the box's bin dir (its `FILES` list gained the same three; missing files are tolerated).

Rollout order (each step is independent and reversible): 1) install `auu-points-stage` on Tokyo and run it once as `auu`;
2) install `auu-sync-export` on Tokyo (+ HK); 3) after the next `auu-sync` the manifest has 15 files (`cat /var/lib/auu-sync/status.json`);
4) update `LEDGER_FILES` in the failover script. Rollback: put back the previous scripts (backups are taken with `cp -a` before install).
