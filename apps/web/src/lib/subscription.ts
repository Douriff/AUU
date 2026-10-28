/** Display-only subscription preview. No billing, no server entitlement. */

export const SUB_PREVIEW_KEY = "auu.sub.preview";

export type SubPreview = "on" | "off" | null;

const PREMIUM = new Set([
  "/",
  "/mainstream",
  "/majors",
  "/pnl",
  "/console",
  "/performance",
  "/leaderboard",
  "/markets",
  "/market",
  "/board",
  "/strategy",
  "/review",
  "/positions",
  "/observe",
  "/alerts",
  "/backtest",
  "/watch",
]);

/** Market quotes and starred/premium desk routes (not news, settings, or account pages). */
export function isPremiumPath(pathname: string): boolean {
  if (PREMIUM.has(pathname)) return true;
  return pathname.startsWith("/trade") || pathname.startsWith("/watch/") || pathname.startsWith("/market/");
}

export function readSubPreview(): SubPreview {
  try {
    const v = window.localStorage.getItem(SUB_PREVIEW_KEY);
    if (v === "1" || v === "on") return "on";
    if (v === "0" || v === "off") return "off";
  } catch {
    /* private mode */
  }
  return null;
}

export function writeSubPreview(next: "on" | "off"): void {
  try {
    window.localStorage.setItem(SUB_PREVIEW_KEY, next === "on" ? "1" : "0");
  } catch {
    /* ignore */
  }
  window.dispatchEvent(new Event("auu-sub-preview"));
}

/**
 * Subscribed desk preview.
 * Explicit local flag wins (`auu.sub.preview` = 1 or 0). Otherwise admins see the unlocked desk
 * so the owner is not locked out; everyone else stays locked until a real subscription exists.
 */
export function subscribedPreview(isAdmin: boolean, flag: SubPreview = readSubPreview()): boolean {
  if (flag === "on") return true;
  if (flag === "off") return false;
  return isAdmin;
}
