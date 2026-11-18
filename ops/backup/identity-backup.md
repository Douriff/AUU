# 用户库与会话密钥的加密备份

官方 SQLite 备份（`ops/backup/auu_backup.py` 的 `FILES`）故意不包含 `users.json`、`exchange.sqlite` 和 `/etc/auu/auu.env`。会话签名和两步验证密钥都来自 `AUU_SESSION_SECRET`。没有这两份材料，账户不能登录，旧纸面资产也不能按用户 id 对上。

本说明不包含任何真实口令、用户名或密钥。密文和口令都不得提交到仓库。

## 备份

在服务器上，用已经加载了 `/etc/auu/auu.env` 的管理员 shell，把产物写到仓库以外的磁盘（模式 0600）：

```bash
# 口令只放在这次命令的环境里，不要写进仓库、不要放在命令行参数里。
export AUU_IDENTITY_BACKUP_PASSPHRASE='(16 个字符以上，另行保管)'
export AUU_USER_STORE=/var/lib/auu/data/users.json
# AUU_SESSION_SECRET 必须已经在环境中，且不能是开发默认值。
umask 077
python3 /opt/auu/ops/backup/identity_backup.py backup /root/auu-identity.bin
unset AUU_IDENTITY_BACKUP_PASSPHRASE
```

成功时只打印用户数和文件字节数。把 `/root/auu-identity.bin` 复制到与 SQLite 备份不同的离线位置。口令单独保管，不和密文放在同一份明文说明里。

## 校验

用同一口令解密到内存，核对用户文件和会话密钥的 SHA-256、JSON 以及用户数。不打印用户名、哈希原文或密钥：

```bash
export AUU_IDENTITY_BACKUP_PASSPHRASE='(同一口令)'
python3 /opt/auu/ops/backup/identity_backup.py verify /root/auu-identity.bin
unset AUU_IDENTITY_BACKUP_PASSPHRASE
```

输出为 `ok users=N`。口令错误或文件被改过时以非零状态退出，并只说明失败类别。

## 恢复

1. 停止 API。确认 `AUU_STRATEGY_RUNNER`、`AUU_SHADOW_H2`、`AUU_SHADOW_S3`、`AUU_SIGNAL_SHADOW`、`AUU_ALERTS`、`AUU_PRICE_ALERTS`、`AUU_LIVE_ENABLED` 都是 `off`。
2. 若目标机已经有 `users.json`，先把它复制到仓库外再覆盖。不要把旧 id 写进新账户。
3. 在离线位置解密（校验命令只检查，不写回用户文件）。把解出的 `users.json` 放到 `AUU_USER_STORE`，`chown auu:auu`，模式 0600。把解出的会话密钥写回 `/etc/auu/auu.env` 的 `AUU_SESSION_SECRET`，模式 0600。不要把这两份放进 git。
4. 启动 API。已有 cookie 在这次会话格式变更后需要重新登录一次。注销只使本机失效；其他设备保持登录，直到「注销其他设备」或修改密码。
5. 再次运行 `verify`。然后 `unset` 口令。

没有这份密文时，不要编造用户、积分、订阅或旧纸面余额。新注册的账户是新 id。

## 策略缺口与实盘（部署顺序）

1. 部署本变更时保持 Runner、H2、S3、信号影子、提醒和实盘关闭。
2. 管理员登录后调用一次 `POST /api/v1/strategy/resume`，正文 `{"confirm":"resume-forward","note":"..."}`。这只写入缺口记录，不补成交、不按现价改旧账。历史行仍在。H2 使用同一段缺口，验证天数不把缺口算成连续样本。
3. 确认状态里缺口为 missing、运行数没有增加之后，再按需打开 Runner。下一根收盘从新的起始净值、空仓开始。不要设置 `catchup=replay`。
4. 实盘保持关闭。账户、币种、额度、杠杆、止损、亏损上限都没有默认值；未同时给出时不能武装。`LIVE_SEND_WIRED` 仍为 false，本变更不发送订单。杠杆高于 5 会被拒绝，而不是被改成某个默认值。

## 回滚

1. 先把 Runner、H2 和实盘保持或重新设为关闭，再回退代码并重启。旧代码在缺口未封住时会按原逻辑补记多日，所以回退前必须确认 Runner 没有打开。
2. 已写入的 `ledger_gaps` 表可留在 SQLite 里，旧代码不会读它，也不会删历史 `runs`。
3. 本变更之后发出的登录 cookie 带设备 id。回退后需要重新登录。
4. 身份密文和口令不在这个回滚里删除；它们不在仓库中。
