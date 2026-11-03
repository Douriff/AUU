import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { quoteAgeKey, skewFreeAgeMs, type QuoteSample } from "@/lib/quoteRefresh";

function useNow(ms = 1000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(id);
  }, [ms]);
  return now;
}

/** "Updated 3s ago" for a public quote. Counts up between polls so a frozen price is visibly stale. */
export function QuoteAge({ sample }: { sample: QuoteSample | null }) {
  const { t } = useTranslation();
  const now = useNow();
  if (!sample || !sample.clientFetchedAt) return null;
  const spec = sample.store
    ? { key: "common.updatedStore" as const, n: undefined }
    : quoteAgeKey(skewFreeAgeMs(sample.eventAt, sample.serverNow, sample.clientFetchedAt, now));
  const text = spec.n == null ? t(spec.key) : t(spec.key, { n: spec.n });
  return (
    <span className="q-age num" data-testid="quote-updated" title={t("common.updatedTitle")} role="status">
      {text}
    </span>
  );
}
