// liveBadgeState: off / bind / live from /api/v1/exchange/availability (pure logic, no network).
import { build } from "esbuild";
import assert from "node:assert/strict";
const out = await build({ entryPoints: ["src/components/trade/liveBadge.ts"], bundle: true, write: false, format: "esm", platform: "node" });
const m = await import("data:text/javascript;base64," + Buffer.from(out.outputFiles[0].text).toString("base64"));
const g = (a) => ({ available: a, reason: a ? null : "X", message: null });
const row = (bound, spot, swap = false) => ({ bound, spot: { open: g(spot), close: g(spot) }, swap: { open: g(swap), close: g(swap) } });
assert.equal(m.liveBadgeState(null), "off");
assert.equal(m.liveBadgeState({ enabled: false, killSwitch: false, exchanges: { binance: row(true, true) } }), "off");
assert.equal(m.liveBadgeState({ enabled: true, killSwitch: true, exchanges: { binance: row(true, true) } }), "off");
assert.equal(m.liveBadgeState({ enabled: true, killSwitch: false, exchanges: { binance: row(false, false), okx: row(false, false) } }), "bind");
assert.equal(m.liveBadgeState({ enabled: true, killSwitch: false, exchanges: { binance: row(true, false) } }), "bind"); // bound, no trade perm
assert.equal(m.liveBadgeState({ enabled: true, killSwitch: false, exchanges: { okx: row(true, false, true) } }), "live");
assert.equal(m.liveBadgeState({ enabled: true, killSwitch: false, exchanges: { binance: row(true, true) } }), "live");
assert.equal(m.LIVE_BADGE_HREF.live, "/exchange");
assert.equal(m.LIVE_BADGE_HREF.bind, "/exchange#bind");
console.log("live-badge ok");
