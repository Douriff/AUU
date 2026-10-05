import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { ShadowS3Summary } from "@/types/mainstream";
import { fmtBjShort, fmtFixed } from "@/i18n/format";
import { errText } from "@/i18n/errors";

/** Shadow record of the S3 hypothesis (long after extreme negative funding). No capital; not evidence. */

const POLL_MS = 120_000;
const bp = (v: number | null | undefined) => (v == null || !Number.isFinite(v) ? "—" : `${v > 0 ? "+" : ""}${fmtFixed(v, 1)} bp`);
const pct = (v: number | null | undefined, d = 2) =>
  v == null || !Number.isFinite(v) ? "—" : `${v > 0 ? "+" : ""}${fmtFixed(v * 100, d)}%`;

export function ShadowS3Panel() {
  const [d, setD] = useState<ShadowS3Summary | null>(null);
  const [err, setErr] = useState("");
  const { t } = useTranslation();
  useEffect(() => {
    let alive = true;
    const load = () =>
      marketProvider
        .getShadowS3()
        .then((x) => alive && (setD(x), setErr("")))
        .catch((e) => alive && setErr(errText(e, "common.loadFailed")));
    load();
    const id = window.setInterval(load, POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);
  const ev = d?.evaluations[d.evaluations.length - 1];
  return (
    <section className="console-card strat shadow" aria-label={t("s3.aria")}>
      <header className="console-card-bar strat-head">
        <div className="console-title">{t("s3.title")}</div>
        <span className="console-badge shadow-badge">{t("s3.label")}</span>
        <span className="console-badge">{t("s3.badge")}</span>
      </header>
      {err ? <p className="console-err">{err}</p> : null}
      <div className="strat-universe">
        <div>
          <b>{d && !d.ruleFrozen ? t("s3.ruleBad") : t("s3.ruleFrozen")}</b>: {t("s3.rule")}
        </div>
        <div>
          {t("s3.registered", { time: d ? fmtBjShort(d.registeredAt) : "—", src: d?.source || "—", hash: d?.ruleHash || "" })}
        </div>
        {d ? ["note1", "note2", "note3", "note4"].map((n) => <div key={n} className="muted">{t(`s3.${n}`)}</div>) : null}
      </div>
      <div className="strat-kpis">
        <Stat label={t("s3.progress")} value={d ? t("s3.trades", { a: d.progress, b: d.evalAt }) : "—"} sub={t("s3.progressSub")} />
        <Stat label={t("s3.openPending")} value={d ? `${d.counts.open} / ${d.counts.pending}` : "—"} />
        <Stat label={t("s3.void")} value={d ? String(d.counts.void) : "—"} sub={t("s3.voidSub")} />
        <Stat label={t("s3.meanNet")} value={bp(d?.running?.mean_net_bps)} sub={d ? t("s3.runningNote") : ""} />
        <Stat label={t("s3.win")} value={d?.running ? `${fmtFixed(d.running.win * 100, 0)}%` : "—"} />
        <Stat
          label={t("s3.verdict")}
          value={ev ? (ev.verdict === "candidate" ? t("s3.candidate") : t("s3.fail")) : t("s3.notYet")}
          sub={ev ? t("s3.evalSub", { mean: bp(ev.mean_net_bps), ci: ev.ci_bps.map((x) => fmtFixed(x, 0)).join(" / "), ex: bp(ev.ex_best3_bps) }) : ""}
        />
      </div>
      <div className="strat-tables single">
        <div>
          <h4>{t("s3.recent")}</h4>
          <table className="num">
            <thead>
              <tr><th>{t("s3.col.coin")}</th><th>{t("s3.col.signal")}</th><th>{t("s3.col.f8")}</th><th>{t("s3.col.status")}</th><th>{t("s3.col.entry")}</th><th>{t("s3.col.exit")}</th><th>{t("s3.col.net")}</th></tr>
            </thead>
            <tbody>
              {d?.trades.length ? (
                d.trades.slice(0, 12).map((tr) => (
                  <tr key={tr.id}>
                    <td>{tr.coin}</td>
                    <td>{fmtBjShort(tr.signal_ts)}</td>
                    <td>{pct(tr.f8, 3)}</td>
                    <td>{t(`s3.st.${tr.status}`, { defaultValue: tr.status })}</td>
                    <td>{tr.entry_px ?? "—"}</td>
                    <td>{tr.exit_px ?? "—"}</td>
                    <td className={tr.net == null ? "" : tr.net > 0 ? "up" : "down"}>{pct(tr.net)}</td>
                  </tr>
                ))
              ) : (
                <tr><td colSpan={7} className="muted">{t("s3.empty")}</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="console-stat">
      <span>{label}</span>
      <strong>{value}</strong>
      {sub ? <small className="muted">{sub}</small> : null}
    </div>
  );
}
