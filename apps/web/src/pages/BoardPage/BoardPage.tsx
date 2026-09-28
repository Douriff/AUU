import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { EquityCurve } from "@/components/board/EquityCurve";
import { useBoard } from "@/hooks/useBoard";
import type { BoardSnapshot, BoardTickerItem } from "@/types/contracts";
import { truncateMint } from "@/venue";

const SHANGHAI: Intl.DateTimeFormatOptions = {
  timeZone: "Asia/Shanghai",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
};

function formatClock(ts: number): string {
  if (!ts) return "—";
  return new Intl.DateTimeFormat("zh-CN", SHANGHAI).format(new Date(ts));
}

function formatPx(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const a = Math.abs(n);
  if (a >= 1000) return n.toLocaleString("en-US", { maximumFractionDigits: 2 });
  if (a >= 1) return n.toFixed(4);
  if (a >= 0.0001) return n.toFixed(6);
  return n.toExponential(2);
}

function formatPct(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const pct = n * 100;
  const body = `${Math.abs(pct).toFixed(2)}%`;
  if (pct > 0) return `+${body}`;
  if (pct < 0) return `-${body}`;
  return body;
}

function formatSigned(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const a = Math.abs(n);
  const digits = a >= 100 ? 2 : a >= 1 ? 2 : a >= 0.01 ? 4 : 6;
  const body = n.toFixed(digits);
  return n > 0 ? `+${body}` : body;
}

function formatBps(n: number): string {
  if (!Number.isFinite(n)) return "—";
  const digits = Math.abs(n) >= 10 ? 0 : 1;
  const body = n.toFixed(digits);
  return n > 0 ? `+${body}` : body;
}

function formatQty(n: number): string {
  if (!Number.isFinite(n)) return "—";
  const a = Math.abs(n);
  if (a >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (a >= 1e3) return `${(n / 1e3).toFixed(2)}K`;
  if (a >= 1) return n.toFixed(4);
  return n.toPrecision(4);
}

function tone(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n === 0) return "flat";
  return n > 0 ? "up" : "down";
}

function baseOf(symbol: string): string {
  const i = symbol.indexOf("/");
  return i > 0 ? symbol.slice(0, i) : symbol;
}

function tile<T>(items: T[], min: number): T[] {
  if (!items.length) return [];
  const out: T[] = [];
  while (out.length < min) out.push(...items);
  return out;
}

function Ticker({ items, pending }: { items: BoardTickerItem[]; pending?: boolean }) {
  const [reduce, setReduce] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const apply = () => setReduce(mq.matches);
    apply();
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
  }, []);

  if (pending) {
    return <div className="board-ticker" aria-hidden="true" />;
  }

  if (!items.length) {
    return (
      <div className="board-ticker board-ticker-empty" role="region" aria-label="监控代币">
        <span>暂无监控代币</span>
      </div>
    );
  }

  const once = reduce ? items : tile(items, 10);
  const loop = reduce ? once : [...once, ...once];

  return (
    <div className="board-ticker" role="region" aria-label="监控代币涨跌">
      <ul className="board-ticker-sr">
        {items.map((item) => (
          <li key={item.symbol}>
            {item.base || baseOf(item.symbol)} {formatPx(item.price)} {formatPct(item.change_pct)}
          </li>
        ))}
      </ul>
      <div className={`board-ticker-track${reduce ? " is-static" : ""}`} aria-hidden="true">
        {loop.map((item, i) => (
          <span className="board-tick" key={`${item.symbol}-${i}`}>
            <span className="base">{item.base || baseOf(item.symbol)}</span>
            <span className="px">{formatPx(item.price)}</span>
            <span className={tone(item.change_pct)}>{formatPct(item.change_pct)}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

function verdictLabel(verdict: string): string {
  return verdict === "go" ? "GO" : "NO-GO";
}

export function BoardPage() {
  const { board, err } = useBoard(4000);
  return (
    <div className="board-page">
      <Ticker items={board?.ticker ?? []} pending={!board} />
      <div className="board-body">
        {!board && !err ? <p className="board-loading">读取盘面…</p> : null}
        {!board && err ? <p className="board-loading board-error">盘面暂时读不到：{err}</p> : null}
        {board ? <BoardView board={board} err={err} /> : null}
      </div>
    </div>
  );
}

function BoardView({ board, err }: { board: BoardSnapshot; err: string }) {
  const pnl = board.session_pnl;
  const down = pnl < 0;
  const stats = board.stats;
  const shadow = board.shadow;

  return (
    <>
      <div className="board-kicker-row">
        <p className="board-kicker">
          纸面监控 · {board.ticker.length} 个代币 · 实盘关闭
          <span className="board-asof"> 上海 {formatClock(board.asof_ts)}</span>
        </p>
        {err ? <p className="board-stale">刷新失败，显示上次数据</p> : null}
      </div>

      <div className="board-hero">
        <section className="board-card board-engine" aria-label="AUU ENGINE PAPER">
          <header className="board-engine-head">
            <div className="board-engine-title">
              <span className="board-engine-name">AUU ENGINE · PAPER</span>
              <span className="board-paper-badge" title="liveEnabled=false">
                PAPER
              </span>
            </div>
            <div className={`board-session ${tone(pnl)}`}>
              <span className="board-session-num">{formatSigned(pnl)}</span>
              <span className="board-session-label">累计净盈亏</span>
            </div>
          </header>
          <EquityCurve points={board.equity} positive={!down} />
          <div className="board-axis">
            <span>{board.equity.length ? "已平仓权益" : "尚无已平仓"}</span>
            <span>EQUITY</span>
          </div>
          <div className="board-tape-wrap">
            <div className="board-tape-head">
              <span>时间</span>
              <span>代币</span>
              <span>出场</span>
              <span>净 bps</span>
              <span>盈亏</span>
            </div>
            {board.tape.length === 0 ? (
              <p className="board-tape-empty">平仓后会出现在这里。时间按上海时区。</p>
            ) : (
              <ol className="board-tape">
                {board.tape.map((row, i) => (
                  <li key={`${row.exit_ts}-${row.symbol}-${i}`}>
                    <span className="t">{formatClock(row.exit_ts)}</span>
                    <span className="sym">
                      <strong>{baseOf(row.symbol)}</strong>
                      {row.mint ? <em>{truncateMint(row.mint, 4, 4)}</em> : null}
                    </span>
                    <span className="why" title={row.exit_reason}>
                      {row.exit_label}
                    </span>
                    <span className={`bps ${tone(row.net_bps)}`}>{formatBps(row.net_bps)}</span>
                    <span className={`pnl ${tone(row.pnl)}`}>{formatSigned(row.pnl)}</span>
                  </li>
                ))}
              </ol>
            )}
          </div>
        </section>

        <section className="board-stats" aria-label="纸面统计">
          <article className="board-stat">
            <div className={`board-stat-value ${stats.win_rate == null ? "flat" : ""}`}>
              {stats.win_rate == null ? "—" : `${(stats.win_rate * 100).toFixed(1)}%`}
            </div>
            <div>
              <div className="board-stat-label">WIN RATE</div>
              <div className="board-stat-sub">
                胜率
                {stats.wins != null && stats.n_closed > 0 ? ` · ${stats.wins}/${stats.n_closed}` : ""}
                {!stats.sample_ok ? " · 样本不足" : ""}
              </div>
            </div>
          </article>
          <article className="board-stat">
            <div className={`board-stat-value ${tone(stats.expectancy)}`}>{formatSigned(stats.expectancy)}</div>
            <div>
              <div className="board-stat-label">EXPECTANCY</div>
              <div className="board-stat-sub">每笔净盈亏</div>
            </div>
          </article>
          <article className="board-stat">
            <div className="board-stat-value">{stats.n_closed}</div>
            <div>
              <div className="board-stat-label">CLOSED</div>
              <div className="board-stat-sub">已平仓 n</div>
            </div>
          </article>
          <article className="board-stat" data-lamp={stats.lamp || "gray"}>
            <div className="board-stat-value">{verdictLabel(stats.verdict)}</div>
            <div>
              <div className="board-stat-label">GO / NO-GO</div>
              <div className="board-stat-sub" title={stats.nogo_reason}>
                {stats.nogo_reason || "执行门槛"} · {stats.go_window_label}
              </div>
            </div>
          </article>
        </section>
      </div>

      <section className="board-card board-strip" aria-label="持仓">
        <header className="board-strip-head">
          <h2>持仓</h2>
          <span className="muted">{board.positions.length ? `${board.positions.length} 笔未平` : "空仓"}</span>
        </header>
        {board.positions.length === 0 ? (
          <p className="board-empty-line">当前没有未平的纸面仓位。</p>
        ) : (
          <div className="board-pos-scroll">
            <table className="board-pos">
              <thead>
                <tr>
                  <th>代币</th>
                  <th>数量</th>
                  <th>入场</th>
                  <th>标记</th>
                  <th>未实现</th>
                </tr>
              </thead>
              <tbody>
                {board.positions.map((p) => (
                  <tr key={`${p.source}-${p.symbol}-${p.entry_ts}`}>
                    <td>
                      <strong>{baseOf(p.symbol)}</strong>
                      {p.mint ? <em>{truncateMint(p.mint, 4, 4)}</em> : null}
                    </td>
                    <td>{formatQty(p.qty)}</td>
                    <td>{formatPx(p.entry_price)}</td>
                    <td>{formatPx(p.mark)}</td>
                    <td className={tone(p.upnl)}>
                      {formatSigned(p.upnl)}
                      {p.upnl_pct != null ? <small> {formatPct(p.upnl_pct)}</small> : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="board-card board-strip" aria-label="影子对照">
        <header className="board-strip-head">
          <h2>影子对照</h2>
          <span className="muted">{shadow.enabled ? "已开启" : "未开启"}</span>
        </header>
        {!shadow.enabled ? (
          <div className="board-empty-block">
            <strong>影子对照未开启</strong>
            <p>主策略窗口保持不变。开启影子参数集后，这里对照胜率与期望，并且不会打开实盘。</p>
            {shadow.note ? <p className="muted">{shadow.note}</p> : null}
          </div>
        ) : (
          <div className="board-pos-scroll">
            <table className="board-pos">
              <thead>
                <tr>
                  <th>集合</th>
                  <th>n</th>
                  <th>胜率</th>
                  <th>期望</th>
                  <th>样本</th>
                </tr>
              </thead>
              <tbody>
                {[shadow.main, ...shadow.sets].map((col) => (
                  <tr key={col.id || col.label}>
                    <td>
                      <strong>{col.label || col.id}</strong>
                      {col.habit_tag ? <em>{col.habit_tag}</em> : null}
                    </td>
                    <td>{col.n}</td>
                    <td>{col.win_rate == null ? "—" : `${(col.win_rate * 100).toFixed(1)}%`}</td>
                    <td className={tone(col.expectancy)}>{formatSigned(col.expectancy)}</td>
                    <td>{col.sample_ok ? "够" : "不足"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {shadow.sets.length === 0 ? <p className="board-empty-line">已开启，尚无影子成交。</p> : null}
          </div>
        )}
      </section>

      <div className="board-actions">
        <Link className="board-pill" to="/market">
          查看行情
        </Link>
        <Link className="board-pill ghost" to="/trade">
          纸面下单
        </Link>
        <Link className="board-pill ghost" to="/strategy">
          策略
        </Link>
      </div>
    </>
  );
}
