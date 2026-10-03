import { SkRows } from "@/components/ui/Skeleton";
import { useEffect, useMemo, useRef, useState } from "react";
import { useConsole } from "@/hooks/useConsole";
import type { ConsoleEvent } from "@/types/contracts";
import { StrategyPanel } from "@/components/strategy/StrategyPanel";
import { ShadowS3Panel } from "@/components/strategy/ShadowS3Panel";
import { ReconPanel } from "@/components/strategy/ReconPanel";

const FILTERS: { id: string; label: string }[] = [
  { id: "all", label: "全部" },
  { id: "discovery", label: "发现" },
  { id: "entry", label: "开仓" },
  { id: "exit", label: "平仓" },
  { id: "reject", label: "拒绝" },
  { id: "shadow", label: "影子" },
  { id: "system", label: "系统" },
];

const CLOCK = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Shanghai",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

function clock(ts: number): string {
  if (!ts) return "--:--:--";
  return CLOCK.format(new Date(ts));
}

function formatSol(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "";
  const digits = Math.abs(n) >= 1 ? 4 : 6;
  const body = Math.abs(n)
    .toFixed(digits)
    .replace(/(\.\d*?[1-9])0+$/, "$1")
    .replace(/\.0+$/, "");
  if (n > 0) return `+${body}`;
  if (n < 0) return `-${body}`;
  return body;
}

function formatBps(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const digits = Math.abs(n) >= 10 ? 0 : 1;
  const body = Math.abs(n).toFixed(digits);
  if (n > 0) return `+${body}`;
  if (n < 0) return `-${body}`;
  return body;
}

function tone(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n === 0) return "flat";
  return n > 0 ? "up" : "down";
}

export function ConsolePage() {
  const { events, stats, err } = useConsole(2000);
  const [filter, setFilter] = useState("all");
  const [paused, setPaused] = useState(false);
  const follow = useRef(true);
  const scroller = useRef<HTMLDivElement>(null);

  const rows = useMemo(
    () => (filter === "all" ? events : events.filter((row) => row.type === filter)),
    [events, filter],
  );

  useEffect(() => {
    const el = scroller.current;
    if (!el || paused || !follow.current) return;
    el.scrollTop = el.scrollHeight;
  }, [rows, paused]);

  function onScroll() {
    const el = scroller.current;
    if (!el || paused) return;
    follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
  }

  function togglePause() {
    setPaused((prev) => {
      const next = !prev;
      if (!next) {
        follow.current = true;
        const el = scroller.current;
        if (el) el.scrollTop = el.scrollHeight;
      }
      return next;
    });
  }

  const verdict = stats?.verdict === "go" ? "Go" : stats?.verdict === "pending" ? "待评估" : "No-Go";
  const lamp = stats?.lamp || "gray";

  return (
    <div className="console-page pro-page">
      <header className="pro-head">
        <h1>策略控制台</h1>
        <p>趋势策略纸面运行、风控与执行记录</p>
      </header>
      <section className="console-stats" aria-label="今日纸面">
        <Stat label="持仓" value={stats ? String(stats.open_positions) : "—"} />
        <Stat label="今日平仓" value={stats ? String(stats.closed_today) : "—"} />
        <Stat
          label="今日净盈亏"
          value={stats ? `${formatSol(stats.pnl_today) || "0"}${stats.go_window_label === "mainstream" ? "" : " SOL"}` : "—"}
          tone={tone(stats?.pnl_today)}
        />
        <Stat
          label="平均净收益 (bp)"
          value={stats ? formatBps(stats.avg_net_bps) : "—"}
          tone={tone(stats?.avg_net_bps)}
        />
        <span
          className={`console-go lamp-${lamp}`}
          title={stats?.nogo_reason || stats?.go_window_label || ""}
        >
          {verdict}
        </span>
      </section>

      <StrategyPanel />

      <ReconPanel />

      <ShadowS3Panel />

      <section className="console-card console-log-card" aria-label="事件日志">
        <header className="console-card-bar">
          <div className="console-title">
            <span className={`console-live${paused ? " is-paused" : ""}`} aria-hidden="true" />
            控制台
          </div>
          <div className="console-chips" role="tablist" aria-label="事件类型">
            {FILTERS.map((item) => (
              <button
                key={item.id}
                type="button"
                role="tab"
                aria-selected={filter === item.id}
                className={filter === item.id ? "is-on" : ""}
                onClick={() => setFilter(item.id)}
              >
                {item.label}
              </button>
            ))}
          </div>
          <button type="button" className="console-pause" onClick={togglePause} aria-pressed={paused}>
            {paused ? "继续跟随" : "暂停滚动"}
          </button>
        </header>
        {err ? <p className="console-err">{err}</p> : null}
        <div className="console-log" ref={scroller} onScroll={onScroll}>
          {rows.length === 0 ? (
            stats ? (
              <p className="console-empty">此类型暂无事件</p>
            ) : (
              <SkRows rows={6} cols={3} h={30} />
            )
          ) : (
            rows.map((row) => <LogRow key={row.id} row={row} />)
          )}
        </div>
      </section>
    </div>
  );
}

function Stat({ label, value, tone: toneName }: { label: string; value: string; tone?: string }) {
  return (
    <div className="console-stat">
      <span>{label}</span>
      <strong className={toneName || ""}>{value}</strong>
    </div>
  );
}

function LogRow({ row }: { row: ConsoleEvent }) {
  const pnl = formatSol(row.pnl);
  const [open, setOpen] = useState(false);
  return (
    <div
      className={`console-row type-${row.type}${open ? " is-open" : ""}`}
      role="button"
      tabIndex={0}
      aria-expanded={open}
      title={row.message}
      onClick={() => setOpen((v) => !v)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          setOpen((v) => !v);
        }
      }}
    >
      <time dateTime={new Date(row.ts).toISOString()}>{clock(row.ts)}</time>
      <span className="console-pill">{row.pill}</span>
      <span className="console-msg">{row.message}</span>
      <span className={`console-pnl ${tone(row.pnl)}`}>{pnl}</span>
    </div>
  );
}
