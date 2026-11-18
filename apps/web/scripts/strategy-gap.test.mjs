import assert from "node:assert/strict";
import { linePoints } from "../src/components/strategy/segmentSeries.ts";

const day = 86_400_000;
const oldEnd = 1_762_300_800_000;
const fresh = oldEnd + 19 * day;
const points = [
  { ts: oldEnd, epoch: 1, strategy: 0.958, ret: -0.001, btc: 1.1, tbill: 1.02 },
  { ts: fresh, epoch: 2, strategy: 1.01, ret: 0.01, btc: 1, tbill: 1, breakBefore: true },
];
const line = linePoints(points, (p) => (p.strategy - 1) * 100);
assert.equal(line.length, 3);
assert.equal("value" in line[1], false);
assert.ok(line[1].time > line[0].time && line[1].time < line[2].time);
assert.equal(line[0].value, (0.958 - 1) * 100);
assert.equal(line[2].value, (1.01 - 1) * 100);
const single = linePoints(points.slice(0, 1), (p) => p.ret * 100);
assert.equal(single.length, 1);
assert.equal(single[0].value, -0.1);
console.log("strategy gap series OK");
