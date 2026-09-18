import type { Section } from "../types";

const BULLET = /^[-*•◦–—>#]\s*|^\d+[.)]\s+/;

interface Block {
  kind: "paragraph" | "list";
  lines: string[];
}

/** The lines of one section grouped into paragraphs and bullet lists, as written. */
function blocks(lines: string[]): Block[] {
  const out: Block[] = [];
  let current: Block | null = null;
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) {
      current = null;
      continue;
    }
    const bullet = BULLET.test(line);
    const kind: Block["kind"] = bullet ? "list" : "paragraph";
    if (!current || current.kind !== kind) {
      current = { kind, lines: [] };
      out.push(current);
    }
    current.lines.push(bullet ? line.replace(BULLET, "") : line);
  }
  return out;
}

interface Props {
  sections: Section[];
  url: string;
}

/** The posting as the employer wrote it: its own headings, paragraphs and lists.
 *  Nothing is summarised or invented. */
export default function Posting({ sections, url }: Props) {
  if (!sections.length) {
    return (
      <p className="muted">
        The employer's posting has no description here —{" "}
        <a href={url} target="_blank" rel="noopener noreferrer">
          open it to read it
        </a>
        .
      </p>
    );
  }
  const hasHeadings = sections.some((s) => s.heading);
  return (
    <div className="posting">
      {sections.map((section, index) => (
        <section key={index}>
          <h3>{section.heading ?? (hasHeadings ? "" : "Job description")}</h3>
          {blocks(section.lines).map((block, i) =>
            block.kind === "list" ? (
              <ul key={i}>
                {block.lines.map((line, j) => (
                  <li key={j}>{line}</li>
                ))}
              </ul>
            ) : (
              <p key={i}>{block.lines.join("\n")}</p>
            ),
          )}
        </section>
      ))}
    </div>
  );
}
