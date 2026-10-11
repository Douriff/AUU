# Cloudflare 后台需要你本人操作的防护项（auutrade.com）

> 这些开关只能在 Cloudflare 后台点。服务器和代码这边已经做完的部分见 `audit-2026-10-11.md`。
> **不要关闭** SSL/TLS → Origin Server → Authenticated Origin Pulls（AOP），也不要把 DNS 记录改成灰云。
> 东京源站强制校验 Cloudflare 客户端证书，关掉 AOP 整个站会 400。
> 每做完一步，用手机和电脑各打开一次 https://auutrade.com 并登录，确认正常再做下一步。

## 1. Bot Fight Mode（机器人对抗模式，免费）
1. 登录 https://dash.cloudflare.com ，点击 **auutrade.com**。
2. 左侧菜单 **Security（安全性）** → **Bots（自动程序）**。
3. 打开 **Bot Fight Mode**。
4. 注意：开了以后 Cloudflare 会给可疑程序出挑战。它可能拦截服务器自检脚本（auu-failover 访问 `https://auutrade.com/api/v1/health`），出现问题就先关掉它，再告诉我。开启后也请让我跑一次 `certbot renew --dry-run`，确认证书续期（Let's Encrypt 经 Cloudflare 回源 80 端口）没被拦。
   （如果你买的是 Pro 及以上套餐，这里显示 **Super Bot Fight Mode**：把 “Definitely automated” 设为 **Block**，“Likely automated” 设为 **Managed Challenge**，“Verified bots” 设为 **Allow**。）

## 2. 拦截 AI 爬虫（免费）
1. 同一页 **Security → Bots**，找到 **Block AI Scrapers and Crawlers（阻止 AI 爬虫）**，打开。

## 3. WAF 自定义规则（免费套餐可建 5 条）
路径：**Security → WAF → Custom rules（自定义规则）→ Create rule（创建规则）**。每条规则：填名字 → 点 **Edit expression（编辑表达式）** → 粘贴表达式 → 选动作 → **Deploy（部署）**。

| 规则名 | 表达式（原样粘贴） | 动作 |
|---|---|---|
| block-scanners | `(http.request.uri.path contains "/wp-") or (http.request.uri.path contains ".php") or (http.request.uri.path contains "/.env") or (http.request.uri.path contains "/.git")` | Block |
| api-no-empty-ua | `(starts_with(http.request.uri.path, "/api/") and http.user_agent eq "")` | Block |
| api-bots-challenge | `(starts_with(http.request.uri.path, "/api/") and cf.client.bot and not cf.verified_bot_category in {"Search Engine Crawler"})` | Managed Challenge |

可选（看你的用户在哪些国家）：**Security → WAF → Custom rules** 再建一条 `(ip.src.country in {"XX" "YY"})` → Managed Challenge，只对你确定没有用户的国家使用。

## 4. 限速规则 Rate limiting（免费套餐 1 条）
路径：**Security → WAF → Rate limiting rules（速率限制规则）→ Create rule**。
1. 名字：`auth-bruteforce`
2. **If incoming requests match**：点 Edit expression，粘贴
   `(http.request.uri.path in {"/api/v1/auth/login" "/api/v1/auth/login/totp" "/api/v1/auth/register" "/api/v1/auth/email/code" "/api/v1/auth/password/reset"})`
3. **With the same characteristics**：IP。
4. **When rate exceeds**：Requests = `10`，Period = `1 minute`（免费套餐只能选 10 秒周期时，就填 10 秒 `5` 次）。
5. **Then take action**：Block，Duration = `10 minutes`（免费套餐可选的最短值即可）。
6. 点 **Deploy**。
服务器这边已经有同样的限制（nginx 每 IP 每分钟 5 次 + 应用内按账号/按 IP 计数），这一条是在 Cloudflare 边缘提前挡掉。

## 5. 其他建议开关
- **Security → Settings**：Security Level 设为 **Medium**；**Browser Integrity Check** 打开。
- **SSL/TLS → Edge Certificates**：确认 **Always Use HTTPS** 开、**Minimum TLS Version** = 1.2、**TLS 1.3** 开（这些之前已经开过，确认即可）。
  不要现在开 **HSTS**（服务器已经在发 HSTS 1 周，两边同时设置容易冲突；等观察一段时间再一起调到 6 个月）。
- **Scrape Shield**（左侧 **Scrape Shield** 菜单）：**Email Address Obfuscation** 开，**Hotlink Protection** 开。
- **Security → Events**：以后可以在这里看被拦截的请求，误伤了就把对应规则改成 Managed Challenge 或关掉。

## 出问题怎么回退
每条规则在列表里都有开关，关掉即可，立即生效，不影响服务器。千万不要动 AOP 和代理（橙色云朵）。
