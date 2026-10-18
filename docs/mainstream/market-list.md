# 主流行情列表

- `/mainstream` 默认显示行情列表，覆盖趋势策略币池的 19 个币。
  - 列：最新价、24h 涨跌（绿涨红跌）、24h 成交额、资金费率、30 天日线迷你走势，以及策略持仓标记（多/空 + 权重）。
  - 支持搜索、点表头排序（缺值永远排在最后），三个标签页：自选 / 全部 / 策略持仓。
  - 自选保存在浏览器 localStorage（`auu.ms.favs`）。
- 点一行进入 `/mainstream?symbol=XRP`：原有 K 线 + 纸面交易面板 + 资金费率。
  - 顶部有币种快速切换（带搜索的下拉框，回车选第一个）。
  - “← 行情”返回列表，浏览器后退键也能用。
- 后端 `GET /api/v1/mainstream/markets`：一次返回全部币，需要登录（由鉴权网关拦截）。
  - 基础数据来自本地行情库：后台刷新本来就在维护全部策略币的 1h/1d K 线和资金费。读库结果缓存 15 秒，不额外请求交易所。
  - 可选叠加交易所 24h 批量行情：一次 `fetch_tickers` 覆盖全部币，缓存 3 秒（`AUU_MAINSTREAM_TICKER_SEC`，默认 3，下限 2，上限 600）。只在有人打开页面（或有生效的价格提醒）时才刷新；触发刷新的那个请求最多等 1 秒，其余请求不等。失败后退避 20 秒（遇到 418/429 限流退避 2 分钟），期间回退到本地库数据。`AUU_MAINSTREAM_TICKERS=off` 可关闭。
  - 出站请求：有人在看时最多每 3 秒 1 次（权重 2，约 40 权重/分钟），没人看时为 0，与用户数无关。前端每 5 秒轮询一次本服务缓存。
  - 币安 REST 域名：`AUU_BINANCE_REST_HOSTS`（逗号分隔，默认 `api1.binance.com,api3.binance.com,data-api.binance.vision,api.binance.com`）。第一个失败（超时/网关错误/403/451）自动换下一个，换下后每 10 分钟回头探测首选；只设 `api.binance.com` 即恢复旧行为。默认顺序按东京实测排（api1/api3/data-api 约 18 ms，api.binance.com 约 450 ms）；香港实测相反（api.binance.com 约 73 ms，其余约 225–280 ms）：`AUU_ROLE=standby` 且未设置该变量时，默认顺序自动改为 `api.binance.com` 在前（备用机行为不变）；香港节点切换为主节点（`AUU_ROLE=primary`）后，如要保持香港最快，请在 `/etc/auu/auu.env` 显式设 `AUU_BINANCE_REST_HOSTS=api.binance.com,api1.binance.com,api3.binance.com,data-api.binance.vision`。限流（418/429）不换域名。只影响公共行情的 ccxt 客户端（无密钥）；用户交易所密钥的连接器不受影响。
  - 最后一根 K 线：1h/4h/1d 在有人看图时每 10 秒（`AUU_MAINSTREAM_TAIL_SEC`）补拉前一根+当前根（1 次请求，只放内存，不写 sqlite；落库仍由 5 分钟刷新循环负责）。图表最后一根的收盘/最高/最低用同一份缓存的最新价覆盖，保证和页头价一致（接口多返回 `priceAt` / `tailAt`）。
- 策略币池里只存了 1h/1d 的币（BTC/ETH/SOL 以外），4h K 线由 1h 聚合生成（按 UTC 对齐）。1m/5m/15m 仍然按需拉取，逻辑不变。
- 纸面交易允许的币种扩大到 19 个。单笔 / 单币限额、价格时效、实盘锁全部不变。策略、记账、风控逻辑都没有改。
