import type { Explanation } from "../types";

interface Props {
  explanation: Explanation | null;
  decision: "match" | "rejected" | null;
  score: number | null;
}

/** The verdict, as the engine recorded it: what failed (with the quoted evidence),
 *  the score ledger, what could not be confirmed, and the rules satisfied. */
export default function WhyMatch({ explanation, decision, score }: Props) {
  const gates = explanation?.gates ?? [];
  const signals = explanation?.signals ?? [];
  const failed = gates.filter((g) => g.outcome === "fail");
  const unknown = gates.filter((g) => g.outcome === "unverifiable");
  const passed = gates.filter((g) => g.outcome === "pass" && g.evidence);
  if (!explanation) {
    return (
      <section className="why" aria-labelledby="why-heading">
        <h3 id="why-heading">Why this matches your preferences</h3>
        <p className="muted">This job was not evaluated by the run on screen.</p>
      </section>
    );
  }
  return (
    <section className="why" aria-labelledby="why-heading">
      <h3 id="why-heading">
        {decision === "rejected" ? "Why this was rejected" : "Why this matches your preferences"}
      </h3>
      {failed.length > 0 && (
        <>
          <p className="fail">
            <strong>Rejected because</strong>
          </p>
          <ul>
            {failed.map((g, i) => (
              <li key={i} className="fail">
                {g.gate}: {g.rule}
                {g.evidence && (
                  <>
                    {" "}
                    — matched <span className="evidence">“{g.evidence}”</span>
                  </>
                )}
                {g.detail && <div className="small muted">{g.detail}</div>}
              </li>
            ))}
          </ul>
        </>
      )}
      {signals.length > 0 && (
        <>
          <p>
            <strong>Score {score != null ? Math.round(score) : ""}</strong>
          </p>
          <ul>
            {signals.map((s, i) => (
              <li key={i} className={s.points >= 0 ? "ok" : "unknown"}>
                {s.points >= 0 ? "+" : ""}
                {s.points} {s.name}
                {s.evidence && <span className="evidence"> ({s.evidence})</span>}
              </li>
            ))}
          </ul>
        </>
      )}
      {unknown.length > 0 && (
        <>
          <p className="unknown">
            <strong>Could not be confirmed</strong>
          </p>
          <ul>
            {unknown.map((g, i) => (
              <li key={i} className="unknown">
                {g.gate}: {g.detail || g.rule}
              </li>
            ))}
          </ul>
        </>
      )}
      {passed.length > 0 && (
        <>
          <p className="muted">
            <strong>Rules satisfied</strong>
          </p>
          <ul>
            {passed.map((g, i) => (
              <li key={i} className="ok">
                {g.gate}: {g.evidence}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
