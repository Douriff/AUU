# 成功概率面板 v0

行情 / 交易页紧凑条，绑 `GET /api/v1/strategy/pump-paper-v1/stats`。

## 布局

1. **摘要条**：胜率 / 期望 / 回撤 / 笔数
2. **权益 sparkline**：`data.equity`（连乘 `1+pnl_pct`）
3. **Journal 表**：最近 RoundTrip（symbol / side / pnl% / source）
4. **蒙特卡洛**：默认关；勾选后请求 `?mc=1`
   - `sample_ok=false` → 只显示 **样本不足**，不画 p5–p95 假带
   - `sample_ok=true` → 重抽样分位 + P(权益>起点) / P(触及日亏)

空态：尚无平仓 → 「暂无平仓样本」。文案标明纸面模拟、非承诺。
