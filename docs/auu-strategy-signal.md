# AUU 自有策略信号指标 · `auu_signal_v0`（实验中）

> **实验中 / experimental。** 默认不宣称有效。本层只记录与展示，**不驱动**真实调仓 / 下单 / live。
> 通用 MA/EMA/BOLL/MACD/RSI/KDJ 留在图表工具箱（PR#92）；本文件定义的是 **AUU 自有策略信号骨架**。

策略工程合同见（外部对齐稿）：`meme-quant/docs/strategies/auu-signal-indicator-v0.md`。

---

## 1. 输出合同（五字段）

每根**已收盘** K 线一条（`asof_ts` = 该 bar 的 open ms；决策只用 ≤ bar close 的数据）：

| 字段 | 取值 | 说明 |
|------|------|------|
| `side` | `long` \| `short` \| `flat` | 多空态 |
| `score` | **[-1, 1]** | 强度；图上可映射为 [-100,100] 仅作显示 |
| `action` | `enter` \| `hold` \| `exit` \| `none` | 进出；默认 UI 只画 `enter`/`exit` |
| `reason_tags` | string[] | 如 `momentum` `volume_imbalance` `funding` `vol_regime`；资讯门禁为 `risk_gate` |
| `asof_ts` | int (ms) | 防未来函数 |

附加（调试，非合同必填）：`factors.{momentum,volume_imbalance,funding}`。

版本号写死：`auu_signal_v0`。改公式必须升版本，便于影子对照。

---

## 2. v0 公式（冻结）

主分 = **价量失衡 + vol 缩放动量**（+ 可选**已结算** funding z）。资讯**不进**预测主分，只预留 `risk_gate`。

设 bar 序列 close / volume；可选 funding 对齐为「≤ bar close 的最近一次已结算费率」。

1. **动量（vol 缩放）**  
   - 日收益 `r1 = pct_change(close,1)`；`vol20 = rolling_std(r1, 20)`  
   - 对 lookback `L ∈ {5,20}`：`scaled_L = pct_change(close,L) / (vol20 * sqrt(L) + ε)`  
   - `momentum = mean_L tanh(scaled_L)`

2. **成交量失衡**  
   - `vs = mean(vol,5)`，`vl = mean(vol,20)`  
   - `imb = (vs - vl) / (vl + ε)`  
   - `volume_imbalance = sign(r1) * tanh(imb)`

3. **Funding z（可选）**  
   - 对已结算费率序列：`z = (f - mean60) / std60`  
   - `funding = -tanh(z)`（极端正费率 → 偏空；与纸面 funding-carry 直觉同向的影子记录用）

4. **合成**  
   - 权重：`0.50` 动量 + `0.30` 量失衡 + `0.20` funding；缺腿时对剩余权重归一  
   - `score = clip(加权和, -1, 1)`

5. **状态机（滞后）**  
   - 入场阈 `enter_th = 0.25`；离场阈 `exit_th = 0.10`  
   - flat→long/short：\|score\|≥enter；持仓回到 exit 内→flat；越过反向 enter→翻转  
   - `action`：状态变化为 `enter`/`exit`；历史延续为 `none`；**仅最新一根**在持仓中可为 `hold`

6. **`risk_gate`（预留）**  
   - 若该 bar 传入门禁 tags（如资讯事件），强制 `side=flat`、`score=0`，并打上 `risk_gate` + 原 tag  
   - **不**把资讯特征加进主分

代码：`apps/api/app/strategies/auu_signal_v0.py`。参数表见模块内 `PARAMS`。

---

## 3. API

均需登录。前缀：`/api/v1/mainstream/signal`

| 方法 | 路径 | 作用 |
|------|------|------|
| GET | `?symbol=&tf=1d&limit=400` | 现算序列 + 写入影子库 + 返回（含 `experimental: true`） |
| GET | `/latest?symbol=&tf=1d` | 同上，只回最新一点 |
| GET | `/shadow?symbol=&tf=1d&limit=` | 只读已落库影子，不重算 |

响应要点：`version=auu_signal_v0`，`experimental=true`，`series[]` 为五字段，`latest`，`shadowWritten`。

影子库：`data/auu_signal_shadow.sqlite`（与 `mainstream_strategy.sqlite` / H2 / S3 / 用户纸面账本**隔离**）。后台可选 loop（`AUU_SIGNAL_SHADOW=on`，默认 on）按日给策略池币种补记。

---

## 4. 前端（MainstreamChart）

- 开关文案：**「AUU信号（实验）」** / `AUU signal (experimental)`  
- 默认关；偏好键 `auu.ms.auuSignal`（与 `auu.ms.indicators` 并列）  
- 主图：仅 `action ∈ {enter,exit}` 画三角标记  
- 副图：弱化 `score` 线（API [-1,1]，显示可 ×100）  
- 点标记可看 `reason_tags`  
- **不**接入 live / 纸面自动调仓

---

## 5. 何时摘「实验」帽子（对齐纸面 Go）

影子有效门槛（P0 **只搭骨架，不过线不宣称**）：

1. **样本**：持出样本外 ≥30 次闭环（或等价交易日；主流币时间切分 walk-forward）  
2. **扣成本**：手续费 + 滑点/冲击；报 **net expectancy**  
3. **对照**：vs buy&hold、vs 同成本纯 MA/MACD；须净优或显著降回撤  
4. **稳态**：滚动窗期望不靠单段行情；分 exit / vol regime  
5. UI 在未过门槛前不得暗示实盘有效；摘帽需升文档 + 产品确认，并保留 `auu_signal_v0` 对照

「有效」≠ 高胜率；以 **扣成本期望 + 样本外稳定** 为准。

---

## 6. 未做项（P0 之外）

- 资讯因子进主分（明确不做；仅 `risk_gate` 预留）  
- 与 DecisionLog / 纸面 Journal 自动对账闭环统计面板  
- 多 timeframe 信号融合、自适应阈值  
- 摘实验帽后的产品文案与 Go 看板  
- 部署（留给望舒）

---

## 7. 如何本地看影子

```bash
# API（需登录 cookie/token）
curl -s 'http://127.0.0.1:8000/api/v1/mainstream/signal?symbol=BTC&tf=1d' -H "Cookie: ..."
curl -s 'http://127.0.0.1:8000/api/v1/mainstream/signal/shadow?symbol=BTC&tf=1d'

# 库文件（勿与 strategy ledger 混用）
ls apps/api/data/auu_signal_shadow.sqlite
```

交易页：打开某主流币 → 日线或当前 tf → 打开底部 **AUU信号（实验）**。
