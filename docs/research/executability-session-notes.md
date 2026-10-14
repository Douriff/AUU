### 2026-09-21 23:39 Asia/Shanghai — executability autopaper session
- Services: API `/api/v1/health` 200 and Web `/` 200 at start and end; paper mode; `auto_paper_orders=true`; `strategy_autopaper=true`; `liveEnabled=false`; `liveDisabled=true`.
- Polls (~75s, 8 samples): `n_closed` 2 → 4 → 4 → 4 → 5 → 5 → 5 → 5; verdict/lamp remained `no-go`/`gray`.
- Final API snapshot: `n_closed=5`, `n_trades=5`, `median_entry_impact_bps=73.93992864814167`, `max_entry_impact_bps=76.61051079620104`, reject buckets progress `count=4 rate=0.15384615384615385`, impact `count=0 rate=0.0`, risk `count=3 rate=0.11538461538461539`, shadow slippage p50 `2.3972911934816263` bps / p90 `74.00145400046813` bps / n `6`, verdict `no-go`, lamp `gray`.
- Blocker observed: `api.err.log` repeatedly reports PumpPortal WebSocket handshake HTTP 400; API remained healthy and paper sample grew, but discovery reliability needs follow-up.


### 2026-09-21 23:56 Asia/Shanghai — executability autopaper monitoring
- Health: API `/api/v1/health` 200 and Web `/` 200; paper mode; discovery=logs; `auto_paper_orders=true`; `strategy_autopaper=true`; `liveEnabled=false`; `liveDisabled=true`.
- Polls every 90s, 7 samples from 23:47:11 to 23:56:12: `n_closed` 14 → 16 → 17 → 17 → 17 → 17 → 17; verdict/lamp remained `no-go`/`gray`.
- Final stats: `n_closed=17/30`, `n_trades=17`, `median_entry_impact_bps=78.57120979432764`, `max_entry_impact_bps=79.97804256216561`, `expectancy=-0.000288121359349712`; reject buckets progress `count=345 rate=0.3326904532304725`, impact `count=353 rate=0.3404050144648023`, risk `count=303 rate=0.29218900675024106`.
- Final shadow slippage: p50/median `0.0` bps, p90 `101.12117965110575` bps, `n=13`, `ok=true`.
- `n_closed` stalled at 17 after 23:50:12; `api.err.log` contained only startup lines during this session, with no repeated discovery-failure entries to support a root-cause claim.

### 2026-09-22 00:30 Asia/Shanghai — executability autopaper monitoring
- Interval: 00:30:38–00:43:07 CST; API `/api/v1/health` stayed HTTP 200 across 9 polls; Web `/` was HTTP 200 at the final check; no restart was needed.
- Safety/state: paper mode; `auto_paper_orders=true`; `strategy_autopaper=true`; `liveEnabled=false`; `liveDisabled=true`; discovery=`logs`; verdict/lamp=`no-go`/`gray`.
- Growth: `n_closed=23/30` at start and `23/30` at final; no growth resumed. Final `expectancy=-0.0014446510030936724`.
- Final executability: `median_entry_impact_bps=76.6235133352539`; max `79.97804256216561`; shadow slippage p50/median `15.0049999999993` bps, p90 `15.005725026026289` bps, `n=23`.
- Reject buckets (start → final): progress `658/0.3395252837977296` → `586/0.32215503023639364`; impact `702/0.3622291021671827` → `627/0.3446948873007147`; risk `577/0.2977296181630547` → `606/0.3331500824628917`.
- Open positions: `3/3` at final; `max_hold_sec=900`; observed ages at final check: `ONLYUP/SOL=2392s`, `ZHUSH/SOL=2369s`, `BIGP1/SOL=2073s`.


### 2026-09-22 01:02 Asia/Shanghai — executability autopaper monitoring
- Interval: 00:47:10–01:02:10 CST; 11 polls at ~90s. API health stayed `up` on every poll; paper mode; `auto_paper_orders=true`; `strategy_autopaper=true`; `liveEnabled=false`; `liveDisabled=true`; discovery=`logs` (reason=`connecting`).
- Poll sequence: `n_closed` 28 → 31 → 34 → 36, then 0 → 1 → 1 → 1 → 1 → 1 → 1; open count 0 → 3 → 3 → 3 → 1 → 0 → 0 → 0 → 0 → 0 → 0.
- Around 00:52:46 CST the API process was replaced (new PID 571356); no HTTP failure was observed by the monitor, but the in-memory journal reset from `n_closed=36` to `0`. Final snapshot at 01:02:21 CST: `n_closed=1/30`, `median_entry_impact_bps=76.62769528118938`, verdict=`no-go`, open count `0`.
- Orphan check: no open position exceeded `max_hold_sec+120` (threshold 1,020s). One poll at 00:51:40 saw 3 open symbols absent from monitor rows, but their ages were only 79.2–119.1s; the stale-age orphan criterion was not triggered.


### 2026-09-22 01:31 Asia/Shanghai — executability autopaper monitoring
- Interval: 01:11:18–01:31:18 CST; 15 polls at ~90s. API health was HTTP 200/up on every poll; no restart was needed.
- Safety/state: paper mode; `auto_paper_orders=true`; `strategy_autopaper=true`; discovery=`logs` (reason=`connecting`); `liveEnabled=false`; `liveDisabled=true` on every poll.
- Poll sequence: `n_closed` 1 → 1 → 1 → 1 → 1 → 1 → 4 → 4 → 4 → 5 → 5 → 5 → 7 → 8 → 8; final open count `3`.
- Final journal-derived gross: `median_gross=79.08099544816187` bps using `entry_estimated_impact_bps`; provisional `median_net=max(0, gross−62.5)=16.58099544816187` bps.
- Verdict remained `no-go` / `gray` because sample is only `8/30`; provisional net `16.58` bps is below the `<60` bps go threshold and gross `79.08` bps is below the `80` bps hard cap.
- Orphan check: no position exceeded `max_hold_sec+180` (threshold `1080s`); final ages were `PUMPDEMO/SOL=63.5s`, `TIP/SOL=22.8s`, `PORSCHE/SOL=17.7s`; no orphan regression flagged.

### 2026-09-22 01:29–01:49 Asia/Shanghai — PR13 net-of-fee executability monitoring
- Interval: 15 polls at ~90s against `/api/v1/stats/executability`; API health stayed up on every poll, so no restart was needed. Paper safety held throughout: `mode=paper`, `auto_paper_orders=true`, `strategy_autopaper=true`, `liveEnabled=false`, `liveDisabled=true`.
- Poll sequence (`n_closed`, verdict, expectancy): 6/no-go/-0.0024625388295145857 → 8/no-go/-0.0006601032790657783 → 8/no-go/-0.0006601032790657783 → 9/no-go/0.0004328321282727835 → 9/no-go/0.0004328321282727835 → 9/no-go/0.0004328321282727835 → 9/no-go/0.0004328321282727835 → 10/no-go/-0.001528528565515476 → 10/no-go/-0.001528528565515476 → 10/no-go/-0.001528528565515476 → 10/no-go/-0.001528528565515476 → 10/no-go/-0.001528528565515476 → 12/no-go/-0.0012525596779586312 → 12/no-go/-0.0012525596779586312 → 12/no-go/-0.0012525596779586312.
- Final snapshot at 01:49:07 CST: `n_closed=12/30`; `sample_ok=false`; `verdict=no-go`; `median_entry_impact_gross_bps=78.69581883707937`; `median_entry_impact_net_bps=16.195818837079365`; `protocol_fee_bps=62.5`; `expectancy=-0.0012525596779586312`.


### 2026-09-22 01:58 Asia/Shanghai — executability autopaper monitoring
- Interval: 01:32:48–01:57:48 CST; 16 polls at 100s; API remained healthy, no restart.
- Safety/state: paper mode; discovery=logs; `auto_paper_orders=true`; `strategy_autopaper=true`; `liveEnabled=false`; `liveDisabled=true`.
- Final: `n_closed=13/30`; `median_gross=77.73046039679531` bps; `median_net=15.230460396795301` bps; `expectancy=-0.0011856479989213816`; verdict=`no-go`.
- Orphan check: no open position exceeded `max_hold_sec+180` (threshold 1,080s) across the session; final open count was 3.


### 2026-09-22 02:05 Asia/Shanghai — PR13 net-of-fee executability monitoring
- Interval: 02:00:19–02:05:19 CST; 4 polls at ~2-minute intervals against `/api/v1/stats/executability`; API stayed healthy/up, so no restart was needed.
- Safety/state: paper mode; `auto_paper_orders=true`; `strategy_autopaper=true`; `liveEnabled=false`; `liveDisabled=true` throughout.
- Poll sequence (`n_closed`, median gross, median net, expectancy, verdict): 13/78.0334310351152/15.5334310351152/-0.0011856479989213816/no-go → 15/78.67427150509432/16.174271505094325/-0.0009942729706776414/no-go → 15/no change → 15/79.96345920651761/17.463459206517612/-0.0009942729706776414/no-go.
- Final snapshot: `n_closed=15/30`; `sample_ok=false`; `median_gross=79.96345920651761` bps; `median_net=17.463459206517612` bps; protocol fee `62.5` bps; expectancy `-0.0009942729706776414`; verdict=`no-go`. Since the sample did not reach 30, no verdict flip was evaluated; expectancy was also negative, while net remained below 60 bps (gross stayed below the 80 bps hard cap).
- Orphan check: no open position exceeded `max_hold_sec+180` (threshold `1080s`) on any poll; final open count was 3 with ages `HOLDORGAY/SOL=647.9s`, `MLOG/SOL=205.5s`, `CATBRAIN/SOL=202.4s`.

### 2026-09-22 02:06–02:41 Asia/Shanghai — executability autopaper monitoring
- Interval: 02:06:46–02:41:47 CST; 15 polls at 150-second intervals against `/api/v1/stats/executability`; API health stayed up on every poll, so no restart was needed.
- Safety/state: paper mode; `auto_paper_orders=true`; `strategy_autopaper=true`; `liveEnabled=false`; `liveDisabled=true` throughout.
- Poll sequence (`n_closed`): 15 → 15 → 16 → 16 → 16 → 20 → 20 → 20 → 20 → 21 → 21 → 23 → 23 → 24 → 24; verdict remained `no-go`.
- Final snapshot: `n_closed=24/30`; `sample_ok=false`; `median_gross=78.5922143315922` bps; `median_net=16.092214331592203` bps; `expectancy=-0.0033427238193056937`; protocol fee `62.5` bps; gross hard cap `80` bps; verdict=`no-go`.
- Gate state at final poll: sample `false`, expectancy `false`, median-impact gate `true` (net below 60 bps and observed gross max remained within the 80 bps hard cap). Because `n_closed<30`, the full go decision was not eligible and remained no-go for sample.
- Orphan check: no open position exceeded `max_hold_sec+180` (threshold `1080s`) on any poll; maximum sampled age was below threshold and no orphan was flagged.

### 2026-09-22T03:04:11+08:00 — overnight executability autopaper (finish: n_closed>=30)
- Interval start: 2026-09-22T02:19:10+08:00; API polls every ~3 minutes; paper mode; discovery=logs; `auto_paper_orders=true`; `liveEnabled=false` required.
- Gate snapshot: `n_closed=30/30`; sample_ok=`True`; median_net=`17.395532582934138` bps (gate `False`); median_gross=`79.89553258293414` bps (gate `False`); expectancy=`-0.0031711952557495976` (gate `False`); verdict=`no-go`.
- Open positions: `3`; orphan age > max_hold+180: none observed.

### 2026-09-22 03:04:11 Asia/Shanghai — executability session final (n_closed>=30)
- Interval: 02:19:10–03:04:11 Asia/Shanghai; API stayed healthy on every ~3-minute poll; no restart was needed.
- Safety: `mode=paper`; `auto_paper_orders=true`; `liveEnabled=false`; monitoring stopped at the threshold.
- Full gate snapshot: `n_closed=30/30`; `sample_ok=true`; `verdict=no-go`; `median_gross=79.89553258293414` bps; `median_net=17.395532582934138` bps; `expectancy=-0.0031711952557495976`; `nogo_reason=期望 -0.003171 < 0`. Gates: sample `true`, median-impact `false`, expectancy `false`.
- Orphan check: none observed; final open positions were 3 and all ages were below `max_hold_sec+180` (threshold `1080s`).

### 2026-09-22T03:50:10+08:00 — overnight executability autopaper (finish: n_closed>=30)
- Interval start: 2026-09-22T03:14:09+08:00; API polls every ~3 minutes; paper mode; discovery=logs; `auto_paper_orders=true`; `liveEnabled=false` required.
- Gate snapshot: `n_closed=30/30`; sample_ok=`True`; median_net=`15.891708114932001` bps (gate `False`); median_gross=`78.391708114932` bps (gate `False`); expectancy=`-0.001154090929545015` (gate `False`); verdict=`no-go`.
- Open positions: `3`; orphan age > max_hold+180: none observed.

### 2026-09-22 03:50:10 Asia/Shanghai — exit-tuned paper session detail
- Fresh in-memory journal interval: `03:09:25–03:50:10`; monitor interval: `03:14:09–03:50:10`; `n_closed=30/30`; stopped at threshold.
- Exit tags among 30 closed rows: `TAKE_PROFIT=10`, `MAX_HOLD=12`, `STOP_LOSS=5`; 3 closed short/flat rows had no one of these exit tags.
- Final gates: sample `true`; expectancy `-0.001154090929545015` (`false`); median gross `78.391708114932` bps / median net `15.891708114932001` bps (`false`); max gross `80.57324457820368` bps, above the 80 bps hard cap. Verdict `no-go`.
- Safety: `mode=paper`; `auto_paper_orders=true`; `liveEnabled=false`; `liveDisabled=true`; no orphan positions observed by the monitor.
- Params held: TP `0.15%`, SL `0.09%`, max hold `480s`, max impact `80` bps.

### 2026-09-22T04:44:39+08:00 — tightened executability autopaper (periodic ~45m)
- Interval start: 2026-09-22T03:59:38+08:00; polls every ~3 minutes; paper mode; `auto_paper_orders=true`; `liveEnabled=false` required. Params: TP 0.14%, SL 0.09%, max_hold 420s, max_impact 75 bps, max_notional 0.12 SOL.
- Gate snapshot: `n_closed=19/30`; sample_ok=`False`; median_net=`12.407892043608058` bps; median_gross=`74.90789204360806` bps; max_gross=`75.95959766140922` bps; expectancy=`-0.0007651145581473991`; verdict=`no-go`.
- Safety: liveEnabled=`False`; auto_paper_orders=`True`; open positions=`3`; exit tags so far=`{'TAKE_PROFIT': 4, 'paper:pump-paper-v1:flat': 16, 'pump_paper_v1_entry': 3, 'paper:pump-paper-v1': 3, 'MAX_HOLD': 10, 'STOP_LOSS': 2}`.

### 2026-09-22T05:05:39+08:00 — tightened executability autopaper (finish: n_closed>=30)
- Interval start: 2026-09-22T03:59:38+08:00; polls every ~3 minutes; paper mode; `auto_paper_orders=true`; `liveEnabled=false` required. Params: TP 0.14%, SL 0.09%, max_hold 420s, max_impact 75 bps, max_notional 0.12 SOL.
- Gate snapshot: `n_closed=30/30`; sample_ok=`True`; median_net=`12.448715442378159` bps; median_gross=`74.94871544237816` bps; max_gross=`75.95959766140922` bps; expectancy=`-0.0021554963582937387`; verdict=`no-go`.
- Safety: liveEnabled=`False`; auto_paper_orders=`True`; open positions=`3`; exit tags so far=`{'TAKE_PROFIT': 4, 'paper:pump-paper-v1:flat': 27, 'pump_paper_v1_entry': 3, 'paper:pump-paper-v1': 3, 'MAX_HOLD': 18, 'STOP_LOSS': 5}`.

### 2026-09-22T05:07:43+08:00 — round 4 steering executability autopaper (start)
- Interval start: 2026-09-22T05:07:43+08:00; polls every ~3 minutes; paper mode; `auto_paper_orders=true`; `liveEnabled=false` required. Params: TP 0.10%, SL 0.07%, max_hold 300s, progress 1200–6500 bps, max_impact 75 bps, max_notional 0.12 SOL, notional_pct_equity 0.002.
- Gate snapshot: `n_closed=0/30`; sample_ok=`False`; median_net=`None` bps; median_gross=`None` bps; max_gross=`None` bps; expectancy=`None`; verdict=`no-go`.
- Safety: liveEnabled=`False`; liveDisabled=`True`; auto_paper_orders=`True`; open positions=`0`; exit tags so far=`{}`.

### 2026-09-22T05:52:44+08:00 — round 4 steering executability autopaper (periodic ~45m)
- Interval start: 2026-09-22T05:07:43+08:00; polls every ~3 minutes; paper mode; `auto_paper_orders=true`; `liveEnabled=false` required. Params: TP 0.10%, SL 0.07%, max_hold 300s, progress 1200–6500 bps, max_impact 75 bps, max_notional 0.12 SOL, notional_pct_equity 0.002.
- Gate snapshot: `n_closed=11/30`; sample_ok=`False`; median_net=`12.48758562033818` bps; median_gross=`74.98758562033818` bps; max_gross=`75.68370537271996` bps; expectancy=`-0.00031603479610603985`; verdict=`no-go`.
- Safety: liveEnabled=`False`; liveDisabled=`True`; auto_paper_orders=`True`; open positions=`2`; exit tags so far=`{'TAKE_PROFIT': 4, 'STOP_LOSS': 1, 'MAX_HOLD': 5}`.

### 2026-09-22T05:46:44+08:00 — round 4 steering executability autopaper (milestone 10)
- `n_closed=10/30`; sample_ok=`False`; expectancy=`-0.0004306575265920669`; median net/gross=`12.486186424165602`/`74.9861864241656` bps; max gross=`75.68370537271996` bps; verdict=`no-go`.
- Safety: `liveEnabled=False`, `liveDisabled=True`, `auto_paper_orders=True`; exit tags=`{'TAKE_PROFIT': 4, 'STOP_LOSS': 1, 'MAX_HOLD': 4}`.

### 2026-09-22T06:21:30+08:00 — round 4 steering executability autopaper (milestone 20)
- `n_closed=25/30`; sample_ok=`False`; expectancy=`0.0011077373123775229`; median net/gross=`12.450685894152045`/`74.95068589415204` bps; max gross=`75.68370537271996` bps; verdict=`no-go`.
- Safety: `liveEnabled=False`, `liveDisabled=True`, `auto_paper_orders=True`; exit tags=`{'TAKE_PROFIT': 14, 'STOP_LOSS': 1, 'MAX_HOLD': 9}`.

### 2026-09-22T06:31:21+08:00 — round 4 steering executability autopaper (finish: n_closed>=30)
- Interval start: 2026-09-22T05:07:43+08:00; polls every ~3 minutes; paper mode; `auto_paper_orders=true`; `liveEnabled=false` required. Params: TP 0.10%, SL 0.07%, max_hold 300s, progress 1200–6500 bps, max_impact 75 bps, max_notional 0.12 SOL, notional_pct_equity 0.002.
- Gate snapshot: `n_closed=30/30`; sample_ok=`True`; median_net=`12.38569609494224` bps; median_gross=`74.88569609494223` bps; max_gross=`75.68370537271996` bps; expectancy=`0.0012301762147348772`; verdict=`go`.
- Safety: liveEnabled=`False`; liveDisabled=`True`; auto_paper_orders=`True`; open positions=`3`; exit tags so far=`{'TAKE_PROFIT': 16, 'STOP_LOSS': 1, 'MAX_HOLD': 12}`.

### 2026-09-22T06:33:30+08:00 — round 4 steering executability autopaper (milestone 30)
- `n_closed=30/30`; sample_ok=`True`; expectancy=`0.0012301762147348772`; median net/gross=`12.38569609494224`/`74.88569609494223` bps; max gross=`75.68370537271996` bps; verdict=`go`.
- Safety: `liveEnabled=False`, `liveDisabled=True`, `auto_paper_orders=True`; exit tags=`{'TAKE_PROFIT': 16, 'STOP_LOSS': 1, 'MAX_HOLD': 12}`.

### 2026-09-22T06:49:32+08:00 — paper Go autopaper (~40 closes)
- Asia/Shanghai checkpoint; paper mode; `auto_paper_orders=true`; `liveEnabled=false` required. Params: progress 1200–6500 bps, TP 0.10%, SL 0.07%, hold 300s, max impact 75 bps.
- Snapshot: `n_closed=40`; expectancy=`0.00010063399047462995`; median gross=`74.96435823030353` bps; max gross=`75.68370537271996` bps; verdict=`no-go`; open=`1`.

### 2026-09-22T07:07:33+08:00 — paper Go autopaper (~50 closes)
- Asia/Shanghai checkpoint; paper mode; `auto_paper_orders=true`; `liveEnabled=false` required. Params: progress 1200–6500 bps, TP 0.10%, SL 0.07%, hold 300s, max impact 75 bps.
- Snapshot: `n_closed=50`; expectancy=`6.81042907955486e-05`; median gross=`74.95475572229574` bps; max gross=`75.68370537271996` bps; verdict=`go`; open=`3`.

### 2026-09-22T07:54:47+08:00 — Round 5 CLEAN checkpoint (~10 closes)
- Asia/Shanghai; paper mode; journal not reset; live remains disabled; requested parameters asserted.
- Snapshot: `n_closed=11`; expectancy=`-0.005691124117669871`; median gross=`74.9152477750987` bps; max gross=`75.72359498218036` bps; win rate=`0.45454545454545453`; liveEnabled=`False`.

### 2026-09-22T08:09:47+08:00 — Round 5 CLEAN checkpoint (~20 closes)
- Asia/Shanghai; paper mode; journal not reset; live remains disabled; requested parameters asserted.
- Snapshot: `n_closed=20`; expectancy=`-0.0031114089162783153`; median gross=`74.86431065506477` bps; max gross=`75.72359498218036` bps; win rate=`0.5`; liveEnabled=`False`.

### 2026-09-22T08:27:48+08:00 — Round 5 CLEAN checkpoint (~30 closes)
- Asia/Shanghai; paper mode; journal not reset; live remains disabled; requested parameters asserted.
- Snapshot: `n_closed=31`; expectancy=`-0.0019248239497166282`; median gross=`74.7840461020787` bps; max gross=`75.72359498218036` bps; win rate=`0.4838709677419355`; liveEnabled=`False`.

### 2026-09-22T08:50:18+08:00 — Round 6 knife checkpoint (~10 closes)
- Asia/Shanghai; paper mode; journal reset; live remains disabled; exit-progress parameter absent from GET schema and skipped; momentum thresholds frozen at 10 / 2.5.
- Snapshot: `n_closed=12`; expectancy=`-0.0005126914462701446`; win rate=`0.6666666666666666`; median net/gross=`12.468038270220156`/`74.96803827022015` bps; max gross=`74.99705142938552` bps; exit mix=`{'TAKE_PROFIT': 4, 'MAX_HOLD': 7, 'STOP_LOSS': 1}`; liveEnabled=`False`.

### 2026-09-22T08:59:18+08:00 — Round 6 knife checkpoint (~20 closes)
- Asia/Shanghai; paper mode; journal reset; live remains disabled; exit-progress parameter absent from GET schema and skipped; momentum thresholds frozen at 10 / 2.5.
- Snapshot: `n_closed=21`; expectancy=`2.1798720458691648e-05`; win rate=`0.6190476190476191`; median net/gross=`12.464579961678822`/`74.96457996167882` bps; max gross=`74.99705142938552` bps; exit mix=`{'TAKE_PROFIT': 5, 'MAX_HOLD': 15, 'STOP_LOSS': 1}`; liveEnabled=`False`.

### 2026-09-22T09:11:18+08:00 — Round 6 knife checkpoint (~30 closes)
- Asia/Shanghai; paper mode; journal reset; live remains disabled; exit-progress parameter absent from GET schema and skipped; momentum thresholds frozen at 10 / 2.5.
- Snapshot: `n_closed=31`; expectancy=`3.4345023971240524e-05`; win rate=`0.45161290322580644`; median net/gross=`12.297704161132359`/`74.79770416113236` bps; max gross=`74.99705142938552` bps; exit mix=`{'TAKE_PROFIT': 6, 'MAX_HOLD': 24, 'STOP_LOSS': 1}`; liveEnabled=`False`.
