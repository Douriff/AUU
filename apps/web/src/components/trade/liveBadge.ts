import type { ExchangeAvailability } from "@/providers/HttpWsProvider";

/** Paper panel live badge. Display only: paper orders never touch an exchange; order endpoints enforce the same rules. */
export type LiveBadgeState = "live" | "bind" | "off";

export function liveBadgeState(a: ExchangeAvailability | null | undefined): LiveBadgeState {
  if (!a || !a.enabled || a.killSwitch) return "off";
  const rows = Object.values(a.exchanges ?? {});
  if (rows.some((r) => r && r.bound && (r.spot?.open?.available || r.swap?.open?.available))) return "live";
  return "bind";
}

export const LIVE_BADGE_HREF: Record<LiveBadgeState, string | null> = { live: "/exchange", bind: "/exchange#bind", off: null };
