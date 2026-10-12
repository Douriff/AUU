import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { prefersReducedMotion } from "./splashPrefs";
import "./splash.css";

const SplashCanvas = lazy(() => import("./SplashCanvas"));

const HOLD_AFTER_MORPH_MS = 3200;
const MORPH_MS = 5800; // brain + spill + coalesce
const FAILSAFE_MS = 16000;

type Props = { children: React.ReactNode };

/**
 * Fullscreen intro on every full page load. Skip/Enter dismiss for this load only
 * (no localStorage). Does not block API/auth; unmounts WebGL on finish.
 */
export function SplashIntro({ children }: Props) {
  const { t } = useTranslation();
  const [show, setShow] = useState(true);
  const [useParticles, setUseParticles] = useState(false);
  const closing = useRef(false);

  useEffect(() => {
    if (!show) return;
    setUseParticles(!prefersReducedMotion());
  }, [show]);

  const finish = useCallback(() => {
    if (closing.current) return;
    closing.current = true;
    setShow(false);
    setUseParticles(false); // drop lazy canvas → dispose
  }, []);

  useEffect(() => {
    if (!show) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" || e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        finish();
      }
    };
    window.addEventListener("keydown", onKey);
    const fail = window.setTimeout(finish, FAILSAFE_MS);
    let auto: number | undefined;
    if (!prefersReducedMotion()) {
      auto = window.setTimeout(finish, MORPH_MS + HOLD_AFTER_MORPH_MS);
    }
    return () => {
      window.removeEventListener("keydown", onKey);
      window.clearTimeout(fail);
      if (auto) window.clearTimeout(auto);
    };
  }, [show, finish]);

  if (!show) return <>{children}</>;

  return (
    <>
      {children}
      <div className="auu-splash" role="dialog" aria-modal="true" aria-label={t("splash.aria")}>
        <div className="auu-splash-stage" aria-hidden={useParticles ? undefined : true}>
          {useParticles ? (
            <Suspense fallback={<div className="auu-splash-static" />}>
              <SplashCanvas />
            </Suspense>
          ) : (
            <div className="auu-splash-static">
              <div className="auu-splash-word">
                AUU<span>TRADE</span>
              </div>
            </div>
          )}
        </div>
        <div className="auu-splash-copy">
          <p className="auu-splash-tag">{t("splash.tag")}</p>
          <p className="auu-splash-lead">{t("splash.lead")}</p>
          <button type="button" className="auu-splash-btn" onClick={finish} autoFocus>
            {t("splash.enter")}
          </button>
          <button type="button" className="auu-splash-skip" onClick={finish}>
            {t("splash.skip")}
          </button>
        </div>
      </div>
    </>
  );
}
