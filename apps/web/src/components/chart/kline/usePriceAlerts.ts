import { useCallback, useEffect, useRef, useState } from "react";
import type { Chart, OverlayCreate } from "klinecharts";
import { ApiError, marketProvider, type PriceAlert, type PriceAlertCaps, type PriceAlertDirection, type PriceAlertEvent } from "@/providers/HttpWsProvider";
import { ALERT_COLOR, ALERT_GROUP, lineSpecs } from "./priceAlerts";

export type AlertStatus = "loading" | "ready" | "login" | "unavailable" | "error";
export type AlertToast = { id: number; symbol: string; firedDir: "up" | "down"; target: number; price: number };

const POLL_MS = 10_000; // the server checks every 5 s; 10 s keeps the in-site toast close behind it

/** Price alerts for one chart: list + unread badge (polled), create/delete, dashed lines on the chart, "picked on chart" price. */
export function usePriceAlerts(chart: Chart | null, symbol: string, getLast: () => { price: number; ts: number } | null) {
  const [alerts, setAlerts] = useState<PriceAlert[]>([]);
  const [caps, setCaps] = useState<PriceAlertCaps>({ active: 0, maxActive: 20, triggered: 0 });
  const [unread, setUnread] = useState(0);
  const [emailOn, setEmailOn] = useState(false);
  const [status, setStatus] = useState<AlertStatus>("loading");
  const [toast, setToast] = useState<AlertToast | null>(null);
  const [fresh, setFresh] = useState<Set<number>>(new Set());
  const [picking, setPicking] = useState(false);
  const [picked, setPicked] = useState<number | null>(null);
  const seen = useRef<Set<number> | null>(null); // triggered alert ids already known (first load only seeds it)
  const alive = useRef(true);
  const symRef = useRef(symbol);
  symRef.current = symbol;
  const lastRef = useRef(getLast);
  lastRef.current = getLast;

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const fail = useCallback((e: unknown) => {
    if (!alive.current) return;
    if (e instanceof ApiError && (e.status === 401 || e.code === "AUTH_REQUIRED")) setStatus("login");
    else if (e instanceof ApiError && e.status === 503) setStatus("unavailable");
    else setStatus("error");
  }, []);

  const refresh = useCallback(async () => {
    try {
      const d = await marketProvider.getPriceAlerts();
      if (!alive.current) return;
      setAlerts(d.alerts);
      setCaps(d.caps);
      setUnread(d.unread);
      setEmailOn(d.emailOn);
      setStatus("ready");
      const trig = d.alerts.filter((a) => a.status === "triggered");
      if (seen.current === null) seen.current = new Set(trig.map((a) => a.id));
      else {
        const news = trig.filter((a) => !seen.current!.has(a.id));
        news.forEach((a) => seen.current!.add(a.id));
        const top = news.sort((a, b) => (b.triggeredAt ?? 0) - (a.triggeredAt ?? 0))[0];
        if (top) setToast({ id: top.id, symbol: top.symbol, firedDir: (top.triggerPrice ?? top.price) >= top.price ? "up" : "down", target: top.price, price: top.triggerPrice ?? top.price });
      }
    } catch (e) {
      fail(e);
    }
  }, [fail]);

  // poll while the tab is visible; stop after sign-out / standby (nothing to poll)
  useEffect(() => {
    void refresh();
    const t = window.setInterval(() => {
      if (document.visibilityState === "visible" && status !== "login" && status !== "unavailable") void refresh();
    }, POLL_MS);
    const vis = () => document.visibilityState === "visible" && status !== "login" && status !== "unavailable" && void refresh();
    document.addEventListener("visibilitychange", vis);
    return () => {
      window.clearInterval(t);
      document.removeEventListener("visibilitychange", vis);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refresh, status === "login" || status === "unavailable"]);

  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(null), 9_000);
    return () => window.clearTimeout(t);
  }, [toast]);

  // dashed lines on the chart for this coin's active alerts
  useEffect(() => {
    if (!chart) return;
    chart.removeOverlay({ groupId: ALERT_GROUP });
    const last = lastRef.current();
    const ts = last?.ts ?? Date.now();
    for (const a of lineSpecs(alerts, symbol)) {
      chart.createOverlay({ name: "auuAlertLine", groupId: ALERT_GROUP, lock: true, points: [{ timestamp: ts, value: a.price }], extendData: { color: ALERT_COLOR, label: a.label } } as OverlayCreate);
    }
    return () => {
      chart.removeOverlay({ groupId: ALERT_GROUP });
    };
  }, [chart, alerts, symbol]);

  const create = useCallback(
    async (price: number, direction: PriceAlertDirection, note: string) => {
      const r = await marketProvider.createPriceAlert({ symbol: symRef.current.toUpperCase(), price, direction, note: note.trim() || undefined });
      setAlerts((l) => [r.alert, ...l]);
      setCaps(r.caps);
      return r.alert;
    },
    [],
  );

  const remove = useCallback(async (id: number) => {
    const r = await marketProvider.deletePriceAlert(id);
    setAlerts((l) => l.filter((a) => a.id !== id));
    setCaps(r.caps);
  }, []);

  const clearHistory = useCallback(async () => {
    const r = await marketProvider.clearPriceAlertHistory();
    setAlerts((l) => l.filter((a) => a.status === "active"));
    setCaps(r.caps);
  }, []);

  /** Panel opened: mark notifications read (rows stay flagged "new" until the panel closes). */
  const openSeen = useCallback(async () => {
    if (!unread) return;
    try {
      const ev: { events: PriceAlertEvent[] } = await marketProvider.getPriceAlertEvents(true);
      setFresh(new Set(ev.events.map((e) => e.alertId)));
      const r = await marketProvider.markPriceAlertEventsRead();
      if (alive.current) setUnread(r.unread);
    } catch (e) {
      fail(e);
    }
  }, [unread, fail]);

  // "pick on chart": the next tap / click on the chart reads the price under the pointer. Pointer events, not
  // "click": KLineChart cancels the touch sequence, so phones never get a synthetic click.
  useEffect(() => {
    if (!picking || !chart) return;
    const dom = chart.getDom() as HTMLElement | null;
    if (!dom) return;
    const host = dom; // the chart container only: taps on the panel / toolbar must not pick a price
    let down: { x: number; y: number; t: number } | null = null;
    const onDown = (e: PointerEvent) => {
      down = { x: e.clientX, y: e.clientY, t: Date.now() };
    };
    const onUp = (e: PointerEvent) => {
      const d = down;
      down = null;
      if (!d || Math.hypot(e.clientX - d.x, e.clientY - d.y) > 8 || Date.now() - d.t > 800) return; // a pan / long press, not a tap
      const rect = dom.getBoundingClientRect();
      const r = chart.convertFromPixel([{ x: e.clientX - rect.left, y: e.clientY - rect.top }], { paneId: "candle_pane" }) as { value?: number }[];
      const v = r?.[0]?.value;
      if (typeof v === "number" && Number.isFinite(v) && v > 0) {
        setPicked(Number(v.toPrecision(7)));
        setPicking(false);
      }
    };
    host.addEventListener("pointerdown", onDown, true);
    host.addEventListener("pointerup", onUp, true);
    return () => {
      host.removeEventListener("pointerdown", onDown, true);
      host.removeEventListener("pointerup", onUp, true);
    };
  }, [picking, chart]);

  return { alerts, caps, unread, emailOn, status, toast, dismissToast: () => setToast(null), fresh, clearFresh: () => setFresh(new Set()), picking, setPicking, picked, clearPicked: () => setPicked(null), refresh, create, remove, clearHistory, openSeen };
}
