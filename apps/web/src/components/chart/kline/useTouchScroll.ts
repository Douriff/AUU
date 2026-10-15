import { useEffect, useRef, type RefObject } from "react";

/**
 * KLineChart claims every touch drag on the canvas (preventDefault on touchmove), so a vertical swipe
 * over the chart could never scroll the page on phones. This hands vertical-dominant one-finger drags
 * back to the browser while keeping horizontal pan / pinch / drawing with the chart.
 * While `locked` (a drawing tool is active or a drawing is selected) the chart keeps every gesture.
 */
export function useTouchScroll(host: RefObject<HTMLElement>, locked: boolean): void {
  const lockedRef = useRef(locked);
  lockedRef.current = locked;

  useEffect(() => {
    const el = host.current;
    if (!el) return;
    let start: { x: number; y: number } | null = null;
    let mode: "undecided" | "page" | "chart" = "undecided";
    let synthetic = false;

    const onStart = (e: TouchEvent) => {
      if (e.touches.length !== 1) {
        mode = "chart"; // pinch etc.
        return;
      }
      start = { x: e.touches[0].clientX, y: e.touches[0].clientY };
      mode = lockedRef.current ? "chart" : "undecided";
    };
    const onMove = (e: TouchEvent) => {
      if (synthetic || !start || e.touches.length !== 1) return;
      if (mode === "page") return void e.stopPropagation();
      if (mode !== "undecided") return;
      const dx = e.touches[0].clientX - start.x;
      const dy = e.touches[0].clientY - start.y;
      if (Math.abs(dx) < 6 && Math.abs(dy) < 6) return;
      if (Math.abs(dy) > Math.abs(dx)) {
        mode = "page";
        e.stopPropagation(); // the chart never sees this drag ...
        // ... and forget the touch it already saw start, so releasing is not read as a tap
        const cancel = new Event("touchcancel", { bubbles: true, cancelable: false });
        Object.defineProperty(cancel, "changedTouches", { value: [] });
        synthetic = true;
        el.dispatchEvent(cancel);
        synthetic = false;
      } else mode = "chart";
    };
    const onEnd = () => {
      start = null;
      mode = "undecided";
    };
    const onCancel = (e: Event) => {
      if (synthetic) return;
      onEnd();
      void e;
    };
    el.addEventListener("touchstart", onStart, { capture: true, passive: true });
    el.addEventListener("touchmove", onMove, { capture: true, passive: true });
    el.addEventListener("touchend", onEnd, { capture: true, passive: true });
    el.addEventListener("touchcancel", onCancel, { capture: true, passive: true });
    return () => {
      el.removeEventListener("touchstart", onStart, { capture: true });
      el.removeEventListener("touchmove", onMove, { capture: true });
      el.removeEventListener("touchend", onEnd, { capture: true });
      el.removeEventListener("touchcancel", onCancel, { capture: true });
    };
  }, [host]);
}
