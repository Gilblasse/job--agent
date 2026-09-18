import { FormEvent, useEffect, useRef, useState } from "react";
import type { RuleSuggestion } from "../types";

const PROMPTS: Record<string, string> = {
  duty: "Exclude a duty this job involves (DUTIES section)",
  skill: "Exclude a skill or tool it names (ANYWHERE, as a name)",
  anywhere: "A phrase that rules a job out wherever it appears (ANYWHERE)",
};

interface Props {
  open: boolean;
  suggestions: RuleSuggestion[];
  busy: boolean;
  error: string | null;
  onCancel: () => void;
  onSubmit: (reason: string, rules: Array<{ kind: string; value: string }>) => void;
}

/** "Not jobs like this": the reason in the user's words, then what it means for the
 *  next run -- the job's own employer, title wording and levels are one tick; duties,
 *  skills and phrases are typed. Nothing is inferred from the reason. */
export default function DismissDialog({ open, suggestions, busy, error, onCancel, onSubmit }: Props) {
  const ref = useRef<HTMLDialogElement>(null);
  const [reason, setReason] = useState("");
  const [picked, setPicked] = useState<Record<string, boolean>>({});
  const [phrases, setPhrases] = useState<Record<string, string>>({ duty: "", skill: "", anywhere: "" });
  const [titleText, setTitleText] = useState("");

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      setReason("");
      setPicked({});
      setPhrases({ duty: "", skill: "", anywhere: "" });
      setTitleText(suggestions.find((s) => s.kind === "title")?.value ?? "");
      dialog.showModal();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [open, suggestions]);

  function submit(event: FormEvent) {
    event.preventDefault();
    const rules: Array<{ kind: string; value: string }> = [];
    for (const s of suggestions) {
      const key = `${s.kind}:${s.value}`;
      if (!picked[key]) continue;
      if (s.kind === "title") rules.push({ kind: "title", value: titleText.trim() });
      else if (s.kind === "hide") continue;
      else rules.push({ kind: s.kind, value: s.value });
    }
    for (const kind of ["duty", "skill", "anywhere"]) {
      if (picked[kind] && phrases[kind].trim()) rules.push({ kind, value: phrases[kind].trim() });
    }
    onSubmit(reason.trim(), rules);
  }

  const fixed = suggestions.filter((s) => s.kind !== "hide");
  return (
    <dialog ref={ref} onClose={onCancel} aria-labelledby="dismiss-heading">
      <form onSubmit={submit}>
        <h2 id="dismiss-heading">Not jobs like this</h2>
        <label htmlFor="dismiss-reason">Why not? Kept with the job, in your words.</label>
        <input id="dismiss-reason" type="text" value={reason} onChange={(e) => setReason(e.target.value)} required autoFocus />
        <fieldset style={{ border: "none", padding: 0, margin: 0 }}>
          <legend style={{ fontWeight: 600, fontSize: 13 }}>What should the next search do about it?</legend>
          {fixed.map((s) => {
            const key = `${s.kind}:${s.value}`;
            return (
              <div key={key}>
                <label className="check">
                  <input type="checkbox" checked={Boolean(picked[key])} onChange={(e) => setPicked({ ...picked, [key]: e.target.checked })} />
                  {s.label}
                </label>
                {s.kind === "title" && picked[key] && (
                  <input
                    type="text"
                    aria-label="Title wording to exclude"
                    value={titleText}
                    onChange={(e) => setTitleText(e.target.value)}
                  />
                )}
              </div>
            );
          })}
          {(["duty", "skill", "anywhere"] as const).map((kind) => (
            <div key={kind}>
              <label className="check">
                <input type="checkbox" checked={Boolean(picked[kind])} onChange={(e) => setPicked({ ...picked, [kind]: e.target.checked })} />
                {PROMPTS[kind]}
              </label>
              {picked[kind] && (
                <input
                  type="text"
                  aria-label={PROMPTS[kind]}
                  placeholder="Phrase"
                  value={phrases[kind]}
                  onChange={(e) => setPhrases({ ...phrases, [kind]: e.target.value })}
                />
              )}
            </div>
          ))}
          <p className="hint">Nothing ticked just hides this job; no rule is added.</p>
        </fieldset>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <div className="actions">
          <button type="button" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button type="submit" className="primary" disabled={busy || !reason.trim()}>
            {busy ? "Saving…" : "Dismiss"}
          </button>
        </div>
      </form>
    </dialog>
  );
}
