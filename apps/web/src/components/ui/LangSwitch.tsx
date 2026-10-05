import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { LANGS, currentLang, setLang, type Lang } from "@/i18n";

function Globe() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
      <circle cx="12" cy="12" r="8.5" />
      <path d="M3.5 12h17M12 3.5c2.5 2.6 2.5 14.4 0 17M12 3.5c-2.5 2.6-2.5 14.4 0 17" />
    </svg>
  );
}

/**
 * Language switcher. `compact` = globe + short code (top bar / auth cards, opens a list);
 * otherwise a full list of radio buttons (settings page, user menu).
 */
export function LangSwitch({ compact = false }: { compact?: boolean }) {
  const { t, i18n } = useTranslation();
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const cur = (i18n.language as Lang) || currentLang();

  useEffect(() => {
    if (!open) return;
    const off = (e: MouseEvent | TouchEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("mousedown", off);
    window.addEventListener("touchstart", off);
    window.addEventListener("keydown", esc);
    return () => {
      window.removeEventListener("mousedown", off);
      window.removeEventListener("touchstart", off);
      window.removeEventListener("keydown", esc);
    };
  }, [open]);

  const pick = (code: Lang) => {
    setOpen(false);
    void setLang(code);
  };

  if (!compact) {
    return (
      <div className="lang-grid" role="radiogroup" aria-label={t("lang.label")}>
        {LANGS.map((l) => (
          <button
            key={l.code}
            type="button"
            role="radio"
            lang={l.code}
            dir={l.dir}
            aria-checked={cur === l.code}
            className={cur === l.code ? "is-on" : ""}
            onClick={() => pick(l.code)}
          >
            {l.name}
          </button>
        ))}
      </div>
    );
  }
  const short = cur === "zh-CN" ? "中" : cur.slice(0, 2).toUpperCase();
  return (
    <div className="lang-sw" ref={box}>
      <button
        type="button"
        className="lang-sw-btn"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={t("lang.label")}
        title={t("lang.label")}
        onClick={() => setOpen((v) => !v)}
      >
        <Globe />
        <span className="lang-sw-code">{short}</span>
      </button>
      {open ? (
        <ul className="lang-sw-pop" role="listbox" aria-label={t("lang.label")}>
          {LANGS.map((l) => (
            <li key={l.code}>
              <button
                type="button"
                role="option"
                lang={l.code}
                dir={l.dir}
                aria-selected={cur === l.code}
                className={cur === l.code ? "is-on" : ""}
                onClick={() => pick(l.code)}
              >
                {l.name}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
