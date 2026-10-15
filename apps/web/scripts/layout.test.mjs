// Unit tests for the multi-chart layout helpers (no browser): npm run test:layout
import { build } from "esbuild";
import { pathToFileURL } from "node:url";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import assert from "node:assert/strict";

const out = join(mkdtempSync(join(tmpdir(), "layout-")), "layout.mjs");
await build({ entryPoints: ["src/components/chart/layout.ts"], bundle: true, format: "esm", platform: "node", outfile: out, logLevel: "error", alias: { "@": "./src" } });
const L = await import(pathToFileURL(out).href);
const mem = () => { const m = new Map(); return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => void m.set(k, String(v)), m }; };

// defaults
const d = L.defaultLayout("4h");
assert.equal(d.n, 1); assert.equal(d.sync, true);
assert.deepEqual(d.extra.map((c) => c.symbol), ["ETH", "SOL", "BNB"]);
assert.ok(d.extra.every((c) => c.tf === "4h"));

// round trip
const kv = mem();
const l = L.defaultLayout("1h"); l.n = 4; l.sync = false; l.extra[1] = { symbol: "DOGE", tf: "15m" };
L.saveLayout(l, kv);
assert.deepEqual(L.loadLayout("1h", kv), l);

// corrupt / hostile values fall back field by field, never throw
for (const raw of [null, "", "{", "[]", "null", '{"n":3}', '{"n":"2"}', '{"extra":"x"}']) {
  const p = L.parseLayout(raw, "1h");
  assert.ok(L.isLayoutN(p.n) && p.extra.length === 3, `raw=${raw}`);
}
const mixed = L.parseLayout('{"n":2,"extra":[{"symbol":"btc","tf":"zz"},{"symbol":"<script>","tf":"1d"},{"symbol":"SOL"}]}', "1h");
assert.equal(mixed.n, 2);
// cell 0: lower-case symbol upper-cased, bad tf -> fallback; cell 1: hostile symbol rejected (default SOL), tf kept; cell 2: symbol taken, tf default
assert.deepEqual(mixed.extra, [{ symbol: "BTC", tf: "1h" }, { symbol: "SOL", tf: "1d" }, { symbol: "SOL", tf: "1h" }]);

// normalize: unknown symbols replaced, cells distinct from the primary
const known = ["BTC", "ETH", "SOL", "BNB", "XRP"];
const n1 = L.normalizeCells({ ...d, extra: [{ symbol: "ETH", tf: "1h" }, { symbol: "ZZZ", tf: "1h" }, { symbol: "ETH", tf: "1h" }] }, known, "ETH");
const syms = n1.extra.map((c) => c.symbol);
assert.ok(!syms.includes("ETH") && !syms.includes("ZZZ"), syms.join());
assert.equal(new Set(syms).size, 3);
// unchanged input returns the same object (no re-render churn)
assert.equal(L.normalizeCells(d, known, "BTC"), d);
// empty market list: untouched
assert.equal(L.normalizeCells(d, [], "BTC"), d);

// cellsFor
const cs = L.cellsFor({ ...d, n: 2 }, { symbol: "BTC", tf: "1h" });
assert.deepEqual(cs.map((c) => c.symbol), ["BTC", "ETH"]);
assert.equal(L.cellsFor({ ...d, n: 4 }, { symbol: "BTC", tf: "1h" }).length, 4);
assert.equal(L.cellsFor({ ...d, n: 1 }, { symbol: "BTC", tf: "1h" }).length, 1);
console.log("layout tests ok");
