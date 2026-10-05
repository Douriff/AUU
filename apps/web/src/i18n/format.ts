/** Locale-aware number / date helpers. Prices keep Latin digits in every language. */
import i18n from "i18next";

const DEFAULT = "zh-CN";

/** Intl locale for the current UI language ("ar" -> Latin digits so prices stay readable). */
export function intlLocale(): string {
  const l = i18n.language || DEFAULT;
  if (l === "ar") return "ar-u-nu-latn";
  return l;
}

const nfCache = new Map<string, Intl.NumberFormat>();
function numberFormat(opts: Intl.NumberFormatOptions): Intl.NumberFormat {
  const loc = intlLocale();
  const key = loc + JSON.stringify(opts);
  let f = nfCache.get(key);
  if (!f) {
    f = new Intl.NumberFormat(loc, opts);
    nfCache.set(key, f);
  }
  return f;
}

/** Fixed decimals (min = max = d). */
export function fmtFixed(n: number, d: number): string {
  return numberFormat({ minimumFractionDigits: d, maximumFractionDigits: d }).format(n);
}

/** Up to d decimals. */
export function fmtMax(n: number, d: number): string {
  return numberFormat({ maximumFractionDigits: d }).format(n);
}

export function fmtNum(n: number, opts: Intl.NumberFormatOptions = {}): string {
  return numberFormat(opts).format(n);
}

/** Compact (1.2K / 1,2 Mio. / 1.2万 …) */
export function fmtCompact(n: number, d = 2): string {
  return numberFormat({ notation: "compact", maximumFractionDigits: d }).format(n);
}

const dfCache = new Map<string, Intl.DateTimeFormat>();
/** Date/time formatter for the current language; pass timeZone explicitly when it matters. */
export function dateFmt(opts: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const loc = intlLocale();
  const key = loc + JSON.stringify(opts);
  let f = dfCache.get(key);
  if (!f) {
    f = new Intl.DateTimeFormat(loc, opts);
    dfCache.set(key, f);
  }
  return f;
}

export function fmtDate(ms: number | Date, opts: Intl.DateTimeFormatOptions): string {
  return dateFmt(opts).format(typeof ms === "number" ? new Date(ms) : ms);
}

/** Beijing-time short stamp "MM/DD HH:mm" in the current language. */
export function fmtBjShort(ms: number): string {
  return fmtDate(ms, { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}

/** Relative time ("5 minutes ago") in the current language. */
export function fmtRelative(ms: number, now = Date.now()): string {
  const rtf = new Intl.RelativeTimeFormat(intlLocale(), { numeric: "auto" });
  const s = Math.round((ms - now) / 1000);
  const a = Math.abs(s);
  if (a < 60) return rtf.format(s, "second");
  if (a < 3600) return rtf.format(Math.round(s / 60), "minute");
  if (a < 86400) return rtf.format(Math.round(s / 3600), "hour");
  return rtf.format(Math.round(s / 86400), "day");
}

/** Decimal separator of the current language ("." or ","). */
export function decimalSep(): string {
  const part = numberFormat({ minimumFractionDigits: 1 }).formatToParts(1.5).find((p) => p.type === "decimal");
  return part?.value ?? ".";
}

/** Signed percent from a ratio: 0.0123 -> "+1.23%" (locale decimal separator). */
export function fmtSignedPct(ratio: number, d = 2): string {
  return `${ratio > 0 ? "+" : ""}${fmtFixed(ratio * 100, d)}%`;
}

/** Percent from a ratio without sign: 0.0123 -> "1.23%". */
export function fmtPctPlain(ratio: number, d = 2): string {
  return `${fmtFixed(ratio * 100, d)}%`;
}

/** Large amounts with K / M / B suffixes (exchange style, same in every language). */
export function fmtKMB(n: number): string {
  const a = Math.abs(n);
  if (a >= 1e9) return `${fmtFixed(n / 1e9, 2)}B`;
  if (a >= 1e6) return `${fmtFixed(n / 1e6, 2)}M`;
  if (a >= 1e3) return `${fmtFixed(n / 1e3, 1)}K`;
  return fmtFixed(n, 0);
}

/** Join a short list with the language's list separator ("A, B and C" style without the conjunction). */
export function listJoin(items: string[]): string {
  try {
    // Intl.ListFormat is ES2021; the project's lib target is older, so reach it untyped.
    const LF = (Intl as unknown as { ListFormat?: new (l: string, o: object) => { format(x: string[]): string } }).ListFormat;
    if (LF) return new LF(intlLocale(), { style: "narrow", type: "unit" }).format(items);
    return items.join(", ");
  } catch {
    return items.join(", ");
  }
}
