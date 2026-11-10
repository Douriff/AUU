import { useEffect, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { LangSwitch } from "@/components/ui/LangSwitch";
import {
  downloadCsv,
  loadExplain,
  loadPlaza,
  ReviewHttpError,
  type ReviewAi,
  type ReviewEvent,
  type ReviewPayload,
  type ReviewRules,
  type ReviewStrategy,
} from "./api";
import "./aiReview.css";

type Gate = "loading" | "ready" | "auth-off" | "auth" | "forbidden" | "error";

function finite(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function pct(value: number | null | undefined, lang: string): string {
  if (!finite(value)) return "—";
  return new Intl.NumberFormat(lang, { style: "percent", maximumFractionDigits: 2, signDisplay: "exceptZero" }).format(value);
}

function plain(value: number | null | undefined, lang: string, digits = 4): string {
  if (!finite(value)) return "—";
  return new Intl.NumberFormat(lang, { maximumFractionDigits: digits }).format(value);
}

function day(ts: number | null | undefined): string {
  if (!finite(ts)) return "—";
  return new Date(ts).toISOString().slice(0, 10);
}

function Spark({ points }: { points: { ts: number; nav: number }[] }) {
  if (points.length < 2) return null;
  const ys = points.map((p) => p.nav);
  const min = Math.min(...ys);
  const max = Math.max(...ys);
  const span = max - min || 1;
  const w = 280;
  const h = 72;
  const line = points
    .map((p, i) => {
      const x = (i / (points.length - 1)) * w;
      const y = h - ((p.nav - min) / span) * (h - 8) - 4;
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`;
    })
    .join(" ");
  const up = ys[ys.length - 1] >= ys[0];
  return (
    <svg className="ai-spark" viewBox={`0 0 ${w} ${h}`} role="img" aria-hidden="true">
      <path d={line} fill="none" stroke={up ? "var(--up)" : "var(--down)"} strokeWidth="2" />
    </svg>
  );
}

function statusKey(status: string, reason: string | null): string {
  if (status === "not_connected") return "aiReview.notConnected";
  if (status === "empty") return "aiReview.emptyRows";
  if (status === "non_finite") return "aiReview.nonFinite";
  if (status === "inconsistent") return "aiReview.inconsistent";
  if (reason === "no_portfolio_nav") return "aiReview.notConnected";
  return "aiReview.missing";
}

export function AiReviewPage() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || "en";
  const [gate, setGate] = useState<Gate>("loading");
  const [data, setData] = useState<ReviewPayload | null>(null);
  const [rules, setRules] = useState<ReviewRules | null>(null);
  const [panelAi, setPanelAi] = useState<ReviewAi | null>(null);
  const [staleClient, setStaleClient] = useState(false);
  const [picked, setPicked] = useState<string>("");
  const loadedAt = useRef(0);
  const hasData = useRef(false);

  useEffect(() => {
    const ac = new AbortController();
    let busy = false;
    let timer = 0;
    const pull = async () => {
      if (document.visibilityState === "hidden" || ac.signal.aborted || busy) return;
      busy = true;
      try {
        const next = await loadPlaza(ac.signal);
        if (ac.signal.aborted) return;
        setData(next);
        hasData.current = true;
        setRules(next.featured?.rules ?? null);
        setPanelAi(next.featured?.ai ?? next.ai);
        setPicked(next.featured ? `${next.featured.rules.strategy_id}:${next.featured.rules.symbol}:${next.featured.rules.asof_ts}` : "");
        loadedAt.current = Date.now();
        setStaleClient(false);
        setGate("ready");
      } catch (err) {
        if (ac.signal.aborted || (err instanceof DOMException && err.name === "AbortError")) return;
        if (err instanceof ReviewHttpError) {
          if (err.status === 403) setGate("forbidden");
          else if (err.code === "AUTH_DISABLED") setGate("auth-off");
          else if (err.status === 401) setGate("auth");
          else if (hasData.current) setStaleClient(true);
          else setGate("error");
        } else if (hasData.current) {
          setStaleClient(true);
        } else {
          setGate("error");
        }
      } finally {
        busy = false;
      }
    };
    const tick = () => {
      if (loadedAt.current && Date.now() - loadedAt.current > 90_000) setStaleClient(true);
      void pull();
    };
    void pull();
    timer = window.setInterval(tick, 60_000);
    const onVis = () => {
      if (document.visibilityState === "visible") void pull();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      ac.abort();
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, []);

  async function choose(strategy: string, symbol: string, asof: number) {
    setPicked(`${strategy}:${symbol}:${asof}`);
    try {
      const body = await loadExplain(strategy, symbol, asof);
      setRules(body.rules);
      setPanelAi(body.ai);
    } catch (err) {
      if (err instanceof ReviewHttpError && err.status === 401) setGate(err.code === "AUTH_DISABLED" ? "auth-off" : "auth");
      if (err instanceof ReviewHttpError && err.status === 403) setGate("forbidden");
    }
  }

  return (
    <div className="ai-lab">
      <header className="ai-top">
        <div className="ai-brand">
          <span className="ai-mark">AUU</span>
          <span className="ai-kicker">{t("aiReview.kicker")}</span>
        </div>
        <h1>{t("aiReview.title")}</h1>
        <div className="ai-top-tools">
          <LangSwitch compact />
          {gate === "ready" ? (
            <button type="button" className="ai-text-btn" onClick={() => downloadCsv().catch(() => setGate("error"))}>
              {t("aiReview.export")}
            </button>
          ) : null}
        </div>
      </header>
      <p className="ai-disclaimer">{t("aiReview.disclaimer")}</p>
      {gate === "loading" ? <p className="ai-state">{t("aiReview.loading")}</p> : null}
      {gate === "auth-off" ? <GateCard title={t("aiReview.authOff")} /> : null}
      {gate === "auth" ? <GateCard title={t("aiReview.authRequired")} action={<a href="/login">{t("aiReview.login")}</a>} /> : null}
      {gate === "forbidden" ? <GateCard title={t("aiReview.forbidden")} /> : null}
      {gate === "error" ? <GateCard title={t("aiReview.error")} /> : null}
      {gate === "ready" && data ? (
        <Main
          data={data}
          lang={lang}
          rules={rules}
          panelAi={panelAi}
          staleClient={staleClient}
          picked={picked}
          onPick={choose}
        />
      ) : null}
    </div>
  );
}

function GateCard({ title, action }: { title: string; action?: ReactNode }) {
  return (
    <section className="ai-gate">
      <h2>{title}</h2>
      {action}
    </section>
  );
}

function Main({
  data,
  lang,
  rules,
  panelAi,
  staleClient,
  picked,
  onPick,
}: {
  data: ReviewPayload;
  lang: string;
  rules: ReviewRules | null;
  panelAi: ReviewAi | null;
  staleClient: boolean;
  picked: string;
  onPick: (strategy: string, symbol: string, asof: number) => void;
}) {
  const { t } = useTranslation();
  const ai = panelAi || data.ai;
  return (
    <>
      {data.sample ? (
        <p className="ai-sample">
          {t("aiReview.sampleBanner")} <span>{t("aiReview.sampleSource", { id: data.fixture_id || "—" })}</span>
        </p>
      ) : null}
      {data.fixture_error ? <p className="ai-sample">{t("aiReview.fixtureError")}</p> : null}
      <ul className="ai-flags">
        <li>{t("aiReview.promiseNone")}</li>
        <li>{t("aiReview.liveOff")}</li>
        <li>{t(aiLabel(data.ai.status))}</li>
        {staleClient ? <li>{t("aiReview.stale")}</li> : null}
      </ul>
      <p className="ai-asof">{t("aiReview.asOf")} {day(data.generated_at)}</p>
      <section className="ai-section">
        <h2>{t("aiReview.plaza")}</h2>
        {data.strategies.length === 0 ? (
          <div className="ai-empty">
            <h3>{t("aiReview.emptyTitle")}</h3>
            <p>{t("aiReview.emptyBody")}</p>
          </div>
        ) : (
          <div className="ai-grid">
            {data.strategies.map((card) => (
              <StrategyCard key={card.id} card={card} lang={lang} />
            ))}
          </div>
        )}
      </section>
      <section className="ai-section">
        <h2>{t("aiReview.compare")}</h2>
        {data.compare.groups.length === 0 ? <p className="ai-note">{t("aiReview.incomparable")}</p> : null}
        {data.compare.groups.map((group) => (
          <div key={group.ids.join("-")} className="ai-table-wrap">
            <p className="ai-note">{t("aiReview.comparable")} {day(group.window?.start_ts)} → {day(group.window?.end_ts)}</p>
            <table>
              <thead>
                <tr>
                  <th>{t("aiReview.plaza")}</th>
                  <th>{t("aiReview.return")}</th>
                  <th>{t("aiReview.maxDd")}</th>
                  <th>{t("aiReview.cost")}</th>
                  <th>{t("aiReview.closedTrades")}</th>
                  <th>{t("aiReview.navPoints")}</th>
                </tr>
              </thead>
              <tbody>
                {group.rows.map((row) => (
                  <tr key={row.id}>
                    <td>{row.id}</td>
                    <td>{pct(row.total_return, lang)}</td>
                    <td>{pct(row.max_drawdown, lang)}</td>
                    <td>{row.cost_status === "recorded" ? plain(row.cost, lang) : t("aiReview.missing")}</td>
                    <td>{row.closed_trades === null ? t("aiReview.missing") : plain(row.closed_trades, lang, 0)}</td>
                    <td>{row.nav_points === null ? t("aiReview.missing") : plain(row.nav_points, lang, 0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
        {data.compare.refused.length > 0 ? <p className="ai-note">{t("aiReview.incomparable")}</p> : null}
      </section>
      <section className="ai-split">
        <div>
          <h2>{t("aiReview.signals")}</h2>
          <p className="ai-note">{t("aiReview.signalNotTrade")}</p>
          <ul className="ai-signal-list">
            {data.signals.length === 0 ? <li className="ai-note">{t("aiReview.emptyRows")}</li> : null}
            {data.signals.map((row) => {
              const id = `${row.strategy_id}:${row.symbol}:${row.asof_ts}`;
              return (
                <li key={id}>
                  <button type="button" className={picked === id ? "is-on" : ""} onClick={() => onPick(row.strategy_id, row.symbol, row.asof_ts)}>
                    <strong>{row.strategy_id}</strong> {row.symbol}
                    <span>{day(row.asof_ts)}</span>
                    <em>{row.action}</em>
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
        <Explain rules={rules} ai={ai} lang={lang} />
      </section>
      <section className="ai-section">
        <h2>{t("aiReview.decisions")}</h2>
        <p className="ai-note">{t("aiReview.timingUnknown")}</p>
        <DecisionLane title={t("aiReview.bookShadow")} rows={data.decisions.filter((row) => row.book === "shadow")} lang={lang} />
        <DecisionLane title={t("aiReview.bookPaper")} rows={data.decisions.filter((row) => row.book === "paper")} lang={lang} />
        <DecisionLane title={t("aiReview.bookBacktest")} rows={data.decisions.filter((row) => row.book === "backtest")} lang={lang} />
        <p className="ai-note">{t("aiReview.liveOff")}</p>
      </section>
      <footer className="ai-foot">
        {data.sources.map((src) => (
          <span key={src.name}>{src.name}: {src.state}</span>
        ))}
      </footer>
    </>
  );
}

function StrategyCard({ card, lang }: { card: ReviewStrategy; lang: string }) {
  const { t } = useTranslation();
  const perf = card.performance;
  const missing = perf.status !== "ok";
  return (
    <article className="ai-card">
      <header>
        <h3>{card.id}</h3>
        <span className={`ai-pill ${card.data_origin === "explicit_fixture" ? "is-sample" : ""}`}>
          {card.data_origin === "explicit_fixture" ? t("aiReview.kicker") : t(`aiReview.book_${card.primary_book}`, card.primary_book)}
        </span>
      </header>
      <p className="ai-version" title={card.version}>{t("aiReview.version")} {card.version.slice(0, 16)}</p>
      <dl>
        <div><dt>{t("aiReview.style")}</dt><dd>{t(`aiReview.style_${card.style}`, card.style)}</dd></div>
        <div><dt>{t("aiReview.direction")}</dt><dd>{t(`aiReview.dir_${card.direction}`, card.direction)}</dd></div>
        <div><dt>{t("aiReview.pool")}</dt><dd>{card.pool.size ?? "—"} · {t("aiReview.coverageUnknown")}</dd></div>
        <div><dt>{t("aiReview.hold")}</dt><dd>{
          card.hold.kind === "hysteresis" && finite(card.hold.enter) && finite(card.hold.exit)
            ? t("aiReview.holdHysteresis", { enter: card.hold.enter, exit: card.hold.exit })
            : card.hold.kind === "fixed_hours" && finite(card.hold.hours)
              ? t("aiReview.holdHours", { hours: card.hold.hours })
              : t(`aiReview.hold_${card.hold.kind}`, card.hold.kind)
        }</dd></div>
      </dl>
      {missing ? <p className="ai-missing">{t(statusKey(perf.status, perf.reason))}</p> : <Spark points={perf.curve} />}
      {!perf.curve.length && !missing ? <p className="ai-note">{t("aiReview.noCurve")}</p> : null}
      <div className="ai-metrics">
        <Metric label={t("aiReview.return")} value={missing ? "—" : pct(perf.total_return, lang)} />
        <Metric label={t("aiReview.maxDd")} value={missing ? "—" : pct(perf.max_drawdown, lang)} />
        <Metric label={t("aiReview.cost")} value={perf.cost_status === "recorded" ? plain(perf.cost, lang) : t("aiReview.missing")} />
        <Metric label={t("aiReview.closedTrades")} value={perf.closed_trades === null ? t("aiReview.missing") : plain(perf.closed_trades, lang, 0)} />
        <Metric label={t("aiReview.navPoints")} value={perf.nav_points === null ? t("aiReview.missing") : plain(perf.nav_points, lang, 0)} />
        <Metric label={t("aiReview.signalRows")} value={perf.signal_rows === null ? t("aiReview.missing") : plain(perf.signal_rows, lang, 0)} />
      </div>
      <p className="ai-note">{t("aiReview.signalNotTrade")}</p>
      <p className="ai-note">{perf.basis === "unit_nav" || perf.basis === "shadow_unit" ? t("aiReview.unitNav") : t("aiReview.openPnlUnknown")}</p>
      <p className="ai-note">{t("aiReview.asOf")} {day(perf.as_of_ts)} · {t("aiReview.backtestMissing")}</p>
      <div className="ai-warns">
        {[...new Set((perf.warnings || []).map((flag) => t(warnKey(flag), flag)))].map((label) => (
          <span key={label}>{label}</span>
        ))}
      </div>
    </article>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function warnKey(flag: string): string {
  if (flag === "stale") return "aiReview.stale";
  if (flag === "short_nav" || flag === "short_trades") return "aiReview.shortSample";
  if (flag === "irregular_spacing") return "aiReview.irregular";
  if (flag === "non_finite") return "aiReview.nonFinite";
  return "aiReview.missing";
}

function Explain({ rules, ai, lang }: { rules: ReviewRules | null; ai: ReviewAi; lang: string }) {
  const { t } = useTranslation();
  if (!rules) return <div className="ai-explain"><h2>{t("aiReview.signals")}</h2><p>{t("aiReview.cannotRebuild")}</p></div>;
  const reason = rules.reason;
  return (
    <div className="ai-explain">
      <h2>{t("aiReview.why")}</h2>
      <p className="ai-rules">{t("aiReview.rulesLabel")}</p>
      <p className="ai-ai">{t(aiLabel(ai.status))}</p>
      {ai.text ? <p className="ai-ai-text">{ai.text}</p> : null}
      {reason ? <p className="ai-missing">{t(reasonKey(reason))}</p> : null}
      <dl>
        <div><dt>{t("aiReview.dataTime")}</dt><dd>{day(rules.asof_ts)}</dd></div>
        <div><dt>{t("aiReview.side")}</dt><dd>{rules.side ?? t("aiReview.missing")}</dd></div>
        <div><dt>{t("aiReview.score")}</dt><dd>{plain(rules.score, lang, 4)}</dd></div>
        <div><dt>{t("aiReview.action")}</dt><dd>{rules.action ?? t("aiReview.missing")}</dd></div>
      </dl>
      <h3>{t("aiReview.conditions")}</h3>
      <ul className="ai-conds">
        {rules.conditions.map((row) => (
          <li key={row.id} data-status={row.status}>
            <span>{row.id}</span>
            <em>{t(`aiReview.cond_${row.status}`, row.status)}</em>
          </li>
        ))}
      </ul>
      <h3>{t("aiReview.risk")}</h3>
      <ul>
        {rules.invalidation.map((row) => (
          <li key={row.id}>{row.id === "stop_loss" ? t("aiReview.noStop") : `${row.id}: ${t(`aiReview.cond_${row.status}`, row.status)}`}</li>
        ))}
      </ul>
    </div>
  );
}

function reasonKey(reason: string): string {
  if (reason === "params_hash_mismatch" || reason === "params_hash_missing") return "aiReview.hashMismatch";
  if (reason === "previous_side_unknown") return "aiReview.prevUnknown";
  if (reason === "gap_before_bar") return "aiReview.gap";
  if (reason === "replay_mismatch") return "aiReview.replayMismatch";
  return "aiReview.cannotRebuild";
}

function aiLabel(status: string): string {
  if (status === "not_started") return "aiReview.aiNotStarted";
  if (status === "mock") return "aiReview.aiMock";
  if (status === "rejected" || status === "timeout" || status === "failed") return "aiReview.aiRejected";
  if (status === "skipped") return "aiReview.aiSkipped";
  if (status === "accepted") return "aiReview.aiChecked";
  return "aiReview.aiOff";
}

function DecisionLane({ title, rows, lang }: { title: string; rows: ReviewEvent[]; lang: string }) {
  const { t } = useTranslation();
  return (
    <div className="ai-lane">
      <h3>{title}</h3>
      {rows.length === 0 ? <p className="ai-note">{t("aiReview.emptyRows")}</p> : null}
      <ol>
        {rows.map((row) => (
          <li key={row.id}>
            <time>{day(row.ts)}</time>
            <span>{row.strategy_id}</span>
            <span>{row.kind}</span>
            <span>{row.symbol || "—"}</span>
            <span>{row.side || "—"}</span>
            <span>{plain(row.price, lang, 4)}</span>
            <span>{finite(row.cost) ? plain(row.cost, lang) : t("aiReview.missing")}</span>
            <span>{row.open ? t("aiReview.openPnlUnknown") : finite(row.net) ? plain(row.net, lang) : t("aiReview.missing")}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}
