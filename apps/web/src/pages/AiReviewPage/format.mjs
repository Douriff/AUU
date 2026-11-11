/** Display helpers. Missing and non-finite values stay blank; they are not zero. */

export function reviewNumber(value, digits = 4, locale = "en") {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return new Intl.NumberFormat(locale, { maximumFractionDigits: digits, useGrouping: false }).format(value);
}

export function reviewPercent(value, locale = "en") {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return new Intl.NumberFormat(locale, {
    style: "percent",
    maximumFractionDigits: 2,
    signDisplay: "exceptZero",
  }).format(value);
}
