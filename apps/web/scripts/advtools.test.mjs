// Unit tests for the advanced drawing tool maths (no browser): npm run test:adv
import { build } from "esbuild";
import { pathToFileURL } from "node:url";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import assert from "node:assert/strict";

const out = join(mkdtempSync(join(tmpdir(), "adv-")), "m.mjs");
await build({ entryPoints: ["src/components/chart/kline/advMath.ts"], bundle: true, format: "esm", platform: "node", outfile: out, logLevel: "error" });
const M = await import(pathToFileURL(out).href);
const near = (a, b, e = 1e-6) => assert.ok(Math.abs(a - b) <= e, `${a} !~ ${b}`);

// rayEnd
let e = M.rayEnd({ x: 10, y: 10 }, { x: 2, y: 1 }, 110);
near(e.x, 110); near(e.y, 60);
e = M.rayEnd({ x: 50, y: 10 }, { x: -1, y: 1 }, 110); // leftwards
near(e.x, 0); near(e.y, 60);
e = M.rayEnd({ x: 50, y: 10 }, { x: 0, y: -1 }, 110); // vertical
near(e.x, 50); assert.ok(e.y < -1000);
e = M.rayEnd({ x: 200, y: 10 }, { x: 1, y: 0 }, 110); // already beyond the edge: no backwards ray
near(e.x, 200);

// pitchfork: median goes through midpoint of P1-P2, parallels share its direction
const pf = M.pitchfork({ x: 0, y: 100 }, { x: 40, y: 20 }, { x: 60, y: 80 }, 500);
near(pf.mid.x, 50); near(pf.mid.y, 50);
const slope = (s) => (s[1].y - s[0].y) / (s[1].x - s[0].x);
near(slope(pf.median), -1); near(slope(pf.upper), -1); near(slope(pf.lower), -1);
near(pf.median[1].x, 500); near(pf.upper[0].x, 40); near(pf.lower[0].y, 80);
// median really passes through the midpoint
near(pf.median[0].y + slope(pf.median) * (pf.mid.x - pf.median[0].x), pf.mid.y);

// fib extension (up move A=100 -> B=200, pullback C=150)
const fx = M.fibExtensionLevels(100, 200, 150);
const by = (r) => fx.find((l) => l.ratio === r).value;
near(by(0), 150); near(by(1), 250); near(by(1.618), 311.8); near(by(2.618), 411.8); near(by(0.618), 211.8);
// down move mirrors
const fd = M.fibExtensionLevels(200, 100, 150);
near(fd.find((l) => l.ratio === 1).value, 50);

// fib channel: P2 is 30px below the base line -> ratio 1 line sits 30px lower, same slope
const ch = M.channelLines({ x: 0, y: 0 }, { x: 100, y: 50 }, { x: 40, y: 50 }, 300);
const l0 = ch.find((l) => l.ratio === 0), l1 = ch.find((l) => l.ratio === 1);
near(l0.from.y, 0); near(l1.from.y, 30); // base y at x=40 is 20 -> offset 30
near((l1.to.y - l1.from.y) / (l1.to.x - l1.from.x), 0.5); near(l1.to.x, 300);
assert.equal(M.channelLines({ x: 5, y: 0 }, { x: 5, y: 50 }, { x: 40, y: 50 }, 300).length, 8); // vertical base: no NaN
assert.ok(M.channelLines({ x: 5, y: 0 }, { x: 5, y: 50 }, { x: 40, y: 50 }, 300).every((l) => Number.isFinite(l.to.x) && Number.isFinite(l.to.y)));

// measure
let m = M.measureStats(100, 110, 0, 3 * 3_600_000 + 40 * 60_000, 3_600_000);
near(m.delta, 10); near(m.pct, 10); assert.equal(m.bars, 4); // 3h40m / 1h rounds to 4
m = M.measureStats(200, 150, 5_000_000, 0, 60_000);
near(m.delta, -50); near(m.pct, -25); assert.equal(m.bars, Math.round(5_000_000 / 60_000));
assert.equal(M.measureStats(0, 5, 0, 1000, 60_000).pct, null); // no division by zero
assert.equal(M.measureStats(1, 2, 0, 1000, 0).bars, 0);

// span text
assert.equal(M.formatSpan(0), "0m");
assert.equal(M.formatSpan(45 * 60_000), "45m");
assert.equal(M.formatSpan((2 * 1440 + 10 * 60 + 15) * 60_000), "2d 10h");
assert.equal(M.formatSpan(3 * 60 * 60_000 + 20 * 60_000), "3h 20m");
assert.equal(M.formatSpan(26 * 3_600_000, { d: "天", h: "时", m: "分" }), "1天 2时");

// bar length: median of recent gaps ignores one missing candle
const ts = Array.from({ length: 30 }, (_, i) => i * 3_600_000);
ts.splice(25, 1);
assert.equal(M.barMsOf(ts), 3_600_000);
assert.equal(M.barMsOf([1]), 0);
console.log("adv tools tests ok");
