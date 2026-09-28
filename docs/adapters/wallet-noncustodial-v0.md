# 非托管钱包 v0

平台把用户自己的 Solana 公钥绑到纸面账户上，用来在 devnet 上走通「服务端组未签名交易 → 用户钱包签名并广播 → 服务端记下签名和确认状态」。

这一步不做主网下单，不做自动跟单，不做服务器端签名。

## 平台不做的事

- 不保存私钥，不接收助记词，不读取 `secrets/live-keypair.json` 来替用户签名。
- 不托管资金，不提供充值或提现。
- 不代替用户签名，不自动跟单，不在纸面循环里发链上交易。
- 每一笔链上交易都要用户本人在自己的钱包里确认。
- `liveEnabled` 保持默认 false。round8b 策略参数和 autopaper 逻辑不走这条链路。
- 钱包流水和纸面成交分开存储，不计入 Go/No-Go，也不进入影子对比。

部署时服务器上不要放置任何用户私钥。`SOLANA_RPC_URL_DEVNET` 只留在服务端环境里；如果 URL 带访问密钥，不要写进前端构建参数。

## 环境变量

| 变量 | 默认 | 作用 |
|------|------|------|
| `AUU_WALLET_MODE` | `off` | `off` 关闭；`devnet` 允许本阶段的 memo；`mainnet` 被本阶段拒绝（`WALLET_DEVNET_ONLY`） |
| `SOLANA_RPC_URL_DEVNET` | `https://api.devnet.solana.com` | 服务端读取最新 blockhash，并查询签名确认状态。超时 4 秒，只在钱包请求里调用 |
| `AUU_WALLET_STORE` | `apps/api/data/wallets.json` | 公钥、风控、同意记录、流水。该目录下的 json 已 gitignore |
| `AUU_WALLET_CHALLENGE_TTL` | `300` | 绑定挑战的有效秒数 |
| `VITE_SOLANA_RPC_DEVNET` | `https://api.devnet.solana.com` | 浏览器连接用的公开 RPC。不要填带密钥的地址 |

本地默认 `AUU_AUTH=off` 时，钱包绑定返回「需要开启账户并登录」。Docker Compose 里 `AUU_AUTH=on` 且 `AUU_WALLET_MODE=off`。

## 绑定

1. 设置页用钱包适配器连接 Phantom 或 Solflare。
2. `POST /api/v1/wallet/challenge` 签发一段带用户名、公钥、nonce、过期时间和网络的挑战。
3. 用户对这段原文做 `signMessage`。
4. `POST /api/v1/wallet/bind` 用公钥做 ed25519 验签。签错、过期、挑战与用户不匹配，或公钥已属于别人，都会拒绝。
5. `POST /api/v1/wallet/unbind` 清掉公钥并关闭该用户的钱包模式。流水保留。

挑战原文说明：此签名只绑定公钥，不授权转账或下单。

## 风控

平台硬上限是单笔 1 SOL、日亏 4.5%、同时持仓 10。用户只能把限额调得更严，前后端都会拒绝更松的值。

开启钱包模式必须勾选风险提示。服务端记下同意版本 `wallet-risk-v1` 和同意时间。新用户默认纸面，绑定公钥本身不会打开钱包模式。

## 总开关

- 环境变量 `AUU_WALLET_MODE=off` 时，谁都不能开钱包模式，也不能生成交易。
- 管理员 `POST /api/v1/wallet/admin/halt` 且 `{"halt": true}` 会持久化关闭所有人的钱包模式。
- 解除总开关不会自动把用户重新打开。每个人要再次勾选风险提示。

## Devnet memo

只在 `AUU_WALLET_MODE=devnet`、未处于总开关关闭、公钥已绑定、用户已开钱包模式时允许。

`POST /api/v1/wallet/devnet/prepare` 向 devnet RPC 取 blockhash，组一笔金额为 0 的 memo 交易（memo 程序 `MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr`，文本 `AUU devnet wallet check`）。返回的交易带一个空签名槽。服务端不广播。

浏览器钱包签名并发送后，`POST /api/v1/wallet/devnet/record` 用绑定公钥验签，再查询 `getSignatureStatuses`，把 `submitted` / `processed` / `confirmed` / `finalized` / `failed` 写入该用户的流水。别人看不到这笔记录。

这是 devnet 测试，不是真钱。网络费从用户自己的 devnet 钱包支付。

## 风险提示全文

非托管钱包风险提示 v1。平台只保存公钥，不保存私钥，不托管资金，不代替签名，不自动下单。每一笔链上交易都要你在自己的钱包里确认，交易不可撤销，网络费从你的钱包支付。当前只开放 devnet 测试，不是真钱。主网默认关闭。平台硬上限是单笔 1 SOL、日亏 4.5%、同时持仓 10。你只能把限额调得更严。

## 手动验证

1. `AUU_AUTH=on`、`AUU_ALLOW_SIGNUP=on`、`AUU_WALLET_MODE=devnet` 启动 API，并启动 web。
2. 注册并登录，打开设置页。应看到「Devnet 测试，不是真钱」。
3. 浏览器安装 Phantom 或 Solflare，把网络切到 Devnet，点「连接钱包」，再点「绑定公钥」并在钱包里签名。
4. 把单笔上限改成大于 1 SOL，保存应被拒绝。改成 0.2 SOL 可以保存。
5. 勾选风险提示后点「开启钱包模式」。
6. 点「签名并发送 devnet memo」。钱包里确认后，下方流水出现签名和状态。
7. 管理员点「关闭所有人的钱包模式」后，再次生成 memo 会被拒绝。解除总开关后，用户仍需重新开启。
8. `AUU_WALLET_MODE` 不设置时，准备交易返回总开关关闭。
