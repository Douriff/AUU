import { useCallback, useEffect, useRef, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import { Empty, SkCards } from "@/components/ui/Skeleton";
import type { NewsItem, NewsPage as NewsData, NewsQuery } from "@/types/mainstream";

/** 行业动态: crypto headlines from public RSS (title + summary + original link, source named). Login required. */

const POLL_MS = 120_000;
const PAGE_SIZE = 20;
const TOP = ["BTC", "ETH", "SOL"];
type Mode = "all" | "focus" | "important";

function bj(ms: number | null | undefined, withDate = true) {
  if (!ms) return "—";
  const t = new Date(ms + 8 * 3_600_000);
  const p = (x: number) => String(x).padStart(2, "0");
  const hm = `${p(t.getUTCHours())}:${p(t.getUTCMinutes())}`;
  return withDate ? `${p(t.getUTCMonth() + 1)}-${p(t.getUTCDate())} ${hm}` : hm;
}

function ago(ms: number, now: number) {
  const m = Math.max(0, Math.round((now - ms) / 60_000));
  if (m < 1) return "刚刚";
  if (m < 60) return `${m} 分钟前`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} 小时前`;
  return bj(ms);
}

function Item({ it, now, onCoin }: { it: NewsItem; now: number; onCoin: (c: string) => void }) {
  return (
    <article className={`news-item${it.important ? " is-important" : ""}`}>
      <div className="news-meta">
        <span className="news-src">{it.sourceName}</span>
        <time dateTime={new Date(it.publishedAt).toISOString()} title={`${bj(it.publishedAt)}（北京时间）`}>
          {ago(it.publishedAt, now)}
        </time>
        {it.important ? <span className="news-hot">要闻</span> : null}
      </div>
      <h2 className="news-title">
        <a href={it.url} target="_blank" rel="noopener noreferrer nofollow">
          {it.title}
        </a>
      </h2>
      {it.summary ? <p className="news-sum">{it.summary}</p> : null}
      <div className="news-foot">
        {it.coins.map((c) => (
          <button key={c} type="button" className={`news-coin${TOP.includes(c) ? " is-top" : ""}`} onClick={() => onCoin(c)} title={`只看 ${c}`}>
            {c}
          </button>
        ))}
        <a className="news-link" href={it.url} target="_blank" rel="noopener noreferrer nofollow">
          阅读原文 · {it.sourceName} ›
        </a>
      </div>
    </article>
  );
}

export function NewsPage() {
  const [mode, setMode] = useState<Mode>("all");
  const [coin, setCoin] = useState("");
  const [source, setSource] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<NewsData | null>(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);
  const [checkedAt, setCheckedAt] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const topRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(
    (quiet = false) => {
      if (!quiet) setLoading(true);
      const q: NewsQuery = { page, size: PAGE_SIZE, coin: coin || undefined, source: source || undefined, focus: mode === "focus", important: mode === "important" };
      return marketProvider
        .getNews(q)
        .then((d) => {
          setData(d);
          setErr("");
          setCheckedAt(Date.now());
        })
        .catch((e: unknown) => setErr(e instanceof Error ? e.message : "无法连接"))
        .finally(() => {
          setLoading(false);
          setNow(Date.now());
        });
    },
    [page, coin, source, mode],
  );

  useEffect(() => {
    void load();
  }, [load]);

  // auto refresh (only while the tab is visible); the server fetches its feeds every ~12 min
  useEffect(() => {
    const t = window.setInterval(() => {
      if (!document.hidden) void load(true);
    }, POLL_MS);
    const onVis = () => {
      if (!document.hidden) void load(true);
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      window.clearInterval(t);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [load]);

  const pick = (fn: () => void) => {
    fn();
    setPage(1);
  };
  const go = (p: number) => {
    setPage(p);
    topRef.current?.scrollIntoView({ block: "start" });
  };
  const coins = data?.coins ?? TOP;
  const items = data?.items ?? [];
  const failing = (data?.sources ?? []).filter((s) => !s.ok && s.lastAttemptAt);

  return (
    <div className="pro-page news-page" ref={topRef}>
      <header className="pro-head">
        <div>
          <h1>行业动态</h1>
          <p>主流币与加密行业新闻、重要公告 · 优先 BTC / ETH / SOL 及策略 19 币</p>
        </div>
        <span className="news-upd muted" title="页面每 2 分钟自动刷新；服务器约每 12 分钟抓取一次来源">
          {checkedAt ? `已更新 ${bj(checkedAt, false)}` : "加载中…"}
        </span>
      </header>

      <div className="news-bar">
        <div className="seg" role="tablist" aria-label="筛选">
          {(
            [
              ["all", "全部"],
              ["focus", "关注币"],
              ["important", "要闻"],
            ] as [Mode, string][]
          ).map(([m, label]) => (
            <button key={m} type="button" role="tab" aria-selected={mode === m} className={mode === m ? "is-active" : ""} onClick={() => pick(() => setMode(m))}>
              {label}
            </button>
          ))}
        </div>
        <div className="news-chips" aria-label="币种">
          {TOP.map((c) => (
            <button key={c} type="button" className={`news-chip${coin === c ? " is-active" : ""}`} onClick={() => pick(() => setCoin(coin === c ? "" : c))}>
              {c}
            </button>
          ))}
          <select
            className="news-select"
            aria-label="更多币种"
            value={TOP.includes(coin) ? "" : coin}
            onChange={(e) => pick(() => setCoin(e.target.value))}
          >
            <option value="">更多币种</option>
            {coins
              .filter((c) => !TOP.includes(c))
              .map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
          </select>
          <select className="news-select" aria-label="来源" value={source} onChange={(e) => pick(() => setSource(e.target.value))}>
            <option value="">全部来源</option>
            {(data?.sources ?? []).map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
          {coin || source || mode !== "all" ? (
            <button type="button" className="news-clear" onClick={() => pick(() => (setCoin(""), setSource(""), setMode("all")))}>
              清除筛选
            </button>
          ) : null}
        </div>
      </div>

      {err ? <div className="pro-alert">行业动态暂时无法加载：{err}（主站其它功能不受影响）</div> : null}
      {failing.length ? (
        <div className="pro-alert is-warn">
          {failing.map((s) => s.name).join("、")} 最近一次抓取失败，显示的是之前保存的内容，下次定时抓取会自动重试。
        </div>
      ) : null}

      {loading && !data ? (
        <SkCards n={6} h={96} />
      ) : items.length === 0 ? (
        <Empty
          icon="search"
          title={data && data.total === 0 && !coin && !source && mode === "all" ? "还没有动态" : "没有符合条件的动态"}
          hint={data && data.total === 0 && !coin && !source && mode === "all" ? "服务器启动后约 1 分钟完成首次抓取，之后每 12 分钟更新。" : "换个筛选条件试试。"}
        />
      ) : (
        <div className="news-list">
          {items.map((it) => (
            <Item key={it.id} it={it} now={now} onCoin={(c) => pick(() => setCoin(c))} />
          ))}
        </div>
      )}

      {data && data.pages > 1 ? (
        <nav className="news-pager" aria-label="分页">
          <button type="button" className="btn-ghost" disabled={page <= 1} onClick={() => go(page - 1)}>
            ‹ 上一页
          </button>
          <span className="num">
            {page} / {data.pages}
          </span>
          <button type="button" className="btn-ghost" disabled={page >= data.pages} onClick={() => go(page + 1)}>
            下一页 ›
          </button>
        </nav>
      ) : null}

      <footer className="news-src-foot">
        <div className="news-src-list">
          来源：
          {(data?.sources ?? []).map((s) => (
            <span key={s.id} className="news-src-st">
              <i className={`st-dot ${s.ok ? "ok" : s.lastAttemptAt ? "bad" : ""}`} aria-hidden="true" />
              <a href={s.home} target="_blank" rel="noopener noreferrer nofollow">
                {s.name}
              </a>
              <span className="muted">{s.lastOkAt ? ` 更新 ${bj(s.lastOkAt)}` : " 等待首次抓取"}</span>
            </span>
          ))}
        </div>
        <p className="muted">{data?.note ?? "标题与摘要来自各媒体公开 RSS，版权归原作者。"} 新闻为英文原文标题与摘要，时间为北京时间。</p>
      </footer>
    </div>
  );
}
