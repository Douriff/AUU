// Unit tests for the 空投雷达 auto-refresh helpers (no browser): npm run test:airdrop
import { build } from "esbuild";
import { pathToFileURL } from "node:url";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import assert from "node:assert/strict";

const out = join(mkdtempSync(join(tmpdir(), "adr-")), "refresh.mjs");
await build({ entryPoints: ["src/pages/AirdropsPage/refresh.ts"], bundle: true, format: "esm", platform: "node", outfile: out, logLevel: "error" });
const R = await import(pathToFileURL(out).href);

assert.equal(R.AUTO_REFRESH_MS, 60_000);

// fake clock + timers + visibility
function harness(hidden = false) {
  const h = { hidden, runs: 0, timers: new Map(), id: 0, vis: null, offCalled: 0, now: 0 };
  const opts = {
    intervalMs: R.AUTO_REFRESH_MS,
    run: () => void h.runs++,
    isHidden: () => h.hidden,
    onVisibility: (cb) => { h.vis = cb; return () => void h.offCalled++; },
    setTimer: (fn, ms) => { const id = ++h.id; h.timers.set(id, { fn, ms }); return id; },
    clearTimer: (id) => void h.timers.delete(id),
  };
  h.tick = () => { for (const t of [...h.timers.values()]) t.fn(); };
  h.setHidden = (v) => { h.hidden = v; h.vis(); };
  return { h, opts };
}

{ // visible: one 60 s timer, each tick refreshes
  const { h, opts } = harness();
  const dispose = R.createAutoRefresh(opts);
  assert.equal(h.timers.size, 1);
  assert.equal([...h.timers.values()][0].ms, 60_000);
  assert.equal(h.runs, 0); // initial load is the page's own effect, not the poller's
  h.tick(); h.tick();
  assert.equal(h.runs, 2);
  dispose();
  assert.equal(h.timers.size, 0);
  assert.equal(h.offCalled, 1);
}
{ // hidden: paused (timer stopped, ticks do nothing); visible again: refresh immediately, cadence restarts
  const { h, opts } = harness();
  R.createAutoRefresh(opts);
  h.setHidden(true);
  assert.equal(h.timers.size, 0);
  h.tick();
  assert.equal(h.runs, 0);
  h.setHidden(false);
  assert.equal(h.runs, 1); // immediate
  assert.equal(h.timers.size, 1);
  h.tick();
  assert.equal(h.runs, 2);
}
{ // starts hidden: no timer until shown
  const { h, opts } = harness(true);
  R.createAutoRefresh(opts);
  assert.equal(h.timers.size, 0);
  h.setHidden(false);
  assert.equal(h.runs, 1);
  assert.equal(h.timers.size, 1);
}
{ // a tick that fires while hidden (race) is ignored
  const { h, opts } = harness();
  R.createAutoRefresh(opts);
  h.hidden = true; // no visibilitychange delivered yet
  h.tick();
  assert.equal(h.runs, 0);
}
{ // repeated visible events never stack timers
  const { h, opts } = harness();
  R.createAutoRefresh(opts);
  h.vis(); h.vis(); h.vis();
  assert.equal(h.timers.size, 1);
  assert.equal(h.runs, 3);
}

// minutes / server clock
assert.equal(R.minutesAgo(1000, 1000), 0);
assert.equal(R.minutesAgo(0, 59_999), 0);
assert.equal(R.minutesAgo(0, 60_000), 1);
assert.equal(R.minutesAgo(0, 5 * 60_000 + 10), 5);
assert.equal(R.minutesAgo(10_000, 0), 0); // future timestamp -> 0
assert.equal(R.serverClock(1_000_000, 500, 800), 1_000_300);
assert.equal(R.serverClock(1_000_000, 500, 100), 1_000_000); // client clock went back: never negative
assert.equal(R.serverClock(null, 500, 800), 800);

// staleness: > max(15 min, 3 ticks)
const NOW = 10_000_000_000;
assert.equal(R.staleMinutes(null, NOW, 240), null);
assert.equal(R.staleMinutes(NOW - 10 * 60_000, NOW, 240), null);
assert.equal(R.staleMinutes(NOW - 16 * 60_000, NOW, 240), 16);
assert.equal(R.staleMinutes(NOW - 16 * 60_000, NOW, 900), null); // 3 x 900 s = 45 min tolerance
assert.equal(R.staleMinutes(NOW - 50 * 60_000, NOW, 900), 50);
assert.equal(R.staleMinutes(NOW - 16 * 60_000, NOW, undefined), 16);

// degraded feeds: failed with an attempt; never-tried (no attempt) is not a warning
const feeds = [
  { name: "A", ok: true, lastAttemptAt: 1 },
  { name: "B", ok: false, lastAttemptAt: 2 },
  { name: "C", ok: false, lastAttemptAt: null },
];
assert.deepEqual(R.degradedFeeds(feeds).map((f) => f.name), ["B"]);
assert.deepEqual(R.degradedFeeds(undefined), []);
console.log("airdrop-refresh tests passed");
