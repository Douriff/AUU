// Unit tests for the realtime market-data client (no browser): npm run test:rt
// Fake clock/timers, fake WebSocket server side, fake BroadcastChannel + Web Locks shared by several "tabs".
import { build } from "esbuild";
import { pathToFileURL } from "node:url";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import assert from "node:assert/strict";

const out = join(mkdtempSync(join(tmpdir(), "rt-")), "hub.mjs");
await build({ entryPoints: ["src/realtime/hub.ts"], bundle: true, format: "esm", platform: "node", outfile: out, logLevel: "error" });
const R = await import(pathToFileURL(out).href);
const outB = join(mkdtempSync(join(tmpdir(), "rtb-")), "backoff.mjs");
await build({ entryPoints: ["src/realtime/backoff.ts"], bundle: true, format: "esm", platform: "node", outfile: outB, logLevel: "error" });
const { backoffDelay } = await import(pathToFileURL(outB).href);

// ---------------------------------------------------------------------------------------------- world
function world() {
  const w = { now: 1_000_000, timers: [], seq: 0, sockets: [], channels: [], chq: [], lock: { holder: null, queue: [] } };
  w.setTimeout = (fn, ms) => {
    const id = ++w.seq;
    w.timers.push({ id, at: w.now + Math.max(0, ms), fn });
    return id;
  };
  w.clearTimeout = (id) => void (w.timers = w.timers.filter((t) => t.id !== id));
  w.flushChannel = () => {
    while (w.chq.length) {
      const { from, data } = w.chq.shift();
      for (const c of w.channels) if (c !== from && !c.closed && c.onmessage) c.onmessage({ data: structuredClone(data) });
    }
  };
  w.advance = (ms) => {
    const end = w.now + ms;
    for (;;) {
      w.flushChannel();
      w.timers.sort((a, b) => a.at - b.at || a.id - b.id);
      const t = w.timers[0];
      if (!t || t.at > end) break;
      w.timers.shift();
      w.now = t.at;
      t.fn();
    }
    w.now = end;
    w.flushChannel();
  };
  // Web Locks (exclusive, FIFO)
  w.locks = {
    request(name, opts, cb) {
      return new Promise((resolve, reject) => {
        const entry = { cb, resolve, reject, aborted: false };
        opts?.signal?.addEventListener("abort", () => {
          if (w.lock.holder === entry) return;
          entry.aborted = true;
          w.lock.queue = w.lock.queue.filter((e) => e !== entry);
          reject(new Error("AbortError"));
        });
        w.lock.queue.push(entry);
        w.grant();
      });
    },
  };
  w.grant = () => {
    if (w.lock.holder || !w.lock.queue.length) return;
    const e = w.lock.queue.shift();
    w.lock.holder = e;
    Promise.resolve(e.cb()).then(() => {
      w.lock.holder = null;
      e.resolve();
      w.grant();
    });
  };
  return w;
}

class FakeWs {
  constructor(w, url) {
    this.w = w;
    this.url = url;
    this.readyState = 0;
    this.sent = [];
    this.closed = false;
    this.onopen = this.onmessage = this.onclose = this.onerror = null;
    w.sockets.push(this);
  }
  send(d) {
    if (this.readyState !== 1) throw new Error("not open");
    this.sent.push(JSON.parse(d));
  }
  close() {
    if (this.closed) return;
    this.closed = true;
    this.readyState = 3;
    this.onclose?.({});
  }
  // server side
  accept(hello = { type: "hello", version: 1, realtime: { channels: ["ticker", "kline"] } }, status = { live: true, source: "binance" }) {
    this.readyState = 1;
    this.onopen?.({});
    this.push(hello);
    if (status) this.push({ type: "rt_status", payload: status });
  }
  push(obj) {
    this.onmessage?.({ data: JSON.stringify(obj) });
  }
  drop() {
    this.closed = true;
    this.readyState = 3;
    this.onclose?.({});
  }
  subs() {
    return this.sent.filter((m) => m.type === "subscribe" || m.type === "unsubscribe");
  }
}

function env(w, { shared = false, rand = () => 1, hidden = { v: false } } = {}) {
  const vis = new Set();
  const hide = new Set();
  const e = {
    makeWs: (url) => new FakeWs(w, url),
    url: () => "ws://test/api/v1/ws",
    now: () => w.now,
    setTimeout: w.setTimeout,
    clearTimeout: w.clearTimeout,
    rand,
    isHidden: () => hidden.v,
    onVisibility: (cb) => (vis.add(cb), () => vis.delete(cb)),
    onPageHide: (cb) => (hide.add(cb), () => hide.delete(cb)),
  };
  if (shared) {
    e.makeChannel = () => {
      const c = { closed: false, onmessage: null, postMessage: (data) => w.chq.push({ from: c, data }), close: () => (c.closed = true) };
      w.channels.push(c);
      return c;
    };
    e.locks = w.locks;
  }
  const ctl = {
    setHidden(v) {
      hidden.v = v;
      for (const cb of vis) cb();
    },
    pageHide() {
      for (const cb of hide) cb();
    },
  };
  return { e, ctl };
}

const CAPS = { push: true, intervals: ["1h", "4h", "1d"], symbols: ["BTC", "ETH", "SOL"], staleMs: 10000, live: true, source: "binance" };
const tk = (symbol, last, ex = "binance", w) => ({ type: "ticker", payload: { symbol, ex, last, change24h: 0.01, quoteVolume: 1, ts: w?.now ?? 0, recvAt: w?.now ?? 0 } });
const settle = async (w, ms = 0) => {
  for (let i = 0; i < 5; i++) {
    await new Promise((r) => setImmediate(r)); // lock promises
    w.advance(0);
  }
  if (ms) w.advance(ms);
  for (let i = 0; i < 5; i++) {
    await new Promise((r) => setImmediate(r));
    w.advance(0);
  }
};
let n = 0;
const test = async (name, fn) => {
  await fn();
  n++;
  console.log("ok", name);
};

// ---------------------------------------------------------------------------------------------- backoff
await test("backoff: exponential, capped, jittered into [0.5, 1)", async () => {
  assert.deepEqual([1, 2, 3, 4, 5, 6, 7, 9].map((a) => backoffDelay(a, () => 1)), [1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000]);
  assert.deepEqual([1, 2, 6].map((a) => backoffDelay(a, () => 0)), [500, 1000, 15000]);
  // 1000 pages reconnecting after the same server restart spread over [500, 1000) ms, not one instant
  let r = 7;
  const rnd = () => ((r = (r * 1103515245 + 12345) % 2 ** 31) / 2 ** 31);
  const ds = Array.from({ length: 1000 }, () => backoffDelay(1, rnd));
  assert.ok(Math.min(...ds) >= 500 && Math.max(...ds) < 1000);
  assert.ok(new Set(ds.map((d) => Math.floor(d / 50))).size >= 10, "spread over the window");
});


// ---------------------------------------------------------------------------------------------- switch off
await test("switch off: no caps -> never a socket, status off, polling untouched", async () => {
  const w = world();
  const { e } = env(w);
  const hub = new R.RtHub(e);
  hub.subscribeTicker(["BTC"], () => assert.fail("no frames when off"));
  hub.configure(undefined, "binance");
  w.advance(60_000);
  assert.equal(w.sockets.length, 0);
  assert.equal(hub.getStatus().state, "off");
  assert.equal(hub.isLive(), false);
});

await test("hello without realtime (push off) -> closed once, never reconnects, off", async () => {
  const w = world();
  const { e } = env(w);
  const hub = new R.RtHub(e);
  hub.subscribeTicker(["BTC"], () => undefined);
  hub.configure(CAPS, "binance");
  assert.equal(w.sockets.length, 1);
  w.sockets[0].accept({ type: "hello", version: 1 }, null);
  assert.equal(w.sockets[0].closed, true);
  w.advance(120_000);
  assert.equal(w.sockets.length, 1);
  assert.equal(hub.getStatus().state, "off");
});

await test("build kill switch VITE_RT_WS=off keeps the hub off", async () => {
  const w = world();
  const hub = new R.RtHub(env(w).e, true);
  hub.subscribeTicker(["BTC"], () => undefined);
  hub.configure(CAPS, "binance");
  assert.equal(w.sockets.length, 0);
  assert.equal(hub.getStatus().state, "off");
});

// ---------------------------------------------------------------------------------------------- fallback / recovery
await test("live -> stale / rt_status down / wrong source fall back to polling, then recover", async () => {
  const w = world();
  const hub = new R.RtHub(env(w).e);
  const got = [];
  const states = [];
  hub.onStatus(() => states.push(hub.getStatus().state + ":" + (hub.getStatus().reason ?? "")));
  hub.subscribeTicker(["BTC", "ETH", "NOPE"], (t) => got.push(t.last));
  hub.configure(CAPS, "binance");
  const ws = w.sockets[0];
  assert.equal(hub.getStatus().state, "connecting");
  ws.accept();
  // one batched subscribe, unknown symbol filtered by caps
  assert.deepEqual(ws.subs(), [{ type: "subscribe", channel: "ticker", symbols: ["BTC", "ETH"] }]);
  assert.equal(hub.getStatus().state, "connecting"); // no data yet: pages keep polling
  ws.push(tk("BTC", 100, "binance", w));
  assert.equal(hub.getStatus().state, "live");
  assert.deepEqual(got, [100]);
  // 5 s without any market frame -> polling ("stale")
  w.advance(4_000);
  assert.equal(hub.getStatus().state, "live");
  w.advance(2_000);
  assert.deepEqual([hub.getStatus().state, hub.getStatus().reason], ["polling", "stale"]);
  ws.push(tk("BTC", 101, "binance", w));
  assert.equal(hub.getStatus().state, "live");
  // server says the stream is down
  ws.push({ type: "rt_status", payload: { live: false, source: null } });
  assert.deepEqual([hub.getStatus().state, hub.getStatus().reason], ["polling", "rt-down"]);
  ws.push({ type: "rt_status", payload: { live: true, source: "binance" } });
  ws.push(tk("BTC", 102, "binance", w));
  assert.equal(hub.getStatus().state, "live");
  // server fails over to OKX while the page's REST data is Binance: not applied, polling
  ws.push({ type: "rt_status", payload: { live: true, source: "okx" } });
  ws.push(tk("BTC", 999, "okx", w));
  assert.deepEqual([hub.getStatus().state, hub.getStatus().reason], ["polling", "source"]);
  assert.deepEqual(got, [100, 101, 102]);
  // a paper-hub ping is answered (keeps the server's idle timer happy)
  ws.push({ type: "ping" });
  assert.equal(ws.sent.at(-1).type, "pong");
  assert.ok(states.includes("live:") && states.includes("polling:stale") && states.includes("polling:rt-down"));
});

await test("subscribe diffing: kline add/remove, ticker unsubscribe, resubscribe all after reconnect", async () => {
  const w = world();
  const hub = new R.RtHub(env(w).e);
  const offT = hub.subscribeTicker(["BTC"], () => undefined);
  hub.configure(CAPS, "binance");
  w.sockets[0].accept();
  const offK = hub.subscribeKline("BTC", "1h", () => undefined);
  hub.subscribeKline("BTC", "5m", () => undefined); // not streamed (1m/5m/15m keep polling)
  offK();
  offT();
  hub.subscribeTicker(["ETH"], () => undefined);
  assert.deepEqual(w.sockets[0].subs(), [
    { type: "subscribe", channel: "ticker", symbols: ["BTC"] },
    { type: "subscribe", channel: "kline", symbol: "BTC", interval: "1h" },
    { type: "unsubscribe", channel: "kline", symbol: "BTC", interval: "1h" },
    { type: "unsubscribe", channel: "ticker", symbols: ["BTC"] },
    { type: "subscribe", channel: "ticker", symbols: ["ETH"] },
  ]);
  w.sockets[0].drop();
  w.advance(1_000);
  w.sockets[1].accept();
  assert.deepEqual(w.sockets[1].subs(), [{ type: "subscribe", channel: "ticker", symbols: ["ETH"] }]);
});

await test("client send budget stays under the server's 20 msgs / 10 s", async () => {
  const w = world();
  const hub = new R.RtHub(env(w).e);
  hub.subscribeTicker(["BTC"], () => undefined);
  hub.configure({ ...CAPS, symbols: Array.from({ length: 40 }, (_, i) => `C${i}`).concat(["BTC"]) }, "binance");
  w.sockets[0].accept();
  for (let i = 0; i < 40; i++) hub.subscribeKline(`C${i}`, "1h", () => undefined);
  assert.ok(w.sockets[0].subs().length <= 12, `burst ${w.sockets[0].subs().length}`);
  w.advance(10_000);
  const c = w.sockets[0].subs().length;
  assert.ok(c <= 12 + 16, `10 s total ${c}`);
  w.advance(30_000);
  assert.equal(w.sockets[0].subs().length, 41); // all eventually sent
});

// ---------------------------------------------------------------------------------------------- reconnect backoff
await test("reconnect: exponential backoff with jitter, reset after a stable connection", async () => {
  const w = world();
  const hub = new R.RtHub(env(w, { rand: () => 1 }).e);
  hub.subscribeTicker(["BTC"], () => undefined);
  hub.configure(CAPS, "binance");
  const gaps = [];
  for (let i = 0; i < 6; i++) {
    const before = w.sockets.length;
    w.sockets.at(-1).drop(); // server restart / refused
    assert.deepEqual([hub.getStatus().state, hub.getStatus().reason], ["polling", "closed"]);
    const t0 = w.now;
    while (w.sockets.length === before) w.advance(100);
    gaps.push(w.now - t0);
  }
  assert.deepEqual(gaps, [1000, 2000, 4000, 8000, 16000, 30000]);
  // a connection that delivered data for >= 10 s resets the backoff
  const ws = w.sockets.at(-1);
  ws.accept();
  for (let i = 0; i < 12; i++) {
    ws.push(tk("BTC", 1, "binance", w));
    w.advance(1_000);
  }
  ws.drop();
  const t0 = w.now;
  const before = w.sockets.length;
  while (w.sockets.length === before) w.advance(100);
  assert.equal(w.now - t0, 1000);
  // jitter: other tabs pick other delays in [0.5, 1) x
  const w2 = world();
  const delays = [0.0, 0.3, 0.77].map((r) => {
    const h = new R.RtHub(env(w2, { rand: () => r }).e);
    h.subscribeTicker(["BTC"], () => undefined);
    h.configure(CAPS, "binance");
    w2.sockets.at(-1).drop();
    return h.connection.delays[0];
  });
  assert.deepEqual(delays, [500, 650, 885]);
});

// ---------------------------------------------------------------------------------------------- visibility
await test("hidden for 15 s -> socket closed, no reconnect; visible -> reconnect at once", async () => {
  const w = world();
  const { e, ctl } = env(w);
  const hub = new R.RtHub(e);
  hub.subscribeTicker(["BTC"], () => undefined);
  hub.configure(CAPS, "binance");
  w.sockets[0].accept();
  ctl.setHidden(true);
  w.advance(10_000);
  assert.equal(w.sockets[0].closed, false); // quick tab switches keep the connection
  ctl.setHidden(false);
  ctl.setHidden(true);
  w.advance(15_000);
  assert.equal(w.sockets[0].closed, true);
  assert.deepEqual([hub.getStatus().state, hub.getStatus().reason], ["polling", "hidden"]);
  w.advance(600_000);
  assert.equal(w.sockets.length, 1);
  ctl.setHidden(false);
  assert.equal(w.sockets.length, 2);
  w.sockets[1].accept();
  assert.deepEqual(w.sockets[1].subs(), [{ type: "subscribe", channel: "ticker", symbols: ["BTC"] }]);
});

// ---------------------------------------------------------------------------------------------- multi-tab
await test("multi-tab: one shared connection, followers get frames, union of subscriptions", async () => {
  const w = world();
  const A = new R.RtHub(env(w, { shared: true }).e);
  const B = new R.RtHub(env(w, { shared: true }).e);
  const gotB = [];
  A.subscribeTicker(["BTC"], () => undefined);
  A.configure(CAPS, "binance");
  await settle(w);
  B.subscribeTicker(["ETH"], (t) => gotB.push(t.last));
  B.configure(CAPS, "binance");
  await settle(w);
  assert.equal(w.sockets.length, 1, "one socket for two tabs");
  assert.ok(A.isLeader && !B.isLeader);
  const ws = w.sockets[0];
  ws.accept();
  await settle(w);
  const tickers = ws.subs().filter((m) => m.channel === "ticker").flatMap((m) => m.symbols).sort();
  assert.deepEqual(tickers, ["BTC", "ETH"]);
  ws.push(tk("BTC", 1, "binance", w));
  ws.push(tk("ETH", 2, "binance", w));
  await settle(w);
  assert.deepEqual(gotB, [2]);
  assert.equal(B.getStatus().state, "live");
  assert.ok(B.getStatus().shared && !B.getStatus().leader);
  // the leader's stream goes stale -> the follower falls back too
  await settle(w, 6_000);
  assert.equal(B.getStatus().state, "polling");
  ws.push(tk("ETH", 3, "binance", w));
  await settle(w);
  assert.equal(B.getStatus().state, "live");
});

await test("multi-tab: leader tab closes -> a follower takes over with its own socket", async () => {
  const w = world();
  const ca = env(w, { shared: true });
  const A = new R.RtHub(ca.e);
  const B = new R.RtHub(env(w, { shared: true }).e);
  A.subscribeTicker(["BTC"], () => undefined);
  A.configure(CAPS, "binance");
  await settle(w);
  B.subscribeTicker(["ETH"], () => undefined);
  B.configure(CAPS, "binance");
  await settle(w);
  w.sockets[0].accept();
  ca.ctl.pageHide(); // tab closed: socket closed, lock released, "bye"
  await settle(w);
  assert.equal(w.sockets[0].closed, true);
  assert.ok(B.isLeader);
  assert.equal(w.sockets.length, 2);
  w.sockets[1].accept();
  assert.deepEqual(w.sockets[1].subs(), [{ type: "subscribe", channel: "ticker", symbols: ["ETH"] }]);
});

await test("multi-tab: hidden leader hands over to a visible tab; all hidden -> no socket", async () => {
  const w = world();
  const ca = env(w, { shared: true });
  const cb = env(w, { shared: true });
  const A = new R.RtHub(ca.e);
  const B = new R.RtHub(cb.e);
  A.subscribeTicker(["BTC"], () => undefined);
  A.configure(CAPS, "binance");
  await settle(w);
  B.subscribeTicker(["BTC"], () => undefined);
  B.configure(CAPS, "binance");
  await settle(w);
  w.sockets[0].accept();
  ca.ctl.setHidden(true);
  await settle(w, 16_000);
  assert.ok(!A.isLeader && B.isLeader);
  assert.equal(w.sockets[0].closed, true);
  assert.equal(w.sockets.length, 2);
  cb.ctl.setHidden(true);
  await settle(w, 16_000);
  assert.ok(!A.isLeader && !B.isLeader);
  assert.equal(w.sockets.filter((s) => !s.closed).length, 0);
  ca.ctl.setHidden(false);
  await settle(w);
  assert.ok(A.isLeader);
  assert.equal(w.sockets.filter((s) => !s.closed).length, 1);
});

await test("multi-tab: follower notices a vanished leader (no status for 5 s)", async () => {
  const w = world();
  const A = new R.RtHub(env(w, { shared: true }).e);
  const B = new R.RtHub(env(w, { shared: true }).e);
  A.subscribeTicker(["BTC"], () => undefined);
  A.configure(CAPS, "binance");
  await settle(w);
  B.subscribeTicker(["BTC"], () => undefined);
  B.configure(CAPS, "binance");
  await settle(w);
  w.sockets[0].accept();
  w.sockets[0].push(tk("BTC", 1, "binance", w));
  await settle(w);
  assert.equal(B.getStatus().state, "live");
  // leader frozen (e.g. process suspended): its channel goes silent
  for (const c of w.channels.slice(0, 1)) c.closed = true;
  w.channels[0].postMessage = () => undefined;
  await settle(w, 6_000);
  assert.notEqual(B.getStatus().state, "live");
});

await test("no BroadcastChannel / Web Locks -> one connection per tab", async () => {
  const w = world();
  const A = new R.RtHub(env(w).e);
  const B = new R.RtHub(env(w).e);
  for (const h of [A, B]) {
    h.subscribeTicker(["BTC"], () => undefined);
    h.configure(CAPS, "binance");
  }
  assert.equal(w.sockets.length, 2);
  assert.ok(!A.getStatus().shared && !B.getStatus().shared);
});

await test("disable (markets stops advertising realtime) closes everything and goes off", async () => {
  const w = world();
  const hub = new R.RtHub(env(w).e);
  hub.subscribeTicker(["BTC"], () => undefined);
  hub.configure(CAPS, "binance");
  w.sockets[0].accept();
  hub.configure(undefined, "binance");
  assert.equal(w.sockets[0].closed, true);
  assert.equal(hub.getStatus().state, "off");
  w.advance(120_000);
  assert.equal(w.sockets.length, 1);
});

console.log(`${n} realtime tests passed`);
