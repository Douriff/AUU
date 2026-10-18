# auu_signal 影子 tick：不再阻塞事件循环（fix/shadow-tick-nonblocking）

范围：只改 `apps/api/app/paper/auu_signal_shadow.py` 的后台 tick 与影子库写入方式。**不改任何信号、成交、净值、统计的计算**
（`auu_signal_v0`、`auu_signal_candidates`、`hypothetical_trades`、`forward_nav` 一行没动，候选参数 hash 测试照旧），不改接口返回字段，
不碰交易 / 积分 / 提现 / 密钥，不部署。

## 问题

`run_loop` 在事件循环里直接调用同步 `_tick_universe()`（每 10 分钟一次）：

1. 19 币 × 500 根日线 × （v0 + 4 个候选）全部重算——纯 Python，占住 uvicorn 主线程；
2. SQLite 连接是 `isolation_level=None`（自动提交），`upsert_rows` / `upsert_cand_bars` 的 `executemany` **每行一次提交**
   （WAL + `synchronous=FULL` = 每行一次 fsync）。一次 tick 约 12 300 次 fsync、约 260 MB 写盘，iowait 高；
3. 结果：tick 期间事件循环停摆，`/api/v1/health` 超时；auu-guard（`curl --max-time 5`，失败后等 20 s 再查一次）有误判重启风险。

而这些数据 99% 的时间没有变化：日线信号一天才变一次（UTC 00:00 收盘），但旧代码每 10 分钟把 ~9 500 + ~3 000 行整个重写一遍。

## 改动

| # | 改动 | 说明 |
|---|---|---|
| 1 | tick 放进 `await asyncio.to_thread(_tick_universe, …)` | 与 recon / shadow_h2 / shadow_s3 / strategy_runner 的写法一致。**影子库连接不跨线程共用**：tick 在工作线程里自己 `SignalShadowStore()` 打开、用完在同一线程 `close()`（HTTP 路由仍用原来的单例连接，两者靠 WAL + 30 s busy timeout 共存）。行情库用的是原有的、带锁的 `MarketStore`，这里只读。 |
| 2 | 批量写入 = 单个 `BEGIN IMMEDIATE … COMMIT` | `upsert_rows`、`upsert_cand_bars`（原来每行一次提交）以及 `replace_cand_trades` / `replace_cand_nav`。出错 `ROLLBACK`（原 `replace_*` 出错会留下一个没关的事务，连接之后无法再 `BEGIN`，一并修掉）。 |
| 3 | 跳过无变化的重算与写入 | 见下节。 |
| 6 | 日志 | 每次实际 tick 打 `start` / `done` 两行，带 UTC 时间戳（`…Z`）、耗时 ms、重算 / 跳过的币数、写入 / 跳过行数、提交数、失败数；不含任何用户或密钥信息。无变化的探测只打 DEBUG。进程没有 root logging 配置（裸 `uvicorn app.main:app`，INFO 会被丢弃），所以给 `auu.signal` 这一个 logger 单独挂了 stderr handler（仅当没有任何 handler 时）。 |

### 跳过无变化（第 3 点）

两层：

* **探测（probe，默认每 120 s）**：对每个币 3 条走主键索引的查询（最近 2 根已收盘日线的 ts/open/close/volume、最后一根日线的 ts/open、最后一条资金费率），
  与上一次成功 tick 看到的比。没变 → 立刻返回，**连影子库都不打开**（0 次写、0 fsync，约 1 ms）。变了（新日线收盘 / 行情库补了数据 / 新资金费率）→ 立即做一次完整检查。
* **完整检查（默认每 600 s，`AUU_SIGNAL_SHADOW_INTERVAL_SEC` 可调，限 60–1800）**：读 500 根日线 + 500 条资金费率，对「计算的全部输入」做指纹
  （已收盘日线的 ts/close/volume、所有日线的 open（次日开盘成交要用）、资金费率、以及 `now` 下哪些日线已收盘）。指纹与上次相同 → 不重算、不写。
  不同 → 用**原函数**重算，只写与库里已有行**不同**的行（比较 side / score / action / reason_tags / factors，或候选交易 / NAV 全部列）。
  另外每 6 小时（`AUU_SIGNAL_SHADOW_FORCE_SEC`）或影子库文件被替换（inode 变化）时忽略指纹整体重算一次（仍按行比较），防止库被外部改动后不自愈。

**心跳**：旧实现每个 tick 把所有行的 `recorded_at` 刷成当前时间，`eval_v0.py` 的 `max(recorded_at)` / `stale_hours` 实际被当作「影子循环还活着」的指标。
不再重写未变行以后，这个值会停在最近一次真有变化的时间（最长一天）。因此每次完整检查用一次极小事务把每币**最新一根**日线的 `recorded_at` 刷成当前时间，
并写 `meta.last_tick_ms`——`max(recorded_at)` 的含义不变（≤ 完整检查间隔）。历史行的 `recorded_at` 保持「首次写入时间」。
**这是唯一的可观察差异**（写入时间戳，不是信号 / 成交 / 统计），见「风险」。

### 对影子盘记录延迟的影响（日线信号 / 新日线 ≤ ~10 分钟）

时间线（UTC 00:00 = 北京时间 08:00 收盘）：

| 环节 | 旧 | 新 |
|---|---|---|
| 行情库拿到新日线 | 刷新循环每 300 s 一轮 → 收盘后 ≤ ~5 分钟（不变） | 同左 |
| 影子循环发现并处理 | 每 600 s 一轮 → 再 ≤ 10 分钟 | 探测每 120 s → 再 ≤ 2 分钟 |
| **收盘 → 写入影子库，最坏** | **~15 分钟 + tick 耗时** | **~7 分钟 + tick 耗时（1–30 s）** |

也就是说，新实现的最坏延迟比旧的更短，满足「新日线收盘后不晚于约 10 分钟内被处理」。

`AUU_SIGNAL_SHADOW_INTERVAL_SEC` 默认保持 600（需求里的 ≤1800 是上限，不是必须）：空闲完整检查只要约 0.07 s、写 ~0.2 MB，拉长间隔几乎省不了什么，反而会拉长心跳间隔。
设成 1800 时：新日线的处理延迟**不受影响**（由探测 `AUU_SIGNAL_SHADOW_PROBE_SEC`，默认 120，限 30–600 决定）；只影响
(a) 「旧日线被行情库修复」「资金费率窗口滑动导致很早的历史行重新推导」这类**非新增**变化最多晚 30 分钟入库；(b) `max(recorded_at)` 心跳最多 30 分钟更新一次——
如果有外部监控把「>30 分钟没更新」当告警，不要调到 1800。

## 结果一致性

测试：`apps/api/tests/test_auu_signal_shadow_tick.py`。

* 「改前」= 本 PR 之前的 `_tick_universe` + `record_candidates` + 逐行自动提交的 `upsert_rows` / `upsert_cand_bars` / `replace_*`，**逐字拷贝自 origin/main 13ff38e**，放在测试里当参照实现。
* 「改后」= 生产代码（`TickState` 缓存、探测 / 指纹 / 行级比较 / 单事务写）。
* 同一份固定合成历史（4 币，日线 + 8 小时资金费率，确定性 LCG，含多空切换，所以 v0 与 4 个候选都有 long / short、已平仓 / 未平仓交易，NAV 非空），
  按真实节奏**逐天回放**：收盘后第一个 tick、今日未收盘那根 K 线价量变化的 tick、08:00 资金费率结算 tick、
  **历史收盘价 / 成交量被修正**（ETH）、**只修正某根 open**（所有币，只影响成交价和 NAV）、**进程重启**（缓存清空、库已最新）。
  每个 tick 之后对 `bars` / `cand_bars` / `cand_trades` / `cand_nav` 四张表按主键排序逐行逐列比较（除 `recorded_at`），必须完全一致。
* 首个 tick 另做一次**含 `recorded_at`** 的逐行比较（同一固定时钟下），也必须完全一致。
* 指纹的敏感性验证过（变异测试）：把 `open` 从候选指纹里拿掉 / 把 `close` 从 v0 指纹里拿掉，测试立刻失败。
* 附带断言：只有行情噪声的 tick 重算 0 币、写 0 行；探测无变化时不打开影子库、不调用任何计算函数；新日线到来时只做探测的 tick（`full=False`）也会处理；
  重启后在已最新的库上写 0 行；批量写入在 sqlite trace 上恰好是 1 个 `BEGIN IMMEDIATE` + 1 个 `COMMIT`，无变化时一个事务都不开；
  失败的批量写入整体回滚且连接可继续使用；tick 在工作线程里开 / 关自己的连接，并且 tick 期间事件循环最大停顿 < 0.25 s（用 0.6 s 的假 tick 验证；旧写法这里会是 ≥ 0.6 s）。

## 实测

方法：`ops/measure/shadow_tick_bench.py`（`io_shim.c` 是 LD_PRELOAD 计数器，只用于测量）。
合成、与线上同量级的数据：19 币（`STRATEGY_UNIVERSE`），行情库里每币 730 根日线 + 2 190 条 8h 资金费率（tick 读最新 500 / 500），
真实 `run_loop` + 同一事件循环里的 uvicorn（`/api/v1/health`，和真实路由一样是普通 `def`）+ 5 ms 的事件循环抖动探针；
**另一个进程**每 50 ms 请求一次 `/api/v1/health`（相当于 guard 的 `curl`），记录最大延迟、超过 5 s 或失败的次数。
写盘量 = LD_PRELOAD 统计的对普通文件的 write 字节数 + `/proc/self/io` 的 `write_bytes`；fsync = `fsync` + `fdatasync` 调用次数。
全部在临时目录，无网络、无交易所请求、无密钥，不碰线上。

场景：`cold`（进程第一次 tick，影子库为空）、`steady`（行情无变化，只有未收盘 K 线在跳——一天 ~143/144 个 tick 是这种）、
`rollover`（19 币同时收盘新日线 + 资金费率结算——每天唯一有真活的一次）、`steady2`、`restart`（新进程状态接一个已最新的库；改前每个 tick 都一样）、
`probe_only_idle`（改后的 120 s 探测）。

机器：本 box（8 vCPU Xeon、virtio 盘、Python 3.13.5）。**这块盘的 fsync 比线上快得多**：改前原样跑一个 tick 只要 1.6–2.0 s，线上观察到的是 8–30 s。
所以做了两组：
* `native`：本 box 实测，没有任何改动；
* `emu1ms`：LD_PRELOAD 在每次 fsync 里 sleep 1 ms，**模拟较慢的云盘**（这是模拟，不是线上实测；改前 fsync 次数乘以 1 ms ≈ 12 s，加上 CPU，落在线上观察到的 8–30 s 区间内）。
写盘量、fsync 次数、CPU 与盘速无关，是可迁移的；耗时 / 停顿以 `emu1ms` 为线上近似。

### native（本 box，未模拟）

| 场景 | 版本 | tick 耗时 s | CPU s | 写入字节 | fsync | 事件循环最大停顿 ms | /health 最大延迟 ms | /health >5 s 或失败 |
|---|---|---|---|---|---|---|---|---|
| cold | 改前 | 1.61 | 1.23 | 172.2 MB | 12 214 | 1 602 | 1 598 | 0 |
| cold | 改后 | 1.05 | 1.00 | 8.3 MB | 130 | 27.6 | 18.5 | 0 |
| steady | 改前 | 1.97 | 1.56 | 210.6 MB | 12 230 | 1 961 | 1 938 | 0 |
| steady | 改后 | **0.07** | 0.07 | **0.2 MB** | **5** | **1.4** | 2.5 | 0 |
| rollover | 改前 | 1.67 | 1.26 | 210.8 MB | 12 309 | 1 668 | 1 645 | 0 |
| rollover | 改后 | 0.88 | 0.82 | 2.3 MB | 110 | 26.8 | 20.0 | 0 |
| steady2 | 改前 | 1.76 | 1.40 | 207.5 MB | 12 306 | 1 758 | 1 751 | 0 |
| steady2 | 改后 | 0.07 | 0.07 | 0.1 MB | 5 | 1.3 | 2.5 | 0 |
| restart | 改前 | 1.84 | 1.31 | 207.5 MB | 12 306 | 1 840 | 1 811 | 0 |
| restart | 改后 | 1.17 | 1.18 | 0.1 MB | 5 | 44.6 | 56.6 | 0 |
| 探测（无变化） | 改后 | 0.001 | 0.001 | 0 | 0 | 1.1 | 2.6 | 0 |

### emu1ms（模拟 1 ms/fsync 的慢盘）

| 场景 | 版本 | tick 耗时 s | CPU s | 写入字节 | fsync | 事件循环最大停顿 ms | /health 最大延迟 ms | /health >5 s 或失败 |
|---|---|---|---|---|---|---|---|---|
| cold | 改前 | 17.56 | 1.91 | 172.2 MB | 12 214 | 17 562 | 17 560 | **1** |
| cold | 改后 | 1.18 | 0.88 | 8.3 MB | 130 | 27.8 | 12.1 | 0 |
| steady | 改前 | 18.22 | 1.92 | 210.6 MB | 12 230 | 18 214 | 18 186 | **1** |
| steady | 改后 | **0.08** | 0.07 | **0.2 MB** | **5** | **1.9** | 2.0 | 0 |
| rollover | 改前 | 17.79 | 1.85 | 210.8 MB | 12 309 | 17 789 | 17 742 | **1** |
| rollover | 改后 | 1.57 | 1.23 | 2.3 MB | 110 | 38.7 | 20.9 | 0 |
| steady2 | 改前 | 17.92 | 1.78 | 207.5 MB | 12 306 | 17 918 | 17 916 | **1** |
| steady2 | 改后 | 0.09 | 0.08 | 0.1 MB | 5 | 3.3 | 6.0 | 0 |
| restart | 改前 | 18.11 | 1.96 | 207.5 MB | 12 306 | 18 108 | 18 086 | **1** |
| restart | 改后 | 0.82 | 0.82 | 0.1 MB | 5 | 28.1 | 41.3 | 0 |
| 探测（无变化） | 改后 | 0.001 | 0.001 | 0 | 0 | 1.1 | 2.1 | 0 |

内核 `write_bytes`（`/proc/self/io`）：改前每个 tick 221–260 MB（与线上观察到的 ~260 MB 吻合），改后 steady 0.1–0.2 MB、rollover 2.8 MB、cold 8.9 MB。
原始数据：`docs/measure/shadow-tick-2026-10-19-{before,after}-{native,emu1ms}.json`。复现：
`python3 ops/measure/shadow_tick_bench.py --api-dir <apps/api> --label X --out X.json`，`--report before.json after.json` 出表；`AUU_SHIM_FSYNC_DELAY_US=1000` 即 emu1ms。

要点：
* **事件循环**：改前每个 tick 停摆等于 tick 耗时（模拟慢盘 ~18 s，期间一次 `/health` 探测 >5 s，正是 guard 超时的情形）；改后最大停顿 ≤ 45 ms（来自 tick 线程的纯 Python 计算与事件循环争 GIL），`/health` 最大 57 ms，没有任何一次 >5 s。
* **每天写盘**：改前 144 个 tick × ~260 MB ≈ 37 GB/天；改后 ~143 次空闲完整检查 × ~0.2 MB + 每天 1 次 rollover 2.8 MB + 每 8 小时资金费率变化时的重算（几乎不写行）≈ 30 MB/天量级。
* 空闲 tick 的 CPU 从 1.2–1.9 s 降到 0.07 s。
* 剩下真有活的 tick（rollover / 重启）仍要 ~1 s CPU（线上 CPU 更慢会更长），但在工作线程里、一天一次（外加每 8 小时资金费率窗口滑动时的一次重算）。

## 其他 run_loop 检查（只报告，没改）

结论：**没有发现第二个在事件循环线程里同步写库 / 重算的 run_loop。**

| loop | 干活方式 | 备注 |
|---|---|---|
| `shadow_s3`（300 s）、`shadow_h2`（300 s）、`strategy_runner`（60 s）、`recon`（600 s）、`alerts`（120 s）、`price_alerts`（5 s）、`status`/uptime（60 s）、`points/trc20`、`news`、`airdrops/ingest`、`MainstreamService.run_loop`（行情刷新，300 s） | 全部 `await asyncio.to_thread(...)` | 写库都在线程里；s3 / h2 / strategy_runner / price_alerts 用 `BEGIN IMMEDIATE` 事务包批量写；`MarketStore` 用默认隔离级别（隐式事务，`executemany` 后一次 `commit`）。 |
| `legacy/pump` 的 `pump_paper_v1.run_loop`、`discovery.run_loop` | `await self.tick()` / websocket 协程 | 仅 `AUU_LEGACY_PUMP=on` 才启动，默认关；没有深入审（内部是否有同步写库未核实）。 |

值得注意的几点（均**未改**）：

1. **单例构造在事件循环线程里执行**：`await asyncio.to_thread(get_shadow().tick)` 里 `get_shadow()`（recon 的 `get_reconciler()`、`production_monitor()`、`production_checker()`、`get_runner()`、`get_fetcher()` 同理）在**调用 to_thread 之前**由事件循环线程求值，所以首次构造（打开 SQLite、建表）在循环线程。空库测得 1–9 ms，可忽略。
   **例外：`NewsStore.__init__` 里的 `retag()`**——启动时读出 `items` 全表并对每条标题跑一遍要闻正则。我造了 30 000 行测得 **约 2.1 s**（即使没有任何行需要改动），第一次 `get_fetcher()` 在 `news.run_loop` 里（启动后 ~20 s）在循环线程执行；线上新闻表多大需要确认。一次性、只在进程启动时，但会让 `/health` 同样超时。
2. 逐行自动提交的模式在别处也存在但量很小且在线程里：`recon` 每天一次 `executemany`（19 币 × 7 天 ≈ 133 行，自动提交）、各处 `INSERT OR REPLACE INTO state` 的小写入。不会阻塞事件循环。
3. 多个 loop 共用 asyncio 默认线程池（`min(32, cpu+4)`），`auu_signal` 的 tick 现在也占其中一个线程（空闲时 ~70 ms，每天几次 ~1–30 s）。CPU 密集部分仍与事件循环争 GIL（实测增加 ≤ 45 ms 停顿）。

## 旋钮

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `AUU_SIGNAL_SHADOW` | `on` | 不变，`off` 关闭整个影子循环 |
| `AUU_SIGNAL_SHADOW_INTERVAL_SEC` | `600`（60–1800） | 完整检查间隔，也是心跳间隔 |
| `AUU_SIGNAL_SHADOW_PROBE_SEC` | `120`（30–600，不超过 INTERVAL） | 探测间隔 = 新日线入库的最大额外延迟 |
| `AUU_SIGNAL_SHADOW_FORCE_SEC` | `21600`（600–604800） | 忽略指纹整体重算的间隔（自愈） |

`run_loop(interval_sec=None, probe_sec=None)`：传参优先于环境变量；旧签名 `run_loop(interval_sec=600)` 仍可用。
