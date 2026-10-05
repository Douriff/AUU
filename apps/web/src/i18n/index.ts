/**
 * Interface languages (i18next + react-i18next).
 *
 * - Default / fallback: Simplified Chinese (zh-CN), bundled. Other languages are lazy chunks.
 * - Choice order: explicit choice in localStorage -> account (after login, see syncAccountLocale)
 *   -> browser languages -> zh-CN.
 * - Not translated on purpose: news headlines/summaries, coin tickers, the AUUTRADE brand.
 * - Numbers/dates follow the language (Arabic keeps Latin digits for prices); up/down colours
 *   are a separate preference (theme/colorPref.ts) and never change with the language.
 */
import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import zh from "./locales/zh-CN.json";

export const LANGS = [
  { code: "zh-CN", name: "简体中文", dir: "ltr" },
  { code: "en", name: "English", dir: "ltr" },
  { code: "de", name: "Deutsch", dir: "ltr" },
  { code: "fr", name: "Français", dir: "ltr" },
  { code: "es", name: "Español", dir: "ltr" },
  { code: "pt", name: "Português", dir: "ltr" },
  { code: "tr", name: "Türkçe", dir: "ltr" },
  { code: "ru", name: "Русский", dir: "ltr" },
  { code: "ja", name: "日本語", dir: "ltr" },
  { code: "ko", name: "한국어", dir: "ltr" },
  { code: "ar", name: "العربية", dir: "rtl" },
] as const;

export type Lang = (typeof LANGS)[number]["code"];
export const DEFAULT_LANG: Lang = "zh-CN";
const CODES = LANGS.map((l) => l.code) as readonly string[];

const KEY = "auu.lang";
const KEY_TS = "auu.lang.ts";

const loaders = import.meta.glob<{ default: Record<string, unknown> }>(["./locales/*.json", "!./locales/zh-CN.json"]);

/** Map any BCP-47 tag (e.g. "pt-BR", "zh-Hans-CN", "de-AT") onto a supported language, or null. */
export function matchLang(tag: string | null | undefined): Lang | null {
  if (!tag) return null;
  const t = tag.trim().toLowerCase().replace("_", "-");
  if (!t) return null;
  if (t.startsWith("zh")) {
    // Traditional Chinese readers still get Simplified (closest available).
    return "zh-CN";
  }
  const base = t.split("-")[0];
  const hit = CODES.find((c) => c.toLowerCase() === base);
  return (hit as Lang) ?? null;
}

export function storedLang(): { lang: Lang; ts: number } | null {
  try {
    const lang = matchLang(window.localStorage.getItem(KEY));
    if (!lang) return null;
    return { lang, ts: Number(window.localStorage.getItem(KEY_TS)) || 0 };
  } catch {
    return null;
  }
}

export function browserLang(): Lang {
  const list = typeof navigator !== "undefined" ? (navigator.languages?.length ? navigator.languages : [navigator.language]) : [];
  for (const tag of list) {
    const hit = matchLang(tag);
    if (hit) return hit;
  }
  return DEFAULT_LANG;
}

export function initialLang(): Lang {
  return storedLang()?.lang ?? browserLang();
}

export function langDir(lang: string): "ltr" | "rtl" {
  return LANGS.find((l) => l.code === lang)?.dir ?? "ltr";
}

async function ensureLoaded(lang: Lang): Promise<void> {
  if (lang === DEFAULT_LANG || i18n.hasResourceBundle(lang, "translation")) return;
  const load = loaders[`./locales/${lang}.json`];
  if (!load) return;
  const mod = await load();
  i18n.addResourceBundle(lang, "translation", mod.default, true, true);
}

function applyDocument(lang: Lang): void {
  const el = document.documentElement;
  el.lang = lang;
  el.dir = langDir(lang);
  el.classList.toggle("rtl", el.dir === "rtl");
}

export async function initI18n(): Promise<void> {
  const lang = initialLang();
  await i18n.use(initReactI18next).init({
    resources: { [DEFAULT_LANG]: { translation: zh } },
    lng: DEFAULT_LANG,
    fallbackLng: DEFAULT_LANG,
    supportedLngs: [...CODES],
    interpolation: { escapeValue: false },
    returnNull: false,
    react: { useSuspense: false },
  });
  try {
    await ensureLoaded(lang);
    await i18n.changeLanguage(lang);
  } catch {
    /* chunk failed to load: stay on Chinese */
  }
  applyDocument((i18n.language as Lang) || DEFAULT_LANG);
  i18n.on("languageChanged", (l) => applyDocument((matchLang(l) ?? DEFAULT_LANG) as Lang));
}

const listeners = new Set<(lang: Lang) => void>();

/**
 * Persist an explicit choice, switch the UI, and tell the account syncer (if logged in).
 * silent: store locally but do not push to the account (used when the value came from the account).
 */
export async function setLang(lang: Lang, opts: { explicit?: boolean; ts?: number; silent?: boolean } = {}): Promise<void> {
  const ts = opts.ts ?? Date.now();
  if (opts.explicit !== false) {
    try {
      window.localStorage.setItem(KEY, lang);
      window.localStorage.setItem(KEY_TS, String(ts));
    } catch {
      /* private mode: this tab only */
    }
  }
  await ensureLoaded(lang);
  await i18n.changeLanguage(lang);
  if (opts.explicit !== false && !opts.silent) listeners.forEach((fn) => fn(lang));
}

export function onExplicitLang(fn: (lang: Lang) => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

export function currentLang(): Lang {
  return (matchLang(i18n.language) ?? DEFAULT_LANG) as Lang;
}

/**
 * After login: the newer of (this device's explicit choice, the account's saved choice) wins.
 * Returns the locale that should be written back to the account, or null if nothing to push.
 */
export function reconcileAccountLocale(account: { locale?: string | null; locale_ts?: number | null } | null | undefined): {
  apply: Lang | null;
  push: Lang | null;
} {
  const local = storedLang();
  const acc = matchLang(account?.locale ?? null);
  const accTs = Number(account?.locale_ts) || 0;
  if (acc && (!local || accTs > local.ts)) {
    return { apply: acc, push: null };
  }
  if (local && local.lang !== acc) return { apply: null, push: local.lang };
  return { apply: null, push: null };
}

export default i18n;
