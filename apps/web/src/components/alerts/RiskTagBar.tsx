interface Props {
  tags: string[];
  allow: boolean | null;
}

export function RiskTagBar({ tags, allow }: Props) {
  const unique = Array.from(new Set(tags));
  const denied = allow === false;
  const liveOff = unique.includes("LIVE_DISABLED");
  return (
    <div
      className={`risk-tag-bar ${denied ? "denied" : unique.length ? "warn" : "ok"}`}
      data-live-disabled={liveOff ? "true" : "false"}
    >
      <span className="label">RiskGate</span>
      {allow === null && !unique.length ? (
        <span className="muted">等待 risk…</span>
      ) : (
        <>
          <span className="allow">{denied ? "DENY" : "ALLOW"}</span>
          {unique.map((t) => (
            <span key={t} className={`tag${t === "LIVE_DISABLED" ? " live-disabled-tag" : ""}`}>
              {t}
            </span>
          ))}
          {!unique.length && !denied ? <span className="muted">no tags</span> : null}
        </>
      )}
    </div>
  );
}
