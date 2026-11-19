/** Mobile (≤900px) board-AI sheet helpers. Desktop ≥901px stays an in-grid dock. */

export const BOARD_AI_SHEET_MQ = "(max-width: 900px)";

export type PageScrollLockHost = {
  matchSheet: () => boolean;
  getScrollY: () => number;
  setScrollY: (y: number) => void;
  getOverflow: () => string;
  setOverflow: (value: string) => void;
};

export type ScrollLockWindow = {
  matchMedia: (query: string) => { matches: boolean };
  scrollY: number;
  scrollTo: (x: number, y: number) => void;
  document: { body: { style: { overflow: string } } };
};

/** Save page scroll and hide overflow while the mobile sheet is open. */
export function createPageScrollLock(host: PageScrollLockHost) {
  const y = host.getScrollY();
  const overflow = host.getOverflow();
  let locked = false;
  return {
    apply() {
      if (host.matchSheet()) {
        host.setOverflow("hidden");
        locked = true;
      } else {
        host.setOverflow(overflow);
      }
    },
    release() {
      host.setOverflow(overflow);
      if (locked) host.setScrollY(y);
    },
  };
}

export function bindPageScrollLockHost(win: ScrollLockWindow): PageScrollLockHost {
  return {
    matchSheet: () => win.matchMedia(BOARD_AI_SHEET_MQ).matches,
    getScrollY: () => win.scrollY,
    setScrollY: (y) => win.scrollTo(0, y),
    getOverflow: () => win.document.body.style.overflow,
    setOverflow: (value) => {
      win.document.body.style.overflow = value;
    },
  };
}
