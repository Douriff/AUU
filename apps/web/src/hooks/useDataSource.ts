import { useCallback, useEffect, useState } from "react";
import type { DataSource } from "@/venue";
import { DATA_SOURCES } from "@/venue";

const KEY = "auu.dataSource";

function isDataSource(v: string | null): v is DataSource {
  return v !== null && (DATA_SOURCES as readonly string[]).includes(v);
}

function read(): DataSource {
  try {
    const v = localStorage.getItem(KEY);
    if (isDataSource(v)) return v;
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
      if (e.key === KEY && isDataSource(e.newValue)) {
        setDataSourceState(e.newValue);
      }
    };
    const onCustom = (e: Event) => {
      const d = (e as CustomEvent).detail;
      if (isDataSource(d)) setDataSourceState(d);
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
