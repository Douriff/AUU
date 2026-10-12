import { useEffect, useRef } from "react";
import { createCyberBrainSplash, type CyberBrainHandle } from "./cyberBrainEngine";
import { isNarrowMobile } from "./splashPrefs";

/**
 * Custom cyber-brain WebGL splash. Parent unmounts after skip/end → dispose.
 */
export function SplashCanvas() {
  const hostRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = hostRef.current;
    if (!el) return;
    const mobile = isNarrowMobile();
    let handle: CyberBrainHandle | null = null;
    try {
      handle = createCyberBrainSplash({
        container: el,
        mobile,
        particleCount: mobile ? 4200 : 11000,
        text: "AUUTRADE",
      });
    } catch (err) {
      console.warn("[splash] WebGL init failed", err);
    }
    return () => {
      handle?.dispose();
      handle = null;
    };
  }, []);

  return <div ref={hostRef} className="auu-splash-canvas-host" style={{ width: "100%", height: "100%" }} />;
}

export default SplashCanvas;
