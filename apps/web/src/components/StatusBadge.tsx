type Props = {
  label: string;
  tone?: "neutral" | "success" | "warning" | "danger";
};

const symbols = { neutral: "○", success: "✓", warning: "!", danger: "×" } as const;

export function StatusBadge({ label, tone = "neutral" }: Props) {
  return <span className="status-badge" data-tone={tone}>
    <span aria-hidden="true">{symbols[tone]}</span> {label}
  </span>;
}
