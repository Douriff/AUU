import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { askNote, ReviewHttpError } from "@/pages/AiReviewPage/api";
import "./boardAi.css";

type Gate = "loading" | "anon" | "admin" | "member" | "off";

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
  deduped?: boolean;
  sample?: boolean;
  ai?: { status?: string; text?: string | null; reason?: string | null; citations?: { key: string; value: unknown }[] };
};

function factTime(ts: number | null): string {
  if (typeof ts !== "number") return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(ts));
}

async function readJson(path: string): Promise<{ status: number; code: string; data: Record<string, unknown> | null }> {
  const res = await fetch(path, { credentials: "include", headers: { Accept: "application/json" } });
  const body = await res.json().catch(() => ({}));
  return { status: res.status, code: body?.error?.code || "", data: res.ok ? (body.data as Record<string, unknown>) : null };
}

export function BoardAiPanel({ symbol, onClose }: { symbol: string; onClose: () => void }) {
  const { t } = useTranslation();
  const [gate, setGate] = useState<Gate>("loading");
  const [rows, setRows] = useState<EventRow[]>([]);
  const [partial, setPartial] = useState(false);
  const [sample, setSample] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [asking, setAsking] = useState("");
  const [note, setNote] = useState<Note | null>(null);
  const [picked, setPicked] = useState("");

  useEffect(() => {
    const ac = new AbortController();
    setNote(null);
    setPicked("");
    setRows([]);
    setPartial(false);
    setLoadError(false);
    setGate("loading");
    (async () => {
      const me = await readJson("/api/v1/auth/me");
      if (ac.signal.aborted) return;
      const user = (me.data?.user || null) as { is_admin?: boolean } | null;
      if (me.data?.auth_enabled && !user) {
        setGate("anon");
        return;
      }
      if (me.data?.auth_enabled && user?.is_admin !== true) {
        setGate("member");
        return;
      }
      if (me.data && me.data.auth_enabled === false) {
        setGate("off");
        return;
      }
      setGate("admin");
      const listed = await readJson(`/api/v1/ai-review/decisions?symbol=${encodeURIComponent(symbol)}`);
      if (ac.signal.aborted) return;
      if (!listed.data) {
        setLoadError(true);
        return;
      }
      const events = (Array.isArray(listed.data.events) ? listed.data.events : []) as EventRow[];
      setRows(events.filter((row) => row.symbol === symbol && row.id && row.id !== "paper:fills:truncated"));
      setSample(Boolean(listed.data.sample));
      const signals = await readJson(`/api/v1/ai-review/signals?symbol=${encodeURIComponent(symbol)}&limit=1`);
      const latest = Array.isArray(signals.data?.signals) ? (signals.data.signals as { strategy_id?: string; asof_ts?: number }[])[0] : undefined;
      if (!ac.signal.aborted && latest?.strategy_id && latest.asof_ts) {
        const explained = await readJson(
          `/api/v1/ai-review/signals/explain?strategy=${encodeURIComponent(latest.strategy_id)}&symbol=${encodeURIComponent(symbol)}&asof=${latest.asof_ts}`,
        );
        const rules = explained.data?.rules as { reconstructed?: string } | undefined;
        setPartial(rules?.reconstructed !== "yes");
      }
    })().catch(() => {
      if (!ac.signal.aborted) setLoadError(true);
    });
    return () => ac.abort();
  }, [symbol]);

  async function ask(row: EventRow) {
    setAsking(row.id);
    setPicked(row.id);
    setNote(null);
    try {
      const result = (await askNote({ kind: "decision", id: row.id })) as Note;
      setNote(result);
    } catch (err) {
      if (err instanceof ReviewHttpError && (err.status === 401 || err.status === 403)) setGate(err.status === 401 ? "anon" : "member");
      else setNote({ sent: false, ai: { status: "failed", reason: "client_error", text: null } });
    } finally {
      setAsking("");
    }
  }

  return (
    <aside className="board-ai" aria-label={t("aiReview.boardTitle")}>
      <header>
        <strong>AI · {symbol}</strong>
        <button type="button" onClick={onClose} aria-label={t("aiReview.boardClose")}>{t("aiReview.boardClose")}</button>
      </header>
      <p className="board-ai-warn">{t("aiReview.boardNotNow")}</p>
      {sample ? <p className="board-ai-sample">{t("aiReview.boardSample")}</p> : null}
      {gate === "loading" ? <p>{t("aiReview.boardLoading")}</p> : null}
      {gate === "anon" ? <p>{t("aiReview.boardLogin")} <a href="/login">{t("aiReview.login")}</a></p> : null}
      {gate === "member" ? <p>{t("aiReview.boardAdmin")}</p> : null}
      {gate === "off" ? <p>{t("aiReview.boardOff")}</p> : null}
      {gate === "admin" && loadError ? <p>{t("aiReview.boardError")}</p> : null}
      {gate === "admin" && partial ? <p>{t("aiReview.boardPartial")}</p> : null}
      {gate === "admin" && !loadError && rows.length === 0 ? <p>{t("aiReview.boardEmpty")}</p> : null}
      {gate === "admin" ? (
        <ul>
          {rows.map((row) => (
            <li key={row.id}>
              <time>{factTime(row.ts)}</time>
              <span>{row.strategy_id || "—"} · {row.kind}</span>
              <button type="button" disabled={asking === row.id} onClick={() => void ask(row)}>{t("aiReview.boardAsk")}</button>
              {picked === row.id && note ? <Result note={note} when={factTime(row.ts)} /> : null}
            </li>
          ))}
        </ul>
      ) : null}
    </aside>
  );
}

function Result({ note, when }: { note: Note; when: string }) {
  const { t } = useTranslation();
  const text = note.ai?.text;
  const reason = note.ai?.reason || "";
  const code = /^[a-z0-9_]{1,40}$/.test(reason) ? reason : "failed";
  return (
    <div className="board-ai-result">
      <p>{t("aiReview.boardFactTime", { when })}</p>
      {note.deduped ? <p>{t("aiReview.boardDeduped")}</p> : null}
      {note.sent === false && !note.deduped ? <p>{t("aiReview.boardNotSent")}</p> : null}
      {text ? <p>{text}</p> : null}
      {note.ai?.citations?.length ? (
        <ul>
          {note.ai.citations.map((item) => (
            <li key={item.key}>{item.key}</li>
          ))}
        </ul>
      ) : null}
      {!text && reason ? <p>{t("aiReview.noteCheckFailed", { code })}</p> : null}
    </div>
  );
}
