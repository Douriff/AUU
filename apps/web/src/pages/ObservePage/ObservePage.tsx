import { useCallback, useEffect, useState, type FormEvent } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import type {
  CompareReport,
  DistillResult,
  HabitProfile,
  TraderSnapshot,
  TraderWatchlistItem,
} from "@/types/contracts";

export function ObservePage() {
  const [items, setItems] = useState<TraderWatchlistItem[]>([]);
  const [reader, setReader] = useState("mock");
  const [copyTrade, setCopyTrade] = useState(false);
  const [selected, setSelected] = useState<string>("");
  const [habits, setHabits] = useState<HabitProfile | null>(null);
  const [snapshot, setSnapshot] = useState<TraderSnapshot | null>(null);
  const [distill, setDistill] = useState<DistillResult | null>(null);
  const [compare, setCompare] = useState<CompareReport | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [addr, setAddr] = useState("");
  const [label, setLabel] = useState("");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    const data = await marketProvider.listWatchedTraders();
    setItems(data.items);
    setReader(data.reader);
    setCopyTrade(Boolean(data.copy_trade_enabled));
    setSelected((cur) => cur || data.items[0]?.watch_id || "");
  }, []);

  useEffect(() => {
    void refresh().catch((e: Error) => setErr(e.message));
  }, [refresh]);

  useEffect(() => {
    if (!selected) return;
    setDistill(null);
    setCompare(null);
    setConfirm(false);
    void Promise.all([
      marketProvider.getTraderHabits(selected),
      marketProvider.getTraderSnapshot(selected),
    ])
      .then(([h, s]) => {
        setHabits(h);
        setSnapshot(s);
        setErr("");
      })
      .catch((e: Error) => setErr(e.message));
  }, [selected]);

  async function onAdd(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      await marketProvider.putWatchedTrader({ address: addr.trim(), label: label.trim() || undefined });
      setAddr("");
      setLabel("");
      await refresh();
      setMsg("已加入观察列表");
    } catch (e) {
      setErr(e instanceof Error ? e.message : "add failed");
    } finally {
      setBusy(false);
    }
  }

  async function onDelete(id: string) {
    setBusy(true);
    try {
      await marketProvider.deleteWatchedTrader(id);
      if (selected === id) setSelected("");
      await refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "delete failed");
    } finally {
      setBusy(false);
    }
  }

  async function onDistill() {
    if (!selected) return;
    setBusy(true);
    setErr("");
    try {
      const result = await marketProvider.distillTrader(selected);
      setDistill(result);
      setMsg(result.reject_reason ? "蒸馏拒绝自动套用入场窗" : "已计算蒸馏建议（未写入）");
    } catch (e) {
      setErr(e instanceof Error ? e.message : "distill failed");
    } finally {
      setBusy(false);
    }
  }

  async function onApply() {
    if (!selected || !confirm) {
      setErr("apply-distill 需要勾选 confirm=true");
      return;
    }
    setBusy(true);
    setErr("");
    try {
      const state = await marketProvider.applyDistill({
        confirm: true,
        source_watch_id: selected,
        suggested_params: distill?.suggested_params,
      });
      setMsg(
        `已写入纸面参数 progress_bps=[${state.params.progress_bps_min},${state.params.progress_bps_max}]；auto_paper_orders=${String(state.auto_paper_orders)}`
      );
    } catch (e) {
      setErr(e instanceof Error ? e.message : "apply failed");
    } finally {
      setBusy(false);
    }
  }

  async function onCompare() {
    if (!selected) return;
    try {
      setCompare(await marketProvider.compareTrader(selected));
    } catch (e) {
      setErr(e instanceof Error ? e.message : "compare failed");
    }
  }

  return (
    <div className="shell-page">
      <h1>观察 / Distill</h1>
      <p className="muted">
        观察公开地址 → 习惯标签 → 蒸馏为自有 <code>pump-paper-v1</code> 参数。
        不是镜像钱包。
        <code>copy_trade_enabled={String(copyTrade)}</code>
        {" · "}
        reader=<code>{reader}</code>
        。须勾选确认后才 apply；默认不改 <code>auto_paper_orders</code>。
      </p>

      <section className="settings-section">
        <h2>观察列表</h2>
        <form className="paper-form" onSubmit={(e) => void onAdd(e)}>
          <label>
            address
            <input type="text" value={addr} onChange={(e) => setAddr(e.target.value)} placeholder="Solana 公钥" />
          </label>
          <label>
            label
            <input type="text" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="备注（可选）" />
          </label>
          <div className="paper-actions">
            <button type="submit" className="ghost" disabled={busy}>
              加入观察
            </button>
          </div>
        </form>
        <table className="fill-table observe-table">
          <thead>
            <tr>
              <th>label</th>
              <th>address</th>
              <th>primary</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {items.map((row) => (
              <tr
                key={row.watch_id}
                className={row.watch_id === selected ? "active-row" : undefined}
                onClick={() => setSelected(row.watch_id)}
              >
                <td>{row.label || "—"}</td>
                <td className="tiny">{row.address}</td>
                <td>{row.watch_id === selected ? habits?.primary?.tag ?? "…" : "—"}</td>
                <td>
                  <button
                    type="button"
                    className="ghost"
                    onClick={(e) => {
                      e.stopPropagation();
                      void onDelete(row.watch_id);
                    }}
                  >
                    删除
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {habits ? (
        <section className="settings-section">
          <h2>HabitTags</h2>
          <p className="muted">
            primary=<code>{habits.primary?.tag ?? "none"}</code>
            {" · "}
            median_entry={habits.features.median_entry_progress_bps ?? "—"}
            {" · "}
            hold_sec={habits.features.median_hold_sec ?? "—"}
            {" · "}
            flip_rate_24h={habits.features.flip_rate_24h ?? "—"}
          </p>
          <ul className="habit-tags">
            {habits.tags.map((t) => (
              <li key={t.tag}>
                <code>{t.tag}</code> {t.confidence.toFixed(2)} — {t.evidence.join("; ")}
              </li>
            ))}
          </ul>
          {snapshot ? (
            <p className="muted tiny">
              open_count={snapshot.open_count} · entry_progress_median_bps=
              {snapshot.entry_progress_median_bps ?? "—"} · buys={snapshot.recent_buys.length} · sells=
              {snapshot.recent_sells.length}
            </p>
          ) : null}
        </section>
      ) : null}

      <section className="settings-section">
        <h2>蒸馏 → 自有策略</h2>
        <div className="paper-actions">
          <button type="button" disabled={busy || !selected} onClick={() => void onDistill()}>
            计算蒸馏
          </button>
          <button type="button" className="ghost" disabled={!selected} onClick={() => void onCompare()}>
            对照（reference_only）
          </button>
        </div>
        {distill ? (
          <div className="trade-result">
            {distill.reject_reason ? <div className="reject-box">{distill.reject_reason}</div> : null}
            <pre className="tiny observe-json">{JSON.stringify(distill.suggested_params, null, 2)}</pre>
            <p className="muted tiny">
              tags={distill.enabled_tags.join(", ") || "—"} · weights=
              {JSON.stringify(distill.feature_weights)}
            </p>
            <label className="checkbox-row">
              <input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} />
              confirm=true（写入纸面参数；不改 auto_paper_orders）
            </label>
            <div className="paper-actions">
              <button type="button" className="buy" disabled={busy || !confirm} onClick={() => void onApply()}>
                应用蒸馏
              </button>
            </div>
          </div>
        ) : (
          <p className="muted">先计算蒸馏。sniper / graduation_chase 默认拒绝放宽入场窗。</p>
        )}
        {compare ? <p className="muted tiny">{compare.note}</p> : null}
      </section>
      {msg ? <p className="ok-text">{msg}</p> : null}
      {err ? <p className="error">{err}</p> : null}
    </div>
  );
}
