interface Props {
  tags: string[];
  allow: boolean | null;
}

export function RiskTagBar({ tags, allow }: Props) {
  const denied = allow === false;
  return (
    <div className={`risk-tag-bar ${denied ? "denied" : tags.length ? "warn" : "ok"}`}>
      <span className="label">RiskGate</span>
      {allow === null && !tags.length ? (
        <span className="muted">等待 risk…</span>
      ) : (
        <>
          <span className="allow">{denied ? "DENY" : "ALLOW"}</span>
          {tags.map((t) => (
            <span key={t} className="tag">
              {t}
            </span>
          ))}
          {!tags.length && !denied ? <span className="muted">no tags</span> : null}
        </>
      )}
    </div>
  );
}
