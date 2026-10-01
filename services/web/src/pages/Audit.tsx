import type { AuditEvent } from "../api";
import { Empty, ErrorBox, Loading, PageHead, useApi, when } from "../components";

export default function Audit() {
  const { data, error, loading } = useApi<AuditEvent[]>("/audit?limit=300", 30000);
  return (
    <>
      <PageHead crumbs={["Home", "Govern", "Audit log"]} title="Audit log"
        subtitle="Every change made through Tensorhelm, newest first. Stored in the hub database." />
      <ErrorBox error={error} />
      <section className="panel">
        {loading && !data ? <Loading /> : !data?.length ? <Empty>No events yet.</Empty> : (
          <div className="table-wrap"><table>
            <thead><tr><th>Time</th><th>Who</th><th>Action</th><th>Target</th><th>Detail</th></tr></thead>
            <tbody>{data.map((e) => (
              <tr key={e.id}>
                <td className="nowrap mono">{when(e.at)}</td><td>{e.actor}</td><td><span className="tag">{e.action}</span></td>
                <td className="mono">{e.target}</td>
                <td className="muted" style={{ fontSize: 12 }}>
                  {Object.entries(e.detail).filter(([, v]) => v !== null && v !== undefined).map(([k, v]) => `${k}: ${String(v)}`).join(" · ")}
                </td>
              </tr>))}
            </tbody>
          </table></div>
        )}
      </section>
    </>
  );
}
