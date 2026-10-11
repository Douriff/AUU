import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { Trc20Config, Trc20Order } from "@/types/contracts";
import { errText } from "@/i18n/errors";
import { Sk } from "@/components/ui/Skeleton";
import { bjTime, newIdemKey } from "@/pages/PointsPage/PointsPage";

const PRESETS = [10, 50, 100, 500, 1000];

function mmss(sec: number) {
  const s = Math.max(0, Math.floor(sec));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function CopyBtn({ text }: { text: string }) {
  const { t } = useTranslation();
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="td-link"
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(() => {
          setDone(true);
          window.setTimeout(() => setDone(false), 1500);
        });
      }}
    >
      {done ? t("usdt.copied") : t("usdt.copy")}
    </button>
  );
}

/** One open order: address, QR, exact amount, countdown, warnings. Polls its own status. */
function OrderCard({ order, onChange }: { order: Trc20Order; onChange: (o: Trc20Order) => void }) {
  const { t } = useTranslation();
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);
  useEffect(() => {
    if (order.status !== "pending") return;
    const id = window.setInterval(() => {
      if (document.hidden) return;
      marketProvider.getTrc20Order(order.orderId).then((r) => r.order.status !== order.status && onChange(r.order)).catch(() => undefined);
    }, 15000);
    return () => window.clearInterval(id);
  }, [order.orderId, order.status, onChange]);
  const left = Math.max(0, (order.expiresAt - now) / 1000);
  const status = order.status === "pending" && left <= 0 ? "expired" : order.status;
  return (
    <div className="usdt-order">
      <div className="usdt-order-head">
        <b>{t("usdt.orderTitle")}</b>
        <span className={`td-badge usdt-st-${status}`}>{t(`usdt.status.${status}`)}</span>
        {status === "pending" ? <span className="usdt-timer" dir="ltr">{t("usdt.countdown", { time: mmss(left) })}</span> : null}
      </div>
      {status === "paid" ? <div className="pro-alert is-ok">{t("usdt.paidNote", { points: order.points.toLocaleString() })}</div> : null}
      {status === "expired" ? <div className="pro-alert is-warn">{t("usdt.expiredNote")}</div> : null}
      {status === "pending" ? (
        <div className="usdt-pay">
          {order.qr ? <img className="usdt-qr" src={order.qr} alt={t("usdt.qrAlt")} width={180} height={180} /> : null}
          <div className="usdt-fields">
            <label>{t("usdt.network")}</label>
            <div className="usdt-val"><b>USDT · TRC20 (TRON)</b></div>
            <label>{t("usdt.address")}</label>
            <div className="usdt-val" dir="ltr"><code className="usdt-addr">{order.address}</code> <CopyBtn text={order.address} /></div>
            <label>{t("usdt.exactAmount")}</label>
            <div className="usdt-val" dir="ltr"><b className="usdt-amount">{order.amount}</b> USDT <CopyBtn text={order.amount} /></div>
            <label>{t("usdt.willGet")}</label>
            <div className="usdt-val" dir="ltr">{order.points.toLocaleString()}</div>
          </div>
        </div>
      ) : null}
      {status === "pending" ? (
        <ul className="usdt-warn">
          <li><b>{t("usdt.warnTrc20")}</b></li>
          <li><b>{t("usdt.warnExact", { amount: order.amount })}</b></li>
          <li>{t("usdt.warnTime", { min: Math.round((order.expiresAt - order.createdAt) / 60000) })}</li>
          <li><b>{t("usdt.warnNoWithdraw")}</b></li>
          <li>{t("usdt.warnConfirm")}</li>
        </ul>
      ) : null}
    </div>
  );
}

export function Trc20Topup() {
  const { t } = useTranslation();
  const [cfg, setCfg] = useState<Trc20Config | null>(null);
  const [orders, setOrders] = useState<Trc20Order[]>([]);
  const [active, setActive] = useState<Trc20Order | null>(null);
  const [usdt, setUsdt] = useState("50");
  const [agree, setAgree] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [loadErr, setLoadErr] = useState("");

  const load = useCallback(() => {
    marketProvider
      .getTrc20()
      .then((r) => {
        setCfg(r.config);
        setOrders(r.orders);
        setActive((cur) => cur ?? r.orders.find((o) => o.status === "pending" && o.expiresAt > Date.now()) ?? null);
      })
      .catch((e: unknown) => setLoadErr(errText(e, "common.loadFailed")));
  }, []);
  useEffect(() => load(), [load]);

  const onChange = useCallback(
    (o: Trc20Order) => {
      setActive(o);
      load();
    },
    [load],
  );

  if (loadErr) return <div className="pro-alert">{loadErr}</div>;
  if (!cfg) return <Sk w={240} h={14} />;
  if (!cfg.enabled) return <div className="pro-alert is-warn pts-closed">{t("points.topupClosed")}</div>;

  const n = /^\d+$/.test(usdt.trim()) ? Number(usdt.trim()) : NaN;
  const amountOk = Number.isInteger(n) && n >= cfg.min && n <= cfg.max;

  const create = async () => {
    if (!amountOk || !agree) return;
    setBusy(true);
    setMsg("");
    try {
      const r = await marketProvider.createTrc20Order(n, newIdemKey());
      setActive(r.order);
      load();
    } catch (e) {
      setMsg(errText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="usdt-box">
      {cfg.adminOnly ? <div className="pro-alert is-warn">{t("usdt.adminOnlyNote")}</div> : null}
      <p className="muted">{t("usdt.intro", { rate: cfg.rate, min: cfg.min, max: cfg.max, daily: cfg.dailyMax })}</p>
      {active ? (
        <>
          <OrderCard order={active} onChange={onChange} />
          {active.status !== "pending" || active.expiresAt <= Date.now() ? (
            <button type="button" className="td-link" onClick={() => setActive(null)}>{t("usdt.newOrder")}</button>
          ) : null}
        </>
      ) : (
        <div className="usdt-form">
          <div className="usdt-presets">
            {PRESETS.filter((p) => p >= cfg.min && p <= cfg.max).map((p) => (
              <button key={p} type="button" className={`td-chip${String(p) === usdt ? " is-active" : ""}`} onClick={() => setUsdt(String(p))} dir="ltr">
                {p} USDT
              </button>
            ))}
          </div>
          <label>
            {t("usdt.amountLabel", { min: cfg.min, max: cfg.max })}
            <input value={usdt} onChange={(e) => setUsdt(e.target.value.replace(/[^\d]/g, ""))} inputMode="numeric" dir="ltr" maxLength={7} />
          </label>
          <p className="td-note" dir="auto">{amountOk ? t("usdt.preview", { usdt: n, points: (n * cfg.rate).toLocaleString() }) : t("usdt.badAmount", { min: cfg.min, max: cfg.max })}</p>
          <label className="usdt-agree">
            <input type="checkbox" checked={agree} onChange={(e) => setAgree(e.target.checked)} />
            <span>{t("usdt.agree")}</span>
          </label>
          <button type="button" className="td-submit buy" disabled={busy || !amountOk || !agree} onClick={() => void create()}>
            {t("usdt.create")}
          </button>
          {msg ? <p className="td-note">{msg}</p> : null}
        </div>
      )}
      <h3>{t("usdt.history")}</h3>
      {orders.length ? (
        <div className="pro-table-wrap">
          <table className="pro-table usdt-table">
            <thead>
              <tr>
                <th>{t("points.col.time")}</th>
                <th>{t("usdt.col.amount")}</th>
                <th>{t("usdt.col.points")}</th>
                <th>{t("usdt.col.status")}</th>
                <th>{t("usdt.col.tx")}</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((o) => (
                <tr key={o.orderId} className="is-link" onClick={() => setActive(o)}>
                  <td>{bjTime(o.createdAt)}</td>
                  <td dir="ltr">{o.amount}</td>
                  <td dir="ltr">{o.points.toLocaleString()}</td>
                  <td><span className={`td-badge usdt-st-${o.status}`}>{t(`usdt.status.${o.status}`)}</span></td>
                  <td dir="ltr">{o.txHash ? <a className="td-link" href={`https://tronscan.org/#/transaction/${o.txHash}`} target="_blank" rel="noopener noreferrer">{o.txHash.slice(0, 10)}…</a> : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">{t("usdt.noOrders")}</p>
      )}
    </div>
  );
}
