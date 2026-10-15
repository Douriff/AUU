import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import type { MainstreamTf } from "@/types/mainstream";
import { CHART_TFS, MainstreamChart } from "@/components/chart/MainstreamChart";
import type { CrosshairBus } from "@/components/chart/crosshairBus";
import type { Cell, LayoutN } from "@/components/chart/layout";

type Props = {
  n: LayoutN;
  cells: Cell[];
  /** symbols offered in every cell's picker (the market list) */
  symbols: string[];
  /** extra-venue data source, only ever applies to the primary cell */
  venue: string | null;
  overlay: boolean;
  bus: CrosshairBus | null;
  onChange: (index: number, next: Cell) => void;
};

/** 2 / 4 charts side by side. Cell 0 is the page's primary chart, the others keep their own symbol + timeframe. */
export function MultiChart({ n, cells, symbols, venue, overlay, bus, onChange }: Props) {
  const { t } = useTranslation();
  const list = useMemo(() => [...symbols].sort(), [symbols]);
  return (
    <div className={`mc-grid mc-n${n}`} data-testid="multi-chart" data-n={n}>
      {cells.map((c, i) => (
        <div className="mc-cell" key={i} data-cell={i}>
          <div className="mc-head">
            <select
              className="mc-sym"
              value={c.symbol}
              aria-label={t("chart.layout.cellSymbol")}
              data-testid={`mc-sym-${i}`}
              onChange={(e) => onChange(i, { ...c, symbol: e.target.value })}
            >
              {list.includes(c.symbol) ? null : <option value={c.symbol}>{c.symbol}</option>}
              {list.map((s) => (
                <option key={s} value={s}>
                  {s}/USDT
                </option>
              ))}
            </select>
            <select
              className="mc-tf"
              value={c.tf}
              aria-label={t("chart.layout.cellTf")}
              data-testid={`mc-tf-${i}`}
              onChange={(e) => onChange(i, { ...c, tf: e.target.value as MainstreamTf })}
            >
              {CHART_TFS.map((x) => (
                <option key={x.id} value={x.id}>
                  {t(x.label)}
                </option>
              ))}
            </select>
            {i === 0 ? <span className="mc-primary muted" title={t("chart.layout.primaryTitle")}>★</span> : null}
          </div>
          <MainstreamChart symbol={c.symbol} tf={c.tf} venue={i === 0 ? venue : null} overlay={i === 0 ? overlay : true} compact bus={bus} busId={`cell${i}`} />
        </div>
      ))}
      <div className="mc-credit muted">
        <a href="/THIRD_PARTY_NOTICES.txt" target="_blank" rel="noopener noreferrer">KLineChart · Apache-2.0</a>
      </div>
    </div>
  );
}
