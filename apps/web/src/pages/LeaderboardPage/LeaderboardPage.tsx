import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { Leaderboard } from "@/types/contracts";

function sol(n: number): string {
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(4)}`;
}

function pct(n: number): string {
  return `${n >= 0 ? "+" : ""}${(n * 100).toFixed(2)}%`;
}

export function LeaderboardPage() {
  const [sort, setSort] = useState<"pnl" | "return">("pnl");
  const [board, setBoard] = useState<Leaderboard | null>(null);
  const [error, setError] = useState("");

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
        if (!stop) setError(e instanceof Error ? e.message : "排行榜读取失败");
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
    <div className="lb-page">
      <header className="lb-head">
        <div>
          <h1>排行榜</h1>
          <span className="td-badge paper">PAPER</span>
          <span className="td-badge live">LIVE OFF</span>
        </div>
        <div className="lb-sort">
          <button type="button" className={sort === "pnl" ? "is-on" : ""} onClick={() => setSort("pnl")}>
            盈亏
          </button>
          <button type="button" className={sort === "return" ? "is-on" : ""} onClick={() => setSort("return")}>
            收益率
          </button>
        </div>
      </header>
      <p className="td-note">{board?.note || (error ? "" : "读取排行榜…")}</p>
      {error && <p className="td-block">{error}</p>}
      {board && !board.auth_enabled && (
        <p className="td-note">
          <Link to="/trade">去交易</Link>
        </p>
      )}
      <div className="lb-scroll">
        <table className="lb-table">
          <thead>
            <tr>
              <th>#</th>
              <th>用户</th>
              <th>起始 SOL</th>
              <th>盈亏 SOL</th>
              <th>收益率</th>
              <th>权益</th>
              <th>平仓</th>
              <th>持仓</th>
              <th>今日</th>
            </tr>
          </thead>
          <tbody>
            {(board?.items ?? []).map((row, index) => (
              <tr key={row.id}>
                <td className="num">{index + 1}</td>
                <td>
                  {row.name}
                  {row.is_admin ? <em> 管理员</em> : null}
                </td>
                <td className="num">{row.start_sol.toFixed(2)}</td>
                <td className={`num ${row.pnl >= 0 ? "up" : "down"}`}>{sol(row.pnl)}</td>
                <td className={`num ${row.return_pct >= 0 ? "up" : "down"}`}>{pct(row.return_pct)}</td>
                <td className="num">{row.equity.toFixed(4)}</td>
                <td className="num">{row.n_closed}</td>
                <td className="num">{row.open_positions}</td>
                <td className={`num ${row.day_pnl >= 0 ? "up" : "down"}`}>{sol(row.day_pnl)}</td>
              </tr>
            ))}
            {board && board.items.length === 0 && (
              <tr>
                <td colSpan={9} className="lb-empty">
                  {board.auth_enabled ? "还没有纸面账户。去登录页注册。" : "账户未开启。"}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
