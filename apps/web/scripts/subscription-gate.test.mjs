import assert from "node:assert/strict";
import { isAccountPath, isPremiumPath } from "../src/lib/subscription.ts";

for (const path of ["/", "/mainstream", "/majors", "/markets", "/market", "/market/BTC", "/news", "/airdrops", "/subscribe", "/login"]) {
  assert.equal(isPremiumPath(path), false, path);
}
for (const path of ["/pnl", "/console", "/performance", "/leaderboard", "/strategy", "/review", "/positions", "/alerts", "/backtest", "/watch", "/watch/abc", "/screener", "/trade", "/trade/mint"]) {
  assert.equal(isPremiumPath(path), true, path);
}
for (const path of ["/exchange", "/exchange/keys", "/points", "/positions", "/settings"]) {
  assert.equal(isAccountPath(path), true, path);
}
assert.equal(isAccountPath("/"), false);
assert.equal(isAccountPath("/pnl"), false);
console.log("subscription gate OK");
