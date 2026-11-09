import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { fmtDate, fmtFixed, fmtKMB, fmtMax } from "@/i18n/format";
import { errText } from "@/i18n/errors";
import { createVisiblePoll } from "@/lib/quoteRefresh";
import {
  chartHref,
  downloadCsv,
  loadMeta,
  metricOf,
  parseThreshold,
  runScreen,
  type Cmp,
  type MetricKey,
  type Query,
  type Reading,
  type Screen,
  type ScreenMeta,
} from "./api";
import "./screener.css";

const METRICS: MetricKey[] = ["price", "quoteVolume24h", "ema10", "ema20", "ema200", "rsi14"];
const CMPS: Cmp[] = ["gt", "gte", "lt", "lte"];
const VALUE_ORDER = ["price", "quoteVolume24h", "ema10", "ema20", "ema200", "rsi14"];
const REASON_KEY: Record<string, string> = {
  stale: "stale",
  missing: "missing",
  short: "short",
  nan: "nan",
  pending: "pending",
  rate_limited: "rateLimited",
  failed: "fetchFailed",
  universe_stale: "universeStale",
};

type Draft = { id: number; left: MetricKey; cmp: Cmp; right: MetricKey | "num"; num: string };
type Tab = "pass" | "fail" | "unknown";

let nextId = 3;

function defaultDraft(): Draft[] {
  return [
    { id: 1, left: "quoteVolume24h", cmp: "gt", right: "num", num: "1000000" },
    { id: 2, left: "price", cmp: "gt", right: "ema20", num: "" },
  ];
}

function buildQuery(op: "all" | "any", rows: Draft[]): { query: Query | null; invalid: boolean } {
  if (!rows.length || rows.length > 8) return { query: null, invalid: true };
  const of = [];
  for (const row of rows) {
    const left = metricOf(row.left);
    if (row.right === "num") {
      const num = parseThreshold(row.num);
      if (num == null) return { query: null, invalid: true };
      of.push({ cmp: row.cmp, left, right: { num } });
    } else {
      of.push({ cmp: row.cmp, left, right: metricOf(row.right) });
    }
  }
  return { query: { op, of }, invalid: false };
}

function fmtUtc(ms: number | null | undefined): string {
  if (!ms) return "—";
  return `${fmtDate(ms, { timeZone: "UTC", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })} UTC`;
}

function formatReading(id: string, reading: Reading | undefined): { text: string; raw: string; warn: boolean } {
  if (!reading) return { text: "—", raw: "", warn: false };
  const number = reading.usable ? reading.value : reading.display;
  if (number == null || !Number.isFinite(number)) return { text: "—", raw: "", warn: !reading.usable };
  const text = id === "quoteVolume24h" ? fmtKMB(number) : id === "rsi14" ? fmtFixed(number, 2) : fmtMax(number, number >= 1000 ? 2 : number >= 1 ? 4 : 6);
  return { text, raw: String(number), warn: !reading.usable };
}

export function ScreenerPage() {
  const { t } = useTranslation();
  const [op, setOp] = useState<"all" | "any">("all");
  const [rows, setRows] = useState<Draft[]>(defaultDraft);
  const [committed, setCommitted] = useState<Query | null>(() => buildQuery("all", defaultDraft()).query);
  const [data, setData] = useState<Screen | null>(null);
  const [meta, setMeta] = useState<ScreenMeta | null>(null);
  const [err, setErr] = useState("");
  const [invalid, setInvalid] = useState(false);
  const [loading, setLoading] = useState(false);
  const [tab, setTab] = useState<Tab>("pass");
  const [search, setSearch] = useState("");
  const gen = useRef(0);
  const committedRef = useRef(committed);
  committedRef.current = committed;

  const load = (query: Query) => {
    const mine = ++gen.current;
    setLoading(true);
    void runScreen(query)
      .then((next) => {
        if (mine !== gen.current) return;
        setData(next);
        setErr("");
      })
      .catch((error: unknown) => {
        if (mine !== gen.current) return;
        setErr(errText(error, "screener.loadFailed"));
      })
      .finally(() => {
        if (mine === gen.current) setLoading(false);
      });
  };

  useEffect(() => {
    let alive = true;
    void loadMeta()
      .then((next) => {
        if (alive) setMeta(next);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    if (committed) load(committed);
    // The committed query is the only input. Draft edits wait for Run.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [committed]);

  useEffect(() => {
    return createVisiblePoll({
      visibleMs: 30_000,
      hiddenMs: 120_000,
      isHidden: () => document.hidden,
      onVisibility: (cb) => {
        const fn = () => cb();
        document.addEventListener("visibilitychange", fn);
        return () => document.removeEventListener("visibilitychange", fn);
      },
      onFocus: (cb) => {
        window.addEventListener("focus", cb);
        return () => window.removeEventListener("focus", cb);
      },
      run: () => {
        if (document.hidden) return;
        const query = committedRef.current;
        if (query) load(query);
      },
    });
  }, []);

  const apply = () => {
    const built = buildQuery(op, rows);
    setInvalid(built.invalid);
    if (!built.query) return;
    setCommitted(built.query);
  };

  const visible = useMemo(() => {
    const q = search.trim().toUpperCase();
    return (data?.rows ?? []).filter((row) => row.verdict === tab && (!q || row.base.includes(q) || row.symbol.includes(q)));
  }, [data, tab, search]);

  const columns = useMemo(() => {
    const present = new Set<string>();
    for (const row of data?.rows ?? []) for (const key of Object.keys(row.values)) present.add(key);
    return VALUE_ORDER.filter((key) => present.has(key));
  }, [data]);

  const fixture = Boolean(meta?.fixture || data?.fixture);
  const status = data?.status;
  const statusClass = !data ? "" : status === "ok" ? "is-ok" : status === "unavailable" ? "is-bad" : "is-warn";
  const statusLabel = !data ? t("screener.running") : status === "ok" ? t("screener.statusOk") : status === "unavailable" ? t("screener.statusUnavailable") : t("screener.statusDegraded");

  return (
    <div className="screener-page pro-page">
      <header className="screener-head">
        <p className="screener-kicker">{t("screener.kicker")}</p>
        <h1>{t("screener.title")}</h1>
        <p className="screener-sub">{t("screener.sub")}</p>
        <p className="screener-note">{t("screener.disclaimer")}</p>
      </header>

      {fixture ? (
        <div className="screener-banner" role="status">
          <strong>{t("screener.fixtureBanner")}</strong>
        </div>
      ) : null}

      <section className="screener-policy">
        <h2>{t("screener.universeTitle")}</h2>
        <p>{t("screener.universeBody")}</p>
        <p>{t("screener.policyPrice")}</p>
        <p>{t("screener.policyClosed")}</p>
        <p>{t("screener.policyStale")}</p>
        <p>{t("screener.policyFlat")}</p>
        <p>{t("screener.noCross")}</p>
      </section>

      <section className="screener-builder">
        <h2>{t("screener.match")}</h2>
        <div className="screener-op">
          <div className="seg" role="group" aria-label={t("screener.match")}>
            <button type="button" className={op === "all" ? "is-on" : ""} onClick={() => setOp("all")}>{t("screener.all")}</button>
            <button type="button" className={op === "any" ? "is-on" : ""} onClick={() => setOp("any")}>{t("screener.any")}</button>
          </div>
          <span className="muted">{t("screener.limit", { n: 8 })}</span>
        </div>
        {rows.map((row) => (
          <div className="screener-row" key={row.id}>
            <select aria-label={t("screener.left")} value={row.left} onChange={(e) => setRows((list) => list.map((item) => item.id === row.id ? { ...item, left: e.target.value as MetricKey } : item))}>
              {METRICS.map((key) => <option key={key} value={key}>{t(`screener.${key}`)}</option>)}
            </select>
            <select aria-label={t("screener.cmp")} value={row.cmp} onChange={(e) => setRows((list) => list.map((item) => item.id === row.id ? { ...item, cmp: e.target.value as Cmp } : item))}>
              {CMPS.map((key) => <option key={key} value={key}>{t(`screener.${key}`)}</option>)}
            </select>
            <div>
              <select aria-label={t("screener.right")} value={row.right} onChange={(e) => setRows((list) => list.map((item) => item.id === row.id ? { ...item, right: e.target.value as MetricKey | "num" } : item))}>
                <option value="num">{t("screener.number")}</option>
                {METRICS.map((key) => <option key={key} value={key}>{t(`screener.${key}`)}</option>)}
              </select>
              {row.right === "num" ? (
                <input
                  aria-label={t("screener.number")}
                  inputMode="decimal"
                  value={row.num}
                  onChange={(e) => setRows((list) => list.map((item) => item.id === row.id ? { ...item, num: e.target.value } : item))}
                />
              ) : null}
            </div>
            <button type="button" className="screener-ghost" disabled={rows.length <= 1} onClick={() => setRows((list) => list.filter((item) => item.id !== row.id))}>
              {t("screener.remove")}
            </button>
          </div>
        ))}
        <div className="screener-actions">
          <button type="button" className="screener-ghost" disabled={rows.length >= 8} onClick={() => setRows((list) => [...list, { id: nextId++, left: "rsi14", cmp: "lt", right: "num", num: "30" }])}>
            {t("screener.add")}
          </button>
          <button type="button" className="td-submit" onClick={apply} disabled={loading}>
            {loading ? t("screener.running") : t("screener.run")}
          </button>
          <button type="button" className="screener-ghost" disabled={!committed || !data} onClick={() => committed && void downloadCsv(committed).catch((error: unknown) => setErr(errText(error, "screener.loadFailed")))}>
            {t("screener.export")}
          </button>
        </div>
        {invalid ? <p className="screener-error">{t("screener.invalidNumber")}</p> : null}
        {err ? <p className="screener-error">{err}</p> : null}
      </section>

      <section className="screener-status" aria-live="polite">
        <span className={`screener-pill ${statusClass}`}>{statusLabel}</span>
        <span>{t("screener.source")}: {data?.asOf.sourceKind || meta?.sourceKind || "—"}</span>
        <span>{t("screener.asOf", { time: fmtUtc(data?.asOf.tickerFetchedAt) })}</span>
        <span>{t("screener.tickerAt", { time: fmtUtc(data?.asOf.tickerSourceTime) })}</span>
        <span>{t("screener.serverNow", { time: fmtUtc(data?.asOf.serverNow) })}</span>
        {data?.asOf.tickerState && data.asOf.tickerState !== "ok" ? <span className="screener-pill is-warn">{t("screener.kept")}</span> : null}
        {data && data.coverage.needed > 0 ? <span>{t("screener.coverage", { ready: data.coverage.ready, total: data.coverage.needed })}</span> : null}
        <span className="muted">{t("screener.refresh")}</span>
      </section>

      <div className="screener-tabs">
        <div className="seg" role="tablist" aria-label={t("screener.verdict")}>
          {(["pass", "fail", "unknown"] as Tab[]).map((key) => (
            <button type="button" key={key} role="tab" aria-selected={tab === key} className={tab === key ? "is-on" : ""} onClick={() => setTab(key)}>
              {t(`screener.${key === "pass" ? "passes" : key === "fail" ? "fails" : "unknown"}`)} {data ? data.counts[key === "pass" ? "passes" : key === "fail" ? "fails" : "unknown"] : 0}
            </button>
          ))}
        </div>
        <label className="screener-search">
          <span className="muted">{t("screener.search")}</span>
          <input value={search} maxLength={20} onChange={(e) => setSearch(e.target.value)} />
        </label>
      </div>
      <p className="muted">{t("screener.rows", { n: visible.length })}</p>

      <div className="pro-table-wrap screener-table-wrap">
        <table className="pro-table screener-table">
          <thead>
            <tr>
              <th>{t("screener.symbol")}</th>
              <th>{t("screener.verdict")}</th>
              {columns.map((key) => <th key={key}>{t(`screener.${key}`)}</th>)}
              <th>{t("screener.closedBars")}</th>
              <th>{t("screener.note")}</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((row) => {
              const href = chartHref(row.base);
              return (
                <tr key={row.symbol}>
                  <td>
                    {href ? <Link className="screener-link" to={href}>{row.base}</Link> : row.base}
                    <span className="muted">/USDT</span>
                  </td>
                  <td className={`is-${row.verdict}`}>{t(`screener.${row.verdict === "pass" ? "passes" : row.verdict === "fail" ? "fails" : "unknown"}`)}</td>
                  {columns.map((key) => {
                    const shown = formatReading(key, row.values[key]);
                    return (
                      <td key={key} title={shown.raw || undefined}>
                        {shown.text}
                        {shown.warn ? <span className="screener-tag">{t(`screener.${REASON_KEY[row.values[key]?.state] || "missing"}`)}</span> : null}
                      </td>
                    );
                  })}
                  <td>{row.closedBars ?? "—"}</td>
                  <td>
                    {row.reasons.map((code) => REASON_KEY[code]).filter(Boolean).map((key) => t(`screener.${key}`)).join(" · ") || "—"}
                    {row.formingExcluded ? <div className="muted">{t("screener.forming", { time: fmtUtc(row.formingOpenTime) })}</div> : null}
                    {row.lastClosedCloseTime ? <div className="muted">{t("screener.lastClosed", { time: fmtUtc(row.lastClosedCloseTime) })}</div> : null}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="screener-cards">
        {visible.map((row) => {
          const href = chartHref(row.base);
          return (
            <article className="screener-card" key={row.symbol}>
              <div>
                {href ? <Link className="screener-link" to={href}>{row.base}</Link> : row.base}
                <span className="muted">/USDT</span>
              </div>
              <strong className={`is-${row.verdict}`}>{t(`screener.${row.verdict === "pass" ? "passes" : row.verdict === "fail" ? "fails" : "unknown"}`)}</strong>
              <dl>
                {columns.map((key) => {
                  const shown = formatReading(key, row.values[key]);
                  return (
                    <span key={key} style={{ display: "contents" }}>
                      <dt>{t(`screener.${key}`)}</dt>
                      <dd title={shown.raw || undefined}>{shown.text}{shown.warn ? " *" : ""}</dd>
                    </span>
                  );
                })}
                <dt>{t("screener.closedBars")}</dt>
                <dd>{row.closedBars ?? "—"}</dd>
              </dl>
              <p className="muted">
                {row.reasons.map((code) => REASON_KEY[code]).filter(Boolean).map((key) => t(`screener.${key}`)).join(" · ")}
                {row.formingExcluded ? ` · ${t("screener.forming", { time: fmtUtc(row.formingOpenTime) })}` : ""}
              </p>
            </article>
          );
        })}
      </div>
      {visible.length === 0 ? <p className="screener-foot">{t("screener.empty")}</p> : null}
      <p className="screener-foot">{t("screener.paperOnly")} {t("screener.binanceNote")}</p>
    </div>
  );
}
