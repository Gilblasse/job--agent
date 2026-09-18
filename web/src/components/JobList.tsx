import { KeyboardEvent, useEffect, useRef } from "react";
import { jobTypeLabel, salaryLabel, workplaceLabel } from "../lib/format";
import type { ResultItem } from "../types";

interface CardProps {
  item: ResultItem;
  selected: boolean;
  onSelect: () => void;
}

export function JobCard({ item, selected, onSelect }: CardProps) {
  const salary = salaryLabel(item.salary_min, item.salary_max, item.salary_currency, item.salary_period);
  const bits = [item.location_raw || null, workplaceLabel(item.workplace), jobTypeLabel(item.employment_type), salary]
    .filter(Boolean)
    .join(" · ");
  return (
    <div
      role="option"
      aria-selected={selected}
      tabIndex={selected ? 0 : -1}
      className="card"
      data-job-id={item.id}
      onClick={onSelect}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect();
        }
      }}
    >
      <h3>
        <span data-testid="card-title">{item.title}</span>
        {item.is_new && <span className="badge new">NEW</span>}
        {item.uncertain && (
          <span className="badge uncertain" title="Something in your rules could not be confirmed from the posting">
            ?
          </span>
        )}
        {item.decision === "rejected" && <span className="badge rejected">Rejected</span>}
      </h3>
      <div className="meta">
        {item.company}
        {bits ? ` · ${bits}` : ""}
      </div>
      {item.preview && <p className="preview">{item.preview}</p>}
    </div>
  );
}

interface ListProps {
  items: ResultItem[];
  total: number;
  selectedId: number | null;
  onSelect: (id: number) => void;
  onLoadMore: () => void;
  loading: boolean;
  scrollKey: string;
  children?: React.ReactNode;
}

/** The matching-jobs column: a listbox with arrow-key selection whose scroll position
 *  is remembered per search and run, so coming back lands where the user left. */
export default function JobList({ items, total, selectedId, onSelect, onLoadMore, loading, scrollKey, children }: ListProps) {
  const box = useRef<HTMLDivElement>(null);
  const restored = useRef<string | null>(null);

  useEffect(() => {
    const node = box.current;
    if (!node) return;
    if (restored.current !== scrollKey && items.length) {
      restored.current = scrollKey;
      try {
        const saved = sessionStorage.getItem(`scroll:${scrollKey}`);
        if (saved) node.scrollTop = Number(saved);
      } catch {
        // no storage: start at the top
      }
    }
    let timer: number | undefined;
    const onScroll = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        try {
          sessionStorage.setItem(`scroll:${scrollKey}`, String(node.scrollTop));
        } catch {
          // ignore
        }
      }, 150);
    };
    node.addEventListener("scroll", onScroll);
    return () => {
      node.removeEventListener("scroll", onScroll);
      window.clearTimeout(timer);
    };
  }, [scrollKey, items.length]);

  function onKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const index = items.findIndex((i) => i.id === selectedId);
    const next = e.key === "ArrowDown" ? Math.min(items.length - 1, index + 1) : Math.max(0, index - 1);
    if (items[next]) {
      onSelect(items[next].id);
      const card = box.current?.querySelector<HTMLElement>(`[data-job-id="${items[next].id}"]`);
      card?.focus();
      card?.scrollIntoView({ block: "nearest" });
    }
  }

  return (
    <section className="list-pane" aria-label="Matching jobs">
      <div className="list-head">{children}</div>
      <div ref={box} className="listbox" role="listbox" aria-label="Jobs" onKeyDown={onKeyDown} data-testid="job-list">
        {items.map((item) => (
          <JobCard key={item.id} item={item} selected={item.id === selectedId} onSelect={() => onSelect(item.id)} />
        ))}
      </div>
      {items.length < total && (
        <div className="list-foot">
          <button onClick={onLoadMore} disabled={loading}>
            {loading ? "Loading…" : `Load more (${items.length} of ${total})`}
          </button>
        </div>
      )}
    </section>
  );
}
