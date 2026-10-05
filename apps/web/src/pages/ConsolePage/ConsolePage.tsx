import { msgText } from "@/i18n/msg";
import { SkRows } from "@/components/ui/Skeleton";
import { useEffect, useMemo, useRef, useState } from "react";
import { useConsole } from "@/hooks/useConsole";
import type { ConsoleEvent } from "@/types/contracts";
import { StrategyPanel } from "@/components/strategy/StrategyPanel";
import { ShadowS3Panel } from "@/components/strategy/ShadowS3Panel";
import { ReconPanel } from "@/components/strategy/ReconPanel";
import { useTranslation } from "react-i18next";
import { dateFmt, decimalSep, fmtFixed } from "@/i18n/format";

// label = i18n key under console.f.*
const FILTERS: string[] = ["all", "discovery", "entry", "exit", "reject", "shadow", "system"];

function clock(ts: number): string {
  if (!ts) return "--:--:--";
  return dateFmt({ timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date(ts));
}

function formatSol(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "";
  const digits = Math.abs(n) >= 1 ? 4 : 6;
  const body = Math.abs(n)
    .toFixed(digits)
    .replace(/(\.\d*?[1-9])0+$/, "$1")
    .replace(/\.0+$/, "")
    .replace(".", decimalSep());
  if (n > 0) return `+${body}`;
  if (n < 0) return `-${body}`;
  return body;
}

function formatBps(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const digits = Math.abs(n) >= 10 ? 0 : 1;
  const body = fmtFixed(Math.abs(n), digits);
  if (n > 0) return `+${body}`;
  if (n < 0) return `-${body}`;
  return body;
}

function tone(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n === 0) return "flat";
  return n > 0 ? "up" : "down";
}

export function ConsolePage() {
  const { t } = useTranslation();
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

  const verdict = stats?.verdict === "go" ? "Go" : stats?.verdict === "pending" ? t("go.pendingShort") : "No-Go";
  const lamp = stats?.lamp || "gray";

  return (
    <div className="console-page pro-page">
      <header className="pro-head">
        <h1>{t("perf.consoleLink")}</h1>
        <p>{t("console.sub")}</p>
      </header>
      <section className="console-stats" aria-label={t("console.todayAria")}>
        <Stat label={t("ml.held")} value={stats ? String(stats.open_positions) : "—"} />
        <Stat label={t("console.closedToday")} value={stats ? String(stats.closed_today) : "—"} />
        <Stat
          label={t("console.pnlToday")}
          value={stats ? `${formatSol(stats.pnl_today) || "0"}${stats.go_window_label === "mainstream" ? "" : " SOL"}` : "—"}
          tone={tone(stats?.pnl_today)}
        />
        <Stat
          label={t("console.avgNet")}
          value={stats ? formatBps(stats.avg_net_bps) : "—"}
          tone={tone(stats?.avg_net_bps)}
        />
        <span
          className={`console-go lamp-${lamp}`}
          title={msgText(stats?.nogo_msg, stats?.nogo_reason || "") || stats?.go_window_label || ""}
        >
          {verdict}
        </span>
      </section>

      <StrategyPanel />

      <ReconPanel />

      <ShadowS3Panel />

      <section className="console-card console-log-card" aria-label={t("console.logAria")}>
        <header className="console-card-bar">
          <div className="console-title">
            <span className={`console-live${paused ? " is-paused" : ""}`} aria-hidden="true" />
            {t("console.title")}
          </div>
          <div className="console-chips" role="tablist" aria-label={t("console.typeAria")}>
            {FILTERS.map((id) => (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={filter === id}
                className={filter === id ? "is-on" : ""}
                onClick={() => setFilter(id)}
              >
                {t(`console.f.${id}`)}
              </button>
            ))}
          </div>
          <button type="button" className="console-pause" onClick={togglePause} aria-pressed={paused}>
            {paused ? t("console.resume") : t("console.pause")}
          </button>
        </header>
        {err ? <p className="console-err">{err}</p> : null}
        <div className="console-log" ref={scroller} onScroll={onScroll}>
          {rows.length === 0 ? (
            stats ? (
              <p className="console-empty">{t("console.empty")}</p>
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
  useTranslation(); // re-render on language change
  const pnl = formatSol(row.pnl);
  const message = msgText(row.msg, row.message);
  const [open, setOpen] = useState(false);
  return (
    <div
      className={`console-row type-${row.type}${open ? " is-open" : ""}`}
      role="button"
      tabIndex={0}
      aria-expanded={open}
      title={message}
      onClick={() => setOpen((v) => !v)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          setOpen((v) => !v);
        }
      }}
    >
      <time dateTime={new Date(row.ts).toISOString()}>{clock(row.ts)}</time>
      <span className="console-pill">{msgText(row.pill_msg, row.pill)}</span>
      <span className="console-msg">{message}</span>
      <span className={`console-pnl ${tone(row.pnl)}`}>{pnl}</span>
    </div>
  );
}
