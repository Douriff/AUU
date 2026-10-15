/** Tiny pub/sub that lets several charts show the hovered bar time of one another (synced crosshair). */
export type BusFn = (src: string, ts: number | null) => void;
export type CrosshairBus = { emit: BusFn; on: (fn: BusFn) => () => void };

export function createCrosshairBus(): CrosshairBus {
  const subs = new Set<BusFn>();
  return {
    emit: (src, ts) => subs.forEach((f) => f(src, ts)),
    on: (fn) => {
      subs.add(fn);
      return () => void subs.delete(fn);
    },
  };
}
