/** Translated Go/No-Go line (server message is Chinese; rebuilt from verdict + reason ids). */
import i18n from "i18next";
import type { StrategyGoNoGo } from "@/types/mainstream";

export function goMessage(g: StrategyGoNoGo): string {
  if (g.verdict === "pending") return i18n.t("go.pending", { n: g.days, min: g.minDays });
  if (g.verdict === "go") return i18n.t("go.pass");
  const reasons = g.reasons;
  if (!reasons) return i18n.language === "zh-CN" ? g.message : i18n.t("go.failGeneric");
  const parts = reasons.map((r) => (i18n.exists(`go.reason.${r}`) ? i18n.t(`go.reason.${r}`) : r));
  return i18n.t("go.fail", { why: parts.join(i18n.t("go.sep")) });
}

export function goVerdict(g: StrategyGoNoGo): string {
  return g.verdict === "go" ? "Go" : g.verdict === "no-go" ? "No-Go" : i18n.t("go.pendingShort");
}
