import { useId } from "react";
import { parseList, shortEntries, type Scope } from "../lib/spec";

const SCOPE_TEXT: Record<Scope, string> = {
  TITLE: "reads the title only",
  DUTIES: "reads the responsibilities section, or the whole posting when there is none",
  ANYWHERE: "reads the entire posting, company blurb included",
  NAMES: "read in context",
  PLACES: "cities or metros",
};

interface Props {
  label: string;
  scope: Scope;
  hint?: string;
  value: string[];
  clarify?: boolean;
  onChange: (value: string[]) => void;
}

/** One entry per line. Entries of three characters or fewer in a prose-scanned list
 *  are warned about, because "go" is also a verb and rules a job out on one hit. */
export default function ListField({ label, scope, hint, value, clarify, onChange }: Props) {
  const id = useId();
  const short = clarify ? shortEntries(value) : [];
  return (
    <div className="list-field">
      <label htmlFor={id}>{label}</label>
      <textarea
        id={id}
        value={value.join("\n")}
        placeholder="One per line"
        onChange={(e) => onChange(parseList(e.target.value))}
        aria-describedby={`${id}-hint`}
      />
      <p className="hint" id={`${id}-hint`}>
        <span className="scope">{scope}</span> — {SCOPE_TEXT[scope]}
        {hint ? `. ${hint}` : ""}
      </p>
      {short.length > 0 && (
        <p className="warn" role="status">
          {short.map((s) => `“${s}”`).join(", ")} {short.length > 1 ? "are" : "is"} very short and
          will match ordinary words; spell it out or remove it.
        </p>
      )}
    </div>
  );
}
