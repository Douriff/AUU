// Mirrors apps/api/app/auth/accounts.py. The server stays the source of truth.
import i18n from "i18next";

export const nameRule = () => i18n.t("auth.rule.name");
export const passwordRule = () => i18n.t("auth.rule.password");
export const displayRule = () => i18n.t("auth.rule.display");
const emailRule = () => i18n.t("auth.rule.email");
const codeRule = () => i18n.t("auth.rule.code");
const startRule = () => i18n.t("auth.rule.start");

const NAME = /^[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_.@\-\u4e00-\u9fff]{1,31}$/;
const DISPLAY = /^[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_ \u4e00-\u9fff]{0,23}$/;
const EMAIL = /^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+$/;
const LETTER = /\p{L}/u;
const DIGIT = /[0-9]/;

export function nameProblem(name: string): string | null {
  return NAME.test(name.trim()) ? null : nameRule();
}

export function passwordProblem(password: string): string | null {
  const chars = Array.from(password).length;
  const bytes = new TextEncoder().encode(password).length;
  if (chars < 8 || bytes > 72) return passwordRule();
  if (!LETTER.test(password) || !DIGIT.test(password)) return passwordRule();
  return null;
}

export function emailProblem(email: string): string | null {
  const text = email.trim();
  if (!text) return i18n.t("auth.rule.emailRequired");
  return text.length <= 254 && EMAIL.test(text) ? null : emailRule();
}

export function codeProblem(code: string): string | null {
  return /^\d{6}$/.test(code.trim()) ? null : codeRule();
}

export function displayProblem(display: string): string | null {
  const shown = display.trim();
  return !shown || DISPLAY.test(shown) ? null : displayRule();
}

// Empty means "use the server default"; the old code sent Number("") === 0 and got BAD_START.
export function parseStartSol(raw: string): { value?: number; problem: string | null } {
  const text = raw.trim();
  if (!text) return { problem: null };
  const value = Number(text);
  if (!Number.isFinite(value) || value < 1 || value > 100000) return { problem: startRule() };
  return { value, problem: null };
}
