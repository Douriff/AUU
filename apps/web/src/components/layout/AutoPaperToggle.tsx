interface Props {
  checked: boolean;
  onChange: (next: boolean) => void;
  label?: string;
  compact?: boolean;
}

export function AutoPaperToggle({ checked, onChange, label = "auto_paper_orders", compact }: Props) {
  return (
    <div className={`auto-paper-toggle ${compact ? "compact" : ""}`}>
      <span>{label}</span>
      <div className="data-source-toggle" role="group" aria-label={label}>
        <button type="button" className={!checked ? "active" : ""} onClick={() => onChange(false)}>
          off
        </button>
        <button type="button" className={checked ? "active" : ""} onClick={() => onChange(true)}>
          on
        </button>
      </div>
    </div>
  );
}
