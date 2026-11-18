# 备份专用 SSH 接入

本文只说明怎么让 box 上已经在跑的 `auu_backup.sh` 连到当前服务器。不要在这份说明里填写私钥、口令、`users.json` 或 `auu.env`。不要为了备份去改 SSH 端口，也不要关闭现有的密码登录。

当前服务器是 `186.244.248.30`，SSH 端口 `16683`。部署目录 `/opt/auu` 是 release 的符号链接，里面没有 `.git`，版本在 `COMMIT`。box 的调度器仍按启动时读入的旧地址每天 09:30（北京时间）连接，改磁盘上的 env 不会改变已经在跑的那一轮，必须停掉再启动。

## 权限：不是 root，也不是只读

远端要做的事是：用 SQLite 在线备份打开正在写入的账本（WAL 模式下读者会登记到 `-shm`），把副本写到临时目录，再由 box 用 SFTP 取走。这不是一组只读系统调用，所以不能把专用账号说成只读。

root 不是必需的。专用账号 `auubackup` 的实际权限是：

- 用一把新的 ed25519 公钥登录，密码锁定。现有 root 和其他账号的密码登录保持原样，`sshd` 端口保持 `16683`。
- 可以执行 `/usr/local/sbin/auu_remote_snapshot.py rev`。它只读 `/opt/auu` 的 `.git` 或 `COMMIT`，并且只接受十六进制版本号。
- 可以通过 sudo 只做这一条，而且以 `auu` 的身份执行：`/usr/bin/python3 /usr/local/sbin/auu_remote_snapshot.py snapshot`。sudo 会清空环境变量，程序没有路径参数，不能改去读别的文件。
- `snapshot` 只打开 `auu_backup.py` 里那份 SQLite 名单。它不打开 `users.json`、`exchange.sqlite`、`/etc/auu/auu.env`，也不读取会话密钥或 AI 密钥。以 `auu` 运行是为了打开自己的 WAL；SQLite 可能更新对应的 `-shm`。它把副本写到 `/var/backups/auu-stage`，不改账本页面。
- 可以读取并删除 stage 目录里的副本。不能因为加入某个组就读到 `users.json`：该文件保持 `0600`、属主 `auu:auu`，并且不要把 `auubackup` 加进 `auu` 组，也不要在数据目录上设默认 ACL（默认 ACL 会在用户库重写时一起泄出去）。

旧的默认仍然可用：不设置 `AUU_BACKUP_USER` 时客户端以 root 执行原来的 `runuser -u auu`。那是兼容路径，新机应使用下面的专用账号。

## 在服务器上安装

在服务器本机操作。公钥从 box 拷过来，私钥不离开 box。

```bash
groupadd --system auubackup
id auubackup >/dev/null 2>&1 || useradd --system --create-home --gid auubackup --shell /bin/bash auubackup
passwd -l auubackup
usermod -a -G auubackup auu
install -d -o auubackup -g auubackup -m 2770 /var/backups/auu-stage
install -o root -g root -m 0755 auu_remote_snapshot.py /usr/local/sbin/auu_remote_snapshot.py
install -d -o auubackup -g auubackup -m 0700 /home/auubackup/.ssh
touch /home/auubackup/.ssh/authorized_keys
chown auubackup:auubackup /home/auubackup/.ssh/authorized_keys
chmod 0600 /home/auubackup/.ssh/authorized_keys
```

`auu_remote_snapshot.py` 用本变更里的文件。当前已部署的 `/opt/auu` 还没有它；不要把脚本放到 `auu` 能写的目录。

`/etc/sudoers.d/auu-backup`，模式 `0440`：

```
Defaults:auubackup !requiretty
Defaults:auubackup env_reset
auubackup ALL=(auu) NOPASSWD: /usr/bin/python3 /usr/local/sbin/auu_remote_snapshot.py snapshot
```

用 `visudo -cf /etc/sudoers.d/auu-backup` 检查。不要给 `ALL`，不要允许额外参数。

box 上单独生成钥匙，不要复用交易所 API、会话密钥、AI 密钥，也不要复用旧机那把 root 钥匙：

```bash
ssh-keygen -t ed25519 -f "$HOME/.ssh/auu_backup_ed25519" -N "" -C auu-backup-box
chmod 0600 "$HOME/.ssh/auu_backup_ed25519"
```

把 `.pub` 的一整行追加到服务器的 `/home/auubackup/.ssh/authorized_keys`。不要写进 root 的 `authorized_keys`。

主机密钥在服务器本机读取，不要用一次公网连接自动接受：

```bash
cat /etc/ssh/ssh_host_ed25519_key.pub
```

box 的 pinned 文件只写这一行（端口不是 22，必须带方括号）：

```
[186.244.248.30]:16683 ssh-ed25519 AAAA...
```

`COMMIT` 保持普通可读。它只是版本号。不要把 `/opt/auu` 改成所有人可写。

## 换到 box 上已有的调度器

调度器没有 cron。正在跑的是 `auu_backup.sh` 的 `run` 循环，锁在 `/tmp/auu_backup.lock`，配置是脚本旁边的 `auu_backup.env`（不入库）。

1. 把 `auu_backup.py`、`auu_remote_snapshot.py`、`auu_backup.sh` 拷到那个目录，覆盖同名脚本。不要覆盖 `auu_backup.env`。
2. 在 env 里改成当前服务器，并删掉已废弃的旧地址：

```
AUU_BACKUP_HOST=186.244.248.30
AUU_BACKUP_PORT=16683
AUU_BACKUP_USER=auubackup
AUU_BACKUP_KEY=<box 上新私钥的路径>
AUU_BACKUP_KNOWN_HOSTS=<上面那个 pinned 文件>
```

不设置端口时仍是 22，不设置用户时仍是 root。`AUU_BACKUP_PY` 必须是带 paramiko 的解释器。

3. 停掉旧循环。旧脚本没有 pid 文件，日志里有 `scheduler started pid`。确认那一行的进程是 `auu_backup.sh` 之后再 `kill` 那个 pid。不要对整个主机使用 `pkill -9`。
4. `<bin>/auu_backup.sh status` 应显示 `NOT running`。然后 `<bin>/auu_backup.sh start`。新循环会把 pid 写到备份目录的 `.scheduler.pid`，之后用 `<bin>/auu_backup.sh stop`。
5. `<bin>/auu_backup.sh now`。成功的 `MANIFEST.json` 里 `port` 是 16683，`ssh_user` 是 `auubackup`，`server_rev_source` 是 `commit`，`server_rev` 等于服务器上 `COMMIT` 的第一个十六进制字段。快照里仍然没有 `users.json`。

身份密文不进这个任务。更新方式写在 `identity-backup.md` 的「与每日 SQLite 备份的关系」。

## 撤销

在服务器上：

```bash
rm -f /etc/sudoers.d/auu-backup
gpasswd -d auu auubackup || true
# 清空或删除 authorized_keys 里的这一行
userdel auubackup
rm -rf /var/backups/auu-stage
```

在 box 上删除 `auu_backup_ed25519` 私钥，并把调度器停掉或把 env 指回一个你仍打算使用的主机。不要顺手修改 `sshd` 端口或全局密码策略。
