# AUU × 交易员学习数据源 · 频道摘要

> 2026-09-21 CST · 完整版：`docs/research/trader-learning-datasources.md`  
> 望舒边界：**学习型因子** · 纸面优先 · **非盲跟单** · 默认不镜像钱包下单  
> 冻结：`TraderSnapshot` / HabitTags=`sniper|mid_curve|graduation_chase|flip|bag`

---

## 一、Pump.fun vs pump.com

| | |
|--|--|
| **主场** | **Pump.fun**（Solana 曲线 → PumpSwap） |
| **官方交易员 API** | **无**（仅程序/IDL/SDK） |
| **pump.com** | **待核实**是否另一产品；勿默认等同 |

---

## 二、数据源（拍板）

| 优先级 | 源 | 借什么 | ToS |
|--------|-----|--------|-----|
| **P0** | 用户自选钱包 + **RPC / Helius** | parsed tx、持仓、自算持有/翻面 | 低（链上公开；守 RPC 商条款） |
| **P0** | 本仓 **`ctx.pump`** | `progress_bps` / `phase` | 低 |
| **后置** | Solana Tracker / Bitquery（**需 Key**） | 排行榜候选、成交聚合对照 | 中（商业） |
| **禁止** | frontend-api · Photon · BullX · **GMGN 刮取** | — | 高 · 不接入 |

```text
Watchlist(自选) → Helius/RPC Snapshot → HabitTags → Distill(需 confirm)
                → pump-paper-v1 → RiskGate → PaperJournal（对照=reference_only）
```

---

## 三、Snapshot → P0 映射（精简）

| 字段组 | P0 源 |
|--------|-------|
| `positions.*` / `open_count` / `gross_exposure_sol` | Helius/RPC 余额 + 自算 cost/PnL；`progress_bps`/`phase`←`ctx.pump` |
| `recent_buys`/`recent_sells` · `buy_notional_1h`/`sell_notional_1h` · `trade_count_1h` | Helius parsed Pump buy/sell |
| `median_hold_sec_24h` · `flip_rate_24h` | 本地 round-trip 聚合 |
| `progress_hist` · `entry_progress_median_bps` | 成交/持仓对齐 `ctx.pump` 进度 |

同义映射：`curve_mid`→`mid_curve`；`quick_flip`→`flip`。

---

## 四、风险 / 文案

- Pump Terms 禁未授权爬虫 → **不刮前端**  
- 蒸馏默认不 apply、不改 auto 下单；`sniper`/`graduation_chase` 慎套入场窗  
- 产品语：**观察 / 蒸馏 / 自有策略**；不用「跟单」

---

## 五、一句落地

> 只读用户自选地址的链上成交与持仓（Helius/RPC），拼 `TraderSnapshot`，蒸馏五类习惯进纸面因子；Tracker/Bitquery 仅后置 enrich；禁止刮取 Pump 前端与 Photon/BullX/GMGN。
