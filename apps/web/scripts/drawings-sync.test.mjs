// Unit tests for the drawings sync engine (no browser): npm run test:sync
import { build } from "esbuild";
import { pathToFileURL } from "node:url";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import assert from "node:assert/strict";

const out = join(mkdtempSync(join(tmpdir(), "dsync-")), "sync.mjs");
await build({ entryPoints: ["src/components/chart/kline/drawingsSync.ts"], bundle: true, format: "esm", platform: "node", outfile: out, logLevel: "error" });
const { DrawingsSync, SyncConflict, SyncUnavailable, mergeDrawings } = await import(pathToFileURL(out).href);

const mem = () => { const m = new Map(); return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => void m.set(k, String(v)), removeItem: (k) => void m.delete(k), m }; };
const seg = (v, t = 1_760_000_000_000) => ({ name: "segment", points: [{ timestamp: t, value: v }, { timestamp: t + 1, value: v + 1 }] });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const LK = "auu.kc.drawings.v1.BTC", MK = "auu.kc.drawings.sync.v1.BTC";

function fakeServer() {
  const doc = { drawings: [], updatedAt: 0 };
  const calls = [];
  let fail = null;
  return {
    doc, calls, setFail: (f) => (fail = f),
    api: {
      async get() { calls.push("get"); if (fail) throw fail; return structuredClone(doc); },
      async put(_s, drawings, base) {
        calls.push(`put:${base}`);
        if (fail) throw fail;
        if (base !== doc.updatedAt) throw new SyncConflict(structuredClone(doc));
        doc.drawings = structuredClone(drawings); doc.updatedAt = drawings.length ? doc.updatedAt + 10 : 0;
        return structuredClone(doc);
      },
    },
  };
}
function mk(srv, kv = mem()) {
  const states = [], remotes = [];
  const s = new DrawingsSync(srv.api, kv, { state: (_, st) => states.push(st), remote: (_, l) => remotes.push(l) }, { debounceMs: 5, retryMs: 20 });
  return { s, kv, states, remotes };
}
const T = [];
const test = (name, fn) => T.push([name, fn]);

test("migrates legacy local drawings to an empty server", async () => {
  const srv = fakeServer(); const { s, kv, states } = mk(srv);
  kv.setItem(LK, JSON.stringify([seg(1), seg(2)]));
  await s.pull("BTC");
  assert.equal(srv.doc.drawings.length, 2);
  assert.equal(states.at(-1), "synced");
  assert.deepEqual(JSON.parse(kv.getItem(MK)), { base: srv.doc.updatedAt, dirty: false });
});

test("legacy local + existing server drawings are merged (nothing lost)", async () => {
  const srv = fakeServer(); srv.doc.drawings = [seg(1)]; srv.doc.updatedAt = 50;
  const { s, kv, remotes } = mk(srv);
  kv.setItem(LK, JSON.stringify([seg(2)]));
  await s.pull("BTC");
  assert.equal(srv.doc.drawings.length, 2);
  assert.equal(JSON.parse(kv.getItem(LK)).length, 2);
  assert.equal(remotes.at(-1).length, 2);
});

test("fresh device adopts the server copy", async () => {
  const srv = fakeServer(); srv.doc.drawings = [seg(1), seg(2)]; srv.doc.updatedAt = 7;
  const { s, kv, remotes } = mk(srv);
  await s.pull("BTC");
  assert.equal(JSON.parse(kv.getItem(LK)).length, 2);
  assert.equal(remotes.length, 1);
  assert.equal(JSON.parse(kv.getItem(MK)).base, 7);
});

test("clean local follows a newer server copy, including deletion elsewhere", async () => {
  const srv = fakeServer(); srv.doc.drawings = [seg(1)]; srv.doc.updatedAt = 7;
  const { s, kv, remotes } = mk(srv);
  await s.pull("BTC");
  srv.doc.drawings = []; srv.doc.updatedAt = 0; // deleted on another device
  await s.pull("BTC");
  assert.equal(JSON.parse(kv.getItem(LK)).length, 0);
  assert.equal(remotes.at(-1).length, 0);
});

test("edits are debounced into one push; deletes propagate when the base did not move", async () => {
  const srv = fakeServer(); srv.doc.drawings = [seg(1), seg(2)]; srv.doc.updatedAt = 7;
  const { s, kv } = mk(srv);
  await s.pull("BTC");
  kv.setItem(LK, JSON.stringify([seg(1)])); s.markDirty("BTC");
  kv.setItem(LK, JSON.stringify([seg(1), seg(3)])); s.markDirty("BTC");
  await sleep(60);
  assert.equal(srv.calls.filter((c) => c.startsWith("put")).length, 1);
  assert.deepEqual(srv.doc.drawings.map((d) => d.points[0].value), [1, 3]);
  assert.equal(JSON.parse(kv.getItem(MK)).dirty, false);
});

test("conflict (other device edited) merges both sides", async () => {
  const srv = fakeServer(); srv.doc.drawings = [seg(1)]; srv.doc.updatedAt = 7;
  const { s, kv, remotes } = mk(srv);
  await s.pull("BTC");
  srv.doc.drawings = [seg(1), seg(9)]; srv.doc.updatedAt = 17; // another device added seg(9)
  kv.setItem(LK, JSON.stringify([seg(1), seg(5)])); s.markDirty("BTC");
  await sleep(60);
  assert.deepEqual(srv.doc.drawings.map((d) => d.points[0].value).sort(), [1, 5, 9]);
  assert.equal(remotes.at(-1).length, 3);
  assert.equal(JSON.parse(kv.getItem(MK)).dirty, false);
});

test("offline: keeps dirty, local copy untouched, retries and syncs when back", async () => {
  const srv = fakeServer(); const { s, kv, states } = mk(srv);
  await s.pull("BTC");
  srv.setFail(new SyncUnavailable("network"));
  kv.setItem(LK, JSON.stringify([seg(1)])); s.markDirty("BTC");
  await sleep(40);
  assert.equal(states.at(-1), "error");
  assert.equal(JSON.parse(kv.getItem(MK)).dirty, true);
  assert.equal(srv.doc.drawings.length, 0);
  srv.setFail(null);
  await sleep(80);
  assert.equal(srv.doc.drawings.length, 1);
  assert.equal(states.at(-1), "synced");
});

test("signed out / standby (auth): stays local, no retry storm", async () => {
  const srv = fakeServer(); const { s, kv, states } = mk(srv);
  srv.setFail(new SyncUnavailable("auth"));
  kv.setItem(LK, JSON.stringify([seg(1)]));
  await s.pull("BTC");
  s.markDirty("BTC");
  await sleep(80);
  assert.equal(states.at(-1), "local");
  assert.equal(srv.calls.filter((c) => c.startsWith("put")).length, 1);
  assert.equal(JSON.parse(kv.getItem(LK)).length, 1);
});

test("edit made while a push is in flight stays dirty and is sent next", async () => {
  const srv = fakeServer(); const { s, kv } = mk(srv);
  await s.pull("BTC");
  const realPut = srv.api.put;
  let first = true;
  srv.api.put = async (...a) => { if (first) { first = false; kv.setItem(LK, JSON.stringify([seg(1), seg(2)])); s.markDirty("BTC"); } return realPut(...a); };
  kv.setItem(LK, JSON.stringify([seg(1)])); s.markDirty("BTC");
  await sleep(80);
  assert.equal(srv.doc.drawings.length, 2);
  assert.equal(JSON.parse(kv.getItem(MK)).dirty, false);
});

test("mergeDrawings de-duplicates identical drawings", () => {
  assert.equal(mergeDrawings([seg(1)], [seg(1), seg(2)]).length, 2);
});

let bad = 0;
for (const [name, fn] of T) {
  try { await fn(); console.log("ok   ", name); } catch (e) { bad++; console.log("FAIL ", name, "\n", e); }
}
process.exit(bad ? 1 : 0);
