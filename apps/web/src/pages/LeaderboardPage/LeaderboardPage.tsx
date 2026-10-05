import { msgText } from "@/i18n/msg";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { Leaderboard } from "@/types/contracts";
import { Empty, SkRows } from "@/components/ui/Skeleton";
import { useTranslation } from "react-i18next";
import { fmtFixed } from "@/i18n/format";
import { errText } from "@/i18n/errors";

function sol(n: number): string {
  const sign = n > 0 ? "+" : "";
  return `${sign}${fmtFixed(n, 4)}`;
}

function pct(n: number): string {
  return `${n >= 0 ? "+" : ""}${fmtFixed(n * 100, 2)}%`;
}

export function LeaderboardPage() {
  const [sort, setSort] = useState<"pnl" | "return">("pnl");
  const [board, setBoard] = useState<Leaderboard | null>(null);
  const [error, setError] = useState("");
  const { t } = useTranslation();

  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const next = await marketProvider.getLeaderboard(sort);
        if (!stop) {
          setBoard(next);
          setError("");
        }
      } catch (e) {
        if (!stop) setError(errText(e, "lb.loadFailed"));
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), 4000);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, [sort]);

  return (
    <div className="lb-page pro-page">
      <header className="lb-head pro-head">
        <div>
          <h1>{t("lb.title")}</h1>
          <p>{t("lb.sub")}</p>
        </div>
        <div className="lb-sort">
          <button type="button" className={sort === "pnl" ? "is-on" : ""} onClick={() => setSort("pnl")}>
            {t("lb.pnl")}
          </button>
          <button type="button" className={sort === "return" ? "is-on" : ""} onClick={() => setSort("return")}>
            {t("lb.ret")}
          </button>
        </div>
      </header>
      {board?.note && board.auth_enabled ? <p className="td-note">{msgText(board.noteMsg, board.note)}</p> : null}
      {error && <div className="pro-alert">{error}</div>}
      <div className="lb-scroll">
        <table className="lb-table">
          <thead>
            <tr>
              <th>#</th>
              <th>{t("lb.col.user")}</th>
              <th>{t("lb.col.start")}</th>
              <th>{t("lb.col.pnl")}</th>
              <th>{t("lb.ret")}</th>
              <th>{t("lb.col.equity")}</th>
              <th>{t("lb.col.closed")}</th>
              <th>{t("lb.col.open")}</th>
              <th>{t("lb.col.today")}</th>
            </tr>
          </thead>
          <tbody>
            {(board?.items ?? []).map((row, index) => (
              <tr key={row.id}>
                <td className="num">
                  <span className={`lb-rank${index < 3 ? ` is-top r${index + 1}` : ""}`}>{index + 1}</span>
                </td>
                <td>
                  {row.display_name || row.name}
                  {row.display_name && row.display_name !== row.name ? <em> {row.name}</em> : null}
                  {row.is_admin ? <em> {t("lb.admin")}</em> : null}
                </td>
                <td className="num">{fmtFixed(row.start_sol, 2)}</td>
                <td className={`num ${row.pnl >= 0 ? "up" : "down"}`}>{sol(row.pnl)}</td>
                <td className={`num ${row.return_pct >= 0 ? "up" : "down"}`}>{pct(row.return_pct)}</td>
                <td className="num">{fmtFixed(row.equity, 4)}</td>
                <td className="num">{row.n_closed}</td>
                <td className="num">{row.open_positions}</td>
                <td className={`num ${row.day_pnl >= 0 ? "up" : "down"}`}>{sol(row.day_pnl)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!board && !error ? <SkRows rows={6} cols={9} h={40} /> : null}
        {board && board.items.length === 0 ? (
          <div className="lb-empty">
            {board.auth_enabled ? (
              <Empty icon="inbox" title={t("lb.empty")} hint={t("lb.emptyHint")} />
            ) : (
              <Empty icon="inbox" title={t("lb.noAuth")} hint={t("lb.noAuthHint")} action={<Link to="/?symbol=BTC" className="btn-ghost">{t("lb.goTrade")}</Link>} />
            )}
          </div>
        ) : null}
      </div>
    </div>
  );
}
