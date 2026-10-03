import { useColorPref } from "@/theme/colorPref";

/** 绿涨红跌 / 红涨绿跌 segmented switch (stored in localStorage, applies site-wide incl. charts). */
export function ColorToggle() {
  const [pref, setPref] = useColorPref();
  return (
    <div className="seg" role="radiogroup" aria-label="涨跌颜色">
      <button type="button" role="radio" aria-checked={pref === "green"} className={pref === "green" ? "is-on" : ""} onClick={() => setPref("green")}>
        <i className="sw up-g" />绿涨红跌
      </button>
      <button type="button" role="radio" aria-checked={pref === "red"} className={pref === "red" ? "is-on" : ""} onClick={() => setPref("red")}>
        <i className="sw up-r" />红涨绿跌
      </button>
    </div>
  );
}
