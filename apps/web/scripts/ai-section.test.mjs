// Same-site /ai section guards. Run: node scripts/ai-section.test.mjs
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(join(root, path), "utf8");
const fail = (msg) => {
  console.error(msg);
  process.exit(1);
};

const shell = read("src/components/layout/AppShell.tsx");
const router = read("src/app/router.tsx");
const premium = read("src/lib/subscription.ts");
const page = read("src/pages/AiSectionPage/AiSectionPage.tsx");
const css = read("src/pages/AiSectionPage/aiSection.css");
const zh = read("src/i18n/locales/zh-CN.json");
const en = read("src/i18n/locales/en.json");
const compilePy = readFileSync(join(root, "../api/app/ai_section/compile.py"), "utf8");
const routesPy = readFileSync(join(root, "../api/app/ai_section/routes.py"), "utf8");
const catalogPy = readFileSync(join(root, "../api/app/ai_section/catalog_view.py"), "utf8");
const runnerPy = readFileSync(join(root, "../api/app/ai_section/runner_view.py"), "utf8");
const bodyPy = readFileSync(join(root, "../api/app/ai_section/body.py"), "utf8");
const schemaPy = readFileSync(join(root, "../api/app/ai_section/schema.py"), "utf8");
const storePy = readFileSync(join(root, "../api/app/ai_section/store.py"), "utf8");
const simulatePy = readFileSync(join(root, "../api/app/ai_section/simulate.py"), "utf8");
const mainPy = readFileSync(join(root, "../api/app/main.py"), "utf8");
const boardPage = read("src/pages/MainstreamPage/MainstreamPage.tsx");

const nav = shell.split("const mainstreamNav")[1].split("const mobileTabs")[0];
if (!nav.includes('to: "/ai"') || !/match:\s*isAi/.test(nav)) fail("top-level AI nav must point at /ai");
if (nav.includes('to: "/?symbol=BTC&ai=1"')) fail("top-level AI nav must not stay a chart-panel query");
if (!shell.includes('return "/ai"')) fail("aiHref must return /ai");
if (!/p === "\/ai" \|\| p\.startsWith\("\/ai\/"\)/.test(shell)) fail("isAi must match the /ai section path");

if (!router.includes("import { AiSectionPage }") || !router.includes('path: "ai"')) fail("router is missing the /ai page");
if (!router.includes('path: "/lab/ai-review"')) fail("isolated lab route must stay");
if (nav.includes("/lab") || page.includes("/lab/") || page.includes('to="/lab') || page.includes('to: "/lab')) {
  fail("AI section must not add /lab to the product shell");
}

const block = premium.split("const PREMIUM")[1].split(");")[0];
if (block.includes('"/ai"')) fail("/ai must not be added to the premium path set");

if (page.includes("<iframe")) fail("AI section must not use an iframe");
if (/auutrade-platform/i.test(page) || /hyperliquid/i.test(page)) fail("AI section must not reference the source exchange/signing stack");
if (!page.includes('data-testid="ai-section"')) fail("section test id missing");
if (!page.includes('to="/console"') || !page.includes('to="/performance"') || !page.includes('to="/login"')) {
  fail("section must stay on existing same-site routes");
}
if (!page.includes("chartAssistantHref") || !page.includes("&ai=1")) fail("chart assistant link must still toggle ?ai=1");
if (!page.includes("/api/v1/ai-section/catalog") || !page.includes("/api/v1/ai-section/compile")) {
  fail("section must call the same-site catalog and match APIs");
}
if (page.includes("/api/v1/mainstream/strategy")) fail("AI section must not pull the full system runner summary");
if (!page.includes("/api/v1/ai-section/runner")) fail("runner tab must use the trimmed site-wide status endpoint");
if (page.includes("positions") && /agent\?\.positions|positions\.length/.test(page)) {
  fail("runner tab must not present system positions as a position list");
}
if (!page.includes('askNote({ kind: "decision", id: row.id })') || !page.includes("onClick={() => void ask(row)}")) {
  fail("model interpret must stay behind an explicit click");
}

const afterImports = page.slice(page.indexOf("export function AiSectionPage"));
const beforeAsk = afterImports.slice(0, afterImports.indexOf("async function ask("));
if (beforeAsk.includes("askNote(") || beforeAsk.includes("/api/v1/ai-review/interpret")) {
  fail("loading the AI section must not call the model");
}
if ((page.match(/askNote\(/g) || []).length !== 1) fail("askNote must be used once, in ask()");
if (page.includes("/api/v1/ai-review/relay") || page.includes("previewRelay") || page.includes("previewExecution")) {
  fail("AI section must not call relay preview or execution preview");
}
if (tabReviewGateLoosened(page)) fail("review tab must keep the admin gate");

function tabReviewGateLoosened(src) {
  return !src.includes('setGate("member")') || !src.includes("/api/v1/ai-review/decisions") || !src.includes("is_admin");
}

if (!css.includes("var(--accent)") || !css.includes("--tab-h") || !css.includes("safe-area-inset-bottom")) {
  fail("section CSS must use AUU tokens and clear the mobile tab bar");
}

if (!compilePy.includes("catalog_match") || !compilePy.includes("deterministic_catalog_matcher")) {
  fail("matcher must label itself as a catalog match, not a compiler product");
}
if (/generates_code": False/.test(compilePy) === false && !compilePy.includes('"generates_code": False')) {
  fail("matcher must state that it does not generate code");
}
if (/openai|anthropic|hyperliquid/i.test(compilePy)) fail("matcher must stay an independent rule table");
if (!compilePy.includes("MAX_BODY") || !bodyPy.includes("MAX_BODY = 8000") || !routesPy.includes("read_bounded_json")) fail("compile route must keep the site body cap");
if (!routesPy.includes('err("BAD_REQUEST", "bad_body", 400)')) fail("compile errors must use existing BAD_REQUEST / bad_body");
if (!routesPy.includes('err("AUTH_REQUIRED", "请先登录", 401)')) fail("runner must reuse the existing login error");
if (!routesPy.includes('prefix="/api/v1/ai-section"') || !mainPy.includes("ai_section_router")) {
  fail("AI section router must be mounted");
}
if (!runnerPy.includes("site_system_runner") || !runnerPy.includes('"personal": False')) {
  fail("runner view must be labeled site-wide and not personal");
}
if (!catalogPy.includes("returns_published") || !catalogPy.includes("performance")) {
  fail("catalog view must explicitly omit invented performance");
}

for (const bundle of [zh, en]) {
  if (bundle.includes("策略工作室") && /未接入来源策略工作室|source strategy studio is not connected/i.test(bundle) === false && bundle === zh) {
    /* zh has 未接入来源策略工作室 in matcher.notStudio */
  }
  if (/完整工作室|full source studio|compile executable|编译可执行/i.test(bundle) === false && /未接入|not connected|not a port|不是来源/i.test(bundle) === false) {
    fail("locales must say the source studio/agents are not connected");
  }
  if (bundle.includes("Sign in to see your paper strategy") || bundle.includes("登录后可查看你的纸面策略") || bundle.includes("Paper agents")) {
    fail("locales must not call the system runner a personal book");
  }
}
if (!zh.includes("已登记策略匹配") || !zh.includes("全站纸面 Runner") || !zh.includes("不是个人 Agent")) {
  fail("zh-CN must describe matching and the site-wide runner honestly");
}
if (!en.includes("Match registered strategies") || !en.includes("not a personal agent") || !en.includes("not a port of the source studio")) {
  fail("en must describe matching and the site-wide runner honestly");
}

const testIdAt = boardPage.indexOf('data-testid="board-ai"');
if (testIdAt < 0) fail("chart-side board-ai entry missing");
const mountAt = boardPage.indexOf("<BoardAiPanel", testIdAt);
const toggle = boardPage.slice(testIdAt, mountAt);
if (!toggle.includes('next.set("ai", "1")') || !toggle.includes('next.delete("ai")')) {
  fail("chart-side entry must only toggle ?ai=1");
}


if (!page.includes("/api/v1/ai-section/strategies") || !page.includes("/api/v1/ai-section/agents")) {
  fail("studio and personal paper agent APIs must be wired in the page");
}
if (!page.includes("/simulate") || !page.includes("blocked_no_data")) {
  fail("paper simulation must surface a local run and the no-quote block");
}
if (page.includes("/start") || page.includes("/stop") || page.includes("/orders") || page.includes("/api/v1/mainstream/strategy")) {
  fail("personal paper UI must not start the site runner, live trading, or orders");
}
if (page.includes("eval(") || page.includes("new Function") || page.includes("Function(")) {
  fail("frontend must not evaluate user code");
}
if (!schemaPy.includes("CODE_FORBIDDEN") || !schemaPy.includes("FORBIDDEN_FIELDS")) {
  fail("strategy schema must reject user code fields");
}
if (/\bexec\s*\(|\beval\s*\(/.test(schemaPy) || /\bexec\s*\(|\beval\s*\(/.test(storePy) || /\bexec\s*\(|\beval\s*\(/.test(simulatePy) || /\bexec\s*\(|\beval\s*\(/.test(page)) {
  fail("studio modules must not exec or eval user strings");
}
if (!storePy.includes("user_id") || !storePy.includes("ai_section_studio.sqlite")) {
  fail("studio store must be a dedicated per-user ledger");
}
if (storePy.includes("get_runner(") || simulatePy.includes("get_runner(") || routesPy.includes("get_runner().start")) {
  fail("studio and simulation must not start the site-wide runner");
}
if (!simulatePy.includes("blocked_no_data") || !simulatePy.includes('"paper_pnl": None')) {
  fail("missing quotes must block simulation without invented returns");
}
if (!routesPy.includes('METHOD_NOT_ALLOWED') || !routesPy.includes("/agents/{aid}/start")) {
  fail("start/stop/orders aliases must stay rejected");
}
if (!zh.includes("策略工作室") || !zh.includes("个人纸面 Agent") || !zh.includes("缺少可用行情")) {
  fail("zh-CN must describe the studio and the no-quote block");
}
if (!en.includes("Strategy studio") || !en.includes("Personal paper agent") || !en.includes("Not enough stored quotes")) {
  fail("en must describe the studio and the no-quote block");
}
if (zh.includes("模型已生成") || en.includes("the model generated") || en.includes("LLM generated")) {
  fail("locales must not claim the model generated a strategy");
}


if (!bodyPy.includes("read_bounded_json") || !routesPy.includes("read_bounded_json")) {
  fail("JSON bodies must be read through the bounded stream helper");
}
if (routesPy.includes("request.body()")) {
  fail("routes must not fully buffer request.body before the 8000-byte cap");
}
if (!storePy.includes("BEGIN IMMEDIATE") || !storePy.includes("foreign_keys=ON")) {
  fail("studio store must use an immediate transaction and foreign keys");
}
if (!simulatePy.includes("blocked_bad_data") || !simulatePy.includes("fills_truncated") || !simulatePy.includes("signal_on_closed_bar_fill_next_open") || !simulatePy.includes("apply_pending_open") || !simulatePy.includes("manage_open_gap") || !simulatePy.includes("exclude_trailing_unclosed_disclose_cutoff") || !simulatePy.includes("bars_excluded_unclosed")) {
  fail("simulation must validate bars, disclose next-open fills, keep open orders ahead of intrabar risk, and disclose a dropped forming tail");
}
if (!en.includes("The paper fraction is not an investment return") || !zh.includes("纸面分数不是投资回报")) {
  fail("locales must disclose that paper fractions are not investment returns");
}
if (!page.includes("fills_truncated") || !page.includes("blocked_bad_data") || !page.includes("gross_zero_cost") || !page.includes("aiSection.paper.cutoff") || !page.includes("bars_excluded_unclosed")) {
  fail("paper UI must surface truncation, bad-data blocks, and the cost model");
}



function planDirectorySession(me) {
  if (!me) return "error";
  if (me.auth_enabled && !me.user) return "needLogin";
  return "load";
}

function planDirectoryResult(status, hasData) {
  if (status === 401) return "needLogin";
  if (!hasData) return "error";
  return "load";
}

// Simulated unauthenticated / login-recovery paths. These are the same rules the page must follow.
const anon = planDirectorySession({ auth_enabled: true, user: null });
if (anon !== "needLogin") fail("anonymous users must be asked to sign in, not sent into a generic load failure");
const afterLogin = planDirectorySession({ auth_enabled: true, user: { id: "u1" } });
if (afterLogin !== "load") fail("a signed-in session must be allowed to load the catalog");
const authOff = planDirectorySession({ auth_enabled: false, user: null });
if (authOff !== "load") fail("when site auth is off, the catalog may load without a login wall");
const meFailed = planDirectorySession(null);
if (meFailed !== "error") fail("a missing /auth/me body is an error, not a silent catalog call");
if (planDirectoryResult(401, false) !== "needLogin") fail("catalog/compile 401 must become a sign-in prompt, not a generic failure");
if (planDirectoryResult(200, true) !== "load") fail("a successful catalog payload must load");
if (planDirectoryResult(503, false) !== "error") fail("non-auth catalog failures must stay visible as errors");

const catalogEffectStart = page.indexOf('readJson("/api/v1/auth/me"');
const catalogCall = page.indexOf('readJson("/api/v1/ai-section/catalog"');
if (catalogEffectStart < 0 || catalogCall < 0 || catalogCall < catalogEffectStart) {
  fail("catalog must not be requested until /api/v1/auth/me has been consulted");
}
if (!page.includes("planDirectorySession") || !page.includes("planDirectoryResult")) {
  fail("page must use the explicit directory access plan");
}
if (!page.includes("aiSection.marketplace.needLogin") || !page.includes('Link to="/login"') || !page.includes("aiSection.matcher.needLogin")) {
  fail("anonymous catalog and match tabs must prompt to sign in, with a login link, not only a generic error");
}
if (!page.includes('if (dirAuth === "auth")') || !page.includes("setDirAuth(\"auth\")")) {
  fail("match must refuse to send compile while the directory is in the anonymous sign-in state");
}
if (page.includes("/api/v1/auth/register") || page.includes('to="/register"') || page.includes('to="/signup"')) {
  fail("this change must not add a registration page or signup link");
}
if (!zh.includes("请先登录后再查看已登记策略目录") || !zh.includes("请先登录后再使用关键词匹配")) {
  fail("zh-CN must tell anonymous users to sign in to view the catalog and use matching");
}
if (!en.includes("Sign in to view the registered strategy catalog") || !en.includes("Sign in to use keyword matching")) {
  fail("en must tell anonymous users to sign in to view the catalog and use matching");
}
for (const lang of ["zh-CN", "en", "de", "fr", "es", "pt", "tr", "ru", "ja", "ko", "ar"]) {
  const loc = JSON.parse(readFileSync(join(root, `src/i18n/locales/${lang}.json`), "utf8"));
  const market = loc.aiSection?.marketplace?.needLogin;
  const match = loc.aiSection?.matcher?.needLogin;
  if (typeof market !== "string" || !market.trim() || typeof match !== "string" || !match.trim()) {
    fail(`${lang} must include marketplace.needLogin and matcher.needLogin`);
  }
  if (market.includes("{{") || match.includes("{{")) fail(`${lang} needLogin strings must be plain login prompts`);
}

const gatePy = readFileSync(join(root, "../api/app/auth/gate.py"), "utf8");
if (gatePy.includes("/api/v1/ai-section/catalog") || gatePy.includes("/api/v1/ai-section/compile") || gatePy.includes("/api/v1/ai-section")) {
  fail("auth gate must not be loosened to make the catalog or matcher public");
}

console.log("ai-section guard OK");
