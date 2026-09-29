// Mirrors apps/api/app/auth/accounts.py. The server stays the source of truth.
export const NAME_RULE = "用户名需为 2–32 位，可用字母、数字、中文和 _ . @ -，须以字母、数字、中文或 _ 开头";
export const PASSWORD_RULE = "密码至少 8 位，必须同时包含字母和数字，可以包含特殊字符（不超过 72 字节）";
export const DISPLAY_RULE = "显示名需为 1–24 位字母、数字、空格或中文";
export const EMAIL_RULE = "邮箱格式不正确";
export const CODE_RULE = "请填写 6 位数字验证码";
export const START_RULE = "起始 SOL 需在 1 到 100000";

const NAME = /^[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_.@\-\u4e00-\u9fff]{1,31}$/;
const DISPLAY = /^[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_ \u4e00-\u9fff]{0,23}$/;
const EMAIL = /^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+$/;
const LETTER = /\p{L}/u;
const DIGIT = /[0-9]/;

export function nameProblem(name: string): string | null {
  return NAME.test(name.trim()) ? null : NAME_RULE;
}

export function passwordProblem(password: string): string | null {
  const chars = Array.from(password).length;
  const bytes = new TextEncoder().encode(password).length;
  if (chars < 8 || bytes > 72) return PASSWORD_RULE;
  if (!LETTER.test(password) || !DIGIT.test(password)) return PASSWORD_RULE;
  return null;
}

export function emailProblem(email: string): string | null {
  const text = email.trim();
  if (!text) return "请填写邮箱";
  return text.length <= 254 && EMAIL.test(text) ? null : EMAIL_RULE;
}

export function codeProblem(code: string): string | null {
  return /^\d{6}$/.test(code.trim()) ? null : CODE_RULE;
}

export function displayProblem(display: string): string | null {
  const shown = display.trim();
  return !shown || DISPLAY.test(shown) ? null : DISPLAY_RULE;
}

// Empty means "use the server default"; the old code sent Number("") === 0 and got BAD_START.
export function parseStartSol(raw: string): { value?: number; problem: string | null } {
  const text = raw.trim();
  if (!text) return { problem: null };
  const value = Number(text);
  if (!Number.isFinite(value) || value < 1 || value > 100000) return { problem: START_RULE };
  return { value, problem: null };
}
