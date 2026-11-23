import assert from "node:assert/strict";
import { isAccountPath, isPremiumPath } from "../src/lib/subscription.ts";

for (const path of ["/", "/mainstream", "/majors", "/markets", "/market", "/market/BTC", "/news", "/airdrops", "/subscribe", "/login", "/register", "/forgot-password", "/ai"]) {
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

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const shell = readFileSync(join(root, "src/components/layout/AppShell.tsx"), "utf8");
const gate = readFileSync(join(root, "src/components/subscribe/SubscribeGate.tsx"), "utf8");
const registerPage = readFileSync(join(root, "src/pages/LoginPage/RegisterPage.tsx"), "utf8");
const registerForm = readFileSync(join(root, "src/pages/LoginPage/RegisterForm.tsx"), "utf8");
const loginPage = readFileSync(join(root, "src/pages/LoginPage/LoginPage.tsx"), "utf8");
const subscribePage = readFileSync(join(root, "src/pages/SubscribePage/SubscribePage.tsx"), "utf8");
const fail = (msg) => {
  console.error(msg);
  process.exit(1);
};

const router = readFileSync(join(root, "src/app/router.tsx"), "utf8");
if (!router.includes('path: "register"')) {
  fail("router must keep a real /register route");
}
if (!registerPage.includes("RegisterForm")) {
  fail("RegisterPage must render the shared RegisterForm");
}
if (!registerForm.includes("registerAccount") || !registerForm.includes("password_confirm") || !registerForm.includes("signup_allowed")) {
  fail("the shared register form must call the existing register API, confirm the password, and respect signup_allowed");
}
if (!registerForm.includes("auth.register.closed") || !registerForm.includes('to="/login"')) {
  fail("closed registration must say so clearly and link to login, not fake success");
}
if ([registerPage, registerForm].some((src) => src.includes("oauth") || src.includes("social") || /terms of service|用户协议/.test(src))) {
  fail("do not invent social login or terms");
}
if (loginPage.includes('to="/register"') === false || loginPage.includes("signup_allowed") === false) {
  fail("login must interlink to register only when signup is allowed");
}
if (!shell.includes('to="/register"') || !shell.includes("signupAllowed")) {
  fail("anonymous account menu must offer register when signup is allowed");
}
if (gate.includes('to="/subscribe"') === false || gate.includes("subscribe.price") === false || gate.includes("subscribe.cta") === false) {
  fail("logged-in locked card and shared price keys must keep the $99 plan");
}
if (!gate.includes("RegisterForm") || !gate.includes("is-register") || !gate.includes("assumeOpen")) {
  fail("anonymous shell/account lock must use the shared RegisterForm as the primary panel");
}
if (!gate.includes('if ((shell || account) && anon)')) {
  fail("only the anonymous shell/account branch should become the registration panel");
}
if (subscribePage.includes('to="/register"') === false) {
  fail("subscribe page should offer a register link for anonymous visitors when signup is allowed");
}

console.log("subscription gate OK");
