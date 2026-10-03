# 邮件告警与每日摘要

- 通道：复用 AUUTRADE 验证码的 SMTP（`AUU_SMTP_*`），不新增任何密钥。
- 收件人：`AUU_ALERT_TO`，默认 `olesaruga00@gmail.com`。总开关 `AUU_ALERTS=on`。
- 立即告警（每 2 分钟检查一次）：
  - 策略停滞（超过 26h 没调仓）、runner 异常；
  - 每条新的 `risk_events`（同一批合成一封）；
  - 行情过期、没有可用交易所、风控数据熔断（连续 2 次检查都异常才发）。
- 去重和限流：同一 key 在 `AUU_ALERT_COOLDOWN_MIN`（默认 360 分钟）内不重发；每小时最多 `AUU_ALERT_HOURLY_MAX`（6）封、每天最多 `AUU_ALERT_DAILY_MAX`（30）封，超出的记为 suppressed，计入摘要。
- 发送失败按 2、4、8… 分钟退避重试，最多 5 次；health 里的 `alerts` 显示是否配置、最近发送时间、24h 失败数。
- 每日摘要：北京时间 08:30（`AUU_DIGEST_TIME_BJ`）。如果当天调仓还没写入，最多等到 10:00。内容：NAV、当日收益/成本/资金费、持仓、当日调仓成交、近 24h 风控事件、账本记录哈希（当天 `runs` 行 + 当天 fills 的规范 JSON 做 SHA-256）、代码版本、Go/No-Go 进度、S3 影子记录计数。
- 记录：`data_dir()/alerts.sqlite`（与账本分开）。
- 命令：`python -m app.alerts test`（发测试邮件）、`digest-preview`（只打印摘要，不发送）、`status`。
