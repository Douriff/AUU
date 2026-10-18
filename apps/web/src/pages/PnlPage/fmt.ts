import { fmtNum } from "@/i18n/format";

export const money = (v: number) => fmtNum(v, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
export const signed = (v: number) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${money(Math.abs(v))}`;
