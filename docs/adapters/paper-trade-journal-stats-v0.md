# PaperTradeJournal + stats / MC 草图 v0

对齐：可视化 `success-prob-panel-v0` · 开源 `jesse-montecarlo-for-auu` · 现有 `pump-paper-v1`  
原则：纸面 only；不嵌 Jesse 运行时；`strategy_autopaper` 默认关不影响 journal 记账（手动 Trade 成交也入账）。

---

## 1. `PaperTradeJournal`（内存 → 可落 SQLite）

每笔 **round-trip**（开→平）：

```text
RoundTrip:
  id: str
  strategy_id: str          # pump-paper-v1 | manual-paper
  symbol: str
  mint: str | null
  entry_ts / exit_ts: int   # ms
  entry_price / exit_price: float
  qty: float                # 绝对数量
  pnl: float                # 报价币（SOL/USD 约定与 equity 一致）
  pnl_pct: float
  fees: float
  tags: list[str]           # TAKE_PROFIT / STOP_LOSS / graduation…
  source: signal|manual
  entry_estimated_impact_gross_bps   # 含费；别名 entry_estimated_impact_bps
  entry_protocol_fee_bps             # 曲线 62.5 / AMM 10
  entry_estimated_impact_net_bps     # max(0, gross − fee)
```

平仓时这三项与开仓 Fill 一起写入，使可执行性门的 `median_entry_impact.n` 能与 `n_closed` 对齐。缺 gross 时不编造冲击。

写入时机：
- 开仓 Fill → 建 `open_lot`
- 平仓 Fill（qty 归零或反向）→ 关闭 lot，append journal，推可选 WS `type:"paper_stats"` 或等前端轮询

---

## 2. 聚合 `aggregate(journal) -> PaperStats`

与面板合同一致：

```text
n_trades        = len(closed)
win_rate        = wins / n_trades          # pnl>0
expectancy      = mean(pnl)
max_drawdown_pct = max peak-to-trough on equity curve
equity[]        = {t, equity} 从初始权益累加 pnl
journal[]       = 最近 N 笔（默认 50）
sample_ok       = n_trades >= 20           # 不足则面板标「样本不足」
```

权益：`equity_0`（默认 10000）+ 累计已实现 pnl；未实现可另条虚线（P1）。

---

## 3. HTTP

| Method | Path | 说明 |
|--------|------|------|
| GET | `/api/v1/strategy/pump-paper-v1/stats` | `PaperStats`；`?mc=1&n_paths=500&seed=42` 可选 |
| POST | `/api/v1/strategy/pump-paper-v1/stats/reset` | 清 journal（调试） |

响应包络仍 `{ ok, data }`。

MC（默认关）：
```text
pnls = [rt.pnl for rt in journal]
for i in 1..n_paths:
  path = shuffle(pnls) or bootstrap(pnls)   # P0: 无放回重排顺序
  equity_i = rebuild(equity_0, path)
  total_i, dd_i = summary(equity_i)
→ monte_carlo: { n_paths, p05_pnl, p50_pnl, p95_pnl, p05_dd, p50_dd, sample_ok }
```
短样本 `n_trades < 20`：仍可算，但 `sample_ok=false`，前端灰显分位带。

---

## 4. 与 autopaper / RiskGate

- Journal **不替代** RiskGate；只记账  
- `day_pnl` 可与 journal 当日已实现对齐（二选一真相：Gate 记账为主，journal 对账）  
- 钩子够用，**无需新风控钩子**  
- **永不**把 `source=live` 填进纸面胜率；实盘成交另账 `GET /api/v1/live/ledger`

---

## 5. 实现顺序（给云端）

1. Journal + 开平仓钩进 `run_paper_order` / strategy engine  
2. `GET .../stats` 无 MC  
3. 可选 MC query 参数  
4. 面板接线（可视化已留位）

