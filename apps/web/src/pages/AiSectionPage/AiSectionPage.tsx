import { FormEvent, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { askNote, ReviewHttpError } from "@/pages/AiReviewPage/api";
import "./aiSection.css";

type Tab = "market" | "match" | "studio" | "paper" | "runner" | "review";
type Gate = "loading" | "anon" | "admin" | "member" | "off";
type Kind = "sma_cross" | "rsi_threshold" | "donchian_break";

type CatalogCard = {
  id: string;
  style?: string | null;
  direction?: string | null;
  timeframe?: string | null;
  records?: string | null;
  primary_book?: string | null;
  orders?: boolean;
  returns_published?: boolean;
  performance?: unknown;
};

type MatchHit = { id: string; keyword: string };
type MatchData = {
  engine: string;
  used_model: boolean;
  generates_code: boolean;
  creates_strategy: boolean;
  matches: MatchHit[];
  unmatched: boolean;
};

type EventRow = {
  id: string;
  ts: number | null;
  strategy_id: string | null;
  kind: string;
  symbol: string | null;
  side: string | null;
};

type Note = {
  sent?: boolean;
  ai?: { status?: string; text?: string | null; reason?: string | null };
};

type RunnerStatus = {
  scope?: string;
  personal?: boolean;
  ledger?: string | null;
  strategy_name?: string | null;
  go_no_go?: { verdict?: string; lamp?: string; days?: number; minDays?: number } | null;
};

type StrategyDraft = {
  id: string;
  name: string;
  kind: Kind;
  symbol: string;
  timeframe: string;
  params: Record<string, number>;
  risk: { max_weight?: number; stop_loss_pct?: number | null; take_profit_pct?: number | null; long_only?: boolean; fee_bps?: number; slippage_bps?: number };
  status: string;
  generates_code?: boolean;
  llm?: boolean;
};

type PaperAgent = {
  id: string;
  name: string;
  strategy_id: string;
  status: string;
};

type SimRecord = {
  id: string;
  status: string;
  created_at?: number;
  result?: {
    status?: string;
    reason?: string | null;
    bars_used?: number;
    bars_required?: number;
    bars_excluded_unclosed?: number;
    data_cutoff_ts?: number | null;
    data_cutoff_close_at?: number | null;
    fills?: unknown[];
    fill_count?: number;
    fills_truncated?: boolean;
    fills_omitted?: number;
    complete_fills?: boolean;
    paper_pnl?: number | null;
    realized_paper_pnl?: number | null;
    unrealized_paper_pnl?: number | null;
    is_investment_return?: boolean;
    gross_zero_cost?: boolean;
    cost_model?: { fee_bps?: number; slippage_bps?: number; gross_zero_cost?: boolean };
    used_model?: boolean;
    live?: boolean;
  };
};

function formatCutoffTs(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return new Date(value).toISOString();
}

type DraftForm = {
  name: string;
  kind: Kind;
  symbol: string;
  timeframe: "1h" | "4h" | "1d";
  params: Record<string, string>;
  max_weight: string;
  stop_loss_pct: string;
  take_profit_pct: string;
  long_only: boolean;
  fee_bps: string;
  slippage_bps: string;
};

const TABS: Tab[] = ["market", "match", "studio", "paper", "runner", "review"];
const LAST_SYMBOL_KEY = "auu.ms.last";
const KINDS: Kind[] = ["sma_cross", "rsi_threshold", "donchian_break"];

function defaultParams(kind: Kind): Record<string, string> {
  if (kind === "sma_cross") return { fast: "10", slow: "30" };
  if (kind === "rsi_threshold") return { period: "14", oversold: "30", overbought: "70" };
  return { period: "20" };
}

function blankForm(): DraftForm {
  return {
    name: "",
    kind: "sma_cross",
    symbol: "BTC",
    timeframe: "1d",
    params: defaultParams("sma_cross"),
    max_weight: "0.2",
    stop_loss_pct: "",
    take_profit_pct: "",
    long_only: true,
    fee_bps: "0",
    slippage_bps: "0",
  };
}

function formFromDraft(row: StrategyDraft): DraftForm {
  const params: Record<string, string> = defaultParams(row.kind);
  for (const [key, value] of Object.entries(row.params || {})) params[key] = String(value);
  return {
    name: row.name,
    kind: row.kind,
    symbol: row.symbol,
    timeframe: (row.timeframe === "1h" || row.timeframe === "4h" ? row.timeframe : "1d") as DraftForm["timeframe"],
    params,
    max_weight: String(row.risk?.max_weight ?? 0.2),
    stop_loss_pct: row.risk?.stop_loss_pct == null ? "" : String(row.risk.stop_loss_pct),
    take_profit_pct: row.risk?.take_profit_pct == null ? "" : String(row.risk.take_profit_pct),
    long_only: row.risk?.long_only !== false,
    fee_bps: String(row.risk?.fee_bps ?? 0),
    slippage_bps: String(row.risk?.slippage_bps ?? 0),
  };
}

function chartAssistantHref(): string {
  try {
    const s = window.localStorage.getItem(LAST_SYMBOL_KEY);
    const symbol = s && /^[A-Z0-9]{1,20}$/.test(s) ? s : "BTC";
    return `/?symbol=${symbol}&ai=1`;
  } catch {
    return "/?symbol=BTC&ai=1";
  }
}

async function readJson(path: string, init?: RequestInit): Promise<{ status: number; code: string; data: Record<string, unknown> | null }> {
  const res = await fetch(path, { credentials: "include", headers: { Accept: "application/json", ...(init?.headers || {}) }, ...init });
  const body = await res.json().catch(() => ({}));
  return { status: res.status, code: body?.error?.code || "", data: res.ok ? (body.data as Record<string, unknown>) : null };
}

/** Whether catalog/match tabs may call their protected APIs. Does not change auth policy. */
export type DirectoryAccess = "needLogin" | "load" | "error";

export function planDirectorySession(me: { auth_enabled?: boolean; user?: unknown } | null | undefined): DirectoryAccess {
  if (!me) return "error";
  if (me.auth_enabled && !me.user) return "needLogin";
  return "load";
}

export function planDirectoryResult(status: number, hasData: boolean): DirectoryAccess {
  if (status === 401) return "needLogin";
  if (!hasData) return "error";
  return "load";
}

function tabOf(raw: string | null): Tab {
  return TABS.includes(raw as Tab) ? (raw as Tab) : "market";
}

function buildBody(form: DraftForm): Record<string, unknown> | null {
  const params: Record<string, number> = {};
  for (const [key, raw] of Object.entries(form.params)) {
    const value = Number(raw);
    if (!Number.isFinite(value)) return null;
    params[key] = value;
  }
  const maxWeight = Number(form.max_weight);
  if (!form.name.trim() || !/^[A-Z0-9]{1,12}$/.test(form.symbol) || !Number.isFinite(maxWeight)) return null;
  const fee = Number(form.fee_bps);
  const slip = Number(form.slippage_bps);
  if (!Number.isFinite(fee) || !Number.isFinite(slip)) return null;
  const risk: Record<string, unknown> = { max_weight: maxWeight, long_only: form.long_only, fee_bps: fee, slippage_bps: slip };
  if (form.stop_loss_pct.trim()) {
    const stop = Number(form.stop_loss_pct);
    if (!Number.isFinite(stop)) return null;
    risk.stop_loss_pct = stop;
  }
  if (form.take_profit_pct.trim()) {
    const take = Number(form.take_profit_pct);
    if (!Number.isFinite(take)) return null;
    risk.take_profit_pct = take;
  }
  return { name: form.name.trim(), kind: form.kind, symbol: form.symbol, timeframe: form.timeframe, params, risk };
}

export function AiSectionPage() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const tab = tabOf(params.get("tab"));
  const setTab = (next: Tab) => {
    const copy = new URLSearchParams(params);
    if (next === "market") copy.delete("tab");
    else copy.set("tab", next);
    setParams(copy, { replace: true });
  };

  const [catalog, setCatalog] = useState<CatalogCard[] | null>(null);
  const [catalogErr, setCatalogErr] = useState(false);
  const [dirAuth, setDirAuth] = useState<"loading" | "auth" | "ok">("loading");
  const [prompt, setPrompt] = useState("");
  const [matching, setMatching] = useState(false);
  const [matched, setMatched] = useState<MatchData | null>(null);
  const [matchErr, setMatchErr] = useState(false);
  const [gate, setGate] = useState<Gate>("loading");
  const [runner, setRunner] = useState<RunnerStatus | null>(null);
  const [runnerErr, setRunnerErr] = useState<"auth" | "empty" | "error" | "">("");
  const [rows, setRows] = useState<EventRow[]>([]);
  const [loadError, setLoadError] = useState(false);
  const [asking, setAsking] = useState("");
  const [note, setNote] = useState<Note | null>(null);
  const [picked, setPicked] = useState("");
  const [studioAuth, setStudioAuth] = useState<"loading" | "auth" | "ok">("loading");
  const [drafts, setDrafts] = useState<StrategyDraft[]>([]);
  const [agents, setAgents] = useState<PaperAgent[]>([]);
  const [sims, setSims] = useState<Record<string, SimRecord[]>>({});
  const [form, setForm] = useState<DraftForm>(blankForm);
  const [editingId, setEditingId] = useState("");
  const [studioErr, setStudioErr] = useState("");
  const [studioBusy, setStudioBusy] = useState(false);
  const [agentName, setAgentName] = useState("");
  const [agentStrategy, setAgentStrategy] = useState("");
  const [paperErr, setPaperErr] = useState("");
  const [paperBusy, setPaperBusy] = useState("");

  useEffect(() => {
    const ac = new AbortController();
    setDirAuth("loading");
    setCatalogErr(false);
    setCatalog(null);
    (async () => {
      const me = await readJson("/api/v1/auth/me", { signal: ac.signal });
      if (ac.signal.aborted) return;
      const session = planDirectorySession(me.data as { auth_enabled?: boolean; user?: unknown } | null);
      if (session === "needLogin") {
        setDirAuth("auth");
        return;
      }
      if (session === "error") {
        setDirAuth("ok");
        setCatalogErr(true);
        setCatalog([]);
        return;
      }
      const listed = await readJson("/api/v1/ai-section/catalog", { signal: ac.signal });
      if (ac.signal.aborted) return;
      const result = planDirectoryResult(listed.status, Boolean(listed.data));
      if (result === "needLogin") {
        setDirAuth("auth");
        setCatalog(null);
        setCatalogErr(false);
        return;
      }
      const rows = Array.isArray(listed.data?.strategies) ? (listed.data!.strategies as CatalogCard[]) : [];
      setDirAuth("ok");
      setCatalogErr(result === "error");
      setCatalog(listed.data ? rows : []);
    })().catch(() => {
      if (!ac.signal.aborted) {
        setDirAuth("ok");
        setCatalogErr(true);
        setCatalog([]);
      }
    });
    return () => ac.abort();
  }, []);

  useEffect(() => {
    if (tab !== "studio" && tab !== "paper") return;
    const ac = new AbortController();
    setStudioAuth("loading");
    (async () => {
      const me = await readJson("/api/v1/auth/me", { signal: ac.signal });
      if (ac.signal.aborted) return;
      const user = (me.data?.user || null) as { id?: string } | null;
      if (me.data?.auth_enabled && !user) {
        setStudioAuth("auth");
        setDrafts([]);
        setAgents([]);
        return;
      }
      const [listed, agentList] = await Promise.all([
        readJson("/api/v1/ai-section/strategies", { signal: ac.signal }),
        readJson("/api/v1/ai-section/agents", { signal: ac.signal }),
      ]);
      if (ac.signal.aborted) return;
      if (listed.status === 401 || agentList.status === 401) {
        setStudioAuth("auth");
        return;
      }
      if (!listed.data || !agentList.data) {
        setStudioAuth("ok");
        setStudioErr("error");
        setPaperErr("error");
        return;
      }
      const nextDrafts = Array.isArray(listed.data.strategies) ? (listed.data.strategies as StrategyDraft[]) : [];
      const nextAgents = Array.isArray(agentList.data.agents) ? (agentList.data.agents as PaperAgent[]) : [];
      setDrafts(nextDrafts);
      setAgents(nextAgents);
      setStudioErr("");
      setPaperErr("");
      setStudioAuth("ok");
      if (!agentStrategy && nextDrafts[0]) setAgentStrategy(nextDrafts[0].id);
    })().catch(() => {
      if (!ac.signal.aborted) {
        setStudioAuth("ok");
        setStudioErr("error");
        setPaperErr("error");
      }
    });
    return () => ac.abort();
  }, [tab]);

  useEffect(() => {
    if (tab !== "runner" && tab !== "review") return;
    const ac = new AbortController();
    setNote(null);
    setPicked("");
    (async () => {
      const me = await readJson("/api/v1/auth/me", { signal: ac.signal });
      if (ac.signal.aborted) return;
      const user = (me.data?.user || null) as { is_admin?: boolean } | null;
      if (tab === "review") {
        if (me.data?.auth_enabled && !user) {
          setGate("anon");
          return;
        }
        if (me.data?.auth_enabled === false) {
          setGate("off");
          return;
        }
        if (me.data?.auth_enabled && user?.is_admin !== true) {
          setGate("member");
          setRows([]);
          return;
        }
        setGate("admin");
        const listed = await readJson("/api/v1/ai-review/decisions", { signal: ac.signal });
        if (ac.signal.aborted) return;
        if (!listed.data) {
          setLoadError(true);
          setRows([]);
          return;
        }
        const events = (Array.isArray(listed.data.events) ? listed.data.events : []) as EventRow[];
        setLoadError(false);
        setRows(events.filter((row) => row.id && row.id !== "paper:fills:truncated"));
        return;
      }

      // runner: same login as the system paper API; not an admin-only lab, not a personal book.
      if (me.data?.auth_enabled && !user) {
        setRunner(null);
        setRunnerErr("auth");
        return;
      }
      const status = await readJson("/api/v1/ai-section/runner", { signal: ac.signal });
      if (ac.signal.aborted) return;
      if (status.status === 401) {
        setRunner(null);
        setRunnerErr("auth");
        return;
      }
      if (!status.data) {
        setRunner(null);
        setRunnerErr("error");
        return;
      }
      setRunner(status.data as RunnerStatus);
      setRunnerErr("");
    })().catch(() => {
      if (!ac.signal.aborted) {
        if (tab === "runner") setRunnerErr("error");
        if (tab === "review") setLoadError(true);
      }
    });
    return () => ac.abort();
  }, [tab]);

  async function onMatch(e: FormEvent) {
    e.preventDefault();
    setMatching(true);
    setMatchErr(false);
    setMatched(null);
    if (dirAuth === "auth") {
      setMatching(false);
      return;
    }
    try {
      const res = await readJson("/api/v1/ai-section/compile", {
        method: "POST",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({ prompt }),
      });
      if (planDirectoryResult(res.status, Boolean(res.data)) === "needLogin") {
        setDirAuth("auth");
        setMatchErr(false);
        setMatched(null);
        return;
      }
      if (!res.data) {
        setMatchErr(true);
        return;
      }
      setMatched({
        engine: String(res.data.engine || "deterministic_catalog_matcher"),
        used_model: Boolean(res.data.used_model),
        generates_code: Boolean(res.data.generates_code),
        creates_strategy: Boolean(res.data.creates_strategy),
        matches: Array.isArray(res.data.matches) ? (res.data.matches as MatchHit[]) : [],
        unmatched: Boolean(res.data.unmatched),
      });
    } catch {
      setMatchErr(true);
    } finally {
      setMatching(false);
    }
  }

  async function onSaveDraft(e: FormEvent) {
    e.preventDefault();
    const body = buildBody(form);
    if (!body) {
      setStudioErr("error");
      return;
    }
    setStudioBusy(true);
    setStudioErr("");
    try {
      const path = editingId ? `/api/v1/ai-section/strategies/${editingId}` : "/api/v1/ai-section/strategies";
      const res = await readJson(path, {
        method: editingId ? "PATCH" : "POST",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (res.status === 401) {
        setStudioAuth("auth");
        return;
      }
      if (!res.data) {
        setStudioErr(res.code === "CODE_FORBIDDEN" ? "code" : "error");
        return;
      }
      const saved = res.data as StrategyDraft;
      setDrafts((rows) => {
        const rest = rows.filter((row) => row.id !== saved.id);
        return [saved, ...rest];
      });
      setEditingId(saved.id);
      setForm(formFromDraft(saved));
      if (!agentStrategy) setAgentStrategy(saved.id);
    } catch {
      setStudioErr("error");
    } finally {
      setStudioBusy(false);
    }
  }

  async function onDeleteDraft(id: string) {
    setStudioBusy(true);
    setStudioErr("");
    try {
      const res = await readJson(`/api/v1/ai-section/strategies/${id}`, { method: "DELETE" });
      if (!res.data) {
        setStudioErr(res.code === "IN_USE" ? "inUse" : "error");
        return;
      }
      setDrafts((rows) => rows.filter((row) => row.id !== id));
      if (editingId === id) {
        setEditingId("");
        setForm(blankForm());
      }
      if (agentStrategy === id) setAgentStrategy("");
    } catch {
      setStudioErr("error");
    } finally {
      setStudioBusy(false);
    }
  }

  async function onCreateAgent(e: FormEvent) {
    e.preventDefault();
    if (!agentName.trim() || !agentStrategy) {
      setPaperErr("needStrategy");
      return;
    }
    setPaperBusy("create");
    setPaperErr("");
    try {
      const res = await readJson("/api/v1/ai-section/agents", {
        method: "POST",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({ name: agentName.trim(), strategy_id: agentStrategy }),
      });
      if (res.status === 401) {
        setStudioAuth("auth");
        return;
      }
      if (!res.data) {
        setPaperErr("error");
        return;
      }
      const saved = res.data as PaperAgent;
      setAgents((rows) => [saved, ...rows]);
      setAgentName("");
    } catch {
      setPaperErr("error");
    } finally {
      setPaperBusy("");
    }
  }

  async function onSimulate(id: string) {
    setPaperBusy(id);
    setPaperErr("");
    try {
      const res = await readJson(`/api/v1/ai-section/agents/${id}/simulate`, {
        method: "POST",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!res.data) {
        setPaperErr("error");
        return;
      }
      const record = res.data as SimRecord;
      setSims((cur) => ({ ...cur, [id]: [record, ...(cur[id] || [])].slice(0, 20) }));
      setAgents((rows) => rows.map((row) => (row.id === id ? { ...row, status: record.status } : row)));
    } catch {
      setPaperErr("error");
    } finally {
      setPaperBusy("");
    }
  }

  async function loadSims(id: string) {
    if (sims[id]) return;
    const res = await readJson(`/api/v1/ai-section/agents/${id}/simulations`);
    if (res.data && Array.isArray(res.data.simulations)) {
      setSims((cur) => ({ ...cur, [id]: res.data!.simulations as SimRecord[] }));
    }
  }

  async function onDeleteAgent(id: string) {
    setPaperBusy(id);
    setPaperErr("");
    try {
      const res = await readJson(`/api/v1/ai-section/agents/${id}`, { method: "DELETE" });
      if (!res.data) {
        setPaperErr("error");
        return;
      }
      setAgents((rows) => rows.filter((row) => row.id !== id));
    } catch {
      setPaperErr("error");
    } finally {
      setPaperBusy("");
    }
  }

  async function ask(row: EventRow) {
    setAsking(row.id);
    setPicked(row.id);
    setNote(null);
    try {
      const result = (await askNote({ kind: "decision", id: row.id })) as Note;
      setNote(result);
    } catch (err) {
      if (err instanceof ReviewHttpError && (err.status === 401 || err.status === 403)) {
        setGate(err.status === 401 ? "anon" : "member");
      }
      setNote({ sent: false, ai: { status: "error", text: null, reason: "error" } });
    } finally {
      setAsking("");
    }
  }

  const chartHref = useMemo(() => chartAssistantHref(), []);
  const go = runner?.go_no_go ?? null;
  const paramKeys = form.kind === "sma_cross" ? ["fast", "slow"] : form.kind === "rsi_threshold" ? ["period", "oversold", "overbought"] : ["period"];

  return (
    <div className="shell-page pro-page ai-section" data-testid="ai-section">
      <header className="pro-head">
        <p className="ai-section-kicker">{t("aiSection.kicker")}</p>
        <h1>{t("aiSection.title")}</h1>
        <p>{t("aiSection.lead")}</p>
        <div className="ai-section-flags">
          <span>{t("aiSection.status.paper")}</span>
          <span>{t("aiSection.status.noLive")}</span>
          <span>{t("aiSection.status.noAutoModel")}</span>
          <span>{t("aiSection.status.notFullSource")}</span>
        </div>
        <div className="ai-section-links">
          <Link to={chartHref}>{t("aiSection.chartAssistant")} ›</Link>
          <Link to="/console">{t("aiSection.existing.console")} ›</Link>
          <Link to="/performance">{t("aiSection.existing.performance")} ›</Link>
        </div>
        <p className="ai-section-note">{t("aiSection.chartAssistantHint")}</p>
        <p className="ai-section-note">{t("aiSection.existing.signalsNote")}</p>
      </header>

      <div className="ai-section-tabs" role="tablist" aria-label={t("aiSection.title")}>
        {TABS.map((id) => (
          <button key={id} type="button" role="tab" aria-selected={tab === id} onClick={() => setTab(id)}>
            {t(`aiSection.tabs.${id}`)}
          </button>
        ))}
      </div>

      {tab === "market" ? (
        <section role="tabpanel">
          <h2 className="dim">{t("aiSection.marketplace.title")}</h2>
          <p className="ai-section-note">{t("aiSection.marketplace.notFull")}</p>
          {dirAuth === "auth" ? (
            <p>
              {t("aiSection.marketplace.needLogin")} <Link to="/login">{t("aiSection.login")}</Link>
            </p>
          ) : null}
          {dirAuth === "ok" && catalogErr ? <p className="ai-section-error">{t("aiSection.error")}</p> : null}
          {dirAuth === "ok" && catalog && catalog.length === 0 && !catalogErr ? <p className="ai-section-empty">{t("aiSection.marketplace.empty")}</p> : null}
          {dirAuth === "ok" && catalog && catalog.length > 0 ? (
            <div className="ai-section-grid">
              {catalog.map((card) => (
                <article key={card.id} className="ai-section-card">
                  <h3 className="mono">{card.id}</h3>
                  <p className="meta">{[card.style, card.direction, card.timeframe].filter(Boolean).join(" · ")}</p>
                  <p className="meta">{card.records || t("aiSection.marketplace.paperOnly")}</p>
                  <p className="meta">{t("aiSection.marketplace.ordersNo")}</p>
                  {card.performance == null && card.returns_published !== true ? (
                    <p className="meta">{t("aiSection.marketplace.noReturns")}</p>
                  ) : null}
                </article>
              ))}
            </div>
          ) : null}
        </section>
      ) : null}

      {tab === "match" ? (
        <section className="ai-section-studio" role="tabpanel">
          <div className="ai-section-panel">
            <h2>{t("aiSection.matcher.title")}</h2>
            <p>{t("aiSection.matcher.hint")}</p>
            <p className="ai-section-note">{t("aiSection.matcher.notStudio")}</p>
            {dirAuth === "auth" ? (
              <p>
                {t("aiSection.matcher.needLogin")} <Link to="/login">{t("aiSection.login")}</Link>
              </p>
            ) : null}
            {dirAuth === "ok" ? (
            <>
            <form onSubmit={onMatch}>
              <label>
                {t("aiSection.matcher.promptLabel")}
                <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} maxLength={2000} placeholder={t("aiSection.matcher.promptPlaceholder")} />
              </label>
              <button type="submit" disabled={matching || !prompt.trim()}>
                {t("aiSection.matcher.run")}
              </button>
            </form>
            {matchErr ? <p className="ai-section-error">{t("aiSection.error")}</p> : null}
            {matched ? (
              <>
                <p className="ai-section-engine">
                  {t("aiSection.matcher.engine")} · {t("aiSection.matcher.usedModelNo")} · {t("aiSection.matcher.noCode")}
                </p>
                {matched.used_model || matched.generates_code || matched.creates_strategy ? (
                  <p className="ai-section-error">{t("aiSection.error")}</p>
                ) : null}
                {matched.unmatched || matched.matches.length === 0 ? <p className="ai-section-empty">{t("aiSection.matcher.noMatch")}</p> : null}
                {matched.matches.length > 0 ? (
                  <ul className="ai-section-events">
                    {matched.matches.map((hit) => (
                      <li key={hit.id}>{t("aiSection.matcher.matched", { id: hit.id, keyword: hit.keyword })}</li>
                    ))}
                  </ul>
                ) : null}
              </>
            ) : null}
            </>
            ) : null}
          </div>
        </section>
      ) : null}

      {tab === "studio" ? (
        <section className="ai-section-studio" role="tabpanel">
          <div className="ai-section-panel">
            <h2>{t("aiSection.studio.title")}</h2>
            <p>{t("aiSection.studio.hint")}</p>
            <p className="ai-section-note">{t("aiSection.studio.notCode")}</p>
            <p className="ai-section-note">{t("aiSection.studio.verifiedMeans")}</p>
            {studioAuth === "auth" ? (
              <p>
                {t("aiSection.studio.needLogin")} <Link to="/login">{t("aiSection.login")}</Link>
              </p>
            ) : null}
            {studioAuth === "ok" ? (
              <>
                <form onSubmit={onSaveDraft} className="ai-section-form">
                  <label>
                    {t("aiSection.studio.name")}
                    <input value={form.name} maxLength={80} onChange={(e) => setForm({ ...form, name: e.target.value })} />
                  </label>
                  <label>
                    {t("aiSection.studio.kind")}
                    <select
                      value={form.kind}
                      onChange={(e) => {
                        const kind = e.target.value as Kind;
                        setForm({ ...form, kind, params: defaultParams(kind) });
                      }}
                    >
                      {KINDS.map((kind) => (
                        <option key={kind} value={kind}>
                          {t(`aiSection.studio.kinds.${kind}`)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    {t("aiSection.studio.symbol")}
                    <input value={form.symbol} maxLength={12} onChange={(e) => setForm({ ...form, symbol: e.target.value.toUpperCase() })} />
                  </label>
                  <label>
                    {t("aiSection.studio.timeframe")}
                    <select value={form.timeframe} onChange={(e) => setForm({ ...form, timeframe: e.target.value as DraftForm["timeframe"] })}>
                      <option value="1h">1h</option>
                      <option value="4h">4h</option>
                      <option value="1d">1d</option>
                    </select>
                  </label>
                  {paramKeys.map((key) => (
                    <label key={key}>
                      {t(`aiSection.studio.params.${key}`)}
                      <input value={form.params[key] || ""} onChange={(e) => setForm({ ...form, params: { ...form.params, [key]: e.target.value } })} />
                    </label>
                  ))}
                  <label>
                    {t("aiSection.studio.risk.max_weight")}
                    <input value={form.max_weight} onChange={(e) => setForm({ ...form, max_weight: e.target.value })} />
                  </label>
                  <label>
                    {t("aiSection.studio.risk.stop_loss_pct")}
                    <input value={form.stop_loss_pct} onChange={(e) => setForm({ ...form, stop_loss_pct: e.target.value })} />
                  </label>
                  <label>
                    {t("aiSection.studio.risk.take_profit_pct")}
                    <input value={form.take_profit_pct} onChange={(e) => setForm({ ...form, take_profit_pct: e.target.value })} />
                  </label>
                  <label>
                    {t("aiSection.studio.risk.fee_bps")}
                    <input value={form.fee_bps} onChange={(e) => setForm({ ...form, fee_bps: e.target.value })} />
                  </label>
                  <label>
                    {t("aiSection.studio.risk.slippage_bps")}
                    <input value={form.slippage_bps} onChange={(e) => setForm({ ...form, slippage_bps: e.target.value })} />
                  </label>
                  <label className="ai-section-check">
                    <input type="checkbox" checked={form.long_only} onChange={(e) => setForm({ ...form, long_only: e.target.checked })} />
                    {t("aiSection.studio.risk.long_only")}
                  </label>
                  <div className="ai-section-actions">
                    <button type="submit" disabled={studioBusy || !form.name.trim()}>
                      {editingId ? t("aiSection.studio.update") : t("aiSection.studio.save")}
                    </button>
                    {editingId ? (
                      <button
                        type="button"
                        className="ai-section-ask"
                        onClick={() => {
                          setEditingId("");
                          setForm(blankForm());
                        }}
                      >
                        {t("aiSection.studio.cancel")}
                      </button>
                    ) : null}
                  </div>
                </form>
                {studioErr === "error" ? <p className="ai-section-error">{t("aiSection.error")}</p> : null}
                {studioErr === "code" ? <p className="ai-section-error">{t("aiSection.studio.notCode")}</p> : null}
                {studioErr === "inUse" ? <p className="ai-section-error">{t("aiSection.studio.inUse")}</p> : null}
                {drafts.length === 0 ? <p className="ai-section-empty">{t("aiSection.studio.empty")}</p> : null}
                <ul className="ai-section-events">
                  {drafts.map((row) => (
                    <li key={row.id}>
                      <p className="mono">
                        {row.name} · {row.kind} · {row.symbol} · {row.timeframe}
                      </p>
                      <p className="meta">{t("aiSection.studio.statusVerified")}</p>
                      <div className="ai-section-actions">
                        <button type="button" className="ai-section-ask" onClick={() => { setEditingId(row.id); setForm(formFromDraft(row)); }}>
                          {t("aiSection.studio.update")}
                        </button>
                        <button type="button" className="ai-section-ask" disabled={studioBusy} onClick={() => void onDeleteDraft(row.id)}>
                          {t("aiSection.studio.delete")}
                        </button>
                      </div>
                    </li>
                  ))}
                </ul>
              </>
            ) : null}
          </div>
        </section>
      ) : null}

      {tab === "paper" ? (
        <section className="ai-section-studio" role="tabpanel">
          <div className="ai-section-panel">
            <h2>{t("aiSection.paper.title")}</h2>
            <p>{t("aiSection.paper.hint")}</p>
            <p className="ai-section-note">{t("aiSection.paper.notRunner")}</p>
            <p className="ai-section-note">{t("aiSection.paper.noLive")}</p>
            <p className="ai-section-note">{t("aiSection.paper.executionNote")}</p>
            <p className="ai-section-note">{t("aiSection.paper.notReturn")}</p>
            {studioAuth === "auth" ? (
              <p>
                {t("aiSection.paper.needLogin")} <Link to="/login">{t("aiSection.login")}</Link>
              </p>
            ) : null}
            {studioAuth === "ok" ? (
              <>
                <form onSubmit={onCreateAgent} className="ai-section-form">
                  <label>
                    {t("aiSection.paper.name")}
                    <input value={agentName} maxLength={80} onChange={(e) => setAgentName(e.target.value)} />
                  </label>
                  <label>
                    {t("aiSection.paper.strategy")}
                    <select value={agentStrategy} onChange={(e) => setAgentStrategy(e.target.value)}>
                      <option value="">{t("aiSection.paper.needStrategy")}</option>
                      {drafts.map((row) => (
                        <option key={row.id} value={row.id}>
                          {row.name} ({row.symbol})
                        </option>
                      ))}
                    </select>
                  </label>
                  <button type="submit" disabled={paperBusy === "create" || !agentName.trim() || !agentStrategy}>
                    {t("aiSection.paper.create")}
                  </button>
                </form>
                {paperErr === "error" ? <p className="ai-section-error">{t("aiSection.error")}</p> : null}
                {paperErr === "needStrategy" ? <p className="ai-section-error">{t("aiSection.paper.needStrategy")}</p> : null}
                {agents.length === 0 ? <p className="ai-section-empty">{t("aiSection.paper.empty")}</p> : null}
                <ul className="ai-section-events">
                  {agents.map((row) => (
                    <li key={row.id}>
                      <p className="mono">
                        {row.name} · {row.status} · {row.strategy_id}
                      </p>
                      <div className="ai-section-actions">
                        <button type="button" className="ai-section-ask" disabled={paperBusy === row.id} onClick={() => void onSimulate(row.id)}>
                          {t("aiSection.paper.simulate")}
                        </button>
                        <button type="button" className="ai-section-ask" disabled={paperBusy === row.id} onClick={() => void loadSims(row.id)}>
                          {t("aiSection.paper.records")}
                        </button>
                        <button type="button" className="ai-section-ask" disabled={paperBusy === row.id} onClick={() => void onDeleteAgent(row.id)}>
                          {t("aiSection.paper.delete")}
                        </button>
                      </div>
                      {(sims[row.id] || []).map((item) => {
                        const result = item.result || {};
                        const noData = item.status === "blocked_no_data" || result.status === "blocked_no_data";
                        const badData = item.status === "blocked_bad_data" || result.status === "blocked_bad_data";
                        const fillCount = typeof result.fill_count === "number" ? result.fill_count : Array.isArray(result.fills) ? result.fills.length : 0;
                        const cutoffNote = typeof result.bars_excluded_unclosed === "number" && result.bars_excluded_unclosed > 0 ? (
                              <p className="meta">{t("aiSection.paper.cutoff", { count: result.bars_excluded_unclosed, cutoff: formatCutoffTs(result.data_cutoff_ts), closeAt: formatCutoffTs(result.data_cutoff_close_at) })}</p>
                            ) : null;
                        return (
                          <div key={item.id} className="ai-section-note-box">
                            {noData ? (
                              <p>{t("aiSection.paper.blocked", { used: result.bars_used ?? 0, need: result.bars_required ?? 0 })}</p>
                            ) : null}
                            {badData ? <p>{t("aiSection.paper.blockedBad", { reason: result.reason || "" })}</p> : null}
                            {cutoffNote}
                            {!noData && !badData ? (
                              <>
                                <p>
                                  {t("aiSection.paper.simulated")} · {t("aiSection.paper.fills", { count: fillCount })}
                                  {typeof result.realized_paper_pnl === "number" ? ` · ${t("aiSection.paper.realized")}: ${result.realized_paper_pnl}` : ""}
                                  {typeof result.unrealized_paper_pnl === "number" ? ` · ${t("aiSection.paper.unrealized")}: ${result.unrealized_paper_pnl}` : ""}
                                </p>
                                <p className="meta">
                                  {t("aiSection.paper.costs", { fee: result.cost_model?.fee_bps ?? 0, slip: result.cost_model?.slippage_bps ?? 0 })}
                                  {result.gross_zero_cost ? ` · ${t("aiSection.paper.grossZero")}` : ""}
                                </p>
                                {result.fills_truncated ? <p className="ai-section-error">{t("aiSection.paper.truncated", { shown: Array.isArray(result.fills) ? result.fills.length : 0, total: fillCount })}</p> : null}
                                <p className="meta">{t("aiSection.paper.notReturn")}</p>
                              </>
                            ) : null}
                            {result.used_model || result.live || result.is_investment_return ? <p className="ai-section-error">{t("aiSection.error")}</p> : null}
                          </div>
                        );
                      })}
                    </li>
                  ))}
                </ul>
              </>
            ) : null}
          </div>
        </section>
      ) : null}

      {tab === "runner" ? (
        <section role="tabpanel">
          <div className="ai-section-panel">
            <h2>{t("aiSection.runner.title")}</h2>
            <p>{t("aiSection.runner.siteWide")}</p>
            <p className="ai-section-note">{t("aiSection.runner.source")}</p>
            <p className="ai-section-note">{t("aiSection.runner.notPersonal")}</p>
            {runnerErr === "auth" ? (
              <p>
                {t("aiSection.runner.needLogin")} <Link to="/login">{t("aiSection.login")}</Link>
              </p>
            ) : null}
            {runnerErr === "error" ? <p className="ai-section-error">{t("aiSection.error")}</p> : null}
            {runnerErr === "" && runner && !runner.strategy_name && !go && !runner.ledger ? (
              <p className="ai-section-empty">{t("aiSection.runner.empty")}</p>
            ) : null}
            {runner && runnerErr === "" ? (
              <>
                {runner.ledger ? (
                  <p>
                    {t("aiSection.runner.ledger")}: <span className="mono">{runner.ledger}</span>
                  </p>
                ) : null}
                {runner.strategy_name ? (
                  <p>
                    {t("aiSection.runner.strategy")}: <span className="mono">{runner.strategy_name}</span>
                  </p>
                ) : null}
                {go && (go.lamp || go.verdict) ? (
                  <p>
                    {t("aiSection.runner.goNoGo")}: <span className="mono">{String(go.lamp || go.verdict)}</span>
                    {typeof go.days === "number" && typeof go.minDays === "number" ? (
                      <span className="meta"> ({go.days}/{go.minDays})</span>
                    ) : null}
                  </p>
                ) : null}
                <div className="ai-section-links">
                  <Link to="/console">{t("aiSection.existing.console")} ›</Link>
                  <Link to="/performance">{t("aiSection.existing.performance")} ›</Link>
                </div>
              </>
            ) : null}
          </div>
        </section>
      ) : null}

      {tab === "review" ? (
        <section role="tabpanel">
          <div className="ai-section-panel">
            <h2>{t("aiSection.llm.title")}</h2>
            <p>{t("aiSection.llm.hint")}</p>
            {gate === "loading" ? <p className="ai-section-note">{t("aiSection.empty")}</p> : null}
            {gate === "anon" ? (
              <p>
                {t("aiSection.llm.needLogin")} <Link to="/login">{t("aiSection.login")}</Link>
              </p>
            ) : null}
            {gate === "member" ? <p>{t("aiSection.llm.needAdmin")}</p> : null}
            {gate === "off" ? <p>{t("aiSection.llm.off")}</p> : null}
            {gate === "admin" && loadError ? <p className="ai-section-error">{t("aiSection.error")}</p> : null}
            {gate === "admin" && !loadError && rows.length === 0 ? <p className="ai-section-empty">{t("aiSection.llm.empty")}</p> : null}
            {gate === "admin" ? (
              <ul className="ai-section-events">
                {rows.map((row) => (
                  <li key={row.id}>
                    <p className="mono">
                      {row.symbol || "—"} · {row.kind} · {row.strategy_id || "—"}
                    </p>
                    <button type="button" className="ai-section-ask" disabled={asking === row.id} onClick={() => void ask(row)}>
                      {t("aiSection.llm.ask")}
                    </button>
                    {picked === row.id && note ? (
                      <p className="ai-section-note-box">{note.ai?.text ? note.ai.text : t("aiSection.llm.noAnswer")}</p>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        </section>
      ) : null}
    </div>
  );
}
