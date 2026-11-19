import { readFileSync } from "node:fs";

const sheetTs = readFileSync(new URL("../src/pages/MainstreamPage/boardAiSheet.ts", import.meta.url), "utf8");
const panel = readFileSync(new URL("../src/pages/MainstreamPage/BoardAiPanel.tsx", import.meta.url), "utf8");
const css = readFileSync(new URL("../src/pages/MainstreamPage/boardAi.css", import.meta.url), "utf8");
const page = readFileSync(new URL("../src/pages/MainstreamPage/MainstreamPage.tsx", import.meta.url), "utf8");

function fail(msg) {
  console.error(msg);
  process.exit(1);
}

function stripExportTypes(ts) {
  let out = "";
  let i = 0;
  while (i < ts.length) {
    const next = ts.indexOf("export type ", i);
    if (next < 0) {
      out += ts.slice(i);
      break;
    }
    out += ts.slice(i, next);
    const eq = ts.indexOf("=", next);
    const open = ts.indexOf("{", eq);
    if (open < 0) fail("export type block is missing a body");
    let depth = 0;
    let j = open;
    for (; j < ts.length; j++) {
      if (ts[j] === "{") depth++;
      else if (ts[j] === "}") {
        depth--;
        if (depth === 0) {
          j++;
          break;
        }
      }
    }
    if (ts[j] === ";") j++;
    if (ts[j] === "\n") j++;
    i = j;
  }
  return out;
}

function loadSheetModule(ts) {
  let js = ts.replace(/\/\*[\s\S]*?\*\//g, "");
  js = stripExportTypes(js);
  js = js.replace(/^export /gm, "");
  js = js.replace(/: PageScrollLockHost/g, "");
  js = js.replace(/: ScrollLockWindow/g, "");
  if (/\bexport type\b|\binterface\b|: PageScrollLockHost|: ScrollLockWindow/.test(js)) fail("boardAiSheet.ts type strip left residual types:\n" + js);
  return new Function(`${js}\nreturn { BOARD_AI_SHEET_MQ, createPageScrollLock, bindPageScrollLockHost };`)();
}

const { BOARD_AI_SHEET_MQ, createPageScrollLock, bindPageScrollLockHost } = loadSheetModule(sheetTs);
if (BOARD_AI_SHEET_MQ !== "(max-width: 900px)") fail("sheet media query must stay max-width 900px");

function makeHost({ sheet, y = 0, overflow = "" }) {
  const state = { sheet, y, overflow };
  return {
    state,
    host: {
      matchSheet: () => state.sheet,
      getScrollY: () => state.y,
      setScrollY: (next) => {
        state.y = next;
      },
      getOverflow: () => state.overflow,
      setOverflow: (value) => {
        state.overflow = value;
      },
    },
  };
}

{
  const { state, host } = makeHost({ sheet: true, y: 840, overflow: "" });
  const lock = createPageScrollLock(host);
  lock.apply();
  if (state.overflow !== "hidden") fail("mobile open must lock body overflow");
  state.y = 12;
  lock.release();
  if (state.overflow !== "") fail("close must restore prior overflow");
  if (state.y !== 840) fail("close on mobile must restore the saved board scroll");
}

{
  const { state, host } = makeHost({ sheet: false, y: 200, overflow: "" });
  const lock = createPageScrollLock(host);
  lock.apply();
  if (state.overflow !== "") fail("desktop open must not lock body overflow");
  state.y = 400;
  lock.release();
  if (state.y !== 400) fail("desktop close must not force the page scroll back");
}

{
  const { state, host } = makeHost({ sheet: true, y: 640, overflow: "auto" });
  const lock = createPageScrollLock(host);
  lock.apply();
  state.sheet = false;
  lock.apply();
  if (state.overflow !== "auto") fail("resizing to desktop must unlock overflow");
  state.y = 0;
  lock.release();
  if (state.overflow !== "auto") fail("release must restore the original overflow");
  if (state.y !== 640) fail("close after a mobile open must still restore the saved board scroll");
}

{
  const win = {
    matchMedia: (q) => {
      if (q !== BOARD_AI_SHEET_MQ) fail("host must use BOARD_AI_SHEET_MQ");
      return { matches: true };
    },
    scrollY: 333,
    scrollTo(x, y) {
      if (x !== 0) fail("scroll restore must keep x=0");
      win.scrollY = y;
    },
    document: { body: { style: { overflow: "" } } },
  };
  const lock = createPageScrollLock(bindPageScrollLockHost(win));
  lock.apply();
  if (win.document.body.style.overflow !== "hidden") fail("window host must set body overflow hidden");
  win.scrollY = 1;
  lock.release();
  if (win.scrollY !== 333) fail("window host must restore scrollY");
  if (win.document.body.style.overflow !== "") fail("window host must restore overflow");
}

function mediaBlocks(src, head) {
  const blocks = [];
  const needle = `@media ${head}`;
  let from = 0;
  while (true) {
    const i = src.indexOf(needle, from);
    if (i < 0) break;
    const open = src.indexOf("{", i);
    let depth = 0;
    let j = open;
    for (; j < src.length; j++) {
      if (src[j] === "{") depth++;
      else if (src[j] === "}") {
        depth--;
        if (depth === 0) {
          j++;
          break;
        }
      }
    }
    blocks.push(src.slice(i, j));
    from = j;
  }
  return blocks;
}

const desktop = mediaBlocks(css, "(min-width: 901px)").join("\n");
if (!/position:\s*absolute/.test(desktop) || !/top:\s*0/.test(desktop) || !/right:\s*0/.test(desktop) || !/width:\s*320px/.test(desktop)) {
  fail("desktop ≥901px panel must stay a 320px right dock");
}
if (/position:\s*fixed/.test(desktop)) fail("desktop panel must not become a fixed overlay");

if (!/\.board-ai-layer \{ display: contents; \}/.test(css)) {
  fail("desktop layer must use display:contents so the dock stays in the trade grid");
}

const mobile = mediaBlocks(css, "(max-width: 900px)").join("\n");
if (!/\.board-ai-layer[\s\S]*?position:\s*fixed/.test(mobile)) fail("≤900px layer must be position:fixed");
if (!/top:\s*calc\(\s*var\(--nav-h\)\s*\+\s*env\(safe-area-inset-top/.test(mobile)) {
  fail("sheet must clear the top nav and top safe area");
}
if (!/\.board-ai header button[\s\S]*?min-width:\s*44px/.test(mobile) || !/\.board-ai header button[\s\S]*?min-height:\s*44px/.test(mobile)) {
  fail("mobile close control must stay at least 44px");
}

const tab = mediaBlocks(css, "(max-width: 760px)").join("\n");
if (!/bottom:\s*calc\(\s*var\(--tab-h\)\s*\+\s*env\(safe-area-inset-bottom/.test(tab)) {
  fail("phone sheet must sit above the tab bar and bottom safe area");
}
if (!/@media \(max-width: 900px\) and \(prefers-reduced-motion: no-preference\)/.test(css)) {
  fail("sheet motion must be gated on prefers-reduced-motion");
}

const effects = panel.slice(panel.indexOf("useEffect("), panel.indexOf("async function ask"));
if (effects.includes("askNote")) fail("opening the board panel calls the model");
if (!panel.includes("onClick={() => void ask(row)}")) fail("board note is not behind an explicit click");
if (!panel.includes("createPageScrollLock") || !panel.includes("lock.release()")) {
  fail("panel must lock and release page scroll through the helper");
}
if (!panel.includes("closeRef.current?.focus()") || !panel.includes('[data-testid="board-ai"]')) {
  fail("open must focus close; close must restore the board entry when present");
}
if (!panel.includes('e.key === "Escape"')) fail("sheet must close on Escape");
if (panel.includes("/lab") || page.includes('navigate("/lab') || page.includes('to: "/lab')) {
  fail("board AI must not navigate to /lab");
}

const testIdAt = page.indexOf('data-testid="board-ai"');
if (testIdAt < 0) fail("board-ai entry missing");
const mountAt = page.indexOf("<BoardAiPanel", testIdAt);
if (mountAt < 0) fail("BoardAiPanel mount missing");
const toggle = page.slice(testIdAt, mountAt);
if (!toggle.includes('next.set("ai", "1")') || !toggle.includes('next.delete("ai")')) {
  fail("chart-side entry must only toggle ?ai=1");
}
if (toggle.includes("askNote") || toggle.includes("ask(")) fail("chart-side entry must not call the model");

console.log("board-ai-sheet guard OK");
