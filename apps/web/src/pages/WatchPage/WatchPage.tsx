import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { DistillConfirmDialog } from "@/components/watch/DistillConfirmDialog";
import { HabitTagChips, habitLabel } from "@/components/watch/HabitTagChips";
import { marketProvider } from "@/providers/HttpWsProvider";
import type {
  DistillResult,
  HabitProfile,
  HabitTagName,
  PumpPaperParams,
  TraderSnapshot,
  TraderWatchlistItem,
  WatchSource,
} from "@/types/contracts";

const COMPARE_KEY = "auu:watchCompareId";
const HIST_KEYS = ["0_800", "800_5000", "5000_7500", "7500_9000", "9000_10000", "migrated"] as const;

function shortAddr(address: string): string {
  if (address.length <= 12) return address;
  return `${address.slice(0, 4)}…${address.slice(-4)}`;
}

function explorerUrl(address: string): string {
  return `https://solscan.io/account/${encodeURIComponent(address)}`;
}

async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    /* ignore */
  }
}

export function WatchPage() {
  const { watchId } = useParams<{ watchId?: string }>();
  const location = useLocation();
  const navigate = useNavigate();
  const autoDistill = Boolean((location.state as { distill?: boolean } | null)?.distill);

  const [items, setItems] = useState<TraderWatchlistItem[]>([]);
  const [primaries, setPrimaries] = useState<Record<string, HabitTagName | null>>({});
  const [addr, setAddr] = useState("");
  const [label, setLabel] = useState("");
  const [source, setSource] = useState<WatchSource>("portal");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const [item, setItem] = useState<TraderWatchlistItem | null>(null);
  const [habits, setHabits] = useState<HabitProfile | null>(null);
  const [snapshot, setSnapshot] = useState<TraderSnapshot | null>(null);
  const [distill, setDistill] = useState<DistillResult | null>(null);
  const [currentParams, setCurrentParams] = useState<PumpPaperParams | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);

  const refreshList = useCallback(async () => {
    const data = await marketProvider.listWatchedTraders();
    setItems(data.items);
    const rows = await Promise.all(
      data.items.map(async (row) => {
        try {
          const profile = await marketProvider.getTraderHabits(row.watch_id);
          return [row.watch_id, profile.primary?.tag ?? null] as const;
        } catch {
          return [row.watch_id, null] as const;
        }
      })
    );
    const next: Record<string, HabitTagName | null> = {};
    for (const [id, tag] of rows) next[id] = tag;
    setPrimaries(next);
  }, []);

  useEffect(() => {
    void refreshList().catch((e: Error) => setErr(e.message));
  }, [refreshList]);

  useEffect(() => {
    if (!watchId) {
      setItem(null);
      setHabits(null);
      setSnapshot(null);
      setDistill(null);
      return;
    }
    try {
      localStorage.setItem(COMPARE_KEY, watchId);
    } catch {
      /* ignore */
    }
    setDistill(null);
    setDialogOpen(false);
    void (async () => {
      const data = await marketProvider.listWatchedTraders();
      const found = data.items.find((r) => r.watch_id === watchId) ?? null;
      setItem(found);
      const [h, s, strat] = await Promise.all([
        marketProvider.getTraderHabits(watchId),
        marketProvider.getTraderSnapshot(watchId),
        marketProvider.getStrategy().catch(() => null),
      ]);
      setHabits(h);
      setSnapshot(s);
      if (strat?.params) setCurrentParams(strat.params);
    })().catch((e: Error) => setErr(e.message));
  }, [watchId]);

  useEffect(() => {
    if (watchId && autoDistill) {
      void onDistill();
      navigate(location.pathname, { replace: true, state: {} });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [watchId, autoDistill]);

  const selected = useMemo(() => items.find((r) => r.watch_id === watchId) ?? item, [items, item, watchId]);

  async function onAdd(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      await marketProvider.putWatchedTrader({
        address: addr.trim(),
        label: label.trim() || undefined,
        source,
      });
      setAddr("");
      setLabel("");
      await refreshList();
      setMsg("已加入观察");
    } catch (e) {
      setErr(e instanceof Error ? e.message : "add failed");
    } finally {
      setBusy(false);
    }
  }

  async function onToggle(row: TraderWatchlistItem, enabled: boolean) {
    await marketProvider.putWatchedTrader({
      watch_id: row.watch_id,
      address: row.address,
      enabled,
    });
    await refreshList();
  }

  async function onDelete(id: string) {
    setBusy(true);
    try {
      await marketProvider.deleteWatchedTrader(id);
      if (watchId === id) navigate("/watch");
      await refreshList();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "delete failed");
    } finally {
      setBusy(false);
    }
  }

  async function onDistill() {
    if (!watchId) return;
    setBusy(true);
    setErr("");
    try {
      const result = await marketProvider.distillTrader(watchId);
      setDistill(result);
      const strat = await marketProvider.getStrategy().catch(() => null);
      if (strat?.params) setCurrentParams(strat.params);
      setMsg(result.reject_reason ? "该习惯不建议自动套用入场窗" : "已计算蒸馏建议（未写入）");
    } catch (e) {
      setErr(e instanceof Error ? e.message : "distill failed");
    } finally {
      setBusy(false);
    }
  }

  async function onApply() {
    if (!watchId || !distill) return;
    setBusy(true);
    setErr("");
    try {
      const state = await marketProvider.applyDistill({
        confirm: true,
        source_watch_id: watchId,
        suggested_params: distill.suggested_params,
      });
      if (state.params) setCurrentParams(state.params);
      setDialogOpen(false);
      setMsg("已写入自有策略参数");
    } catch (e) {
      setErr(e instanceof Error ? e.message : "apply failed");
    } finally {
      setBusy(false);
    }
  }

  const rejected = Boolean(distill?.reject_reason);
  const histMax = snapshot
    ? Math.max(1, ...HIST_KEYS.map((k) => snapshot.progress_hist[k] ?? 0))
    : 1;

  return (
    <div className="shell-page">
      <h1>观察</h1>
      <p className="muted">只读观察 · 蒸馏调参 · 自有纸面策略。添加公开地址即可，不会下单。</p>

      {!watchId ? (
        <section className="settings-section">
          <h2>观察列表</h2>
          <form className="paper-form" onSubmit={(e) => void onAdd(e)}>
            <label>
              address
              <input
                type="text"
                value={addr}
                required
                autoComplete="off"
                onChange={(e) => setAddr(e.target.value)}
                placeholder="Solana 公钥"
              />
            </label>
            <label>
              备注
              <input
                type="text"
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                placeholder="选填"
              />
            </label>
            <label>
              源
              <select value={source} onChange={(e) => setSource(e.target.value as WatchSource)}>
                <option value="portal">portal</option>
                <option value="rpc">rpc</option>
                <option value="indexer">indexer</option>
              </select>
            </label>
            <div className="paper-actions">
              <button type="submit" className="ghost" disabled={busy}>
                加入观察
              </button>
            </div>
          </form>
          {items.length === 0 ? (
            <p className="muted">添加要观察的钱包地址（公开成交，不会下单）</p>
          ) : (
            <table className="fill-table observe-table watch-table">
              <thead>
                <tr>
                  <th>备注</th>
                  <th>地址</th>
                  <th>状态</th>
                  <th>源</th>
                  <th>主习惯</th>
                  <th>人工钉</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {items.map((row) => (
                  <tr key={row.watch_id} className={row.watch_id === watchId ? "active-row" : undefined}>
                    <td>
                      {row.label || shortAddr(row.address)}
                      {row.risk_notes ? (
                        <span className="risk-bang" title={row.risk_notes}>
                          !
                        </span>
                      ) : null}
                    </td>
                    <td className="tiny">
                      <span title={row.address}>{shortAddr(row.address)}</span>{" "}
                      <button type="button" className="ghost tiny" onClick={() => void copyText(row.address)}>
                        复制
                      </button>{" "}
                      <a href={explorerUrl(row.address)} target="_blank" rel="noreferrer">
                        浏览器
                      </a>
                    </td>
                    <td>
                      <label className="tiny">
                        <input
                          type="checkbox"
                          checked={row.enabled}
                          onChange={(e) => void onToggle(row, e.target.checked)}
                        />{" "}
                        {row.enabled ? "开" : "关"}
                      </label>
                    </td>
                    <td>
                      <span className={`src-badge src-${row.source}`}>{row.source}</span>
                    </td>
                    <td>
                      {primaries[row.watch_id] ? (
                        <span className={`habit-chip habit-${primaries[row.watch_id]} inline`} title={primaries[row.watch_id] ?? ""}>
                          {habitLabel(primaries[row.watch_id] as string)}
                        </span>
                      ) : (
                        <span className="muted tiny">分析中</span>
                      )}
                    </td>
                    <td>
                      {row.tags_override.length
                        ? row.tags_override.map((t) => (
                            <span key={t} className="habit-chip inline">
                              {habitLabel(t)}
                            </span>
                          ))
                        : "—"}
                    </td>
                    <td className="paper-actions">
                      <Link className="ghost" to={`/watch/${row.watch_id}`}>
                        详情
                      </Link>
                      <button
                        type="button"
                        className="ghost"
                        onClick={() => navigate(`/watch/${row.watch_id}`, { state: { distill: true } })}
                      >
                        蒸馏
                      </button>
                      <button type="button" className="ghost" onClick={() => void onDelete(row.watch_id)}>
                        移除
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      ) : (
        <section className="settings-section">
          <p className="muted">
            <Link to="/watch">← 观察列表</Link>
            {" · "}
            {selected?.label || selected?.address || watchId}
          </p>
          <p className="tiny muted">
            {selected?.address ? (
              <>
                <code>{selected.address}</code>{" "}
                <button type="button" className="ghost tiny" onClick={() => void copyText(selected.address)}>
                  复制
                </button>
              </>
            ) : null}
          </p>

          {habits ? (
            <>
              <h2>HabitTags</h2>
              <p className="muted tiny">
                观察该地址习惯 · median_entry={habits.features.median_entry_progress_bps ?? "—"} · hold_sec=
                {habits.features.median_hold_sec ?? "—"} · flip_rate_24h={habits.features.flip_rate_24h ?? "—"}
              </p>
              <HabitTagChips tags={habits.tags} primary={habits.primary} />
            </>
          ) : (
            <p className="muted">加载习惯…</p>
          )}

          {snapshot ? (
            <div className="snapshot-strip">
              <p className="muted tiny">
                asof={snapshot.asof_ts} · open={snapshot.open_count} · 买={snapshot.buy_notional_1h} · 卖=
                {snapshot.sell_notional_1h} · n={snapshot.trade_count_1h} · hold=
                {snapshot.median_hold_sec_24h ?? "—"}s · flip={snapshot.flip_rate_24h ?? "—"}
                <button
                  type="button"
                  className="ghost tiny"
                  onClick={() => watchId && marketProvider.getTraderSnapshot(watchId).then(setSnapshot)}
                >
                  刷新
                </button>
              </p>
              <div className="hist-bars" title="progress_hist">
                {HIST_KEYS.map((k) => (
                  <i key={k} style={{ height: `${((snapshot.progress_hist[k] ?? 0) / histMax) * 28 + 2}px` }} title={k} />
                ))}
              </div>
              {snapshot.positions.length ? (
                <table className="fill-table tiny">
                  <thead>
                    <tr>
                      <th>mint</th>
                      <th>hold_sec</th>
                      <th>progress_bps</th>
                      <th>phase</th>
                    </tr>
                  </thead>
                  <tbody>
                    {snapshot.positions.map((p) => (
                      <tr key={p.mint}>
                        <td>{p.symbol || shortAddr(p.mint)}</td>
                        <td>{Math.round(p.hold_sec)}</td>
                        <td>{p.progress_bps ?? "—"}</td>
                        <td>{p.phase}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : null}
            </div>
          ) : null}

          <h2>蒸馏为自有策略</h2>
          <div className="paper-actions">
            <button type="button" disabled={busy} onClick={() => void onDistill()}>
              蒸馏为自有策略
            </button>
          </div>
          {distill ? (
            <div className="trade-result">
              {rejected ? <div className="reject-box">{distill.reject_reason}</div> : null}
              <p className="tiny muted">来源 {distill.source_watch_id}</p>
              <table className="fill-table tiny">
                <thead>
                  <tr>
                    <th>参数</th>
                    <th>当前</th>
                    <th>建议</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.keys(distill.suggested_params).length === 0 ? (
                    <tr>
                      <td colSpan={3}>无建议写入键</td>
                    </tr>
                  ) : (
                    Object.entries(distill.suggested_params).map(([k, v]) => (
                      <tr key={k}>
                        <td>{k}</td>
                        <td>{String(currentParams?.[k as keyof PumpPaperParams] ?? "—")}</td>
                        <td>{String(v)}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
              <div className="weight-bars tiny">
                {(["progress", "momentum", "impact"] as const).map((k) => (
                  <label key={k}>
                    {k}
                    <span className="habit-conf">
                      <i style={{ width: `${Math.min(100, (distill.feature_weights[k] / 2) * 100)}%` }} />
                    </span>
                    {distill.feature_weights[k]}
                  </label>
                ))}
              </div>
              <p className="tiny muted">将套用：{distill.enabled_tags.map(habitLabel).join("、") || "—"}</p>
              {rejected ? (
                <button type="button" className="ghost" onClick={() => navigate("/watch")}>
                  仅观察
                </button>
              ) : (
                <button type="button" className="primary-btn" disabled={busy} onClick={() => setDialogOpen(true)}>
                  应用蒸馏
                </button>
              )}
            </div>
          ) : (
            <p className="muted tiny">计算后预览参数差。极早进入 / 临近毕业追涨 默认不改入场窗。</p>
          )}
        </section>
      )}

      {msg ? (
        <p className="ok-text">
          {msg}
          {msg.includes("已写入") ? (
            <>
              {" · "}
              <Link to="/strategy">打开策略参数</Link>
            </>
          ) : null}
        </p>
      ) : null}
      {err ? <p className="error">{err}</p> : null}

      <DistillConfirmDialog
        open={dialogOpen}
        busy={busy}
        onCancel={() => setDialogOpen(false)}
        onConfirm={() => void onApply()}
      />
    </div>
  );
}
