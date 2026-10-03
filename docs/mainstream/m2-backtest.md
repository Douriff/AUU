# M2 回测引擎（自研）

代码：`apps/api/app/backtest/`（引擎）、`apps/api/app/strategies/`（策略，回测和纸面共用）。
纯 Python 标准库实现，不依赖 numpy/pandas，小内存服务器上也能跑；参考了研究脚本的做法（plan §2.1），没有引入 GPL 代码。

## 组成

| 模块 | 作用 |
|---|---|
| `panel.py` | 日线面板：现货收盘/高/低（信号）、永续收盘（盈亏）、当日资金费率之和。数据源：Binance 公开归档（data.binance.vision 月度 zip），或 AUU 自己的 `mainstream.sqlite`（M1 行情；永续价用现货价代替） |
| `costs.py` | 成本模型：taker 0.05% + 每边滑点（BTC/ETH 1bp，SOL/XRP/DOGE/BNB 2bp，其他 3bp），`mult` 做 2 倍成本敏感性 |
| `engine.py` | 逐日组合回测：权重漂移、20% 再平衡带、费用和滑点、资金费（多头付正费率）。第 t 日收盘决定的目标，只赚第 t+1 日的收益 |
| `stats.py` | 年化均值 / CAGR / 波动 / 夏普；块长 20 天、2000 次的 block bootstrap 95% CI；最大回撤、最差月、去掉最好 3 个月；相对国债超额（含 CI） |
| `research.py` | 研究协议：最后 30% 做 hold-out（只看一次）、逐年 walk-forward（有参数网格时每年只用之前的数据选参）、2 倍成本、执行延迟 1 天、基准（BTC 买入持有、币池等权持有、国债）、前视偏差检查 |
| `acceptance.py` | 验收：复现 plan §2.2 的数字 |

## 策略与纸面共用

`app.strategies.TrendTSMOM`（`trend_tsmom_v1`，即 plan 里的 T1b）：
- `targets(panel)`：每天收盘时的目标权重（因果，只用当天及以前的数据），回测用它。
- `decide(panel)`：只取最新一行，纸面/实盘运行器每天收盘调用它。
- 前视检查：在随机截断点上用截断后的数据重算 `decide`，必须和全量 `targets` 的同一行完全相等；测试里还放了一个"偷看明天"的策略，检查必须报错。

## 预注册（结果出来前写死）

- 策略参数：回看 20/60/120 日，只做多（信号 < 0 记 0），单币目标波动 25%，90 日波动率，单币杠杆上限 2，按当日可交易币数平分；有 90 天现货和永续数据才纳入。
- 币池：BTC ETH SOL XRP DOGE BNB ADA AVAX LINK LTC TRX DOT BCH ETC XLM ATOM FIL UNI NEAR（今天还活着的 19 个，存在幸存者偏差）。
- 区间：2020 年只用于预热；评估 2021-01-01 → 2026-09-30；最后 30%（2025-01-09 起）为 hold-out。
- 门槛："有 edge" 只在 hold-out 扣成本后 CI 下限 > 0 时成立；"跑赢国债" 要求超额 CI 下限 > 0。资金费年化仍按 8 小时一期假设。

## 验收结果（2026-10-03）

`python -m app.backtest.acceptance --archive <归档目录>`：**31/31 项通过**，与研究数字差 0.0000 个百分点（容差 0.1pp）。

| 指标 | 引擎 | 研究 |
|---|---|---|
| 训练期年化（夏普） | +15.19%（1.40） | +15.19%（1.40） |
| hold-out 年化 / CAGR | +3.25% / +3.00% | +3.25% / +3.00% |
| hold-out 95% CI | [−6.15%, +11.60%] | [−6.15%, +11.60%] |
| 最大回撤 / 最差月 | −8.13% / −1.33% | −8.13% / −1.33% |
| 去掉最好 3 个月 | −3.65% | −3.65% |
| 2 倍成本 hold-out | +2.79% | +2.79% |
| 年换手 | 6.03 | 6.03 |
| 逐年 2021…2026 | +30.4 / −4.7 / +14.5 / +25.9 / −0.1 / +5.8% | 同左 |
| BTC 买入持有 hold-out | CAGR −7.19%，MDD −52.97% | 同左 |
| 19 币等权持有 hold-out | CAGR −24.36%，MDD −66.75% | 同左 |
| 国债 | 3.99% | 3.99% |
| 前视检查 | 差值 0 | — |

CI 依赖随机数流。研究用的是一个 numpy `default_rng(7)`、按调用顺序共用；装了 numpy（`requirements-dev.txt`）时，验收会重放同一条随机数流，CI 精确一致。没装 numpy 时用引擎自带的固定种子随机数，CI 按"参考 CI 宽度的 5%"做蒙特卡洛容差（例如 hold-out 得到 [−6.23%, +12.20%]）。

结论不变：hold-out CI 跨 0，**edge 未证明，也没有证据稳定跑赢国债**。

## 用法

```bash
cd apps/api
# 研究归档（文件名 {spot,perp,fund}_{COIN}_{YYYY-MM}.zip，下载地址见 panel.ARCHIVE_URLS）
python -m app.backtest --archive ./binance-archive --walk-forward-from 2023 --json out.json
# AUU 自己的行情库（读 AUU_DATA_DIR 下的 mainstream.sqlite）
python -m app.backtest --store --coins BTC,ETH,SOL --start 2025-01-01
# 验收
python -m app.backtest.acceptance --archive ./binance-archive
```

## 还没做（M3）

纸面运行器（每天收盘调用 `decide`、CEX PaperBroker 成交、资金费计提、新风控）和仪表盘。
