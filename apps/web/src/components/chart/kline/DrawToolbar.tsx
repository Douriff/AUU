import { useState } from "react";
import { useTranslation } from "react-i18next";
import { DRAW_TOOLS, type ToolId } from "./drawings";

type Props = {
  active: ToolId | null;
  hasSelection: boolean;
  count: number;
  magnet: boolean;
  onPick: (t: ToolId) => void;
  onMagnet: () => void;
  onDelete: () => void;
  onClear: () => void;
};

/** Left toolbar on desktop; on phones a collapsible strip above the chart. */
export function DrawToolbar({ active, hasSelection, count, magnet, onPick, onMagnet, onDelete, onClear }: Props) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const pick = (id: ToolId) => {
    onPick(id);
    setOpen(false); // phones: collapse again so the chart gets the room back while drawing
  };
  return (
    <div className={`kc-tools${open ? " is-open" : ""}${active ? " is-drawing" : ""}`} role="toolbar" aria-label={t("chart.draw.aria")}>
      <button type="button" className="kc-tools-toggle" aria-expanded={open} onClick={() => setOpen((v) => !v)} title={t("chart.draw.aria")}>
        <span aria-hidden>✎</span> {active ? t(`chart.draw.${active}`) : t("chart.draw.toggle")}
      </button>
      <div className="kc-tools-list">
        {DRAW_TOOLS.map((d) => (
          <button
            key={d.id}
            type="button"
            className={`kc-tool${active === d.id ? " is-on" : ""}`}
            aria-pressed={active === d.id}
            title={t(`chart.draw.${d.id}`)}
            aria-label={t(`chart.draw.${d.id}`)}
            data-tool={d.id}
            onClick={() => pick(d.id)}
          >
            <span className="kc-glyph" aria-hidden>{d.glyph}</span>
            <span className="kc-tool-label">{t(`chart.draw.${d.id}`)}</span>
          </button>
        ))}
        <i className="kc-sep" />
        <button type="button" className={`kc-tool${magnet ? " is-on" : ""}`} aria-pressed={magnet} title={t("chart.draw.magnetTitle")} aria-label={t("chart.draw.magnet")} data-tool="magnet" onClick={onMagnet}>
          <span className="kc-glyph" aria-hidden>🧲</span>
          <span className="kc-tool-label">{t("chart.draw.magnet")}</span>
        </button>
        <button type="button" className="kc-tool" disabled={!hasSelection} title={t("chart.draw.deleteTitle")} aria-label={t("chart.draw.delete")} data-tool="delete" onClick={onDelete}>
          <span className="kc-glyph" aria-hidden>⌫</span>
          <span className="kc-tool-label">{t("chart.draw.delete")}</span>
        </button>
        <button
          type="button"
          className="kc-tool"
          disabled={!count}
          title={t("chart.draw.clearTitle")}
          aria-label={t("chart.draw.clear")}
          data-tool="clear"
          onClick={() => {
            if (window.confirm(t("chart.draw.clearConfirm", { n: count }))) onClear();
          }}
        >
          <span className="kc-glyph" aria-hidden>🗑</span>
          <span className="kc-tool-label">{t("chart.draw.clear")}</span>
        </button>
      </div>
    </div>
  );
}
