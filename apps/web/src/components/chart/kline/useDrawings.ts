import { useCallback, useEffect, useRef, useState } from "react";
import type { Chart, OverlayCreate } from "klinecharts";
import { DRAW_TOOLS, USER_GROUP, loadDrawings, saveDrawings, type ToolId } from "./drawings";

export type TextEdit = { id: string; text: string } | null;

/** Drawing-tool controller for one chart: pick a tool, draw, select/delete, clear, persist per symbol. */
export function useDrawings(chart: Chart | null, symbol: string) {
  const [active, setActive] = useState<ToolId | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [count, setCount] = useState(0);
  const [magnet, setMagnet] = useState(true);
  const [textEdit, setTextEdit] = useState<TextEdit>(null);
  const drawingId = useRef<string | null>(null);
  const suppress = useRef(false);
  const symRef = useRef(symbol);
  symRef.current = symbol;
  const magnetRef = useRef(magnet);
  magnetRef.current = magnet;

  const persist = useCallback(() => {
    if (!chart || suppress.current) return;
    setCount(saveDrawings(chart, symRef.current));
  }, [chart]);

  const handlers = useCallback(
    (): Partial<OverlayCreate> => ({
      onDrawEnd: ({ overlay }) => {
        drawingId.current = null;
        setActive(null);
        persist();
        if (overlay.name === "auuText") setTextEdit({ id: overlay.id, text: "" });
        return undefined as never;
      },
      onPressedMoveEnd: () => {
        persist();
        return undefined as never;
      },
      onRemoved: ({ overlay }) => {
        setSelected((s) => (s === overlay.id ? null : s));
        window.setTimeout(persist, 0); // the overlay is still in the list while onRemoved runs
        return undefined as never;
      },
      onSelected: ({ overlay }) => {
        setSelected(overlay.id);
        return undefined as never;
      },
      onDeselected: ({ overlay }) => {
        setSelected((s) => (s === overlay.id ? null : s));
        return undefined as never;
      },
      onDoubleClick: ({ overlay }) => {
        if (overlay.name === "auuText") setTextEdit({ id: overlay.id, text: typeof overlay.extendData === "string" ? overlay.extendData : "" });
        return undefined as never;
      },
    }),
    [persist],
  );

  // (Re)load stored drawings when the chart or symbol changes.
  useEffect(() => {
    if (!chart) return;
    suppress.current = true;
    chart.removeOverlay({ groupId: USER_GROUP });
    drawingId.current = null;
    setActive(null);
    setSelected(null);
    const saved = loadDrawings(symbol);
    for (const o of saved) {
      chart.createOverlay({ name: o.name, groupId: USER_GROUP, points: o.points, extendData: o.extendData, styles: o.styles as never, ...handlers() } as OverlayCreate);
    }
    suppress.current = false;
    setCount(saved.length);
  }, [chart, symbol, handlers]);

  const cancel = useCallback(() => {
    if (chart && drawingId.current) {
      suppress.current = true;
      chart.removeOverlay({ id: drawingId.current });
      suppress.current = false;
    }
    drawingId.current = null;
    setActive(null);
  }, [chart]);

  const pick = useCallback(
    (tool: ToolId) => {
      if (!chart) return;
      if (active === tool) return cancel();
      cancel();
      if (!DRAW_TOOLS.some((t) => t.id === tool)) return;
      const id = chart.createOverlay({ name: tool, groupId: USER_GROUP, mode: magnetRef.current ? "weak_magnet" : "normal", ...handlers() } as OverlayCreate);
      if (typeof id === "string") {
        drawingId.current = id;
        setActive(tool);
      }
    },
    [chart, active, cancel, handlers],
  );

  const removeSelected = useCallback(() => {
    if (chart && selected) chart.removeOverlay({ id: selected });
  }, [chart, selected]);

  const clearAll = useCallback(() => {
    if (!chart) return;
    cancel();
    chart.removeOverlay({ groupId: USER_GROUP });
    persist();
    setCount(0);
  }, [chart, cancel, persist]);

  const commitText = useCallback(
    (text: string) => {
      if (!chart || !textEdit) return;
      const t = text.trim();
      if (!t) chart.removeOverlay({ id: textEdit.id });
      else {
        chart.overrideOverlay({ id: textEdit.id, extendData: t } as never);
        persist();
      }
      setTextEdit(null);
    },
    [chart, textEdit, persist],
  );

  return { active, selected, count, magnet, setMagnet, textEdit, setTextEdit, pick, cancel, removeSelected, clearAll, commitText };
}
