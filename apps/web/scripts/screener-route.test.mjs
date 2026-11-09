// The spot screener is a new route only. Existing nav targets and premium paths stay.
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(join(root, path), "utf8");
const fail = (msg) => {
  console.error(msg);
  process.exit(1);
};

const router = read("src/app/router.tsx");
const shell = read("src/components/layout/AppShell.tsx");
const premium = read("src/lib/subscription.ts");
if (!router.includes('path: "screener"')) fail("screener route missing");
for (const path of ["mainstream", "majors", "news", "pnl", "console", "settings", "subscribe"]) {
  if (!router.includes(`path: "${path}"`)) fail(`old route missing: ${path}`);
}
const nav = shell.split("const mainstreamNav")[1].split("const mobileTabs")[0];
if (nav.includes("screener")) fail("screener was added to the main nav");
for (const path of ["/", "/pnl", "/console", "/performance", "/majors", "/news", "/airdrops", "/exchange"]) {
  const needle = path === "/" ? 'to: "/"' : `to: "${path}"`;
  if (!nav.includes(needle)) fail(`nav target missing: ${path}`);
}
if (!nav.includes('to: "/?symbol=BTC"')) fail("trade link changed");
const block = premium.split("const PREMIUM")[1].split(");")[0];
for (const path of ["/", "/mainstream", "/majors", "/pnl", "/news"]) {
  if (path === "/news") {
    if (block.includes('"/news"')) fail("news became premium");
  } else if (!block.includes(`"${path}"`)) fail(`premium path missing: ${path}`);
}
if (!block.includes('"/screener"')) fail("screener is not behind the existing subscription gate");
for (const path of ['"/settings"', '"/login"', '"/subscribe"', '"/airdrops"']) {
  if (block.includes(path)) fail(`${path} became premium`);
}
const pageDir = join(root, "src/pages/ScreenerPage");
const page = readdirSync(pageDir)
  .map((name) => readFileSync(join(pageDir, name), "utf8"))
  .join("\n")
  .toLowerCase();
for (const banned of ["dangerouslysetinnerhtml", "innerhtml", "localstorage", "websocket", "/api/v1/ws", "ema50", "crossed"]) {
  if (page.includes(banned)) fail(`screener page contains ${banned}`);
}
console.log("screener route check OK");
