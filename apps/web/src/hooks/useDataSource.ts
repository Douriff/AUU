import { useCallback, useEffect, useState } from "react";
import type { DataSource } from "@/types/contracts";

const KEY = "auu.dataSource";

function read(): DataSource {
  try {
    const v = localStorage.getItem(KEY);
    if (v === "paper" || v === "mock") return v;
  } catch {
    /* ignore */
  }
  return "mock";
}

export function useDataSource() {
  const [dataSource, setDataSourceState] = useState<DataSource>(() =>
    typeof window === "undefined" ? "mock" : read()
  );

  useEffect(() => {
    setDataSourceState(read());
  }, []);

  const setDataSource = useCallback((next: DataSource) => {
    setDataSourceState(next);
    try {
      localStorage.setItem(KEY, next);
    } catch {
      /* ignore */
    }
    window.dispatchEvent(new CustomEvent("auu:dataSource", { detail: next }));
  }, []);

  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === KEY && (e.newValue === "mock" || e.newValue === "paper")) {
        setDataSourceState(e.newValue);
      }
    };
    const onCustom = (e: Event) => {
      const d = (e as CustomEvent).detail;
      if (d === "mock" || d === "paper") setDataSourceState(d);
    };
    window.addEventListener("storage", onStorage);
    window.addEventListener("auu:dataSource", onCustom);
    return () => {
      window.removeEventListener("storage", onStorage);
      window.removeEventListener("auu:dataSource", onCustom);
    };
  }, []);

  return { dataSource, setDataSource };
}
