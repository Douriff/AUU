interface Props {
  checked: boolean;
  onChange: (next: boolean) => void;
  label?: string;
  compact?: boolean;
  disabled?: boolean;
  title?: string;
}

export function AutoPaperToggle({ checked, onChange, label = "auto_paper_orders", compact, disabled, title }: Props) {
  return (
    <div className={`auto-paper-toggle ${compact ? "compact" : ""}`} title={title}>
      <span>{label}</span>
      <div className="data-source-toggle" role="group" aria-label={label}>
        <button type="button" className={!checked ? "active" : ""} disabled={disabled} onClick={() => onChange(false)}>
          off
        </button>
        <button type="button" className={checked ? "active" : ""} disabled={disabled} onClick={() => onChange(true)}>
          on
        </button>
      </div>
    </div>
  );
}
