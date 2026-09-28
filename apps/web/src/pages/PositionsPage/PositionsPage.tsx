import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { marketProvider } from "@/providers/HttpWsProvider";
import { TradePage } from "@/pages/TradePage/TradePage";
import type { AuthMe, AuthUser, UserBook } from "@/types/contracts";

function sol(n: number): string {
  return n.toFixed(4);
}

export function PositionsPage() {
  const [me, setMe] = useState<AuthMe | null>(null);

  useEffect(() => {
    marketProvider
      .getMe()
      .then(setMe)
      .catch(() =>
        setMe({
          auth_enabled: false,
          signup_allowed: false,
          invite_required: false,
          user: null,
          liveEnabled: false,
          liveDisabled: true,
          mode: "paper",
        })
      );
  }, []);

  if (!me) return <p className="td-note">读取账户…</p>;
  if (!me.auth_enabled) return <TradePage />;
  return <UserPositions me={me} />;
}

function UserPositions({ me }: { me: AuthMe }) {
  const [accounts, setAccounts] = useState<AuthUser[]>([]);
  const [userId, setUserId] = useState(me.user?.id ?? "");
  const [book, setBook] = useState<UserBook | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!me.user?.is_admin) return;
    marketProvider
      .getAccounts()
      .then((data) => setAccounts(data.items))
      .catch(() => setAccounts([]));
  }, [me.user?.is_admin]);

  useEffect(() => {
    if (!me.user) return;
    let stop = false;
    const tick = async () => {
      try {
        const next = await marketProvider.getTradeBook(me.user?.is_admin ? userId : undefined);
        if (!stop) {
          setBook(next);
          setError("");
        }
      } catch (e) {
        if (!stop) setError(e instanceof Error ? e.message : "持仓读取失败");
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), 2500);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, [me.user, userId]);

  if (!me.user) {
    return (
      <div className="lb-page">
        <h1>持仓</h1>
        <p className="td-note">
          请先 <Link to="/login">登录</Link>。每个账户的纸面仓位互相隔离。
        </p>
      </div>
    );
  }

  return (
    <div className="lb-page">
      <header className="lb-head">
        <div>
          <h1>持仓</h1>
          <span className="td-badge paper">PAPER</span>
          <span className="td-badge live">LIVE OFF</span>
        </div>
        {me.user.is_admin && accounts.length > 0 && (
          <label className="lb-sort">
            账户
            <select value={userId} onChange={(e) => setUserId(e.target.value)}>
              {accounts.map((row) => (
                <option key={row.id} value={row.id}>
                  {row.name}
                  {row.is_admin ? " · 管理员" : ""}
                </option>
              ))}
            </select>
          </label>
        )}
      </header>
      <p className="td-note">
        {book?.user ? `${book.user.name} 的纸面仓位` : "我的纸面仓位"} · 单笔 ≤ 1 SOL · 同时持仓 ≤ 10 · 日亏 4.5% 停止买入
      </p>
      {error && <p className="td-block">{error}</p>}
      <div className="lb-scroll">
        <table className="lb-table">
          <thead>
            <tr>
              <th>代币</th>
              <th>数量</th>
              <th>开仓价</th>
              <th>标记</th>
              <th>名义 SOL</th>
              <th>浮盈</th>
            </tr>
          </thead>
          <tbody>
            {(book?.items ?? []).map((row) => (
              <tr key={`${row.symbol}-${row.ts}`}>
                <td>
                  <Link to={`/trade/${encodeURIComponent(row.mint || row.symbol)}`}>{row.symbol}</Link>
                </td>
                <td className="num">{row.qty.toPrecision(6)}</td>
                <td className="num">{sol(row.entry_price)}</td>
                <td className="num">{sol(row.mark)}</td>
                <td className="num">{sol(row.notional_sol)}</td>
                <td className={`num ${row.upnl >= 0 ? "up" : "down"}`}>{sol(row.upnl)}</td>
              </tr>
            ))}
            {book && book.items.length === 0 && (
              <tr>
                <td colSpan={6} className="lb-empty">
                  没有未平仓位。去 <Link to="/trade">交易</Link> 开一笔纸面仓。
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
