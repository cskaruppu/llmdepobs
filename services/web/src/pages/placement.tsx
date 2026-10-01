import type { Candidate } from "../api";
import { Pill } from "../components";

const RESULT: Record<Candidate["result"], [string, "ok" | "warn" | "bad" | "neutral"]> = {
  fits: ["Fits", "ok"],
  fits_with_changes: ["Fits with changes", "warn"],
  blocked: ["Blocked", "bad"],
  not_eligible: ["Not eligible", "neutral"],
};

export function ResultPill({ result }: { result: Candidate["result"] }) {
  const [label, tone] = RESULT[result];
  return <Pill tone={tone}>{label}</Pill>;
}

export function CandidateTable({ candidates, selected, onSelect }: {
  candidates: Candidate[]; selected?: string | null; onSelect?: (cluster: string) => void;
}) {
  return (
    <div className="table-wrap"><table>
      <thead><tr>{onSelect && <th />}<th>Cluster</th><th>GPU per replica</th><th>GPUs free</th><th>Memory needed</th><th>Result</th></tr></thead>
      <tbody>{candidates.flatMap((c) => {
        const selectable = !!onSelect && (c.result === "fits" || c.result === "fits_with_changes");
        const isSelected = selected === c.cluster;
        const rowProps = {
          className: `${selectable ? "clickable" : ""} ${isSelected ? "selected" : ""}`,
          onClick: selectable ? () => onSelect!(c.cluster) : undefined,
        };
        return [
          <tr key={c.cluster} {...rowProps} style={{ borderBottom: 0 }}>
            {onSelect && <td style={{ borderBottom: 0 }}><span className={`radio ${isSelected ? "on" : ""}`} style={{ opacity: selectable ? 1 : 0.35 }} /></td>}
            <td className="nowrap" style={{ borderBottom: 0 }}><b>{c.cluster}</b>{c.recommended && <span className="rec">Recommended</span>}</td>
            <td className="nowrap" style={{ borderBottom: 0 }}>{c.tensor_parallel ? `${c.tensor_parallel}× ${c.gpu_product}` : "—"}</td>
            <td className="mono" style={{ borderBottom: 0 }}>{c.gpus_free}</td>
            <td className="mono nowrap" style={{ borderBottom: 0 }}>{c.estimate ? `~${c.estimate.total_gb} GB` : "—"}</td>
            <td style={{ borderBottom: 0 }}><ResultPill result={c.result} /></td>
          </tr>,
          <tr key={`${c.cluster}-why`} {...rowProps}>
            {onSelect && <td />}
            <td colSpan={5} className="sub">{c.reason}</td>
          </tr>,
        ];
      })}</tbody>
    </table></div>
  );
}
