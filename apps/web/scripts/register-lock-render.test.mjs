// Actual React render of SubscribeLock / RegisterForm. Run: node scripts/register-lock-render.test.mjs
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import vm from "node:vm";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(join(root, "package.json"));
const ts = require("typescript");
const React = require("react");
const server = require("react-dom/server");
const zh = JSON.parse(readFileSync(join(root, "src/i18n/locales/zh-CN.json"), "utf8"));

const t = (key) => key.split(".").reduce((x, k) => x?.[k], zh) || key;
const Link = ({ to, children, ...rest }) => React.createElement("a", { href: to, ...rest }, children);

let hookState = [];
let hookIndex = 0;
let sub = { anon: true, signupAllowed: true };
let registerError = "SIGNUP_CLOSED";
let navigated = false;

function load(file) {
  const source = readFileSync(join(root, "src", file), "utf8");
  const code = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const mod = { exports: {} };
  const req = (name) => {
    if (name === "react") {
      return {
        ...React,
        useContext: () => sub,
        useEffect: () => {},
        useState: (init) => {
          const i = hookIndex++;
          return [i in hookState ? hookState[i] : init, (v) => { hookState[i] = typeof v === "function" ? v(hookState[i]) : v; }];
        },
      };
    }
    if (name === "react/jsx-runtime") return require("react/jsx-runtime");
    if (name === "react-router-dom") {
      return { Link, useNavigate: () => () => { navigated = true; throw new Error("unexpected successful navigation"); } };
    }
    if (name === "react-i18next") {
      return { useTranslation: () => ({ t }), Trans: ({ i18nKey }) => React.createElement("span", null, t(i18nKey)) };
    }
    if (name === "@/providers/HttpWsProvider") {
      return { marketProvider: { registerAccount: async () => { throw { code: registerError }; }, getMe: async () => ({ auth_enabled: true, signup_allowed: sub.signupAllowed, user: null, email_verify: false }) } };
    }
    if (name === "@/lib/authRules") {
      return { nameRule: () => "", passwordRule: () => "", codeProblem: () => null, emailProblem: () => null, nameProblem: () => null, passwordProblem: () => null };
    }
    if (name === "@/i18n/errors") return { errText: (e) => e.code };
    if (name === "./AuthHead" || name.endsWith("/AuthHead")) {
      return { AuthHead: ({ title, sub }) => React.createElement("header", null, React.createElement("h1", null, title), sub ? React.createElement("p", null, sub) : null) };
    }
    if (name === "./EmailCodeField" || name.endsWith("/EmailCodeField")) {
      return { EmailCodeField: () => React.createElement("input", { "data-email-code": true }) };
    }
    if (name === "@/pages/LoginPage/RegisterForm") return load("pages/LoginPage/RegisterForm.tsx");
    if (name === "@/components/subscribe/SubscribeGate") throw new Error("cycle");
    throw new Error("unhandled import " + name);
  };
  vm.runInThisContext("(function(require,module,exports){" + code + "\n})")(req, mod, mod.exports);
  return mod.exports;
}

function renderLock(variant, state) {
  sub = state;
  hookState = [];
  hookIndex = 0;
  const { SubscribeLock } = load("components/subscribe/SubscribeGate.tsx");
  hookIndex = 0;
  return server.renderToStaticMarkup(React.createElement(SubscribeLock, { variant }));
}

const openAnon = { anon: true, signupAllowed: true };
const closedAnon = { anon: true, signupAllowed: false };
const lockedUser = { anon: false, signupAllowed: false };

const shellOpen = renderLock("shell", openAnon);
assert.ok(shellOpen.includes("<form"), shellOpen);
assert.ok(shellOpen.includes("注册后使用此功能"), shellOpen);
assert.ok(shellOpen.includes('type="submit"'), shellOpen);
assert.equal(shellOpen.includes("$99"), false, shellOpen);
assert.equal(shellOpen.includes('href="/subscribe"'), false, shellOpen);
assert.ok(shellOpen.includes('href="/login"'), shellOpen);
assert.equal(shellOpen.includes("查看 $99/月 方案"), false, shellOpen);
console.log("SHELL_OPEN_IS_REGISTER_FORM");

const shellClosed = renderLock("shell", closedAnon);
assert.equal(shellClosed.includes("<form"), false, shellClosed);
assert.ok(shellClosed.includes("注册已关闭"), shellClosed);
assert.ok(shellClosed.includes('href="/login"'), shellClosed);
assert.equal(shellClosed.includes("$99"), false, shellClosed);
assert.equal(shellClosed.includes('href="/subscribe"'), false, shellClosed);
assert.ok(shellClosed.includes("td-submit"), shellClosed);
console.log("SHELL_CLOSED_NO_FORM_NO_PRICE");

const accountOpen = renderLock("account", openAnon);
assert.ok(accountOpen.includes("<form"), accountOpen);
assert.equal(accountOpen.includes("$99"), false, accountOpen);
console.log("ACCOUNT_OPEN_IS_REGISTER_FORM");

const locked = renderLock("locked", lockedUser);
assert.ok(locked.includes("$99"), locked);
assert.ok(locked.includes('href="/subscribe"'), locked);
assert.ok(locked.includes("查看 $99/月 方案"), locked);
assert.equal(locked.includes("<form"), false, locked);
console.log("LOCKED_KEEPS_99_PLAN");

(async () => {
  // Stale open form that receives SIGNUP_CLOSED must not navigate as success.
  sub = openAnon;
  hookState = [];
  hookIndex = 0;
  const { RegisterForm } = load("pages/LoginPage/RegisterForm.tsx");
  hookState = [
    { auth_enabled: true, signup_allowed: true, email_verify: false, user: null },
    "reviewuser",
    "password1",
    "password1",
    "",
    "",
    "",
    false,
    "",
  ];
  function tree() {
    hookIndex = 0;
    return RegisterForm({ embed: true, assumeOpen: true });
  }
  function find(el, type) {
    if (!el || typeof el !== "object") return;
    if (el.type === type) return el;
    const children = [el.props?.children].flat(Infinity);
    for (const c of children) {
      const r = find(c, type);
      if (r) return r;
    }
  }
  const form = find(tree(), "form");
  assert.ok(form, "embedded form should render when assumeOpen");
  navigated = false;
  registerError = "SIGNUP_CLOSED";
  form.props.onSubmit({ preventDefault() {} });
  await new Promise(setImmediate);
  assert.equal(navigated, false);
  hookIndex = 0;
  const stale = server.renderToStaticMarkup(tree());
  assert.ok(stale.includes("SIGNUP_CLOSED"), stale);
  assert.ok(stale.includes("<form"), stale);
  console.log("STALE_SUBMIT_REJECTED_NO_FAKE_SUCCESS");
  console.log("register-lock-render OK");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
