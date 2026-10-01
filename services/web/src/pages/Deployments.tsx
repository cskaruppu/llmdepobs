import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type Deployment } from "../api";
import { Empty, ErrorBox, Loading, Modal, PageHead, Pill, useApi, useCan, when } from "../components";

function liveStatus(d: Deployment) {
  if (d.live?.ready) return <Pill tone="ok">Ready</Pill>;
  if (d.live) return <Pill tone="warn">Starting</Pill>;
  if (d.status === "committed") return <Pill tone="neutral">Waiting for Argo CD</Pill>;
  if (d.status === "applied") return <Pill tone="neutral">Applied</Pill>;
  return <Pill tone="bad">{d.status}</Pill>;
}

function DetailDialog({ id, onClose, onDeleted }: { id: string; onClose: () => void; onDeleted: () => void }) {
  const { data, error } = useApi<Deployment>(`/deployments/${id}`);
  const canDelete = useCan("engineer");
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const remove = async () => {
    setBusy(true);
    try { await api.del(`/deployments/${id}`); onDeleted(); } catch (e) { setDeleteError((e as Error).message); } finally { setBusy(false); }
  };
  return (
    <Modal title={data ? `${data.name} on ${data.cluster}` : "Deployment"} onClose={onClose}
      footer={canDelete && data ? (confirm ? (
        <><span className="muted">{data.delivery === "gitops" ? "Removes the files from Git; Argo CD deletes the endpoint." : "Deletes the endpoint from the hub."}</span>
          <button className="btn" onClick={() => setConfirm(false)}>Keep it</button>
          <button className="btn danger" disabled={busy} onClick={remove}>Delete deployment</button></>
      ) : <button className="btn danger" onClick={() => setConfirm(true)}>Delete…</button>) : undefined}>
      <ErrorBox error={error || deleteError} />
      {!data ? <Loading /> : (
        <>
          <div className="tags">
            <span className="tag">{data.model_id}</span><span className="tag">{data.precision}</span>
            <span className="tag">{data.gpus_per_replica}× {data.gpu_product ?? "GPU"}</span>
            <span className="tag">context {data.max_model_len}</span><span className="tag">{data.namespace}</span>
          </div>
          <div className="muted" style={{ fontSize: 12.5 }}>{data.message}</div>
          {data.live?.url && <div>Endpoint: <code>{data.live.url}/v1/chat/completions</code></div>}
          {data.git_paths.length > 0 && <div className="mono" style={{ fontSize: 12 }}>{data.git_paths.join("\n")}</div>}
          {Object.entries(data.manifests ?? {}).map(([file, text]) => (
            <details key={file}><summary className="mono">{file}</summary><pre className="code">{text}</pre></details>
          ))}
        </>
      )}
    </Modal>
  );
}

export default function Deployments() {
  const { data, error, loading, reload } = useApi<Deployment[]>("/deployments", 20000);
  const canDeploy = useCan("engineer");
  const [open, setOpen] = useState<string | null>(null);
  return (
    <>
      <PageHead crumbs={["Home", "Operate", "Deployments"]} title="Deployments"
        subtitle="Models deployed through Tensorhelm, with their live state from each cluster.">
        {canDeploy && <Link className="btn primary" to="/deployments/new">New deployment</Link>}
      </PageHead>
      <ErrorBox error={error} />
      <section className="panel">
        {loading && !data ? <Loading /> : !data?.length ? (
          <Empty>No deployments yet. {canDeploy && <Link to="/deployments/new">Deploy your first model</Link>}</Empty>
        ) : (
          <div className="table-wrap"><table>
            <thead><tr><th>Name</th><th>Cluster</th><th>Team</th><th>Model</th><th>GPUs</th><th>Delivery</th><th>Created</th><th>Status</th></tr></thead>
            <tbody>{data.map((d) => (
              <tr key={d.id} className="clickable" onClick={() => setOpen(d.id)}>
                <td><b>{d.name}</b></td><td>{d.cluster}</td><td>{d.team}</td><td className="mono">{d.model_id}</td>
                <td className="nowrap">{d.gpus_per_replica}× · {d.min_replicas}–{d.max_replicas}</td>
                <td>{d.delivery === "gitops" ? <span className="tag">git {d.git_commit?.slice(0, 7)}</span> : <span className="tag">direct</span>}</td>
                <td className="nowrap muted">{when(d.created_at)} · {d.created_by}</td>
                <td>{liveStatus(d)}</td>
              </tr>))}
            </tbody>
          </table></div>
        )}
      </section>
      {open && <DetailDialog id={open} onClose={() => setOpen(null)} onDeleted={() => { setOpen(null); reload(); }} />}
    </>
  );
}
