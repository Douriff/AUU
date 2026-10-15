import { Fragment, useState } from "react";
import { useTranslation } from "react-i18next";
import { DRAW_TOOLS, type ToolId } from "./drawings";
import type { SyncState } from "./drawingsSync";

type Props = {
  active: ToolId | null;
  hasSelection: boolean;
  count: number;
  sync: SyncState;
  magnet: boolean;
  onPick: (t: ToolId) => void;
  onMagnet: () => void;
  onDelete: () => void;
  onClear: () => void;
  /** price alerts: bell button with unread badge; the panel itself is rendered by the chart */
  alerts?: { open: boolean; unread: number; active: number; onToggle: () => void };
};

/** Left toolbar on desktop; on phones a collapsible strip above the chart. */
export function DrawToolbar({ active, hasSelection, count, sync, magnet, onPick, onMagnet, onDelete, onClear, alerts }: Props) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const pick = (id: ToolId) => {
    onPick(id);
    setOpen(false); // phones: collapse again so the chart gets the room back while drawing
  };
  return (
    <div className={`kc-tools${open ? " is-open" : ""}${active ? " is-drawing" : ""}`} role="toolbar" aria-label={t("chart.draw.aria")}>
      <div className="kc-tools-head">
        <button type="button" className="kc-tools-toggle" aria-expanded={open} onClick={() => setOpen((v) => !v)} title={t("chart.draw.aria")}>
          <span aria-hidden>✎</span> {active ? t(`chart.draw.${active}`) : t("chart.draw.toggle")}
        </button>
        {alerts ? (
            <button
              type="button"
              className={`kc-tool kc-bell${alerts.open ? " is-on" : ""}`}
              aria-pressed={alerts.open}
              aria-label={t("chart.alert.title")}
              title={t("chart.alert.btnTitle", { n: alerts.active })}
              data-tool="alert"
              data-testid="alert-btn"
              onClick={alerts.onToggle}
            >
              <span className="kc-glyph" aria-hidden>🔔</span>
              <span className="kc-tool-label">{t("chart.alert.btn")}</span>
              {alerts.unread ? <i className="kc-badge" data-testid="alert-badge" aria-label={t("chart.alert.unread", { n: alerts.unread })}>{alerts.unread > 9 ? "9+" : alerts.unread}</i> : null}
            </button>
        ) : null}
      </div>
      <div className="kc-tools-list">
        {DRAW_TOOLS.map((d, i) => (
          <Fragment key={d.id}>
            {d.adv && !DRAW_TOOLS[i - 1]?.adv ? <i className="kc-sep" /> : null}
            <button
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
          </Fragment>
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
        <span className={`kc-sync is-${sync}`} role="status" title={t(`chart.draw.sync.${sync}Title`)} data-sync={sync}>
          <i aria-hidden /> <span className="kc-tool-label">{t(`chart.draw.sync.${sync}`)}</span>
        </span>
      </div>
    </div>
  );
}
