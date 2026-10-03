/** AUUTRADE brand: geometric "A" mark (two rising strokes + amber bar) + wordmark. */
export function LogoMark({ size = 22 }: { size?: number }) {
  return (
    <svg className="brand-mark" width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
      <rect width="24" height="24" rx="5" fill="#f0a531" />
      <path d="M5.5 18.5 12 5l6.5 13.5" fill="none" stroke="#0b0d10" strokeWidth="2.6" strokeLinejoin="round" strokeLinecap="round" />
      <path d="M8.6 13.6h6.8" stroke="#0b0d10" strokeWidth="2.4" strokeLinecap="round" />
    </svg>
  );
}

export function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <span className="brand" aria-label="AUUTRADE">
      <LogoMark />
      {compact ? null : (
        <span className="brand-word">
          AUU<b>TRADE</b>
        </span>
      )}
    </span>
  );
}

/** The one site-wide mode badge: paper trading, live trading locked. */
export function ModeBadge({ liveOff = true }: { liveOff?: boolean }) {
  return liveOff ? (
    <span className="mode-pill" title="所有成交均为纸面模拟；实盘交易已锁定，不会动用真实资金">
      <i aria-hidden="true" />
      纸面 · 实盘锁定
    </span>
  ) : (
    <span className="mode-pill is-warn" title="实盘锁未处于锁定状态，请检查配置">
      <i aria-hidden="true" />
      实盘检查
    </span>
  );
}
