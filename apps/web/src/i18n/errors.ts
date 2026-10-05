/** Turn an API / network error into text in the current interface language. */
import i18n from "i18next";
import { ApiError } from "@/providers/HttpWsProvider";

/**
 * Chinese UI: the server's own (Chinese) message, unchanged from before i18n.
 * Other languages: apiErr.<key> (finer reason, e.g. tpAbove) -> apiErr.<code> -> the caller's fallback key.
 */
export function errText(e: unknown, fallbackKey = "common.requestFailed"): string {
  if (e instanceof ApiError) {
    if (i18n.language === "zh-CN" && e.message && e.message !== "request failed") return e.message;
    const params = { ...(e.params ?? {}), wait: e.retryAfter ?? (e.params as { wait?: number } | undefined)?.wait };
    if (e.key && i18n.exists(`apiErr.${e.key}`)) return i18n.t(`apiErr.${e.key}`, params);
    if (e.code === "EMAIL_THROTTLE" && e.retryAfter) return i18n.t("apiErr.EMAIL_THROTTLE_WAIT", params);
    if (i18n.exists(`apiErr.${e.code}`)) return i18n.t(`apiErr.${e.code}`, params);
    return i18n.t(fallbackKey);
  }
  if (e instanceof TypeError) return i18n.t("common.networkError");
  if (e instanceof Error && e.message && i18n.language === "zh-CN") return e.message;
  return i18n.t(fallbackKey);
}
