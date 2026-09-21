import type { HabitTag, HabitTagName } from "@/types/contracts";

export const HABIT_LABEL: Record<HabitTagName, string> = {
  sniper: "极早进入",
  mid_curve: "曲线中段",
  graduation_chase: "临近毕业追涨",
  flip: "短持快出",
  bag: "长持持仓",
};

export function habitLabel(tag: string): string {
  return HABIT_LABEL[tag as HabitTagName] ?? tag;
}

export function HabitTagChips({
  tags,
  primary,
}: {
  tags: HabitTag[];
  primary?: HabitTag | null;
}) {
  const primaryTag = primary?.tag;
  return (
    <ul className="habit-chip-row">
      {tags.map((t) => {
        const isPrimary = t.tag === primaryTag;
        return (
          <li key={t.tag} className={`habit-chip habit-${t.tag}${isPrimary ? " primary" : ""}`}>
            <span className="habit-chip-title" title={t.tag}>
              {habitLabel(t.tag)}
              {isPrimary ? <em className="habit-primary-badge">主习惯</em> : null}
            </span>
            <span className="habit-conf" aria-label="confidence">
              <i style={{ width: `${Math.round(t.confidence * 100)}%` }} />
            </span>
            <span className="tiny muted">{Math.round(t.confidence * 100)}%</span>
            {t.evidence.length ? (
              <details>
                <summary>证据</summary>
                <ul>
                  {t.evidence.map((ev) => (
                    <li key={ev}>{ev}</li>
                  ))}
                </ul>
              </details>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}
