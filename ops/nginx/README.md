# nginx snippets (Tokyo + HK, identical)

Copies of what is installed on both hosts after PR #87:
- `auu-app.conf` → `/etc/nginx/snippets/auu-app.conf` (app locations, rate limits, headers, bot/method filter)
- `auu-bots.conf` → `/etc/nginx/conf.d/auu-bots.conf` (`$auu_bad_ua`, `$auu_bad_method` maps)

Install: back up both files, copy, `nginx -t && systemctl reload nginx`. Rollback: restore the backups and reload.
