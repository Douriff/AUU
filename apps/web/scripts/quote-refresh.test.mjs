// Unit tests for public-quote poll cadence + freshness math (no browser): npm run test:quote
import { build } from "esbuild";
import { pathToFileURL } from "node:url";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import assert from "node:assert/strict";

const out = join(mkdtempSync(join(tmpdir(), "qref-")), "quoteRefresh.mjs");
await build({
  entryPoints: ["src/lib/quoteRefresh.ts"],
  bundle: true,
  format: "esm",
  platform: "node",
  outfile: out,
  logLevel: "error",
});
const Q = await import(pathToFileURL(out).href);

// Before this pass: list 5s (30s while push was "live"), chart 10s (30s live), majors 5s, book 3s.
assert.equal(Q.MARKET_POLL_MS, 2_000);
assert.equal(Q.MARKET_POLL_LIVE_MS, 12_000);
assert.equal(Q.MARKET_POLL_HIDDEN_MS, 20_000);
assert.equal(Q.CHART_POLL_MS, 2_000);
assert.equal(Q.CHART_POLL_LIVE_MS, 12_000);
assert.equal(Q.CHART_POLL_HIDDEN_MS, 20_000);
assert.equal(Q.MAJORS_POLL_MS, 4_000);
assert.equal(Q.MAJORS_POLL_HIDDEN_MS, 20_000);
assert.equal(Q.BOOK_POLL_MS, 2_000);
assert.equal(Q.SLOW_EVERY * Q.MARKET_POLL_MS, 30_000); // funding history stays ~30s
assert.ok(Q.MARKET_POLL_MS < Q.MARKET_POLL_LIVE_MS);
assert.ok(Q.MARKET_POLL_LIVE_MS < 30_000);
assert.ok(Q.CHART_POLL_MS < 10_000);

function harness({ hidden = false, visibleMs = 2_000, hiddenMs = 20_000, now = 10_000 } = {}) {
  const h = { hidden, runs: [], timers: new Map(), id: 0, vis: null, focus: null, off: 0, now };
  const opts = {
    visibleMs: () => visibleMs,
    hiddenMs,
    run: (why) => h.runs.push(why),
    isHidden: () => h.hidden,
    onVisibility: (cb) => {
      h.vis = cb;
      return () => void h.off++;
    },
    onFocus: (cb) => {
      h.focus = cb;
      return () => void h.off++;
    },
    now: () => h.now,
    setTimer: (fn, ms) => {
      const id = ++h.id;
      h.timers.set(id, { fn, ms });
      return id;
    },
    clearTimer: (id) => void h.timers.delete(id),
  };
  h.timer = () => [...h.timers.values()][0];
  h.tick = () => {
    for (const t of [...h.timers.values()]) t.fn();
  };
  h.setHidden = (v) => {
    h.hidden = v;
    h.vis();
  };
  return { h, opts };
}

{
  const { h, opts } = harness();
  const stop = Q.createVisiblePoll(opts);
  assert.equal(h.runs.length, 0); // no fetch at subscribe
  assert.equal(h.timer().ms, 2_000);
  h.tick();
  assert.deepEqual(h.runs, ["timer"]);
  assert.equal(h.timer().ms, 2_000);
  stop();
  assert.equal(h.timers.size, 0);
  assert.equal(h.off, 2);
}

{
  const { h, opts } = harness();
  Q.createVisiblePoll(opts);
  h.setHidden(true);
  assert.equal(h.runs.length, 0);
  assert.equal(h.timer().ms, 20_000);
  h.tick();
  assert.deepEqual(h.runs, ["timer"]); // slow heartbeat, not a freeze
  assert.equal(h.timer().ms, 20_000);
}

{
  const { h, opts } = harness({ hidden: true });
  Q.createVisiblePoll(opts);
  assert.equal(h.runs.length, 0);
  assert.equal(h.timer().ms, 20_000);
  h.now += 500; // a fetch at subscribe is still fresh; only a later reveal pulls again
  h.setHidden(false);
  assert.deepEqual(h.runs, ["show"]);
  assert.equal(h.timer().ms, 2_000);
}

{
  // visible timer armed, then the tab hides before it fires (no event delivered to the poller yet)
  const { h, opts } = harness();
  Q.createVisiblePoll(opts);
  h.hidden = true;
  h.tick();
  assert.equal(h.runs.length, 0);
  assert.equal(h.timer().ms, 20_000);
}

{
  const { h, opts } = harness();
  Q.createVisiblePoll(opts);
  h.now += 5_000;
  h.focus();
  assert.deepEqual(h.runs, ["show"]);
  h.now += 100; // visibility + focus usually arrive together
  h.vis();
  assert.deepEqual(h.runs, ["show"]);
  h.now += 500;
  h.focus();
  assert.deepEqual(h.runs, ["show", "show"]);
}

{
  // cadence can change between arms (push goes live)
  let live = false;
  const { h, opts } = harness();
  Q.createVisiblePoll({ ...opts, visibleMs: () => (live ? 12_000 : 2_000) });
  assert.equal(h.timer().ms, 2_000);
  live = true;
  h.tick();
  assert.equal(h.timer().ms, 12_000);
}

// skew-free age: sample was already 1.5s old on the server, and the response has been on screen 2s
assert.equal(Q.skewFreeAgeMs(1_000, 2_500, 10_000, 12_000), 1_500 + 2_000);
assert.equal(Q.skewFreeAgeMs(null, null, 10_000, 10_400), 400);
assert.equal(Q.skewFreeAgeMs(5_000, 1_000, 10_000, 10_000), 0); // server clock went backwards: never negative
assert.equal(Q.skewFreeAgeMs(1, 2, 0, 99), 0);

assert.deepEqual(Q.quoteAgeKey(500), { key: "common.updatedNow" });
assert.deepEqual(Q.quoteAgeKey(3_200), { key: "common.updatedAgo", n: 3 });
assert.deepEqual(Q.quoteAgeKey(125_000), { key: "common.updatedMin", n: 2 });

assert.equal(Q.tickerSample("store", 1, 2, 3).store, true);
assert.deepEqual(Q.tickerSample("ticker", 1_000, 2_500, 9_000), {
  eventAt: 1_000,
  serverNow: 2_500,
  clientFetchedAt: 9_000,
  store: false,
});
assert.equal(Q.skewFreeAgeMs(Q.pushSample(8_000).eventAt, Q.pushSample(8_000).serverNow, Q.pushSample(8_000).clientFetchedAt, 9_200), 1_200);

console.log("quote refresh tests OK");
