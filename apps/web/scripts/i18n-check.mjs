#!/usr/bin/env node
// i18n parity check: every locale must have exactly the zh-CN keys, the same {{placeholders}}
// and <tags>, no empty values, and (except ja/ko which share some CJK) no Chinese left over.
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const dir = join(dirname(fileURLToPath(import.meta.url)), "..", "src", "i18n", "locales");
const EXPECTED = ["zh-CN", "en", "de", "fr", "es", "pt", "tr", "ru", "ja", "ko", "ar"];

function flat(obj, pre = "", out = {}) {
  for (const [k, v] of Object.entries(obj)) {
    const key = pre ? `${pre}.${k}` : k;
    if (v && typeof v === "object") flat(v, key, out);
    else out[key] = String(v);
  }
  return out;
}
const sig = (s) => [...(s.match(/\{\{\s*\w+\s*\}\}|<\/?\w+>/g) ?? [])].map((x) => x.replace(/\s/g, "")).sort().join("|");
const HAN = /[\u3400-\u9fff]/;
const KANA_HANGUL = /[\u3040-\u30ff\uac00-\ud7af]/;

const files = readdirSync(dir).filter((f) => f.endsWith(".json")).map((f) => f.slice(0, -5));
const errors = [];
for (const want of EXPECTED) if (!files.includes(want)) errors.push(`missing locale file ${want}.json`);
const zh = flat(JSON.parse(readFileSync(join(dir, "zh-CN.json"), "utf8")));
const zhKeys = Object.keys(zh);
for (const lang of files) {
  if (lang === "zh-CN") continue;
  const m = flat(JSON.parse(readFileSync(join(dir, `${lang}.json`), "utf8")));
  const missing = zhKeys.filter((k) => !(k in m));
  const extra = Object.keys(m).filter((k) => !(k in zh));
  if (missing.length) errors.push(`${lang}: ${missing.length} missing keys, e.g. ${missing.slice(0, 5).join(", ")}`);
  if (extra.length) errors.push(`${lang}: ${extra.length} extra keys, e.g. ${extra.slice(0, 5).join(", ")}`);
  let same = 0;
  for (const k of zhKeys) {
    if (!(k in m)) continue;
    const v = m[k];
    if (!v.trim()) errors.push(`${lang}: empty value ${k}`);
    if (sig(v) !== sig(zh[k])) errors.push(`${lang}: placeholder/tag mismatch at ${k}`);
    if (lang !== "ja" && lang !== "ko" && HAN.test(v)) errors.push(`${lang}: Chinese characters left in ${k}`);
    if (HAN.test(zh[k]) && v === zh[k]) same++;
  }
  // Japanese legitimately shares some words with Chinese (正常, 数量, 年 …); a large share means untranslated copies.
  if ((lang === "ja" || lang === "ko") && same > zhKeys.length * 0.05) errors.push(`${lang}: ${same} values identical to zh-CN (untranslated?)`);
  if (lang === "ja" || lang === "ko") {
    const native = Object.values(m).filter((v) => KANA_HANGUL.test(v)).length;
    if (native < zhKeys.length * 0.5) errors.push(`${lang}: only ${native} values contain kana/hangul`);
  }
  console.log(`${lang.padEnd(5)} ${Object.keys(m).length} keys${same ? `, ${same} same-as-zh` : ""}`);
}
console.log(`zh-CN ${zhKeys.length} keys (reference)`);
if (errors.length) {
  console.error(errors.join("\n"));
  process.exit(1);
}
console.log("i18n check OK");
