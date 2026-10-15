import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { errText } from "@/i18n/errors";
import { fmtDate, fmtFixed } from "@/i18n/format";
import type { PriceAlert, PriceAlertCaps, PriceAlertDirection } from "@/providers/HttpWsProvider";
import type { AlertStatus } from "./usePriceAlerts";
import { DIR_GLYPH, autoDirection, parsePrice } from "./priceAlerts";

type Props = {
  symbol: string;
  last: number | null;
  alerts: PriceAlert[];
  caps: PriceAlertCaps;
  emailOn: boolean;
  status: AlertStatus;
  fresh: Set<number>;
  picking: boolean;
  picked: number | null;
  onPick: (on: boolean) => void;
  onPicked: () => void;
  onCreate: (price: number, direction: PriceAlertDirection, note: string) => Promise<unknown>;
  onRemove: (id: number) => Promise<unknown>;
  onClearHistory: () => Promise<unknown>;
  onClose: () => void;
};

const priceTxt = (p: number) => (p >= 1000 ? fmtFixed(p, 2) : p >= 1 ? fmtFixed(p, 4) : String(Number(p.toPrecision(6))));
const timeTxt = (ms: number) => fmtDate(ms, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });

/** Popover (bottom sheet on phones): create an alert at a price (typed or picked on the chart), list / delete, recent triggers. */
export function AlertPanel(p: Props) {
  const { t } = useTranslation();
  const [price, setPrice] = useState("");
  const [dir, setDir] = useState<PriceAlertDirection | null>(null); // null = follow the price automatically
  const [note, setNote] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [all, setAll] = useState(false);
  const sym = p.symbol.toUpperCase();

  // a price picked on the chart lands in the form
  useEffect(() => {
    if (p.picked != null) {
      setPrice(String(p.picked));
      p.onPicked();
      setErr("");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.picked]);

  const parsed = parsePrice(price);
  const effDir = dir ?? (parsed != null ? autoDirection(parsed, p.last) : "cross");
  const active = useMemo(() => p.alerts.filter((a) => a.status === "active" && (all || a.symbol === sym)), [p.alerts, all, sym]);
  const history = useMemo(() => p.alerts.filter((a) => a.status !== "active" && (all || a.symbol === sym)).slice(0, 8), [p.alerts, all, sym]);
  const full = p.caps.active >= p.caps.maxActive;
  const blocked = p.status === "login" || p.status === "unavailable";

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (parsed == null) return setErr(t("chart.alert.badPrice"));
    setBusy(true);
    setErr("");
    try {
      await p.onCreate(parsed, effDir, note);
      setPrice("");
      setNote("");
      setDir(null);
    } catch (x) {
      setErr(errText(x, "chart.alert.failed"));
    } finally {
      setBusy(false);
    }
  };
  const run = async (fn: () => Promise<unknown>) => {
    setErr("");
    try {
      await fn();
    } catch (x) {
      setErr(errText(x, "chart.alert.failed"));
    }
  };

  return (
    <div className={`kc-alert-panel${p.picking ? " is-picking" : ""}`} role="dialog" aria-label={t("chart.alert.title")} data-testid="alert-panel">
      <div className="kc-ind-head">
        <b>🔔 {t("chart.alert.title")}</b>
        <span>
          <button type="button" onClick={p.onClose} aria-label={t("chart.alert.close")}>✕</button>
        </span>
      </div>
      {blocked ? (
        <p className="kc-alert-note">{p.status === "login" ? t("chart.alert.login") : t("chart.alert.unavailable")}</p>
      ) : (
        <>
          <form className="kc-alert-form" onSubmit={submit}>
            <div className="kc-alert-meta num">
              <span>{sym}</span>
              <span className="muted">{p.last != null ? t("chart.alert.last", { p: priceTxt(p.last) }) : ""}</span>
              <span className={full ? "bad" : "muted"} data-testid="alert-cap">{t("chart.alert.cap", { n: p.caps.active, max: p.caps.maxActive })}</span>
            </div>
            <div className="kc-alert-row">
              <input
                value={price}
                onChange={(e) => {
                  setPrice(e.target.value);
                  setErr("");
                }}
                inputMode="decimal"
                autoComplete="off"
                placeholder={t("chart.alert.pricePlaceholder")}
                aria-label={t("chart.alert.price")}
                data-testid="alert-price"
                maxLength={20}
              />
              <button type="button" className={p.picking ? "is-on" : ""} aria-pressed={p.picking} onClick={() => p.onPick(!p.picking)} data-testid="alert-pick" title={t("chart.alert.pickTitle")}>
                {p.picking ? t("chart.alert.picking") : t("chart.alert.pick")}
              </button>
            </div>
            <div className="kc-alert-row">
              <select value={effDir} onChange={(e) => setDir(e.target.value as PriceAlertDirection)} aria-label={t("chart.alert.direction")} data-testid="alert-dir">
                {(["above", "below", "cross"] as const).map((d) => (
                  <option key={d} value={d}>{DIR_GLYPH[d]} {t(`chart.alert.dir.${d}`)}</option>
                ))}
              </select>
              <input value={note} onChange={(e) => setNote(e.target.value)} maxLength={80} placeholder={t("chart.alert.notePlaceholder")} aria-label={t("chart.alert.note")} data-testid="alert-note" />
              <button type="submit" disabled={busy || full || parsed == null} data-testid="alert-add" className="kc-alert-add">{t("chart.alert.add")}</button>
            </div>
            {err ? <p className="kc-alert-err" role="alert" data-testid="alert-err">{err}</p> : null}
          </form>
          <div className="kc-alert-sec">
            <b>{t("chart.alert.active")}</b>
            <label className="kc-alert-all"><input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> {t("chart.alert.allCoins")}</label>
          </div>
          {active.length ? (
            <ul className="kc-alert-list" data-testid="alert-list">
              {active.map((a) => (
                <li key={a.id} data-alert-id={a.id}>
                  <span className="kc-alert-dir" aria-hidden>{DIR_GLYPH[a.direction]}</span>
                  <span className="num">{all ? <em>{a.symbol} </em> : null}{priceTxt(a.price)}</span>
                  <span className="kc-alert-n muted" title={a.note}>{a.note || t(`chart.alert.dir.${a.direction}`)}</span>
                  <button type="button" onClick={() => void run(() => p.onRemove(a.id))} aria-label={t("chart.alert.delete")} title={t("chart.alert.delete")} data-testid="alert-del">✕</button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="kc-alert-note muted">{t("chart.alert.empty")}</p>
          )}
          {history.length ? (
            <>
              <div className="kc-alert-sec">
                <b>{t("chart.alert.recent")}</b>
                <button type="button" onClick={() => void run(p.onClearHistory)} data-testid="alert-clear">{t("chart.alert.clearHistory")}</button>
              </div>
              <ul className="kc-alert-list is-history" data-testid="alert-history">
                {history.map((a) => (
                  <li key={a.id} className={p.fresh.has(a.id) ? "is-new" : ""}>
                    <span className="kc-alert-dir" aria-hidden>{DIR_GLYPH[a.direction]}</span>
                    <span className="num">{all ? <em>{a.symbol} </em> : null}{priceTxt(a.price)}</span>
                    <span className="kc-alert-n muted">
                      {a.triggeredAt ? t("chart.alert.firedAt", { time: timeTxt(a.triggeredAt), p: priceTxt(a.triggerPrice ?? a.price) }) : ""}
                    </span>
                    {p.fresh.has(a.id) ? <b className="kc-alert-newtag">{t("chart.alert.new")}</b> : null}
                  </li>
                ))}
              </ul>
            </>
          ) : null}
          <p className="kc-alert-note muted">{p.emailOn ? t("chart.alert.emailOn") : t("chart.alert.emailOff")}</p>
        </>
      )}
    </div>
  );
}
