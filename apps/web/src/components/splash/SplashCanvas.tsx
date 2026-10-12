import { useEffect } from "react";
import { ShapeParticleText } from "3d-shape-particle-text";
import { isNarrowMobile } from "./splashPrefs";

/** Amber accent aligned with pro --accent (#f0a531). Lazy-loaded chunk only. */
const PRIMARY = { r: 0.941, g: 0.647, b: 0.192 };
const SECONDARY = { r: 0.965, g: 0.78, b: 0.42 };

/** Cap devicePixelRatio for the splash lifetime (package uses dpr up to 2–3). */
function useSplashDprCap(cap: number) {
  useEffect(() => {
    const desc = Object.getOwnPropertyDescriptor(window, "devicePixelRatio");
    const native =
      typeof desc?.get === "function"
        ? () => desc.get!.call(window) as number
        : () => (window as Window & { devicePixelRatio: number }).devicePixelRatio;
    try {
      Object.defineProperty(window, "devicePixelRatio", {
        configurable: true,
        get: () => Math.min(Number(native()) || 1, cap),
      });
    } catch {
      return;
    }
    return () => {
      try {
        if (desc) Object.defineProperty(window, "devicePixelRatio", desc);
        else delete (window as { devicePixelRatio?: number }).devicePixelRatio;
      } catch {
        /* ignore restore failures */
      }
    };
  }, [cap]);
}

/**
 * Heavy WebGL particles. Parent unmounts this after skip/end so the package
 * cleanup (cancelAnimationFrame + dispose) runs.
 */
export function SplashCanvas() {
  const mobile = isNarrowMobile();
  const count = mobile ? 3600 : 9000;
  useSplashDprCap(mobile ? 1.5 : 1.75);
  return (
    <ShapeParticleText
      className="auu-splash-canvas"
      text="AUUTRADE"
      shape="crystal"
      particleCount={count}
      particleSize={mobile ? 0.014 : 0.011}
      primaryColor={PRIMARY}
      secondaryColor={SECONDARY}
      backgroundColor="#0b0d10"
      transparent={false}
      morphDuration={2.4}
      rotationSpeed={0.12}
      hoverIntensity={0.03}
      lightningIntensity={0.55}
      lightningColor="#f6d48a"
      zapSpread={0.55}
      zapWidth={0.0025}
      cameraDistance={mobile ? 6.2 : 5.4}
      globeOpacity={0.1}
      globeColor={PRIMARY}
      showGlobe={!mobile}
      glowEffect
      style={{ width: "100%", height: "100%" }}
    />
  );
}

export default SplashCanvas;
