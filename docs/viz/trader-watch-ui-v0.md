# 可视化 · 交易员观察 / 习惯蒸馏 UI 规格 v0

对齐：`docs/adapters/trader-watch-distill-v0.md`  
产品词：**观察 / 蒸馏 / 自有策略**。  
**禁止**文案：「跟单」「复制交易」「镜像下单」「copy trade」等（含英文同义营销语）。

纸面优先；观察与蒸馏永不触发 live；私钥不进 UI。

---

## 1. 信息架构

| 入口 | 路由建议 | 说明 |
|------|----------|------|
| 观察列表 | `/watch` 或 Market 侧栏 Tab「观察」 | `TraderWatchlist` |
| 对象详情 | `/watch/:watchId` | Snapshot + HabitTags + 蒸馏 |
| 自有策略 | 现有 Strategy / Settings | 应用蒸馏后的 params；autopaper 默认仍关 |
| 成功概率 | PaperStatsPanel | 胜率旁「对照」折叠（reference_only） |

顶栏副文案示例：「只读观察 · 蒸馏调参 · 自有纸面策略」——不用跟单语。

---

## 2. Watchlist（观察列表）

### 2.1 列表列

| 列 | 字段 | UI |
|----|------|-----|
| 备注 | `label` | 可编辑；空则显示短地址 |
| 地址 | `address` | 截断 + 复制；外链区块浏览器（只读） |
| 状态 | `enabled` | 开关；关=停订阅 Snapshot |
| 源 | `source` | `portal` / `rpc` / `indexer` 小徽标 |
| 主习惯 | `primary.tag` | 见 §3 色点；无则「分析中」 |
| 人工钉 | `tags_override` | 可选 chip |
| 风险备忘 | `risk_notes` | 叹号 tooltip（如「疑似 bot」） |
| 操作 | — | 详情 / 蒸馏 / 移除 |

空态：「添加要观察的钱包地址（公开成交，不会下单）」

### 2.2 添加

- 表单：`address`（必填）· `label`（选填）· `source`（默认 portal）
- 校验：Solana 地址格式；**无**私钥/助记词字段
- 成功 toast：「已加入观察」

API：`GET/PUT /api/v1/watch/traders`

### 2.3 与盘面关系

- Snapshot **不**画进 K 线 Overlay 成交点
- 可选：从详情「在盘面打开某 mint」→ 切换 Market symbol（仍是自有图）

---

## 3. HabitTags（习惯标签）

展示 `HabitProfile.tags[]`：

| tag | 中文标签（固定） | 色 |
|-----|------------------|-----|
| `sniper` | 极早进入 | 警示橙 |
| `mid_curve` | 曲线中段 | 主色绿 |
| `graduation_chase` | 临近毕业追涨 | 警示红 |
| `flip` | 短持快出 | 紫 |
| `bag` | 长持持仓 | 蓝灰 |

每条 chip：
- 标题 = 中文标签（tooltip 显示英文 `tag` 键）
- `confidence` → 条或百分比
- 展开 `evidence[]` 短句

`primary` 加大号 +「主习惯」角标。

文案禁区示例替换：
- ❌「跟单该地址」→ ✅「观察该地址习惯」
- ❌「复制其交易」→ ✅「蒸馏为自有参数（需确认）」

API：`GET /api/v1/watch/traders/{id}/habits`  
可选 WS：`habit_alert` → 顶栏/详情轻提示，不进 RiskTagBar 原因码（除非后端另映射）。

---

## 4. 蒸馏确认对话框（核心）

触发：详情页按钮 **「蒸馏为自有策略」**（计算）→ 出结果卡 → **「应用蒸馏」** 才开确认框。

### 4.1 结果卡（`DistillResult`，只读预览）

- 来源：`source_watch_id` / 备注名
- 建议参数表：`suggested_params` 键值「当前 → 建议」diff
- `feature_weights`：progress / momentum / impact 条
- `enabled_tags`：将套用的习惯
- 若 `reject_reason`：红底说明（如 sniper 主导不建议改入场窗），**隐藏或禁用「应用」**，仅可「仅观察」

### 4.2 确认对话框文案（必须）

标题：`应用蒸馏参数到自有策略？`

正文要点（固定句，可微调）：
1. 将把下列参数写入 **自有** `pump-paper-v1`，不是自动复制该钱包的下一笔成交。
2. **不会**打开实盘（`liveEnabled` 不变）；**不会**自动打开 `strategy_autopaper`（保持当前默认关，除非用户另开）。
3. 仍走 RiskGate → PaperBroker 纸面路径。

勾选（必勾才能确认）：
- [ ] 我理解这是参数蒸馏，不是跟单/复制交易。

按钮：
- 主按钮：`确认应用` → `POST .../apply-distill` with `confirm=true`
- 次按钮：`取消`
- 危险样式不用（非删数据）；主按钮 primary

### 4.3 成功后

- toast：「已写入自有策略参数」
- 链到 Strategy/Settings 参数页
- 不跳转开 live

---

## 5. Journal 旁 reference_only 对照

在 `PaperStatsPanel` 胜率摘要旁：

```
胜率 xx%  期望 …  回撤 …     [对照 ▾]
```

折叠面板标题：`观察对照（仅参考）`

内容（`CompareReport`）：
- 自有纸面：`self`（现有 stats）
- 观察对象：`trader_ref`（n_trades / approx_pnl / tags_hist）
- 页脚固定小字：`reference_only — 非复制交易归因；胜率分母仍只含自有纸面 Journal`

交互：
- 默认折叠
- 无 `trader_ref` 时折叠入口 disabled + tooltip「先选择观察对象并生成对照」
- **禁止**把 `trader_ref` 笔数并入胜率环图

---

## 6. Snapshot 详情区（辅助）

详情页中栏：
- 持仓表：`positions[]`（mint、hold_sec、progress_bps、phase）
- 近窗：买/卖 notional、trade_count、median_hold、flip_rate
- `progress_hist` 小柱（与盘面 progress 视觉同源，数据属观察对象）

刷新：`asof_ts` 展示；手动刷新按钮。

WS：`trader_snapshot` 更新详情；不污染 Overlay Fill。

---

## 7. 与现有模块边界

| 模块 | 关系 |
|------|------|
| Watchlist 新币 `new_token` | 产品不同：币 vs 钱包；侧栏可并列，勿混列表 |
| Overlay `signal`/`fill` | 仅自有策略事件 |
| RiskTagBar | 仍吃 RiskOut；蒸馏警示优先详情/habit_alert |
| live-ui-gates | 本模块零耦合；无开闸入口 |
| success-prob-panel | 只扩展「对照」折叠 |

---

## 8. 实现切片

**P0**
1. `/watch` CRUD 列表 + 添加地址（无私钥）  
2. 详情 HabitTags chip + evidence  
3. 蒸馏结果卡 + 确认对话框（禁词文案）  
4. PaperStats「对照」折叠壳（可先 mock reference_only）

**P1** Snapshot 持仓/近窗真数据  
**P2** habit_alert 轻提示；对照曲线图

---

## 9. 验收

- [ ] 全产品搜不到「跟单」「复制交易」作为功能文案  
- [ ] 应用蒸馏须勾选理解项 + `confirm=true`  
- [ ] 应用后 autopaper / live 默认状态不变  
- [ ] 胜率分母不含观察对象成交  
- [ ] 无私钥输入框  

版本：v0。字段名跟 adapter；算法阈值可调。
