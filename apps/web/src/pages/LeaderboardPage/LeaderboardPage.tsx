import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { Leaderboard } from "@/types/contracts";
import { Empty, SkRows } from "@/components/ui/Skeleton";

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
    <div className="lb-page pro-page">
      <header className="lb-head pro-head">
        <div>
          <h1>排行榜</h1>
          <p>纸面账户收益排名</p>
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
      {board?.note && board.auth_enabled ? <p className="td-note">{board.note}</p> : null}
      {error && <div className="pro-alert">{error}</div>}
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
                <td className="num">
                  <span className={`lb-rank${index < 3 ? ` is-top r${index + 1}` : ""}`}>{index + 1}</span>
                </td>
                <td>
                  {row.display_name || row.name}
                  {row.display_name && row.display_name !== row.name ? <em> {row.name}</em> : null}
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
          </tbody>
        </table>
        {!board && !error ? <SkRows rows={6} cols={9} h={40} /> : null}
        {board && board.items.length === 0 ? (
          <div className="lb-empty">
            {board.auth_enabled ? (
              <Empty icon="inbox" title="还没有纸面账户" hint="注册账户后即可参与纸面交易排名" />
            ) : (
              <Empty icon="inbox" title="账户系统未开启" hint="单机模式下没有多用户排名" action={<Link to="/?symbol=BTC" className="btn-ghost">去交易</Link>} />
            )}
          </div>
        ) : null}
      </div>
    </div>
  );
}
