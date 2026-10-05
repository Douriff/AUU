import { useColorPref } from "@/theme/colorPref";
import { useTranslation } from "react-i18next";

/** 绿涨红跌 / 红涨绿跌 segmented switch (stored in localStorage, applies site-wide incl. charts). */
export function ColorToggle() {
  const { t } = useTranslation();
  const [pref, setPref] = useColorPref();
  return (
    <div className="seg" role="radiogroup" aria-label={t("color.label")}>
      <button type="button" role="radio" aria-checked={pref === "green"} className={pref === "green" ? "is-on" : ""} onClick={() => setPref("green")}>
        <i className="sw up-g" />{t("color.greenUp")}
      </button>
      <button type="button" role="radio" aria-checked={pref === "red"} className={pref === "red" ? "is-on" : ""} onClick={() => setPref("red")}>
        <i className="sw up-r" />{t("color.redUp")}
      </button>
    </div>
  );
}
