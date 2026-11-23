import { createContext, useContext, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { RegisterForm } from "@/pages/LoginPage/RegisterForm";
import { useTranslation } from "react-i18next";

export type SubscriptionState = {
  anon: boolean;
  authOn: boolean;
  isAdmin: boolean;
  /** True when /auth/me says public registration is open. Does not unlock billing. */
  signupAllowed: boolean;
  /** Unlocked market/premium desk. Signed-out visitors are never unlocked. */
  subscribed: boolean;
  setPreview: (next: "on" | "off") => void;
};

const Ctx = createContext<SubscriptionState | null>(null);

export function SubscriptionProvider({ value, children }: { value: SubscriptionState; children: ReactNode }) {
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSubscription(): SubscriptionState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useSubscription requires SubscriptionProvider");
  return v;
}

/** Admin (or local-flag) switch that previews the subscribed desk in this browser only. */
export function SubPreviewToggle() {
  const { t } = useTranslation();
  const { subscribed, setPreview } = useSubscription();
  return (
    <div className="seg" role="group" aria-label={t("subscribe.preview")}>
      <button type="button" className={subscribed ? "is-on" : ""} onClick={() => setPreview("on")}>
        {t("subscribe.previewOn")}
      </button>
      <button type="button" className={!subscribed ? "is-on" : ""} onClick={() => setPreview("off")}>
        {t("subscribe.previewOff")}
      </button>
    </div>
  );
}

/** Gray star-lock with a subscribe entry. No payment control. */
export function SubscribeLock({ variant, compact = false }: { variant: "shell" | "locked" | "account"; compact?: boolean }) {
  const { t } = useTranslation();
  const { anon, signupAllowed } = useSubscription();
  const account = variant === "account";
  const shell = variant === "shell";
  // Anonymous shell / account entry: registration is the primary experience when signup is open.
  // The $99 star plan stays on the logged-in locked card and the standalone /subscribe page.
  if ((shell || account) && anon) {
    return (
      <section className={`sub-lock is-register${compact ? " is-compact" : ""}`}>
        <RegisterForm embed assumeOpen={signupAllowed} />
      </section>
    );
  }
  return (
    <section className={`sub-lock${compact ? " is-compact" : ""}`} aria-labelledby="sub-lock-title">
      <span className="sub-star" aria-hidden="true">★</span>
      <h1 id="sub-lock-title">{t(account ? "subscribe.accountTitle" : shell ? "subscribe.shellTitle" : "subscribe.lockedTitle")}</h1>
      <p>{t(account ? "subscribe.accountSub" : shell ? "subscribe.shellSub" : "subscribe.lockedSub")}</p>
      {account ? null : (
        <p className="sub-price">
          <b>{t("subscribe.price")}</b> <span>{t("subscribe.per")}</span>
        </p>
      )}
      {account ? null : (
        <Link className="td-submit buy sub-cta" to="/subscribe">
          {t("subscribe.cta")}
        </Link>
      )}
      {anon || account ? (
        <Link className="td-link" to="/login">
          {t("subscribe.loginCta")}
        </Link>
      ) : null}
      {account ? null : <p className="muted sub-note">{t("subscribe.noBilling")}</p>}
    </section>
  );
}

