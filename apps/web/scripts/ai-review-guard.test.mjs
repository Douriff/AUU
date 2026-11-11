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
console.log("ai-review guard OK");
