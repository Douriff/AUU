import { readFileSync } from "node:fs";

const page = readFileSync(new URL("../src/pages/AiReviewPage/AiReviewPage.tsx", import.meta.url), "utf8");
const api = readFileSync(new URL("../src/pages/AiReviewPage/api.ts", import.meta.url), "utf8");
const router = readFileSync(new URL("../src/app/router.tsx", import.meta.url), "utf8");
const banned = ["dangerouslySetInnerHTML", "localStorage", "HttpWsProvider", "marketProvider", "SUB_PREVIEW", "auu.sub.preview"];
for (const word of banned) {
  if (page.includes(word) || api.includes(word)) {
    console.error("lab page references " + word);
    process.exit(1);
  }
}
if (!router.includes('path: "/lab/ai-review"') || !router.includes("lazy(")) {
  console.error("isolated lazy route missing");
  process.exit(1);
}
if (router.includes('to: "/lab/ai-review"')) {
  console.error("lab route was added to navigation");
  process.exit(1);
}
if (!api.includes("/api/v1/ai-review/interpret") || !page.includes("askNote(")) {
  console.error("click-to-call path missing");
  process.exit(1);
}
if (page.includes("gpt-4o-mini")) {
  console.error("page hardcodes a model");
  process.exit(1);
}
const pull = page.slice(page.indexOf("const pull"), page.indexOf("const tick"));
if (pull.includes("askNote")) {
  console.error("plaza refresh calls the model");
  process.exit(1);
}
const board = readFileSync(new URL("../src/pages/MainstreamPage/BoardAiPanel.tsx", import.meta.url), "utf8");
const shell = readFileSync(new URL("../src/components/layout/AppShell.tsx", import.meta.url), "utf8");
const effect = board.slice(board.indexOf("useEffect("), board.indexOf("async function ask"));
if (effect.includes("askNote")) {
  console.error("opening the board panel calls the model");
  process.exit(1);
}
if (!board.includes("onClick={() => void ask(row)}")) {
  console.error("board note is not behind an explicit click");
  process.exit(1);
}
if (!shell.includes('label: "ai"')) {
  console.error("main nav has no AI entry");
  process.exit(1);
}
console.log("ai-review guard OK");
