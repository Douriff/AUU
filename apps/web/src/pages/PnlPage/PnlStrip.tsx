import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { fmtSignedPct } from "@/i18n/format";
import { money, signed } from "@/pages/PnlPage/PnlPage";
import { tone, usePnlSummary } from "@/pages/PnlPage/usePnl";

/** One-line P&L summary above the trade page (own manual paper account). Hidden when logged out. */
export function PnlStrip() {
  const { t } = useTranslation();
  const { data: s, needLogin } = usePnlSummary(10_000);
  if (needLogin || !s) return null;
  return (
    <Link to="/pnl" className="pnl-strip" aria-label={t("pnl.stripAria")}>
      <span className="pnl-strip-item">
        <small>{t("pnl.equity")}</small>
        <b className="num">{money(s.equity)}</b>
      </span>
      <span className="pnl-strip-item">
        <small>{t("pnl.today")}</small>
        <b className={`num ${tone(s.today)}`}>{s.today == null ? "—" : signed(s.today)}</b>
      </span>
      <span className="pnl-strip-item">
        <small>{t("pnl.unrealized")}</small>
        <b className={`num ${tone(s.unrealized)}`}>{signed(s.unrealized)}</b>
      </span>
      <span className="pnl-strip-item hide-sm">
        <small>{t("pnl.realized")}</small>
        <b className={`num ${tone(s.realized)}`}>{signed(s.realized)}</b>
      </span>
      <span className="pnl-strip-item">
        <small>{t("pnl.totalReturn")}</small>
        <b className={`num ${tone(s.totalPnl)}`}>{fmtSignedPct(s.totalReturn)}</b>
      </span>
      {s.stale ? <span className="pnl-stale-tag">{t("pnl.staleTag")}</span> : null}
      <span className="pnl-strip-more">{t("pnl.details")} ›</span>
    </Link>
  );
}
