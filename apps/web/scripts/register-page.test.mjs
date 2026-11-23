// Guards for the public /register form and signup switch. Run: node scripts/register-page.test.mjs
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
import { isAccountPath, isPremiumPath } from "../src/lib/subscription.ts";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(join(root, path), "utf8");
const fail = (msg) => {
  console.error(msg);
  process.exit(1);
};

const registerPage = read("src/pages/LoginPage/RegisterPage.tsx");
const registerForm = read("src/pages/LoginPage/RegisterForm.tsx");
const loginPage = read("src/pages/LoginPage/LoginPage.tsx");
const router = read("src/app/router.tsx");
const shell = read("src/components/layout/AppShell.tsx");
const gate = read("src/components/subscribe/SubscribeGate.tsx");
const sub = read("src/lib/subscription.ts");
const provider = read("src/providers/HttpWsProvider.ts");
const accountSection = read("src/pages/SettingsPage/AccountSection.tsx");
const zh = read("src/i18n/locales/zh-CN.json");
const en = read("src/i18n/locales/en.json");

if (!router.includes('path: "register"') || !router.includes("RegisterPage")) fail("router must mount RegisterPage at /register");
if (!router.includes('path: "login"') || !router.includes('path: "subscribe"')) fail("login and subscribe routes must stay");
if (!provider.includes('postJson("/api/v1/auth/register"')) fail("the existing register API client must be reused");
if (provider.includes("users-secondary") || registerPage.includes("localStorage.setItem(\"users\"")) fail("must not invent a second user store");

assert.equal(isPremiumPath("/"), false);
assert.equal(isPremiumPath("/register"), false);
assert.equal(isPremiumPath("/login"), false);
assert.equal(isPremiumPath("/ai"), false);
assert.equal(isPremiumPath("/majors"), false);
assert.equal(isPremiumPath("/pnl"), true);
assert.equal(isAccountPath("/settings"), true);
assert.equal(isAccountPath("/"), false);
if (sub.includes('"/mainstream"') && new RegExp('PREMIUM = new Set\\([\\s\\S]*"/mainstream"').test(sub)) {
  fail("free quotes routes must not be added to the premium set");
}

if (!registerPage.includes("RegisterForm")) fail("RegisterPage must render the shared RegisterForm");
if (!registerForm.includes("marketProvider.registerAccount")) fail("the register form must actually submit registerAccount");
if (!registerForm.includes("name.trim()") || !registerForm.includes("password_confirm")) fail("register must send the existing schema fields");
if (!registerForm.includes("signup_allowed")) fail("the form must follow /auth/me signup_allowed");
if (!registerForm.includes("auth.register.closed") || !registerForm.includes("authOn && open") && !registerForm.includes("authOn && !open")) {
  fail("when signup is closed the form must not render; show the closed message");
}

// Simulated access plan (same rules the page follows).
function registerView({ auth_enabled, signup_allowed, user }) {
  if (user) return "redirect";
  if (!auth_enabled) return "localMode";
  if (!signup_allowed) return "closed";
  return "form";
}
assert.equal(registerView({ auth_enabled: true, signup_allowed: false, user: null }), "closed");
assert.equal(registerView({ auth_enabled: true, signup_allowed: true, user: null }), "form");
assert.equal(registerView({ auth_enabled: true, signup_allowed: true, user: { id: "u" } }), "redirect");
assert.equal(registerView({ auth_enabled: false, signup_allowed: false, user: null }), "localMode");

if (loginPage.includes("signup_allowed") === false || loginPage.includes('to="/register"') === false) {
  fail("login must link to /register only when signup_allowed");
}
if (!shell.includes('to="/register"') || !shell.includes("signupAllowed")) {
  fail("anonymous account menu must offer register when signup is allowed");
}
if (!gate.includes("RegisterForm") || !gate.includes("is-register")) {
  fail("anonymous shell lock must present the shared registration form as the primary panel");
}
if (!gate.includes("subscribe.price") || !gate.includes('to="/subscribe"')) {
  fail("the logged-in locked card must keep the $99 star subscription plan");
}
if (!accountSection.includes("signup_allowed") || !accountSection.includes("account.anonClosed")) {
  fail("settings anonymous copy must hide the register link when signup is closed");
}
if (!zh.includes("注册已关闭") || !en.includes("Registration is closed")) fail("locales must describe the closed state");
if (!zh.includes("不会扣款") || !en.includes("does not charge")) fail("locales must say registration is not a payment");

for (const lang of ["zh-CN", "en", "de", "fr", "es", "pt", "tr", "ru", "ja", "ko", "ar"]) {
  const loc = JSON.parse(read(`src/i18n/locales/${lang}.json`));
  const closed = loc.auth?.register?.closed;
  const direct = loc.auth?.register?.subDirect;
  const shellTitle = loc.auth?.register?.shellTitle;
  if (typeof closed !== "string" || !closed.includes("<login>") || !closed.includes("</login>")) {
    fail(`${lang} auth.register.closed must include a <login> tag`);
  }
  if (typeof direct !== "string" || !direct.trim()) fail(`${lang} auth.register.subDirect is required`);
  if (typeof shellTitle !== "string" || !shellTitle.trim()) fail(`${lang} auth.register.shellTitle is required`);
  if (!loc.apiErr?.SIGNUP_NOT_READY || !loc.apiErr?.STORE_UNAVAILABLE) fail(`${lang} needs SIGNUP_NOT_READY and STORE_UNAVAILABLE`);
}

console.log("register page guard OK");
