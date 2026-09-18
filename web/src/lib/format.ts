export function salaryLabel(
  min: number | null | undefined,
  max: number | null | undefined,
  currency: string | null | undefined,
  period: string | null | undefined,
): string | null {
  if (min == null && max == null) return null;
  const symbol = !currency || currency === "USD" ? "$" : `${currency} `;
  const fmt = (v: number) => (v >= 1000 ? `${symbol}${Math.round(v / 1000)}k` : `${symbol}${v}`);
  const range = min != null && max != null ? `${fmt(min)}–${fmt(max)}` : fmt((min ?? max) as number);
  return `${range} / ${period ?? "year"}`;
}

export function whenLabel(iso: string | null | undefined): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function dateLabel(iso: string | null | undefined): string {
  if (!iso) return "";
  const date = new Date(iso.length === 10 ? `${iso}T00:00:00` : iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

export function ago(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "";
  const seconds = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return `${seconds} s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

export function workplaceLabel(value: string | null | undefined): string | null {
  if (!value || value === "unknown") return null;
  return value === "onsite" ? "On-site" : value.charAt(0).toUpperCase() + value.slice(1);
}

export function jobTypeLabel(value: string | null | undefined): string | null {
  if (!value) return null;
  return value.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

export function number(n: number): string {
  return n.toLocaleString();
}
