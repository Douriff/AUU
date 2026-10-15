import { useState } from "react";
import { useTranslation } from "react-i18next";
import { INDICATOR_DEFS, defaultPrefs, sanitizeParam, type IndicatorId, type IndicatorPrefs } from "./indicatorPrefs";

type Props = { prefs: IndicatorPrefs; onChange: (p: IndicatorPrefs) => void };

/** Popover (bottom sheet on phones): toggle every indicator and edit its parameters. */
export function IndicatorPanel({ prefs, onChange }: Props) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const toggle = (id: IndicatorId) => onChange({ ...prefs, on: { ...prefs.on, [id]: !prefs.on[id] } });
  const setParam = (id: IndicatorId, i: number, raw: string) => {
    const def = INDICATOR_DEFS.find((d) => d.id === id)!;
    const params = [...prefs.params[id]];
    params[i] = sanitizeParam(def, i, Number(raw));
    onChange({ ...prefs, params: { ...prefs.params, [id]: params } });
  };
  return (
    <span className="kc-ind-wrap">
      <button type="button" className={open ? "is-on" : ""} aria-expanded={open} data-testid="ind-panel-btn" onClick={() => setOpen((v) => !v)}>
        {t("chart.ind.panel")}
      </button>
      {open ? (
        <div className="kc-ind-panel" role="dialog" aria-label={t("chart.ind.panelTitle")}>
          <div className="kc-ind-head">
            <b>{t("chart.ind.panelTitle")}</b>
            <span>
              <button type="button" onClick={() => onChange(defaultPrefs())}>{t("chart.ind.reset")}</button>
              <button type="button" onClick={() => setOpen(false)} aria-label={t("chart.ind.close")}>✕</button>
            </span>
          </div>
          <ul>
            {INDICATOR_DEFS.map((d) => (
              <li key={d.id} data-ind={d.id}>
                <label title={t(`chart.ind.${d.id}Title`)}>
                  <input type="checkbox" checked={prefs.on[d.id]} onChange={() => toggle(d.id)} />
                  <b>{t(`chart.ind.${d.id}`)}</b>
                  <em>{d.main ? t("chart.ind.main") : t("chart.ind.sub")}</em>
                </label>
                <span className="kc-ind-params">
                  {prefs.params[d.id].map((v, i) => (
                    <label key={i}>
                      <small>{d.labels[i]}</small>
                      <input
                        type="number"
                        inputMode="decimal"
                        min={d.bounds[i][0]}
                        max={d.bounds[i][1]}
                        step={d.bounds[i][2]}
                        defaultValue={v}
                        key={`${d.id}${i}${v}`}
                        onBlur={(e) => setParam(d.id, i, e.currentTarget.value)}
                        onKeyDown={(e) => e.key === "Enter" && (e.currentTarget.blur() as unknown as void)}
                      />
                    </label>
                  ))}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </span>
  );
}
