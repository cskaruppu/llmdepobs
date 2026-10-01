import { Link } from "react-router-dom";
import type { Cluster, Overview as OverviewData } from "../api";
import { Bar, ClusterStatusPill, Empty, ErrorBox, Loading, PageHead, Pill, TypeBadge, ago, useCan } from "../components";

function agentDots(c: Cluster) {
  if (c.source === "hub") return <><span className="dot ok" /> Hub services running</>;
  const tone = c.status === "active" ? "ok" : c.status === "pending" || c.status === "managed" ? "warn" : "bad";
  const text = c.agent.installed ? `Agent ${c.agent.version ?? ""} · ${ago(c.agent.seconds_ago)}` : "Agent not installed";
  return (
    <>
      <span className={`dot ${c.acm.managed ? (c.acm.available ? "ok" : "bad") : "off"}`} title="RHACM" />
      <span className={`dot ${tone}`} title="Tensorhelm agent" />
      {text}
    </>
  );
}

function ClusterCard({ c }: { c: Cluster }) {
  const isHub = c.type === "hub";
  return (
    <Link to={`/clusters?name=${encodeURIComponent(c.name)}`} className={`ccard ${isHub ? "hub" : ""}`}>
      <div className="top"><h3>{c.name}</h3><TypeBadge type={c.type} /></div>
      <div className="muted" style={{ fontSize: 12 }}>
        {[c.region, c.gpus.map((g) => `${g.count}× ${g.product}`).join(", ") || (isHub ? "management only" : null),
          c.facts.rhoai_version ? `OpenShift AI ${c.facts.rhoai_version}` : null].filter(Boolean).join(" · ")}
      </div>
      {c.gpu_total > 0 ? (
        <>
          <div className="row"><span>GPUs in use</span><b>{c.gpu_used} / {c.gpu_total}</b></div>
          <Bar value={c.gpu_used} max={c.gpu_total} />
        </>
      ) : (
        <div className="row"><span>CPU cores</span><b>{c.facts.cpu_allocatable ?? "—"}</b></div>
      )}
      <div className="row"><span>Endpoints</span><b>{c.endpoints.length}</b></div>
      <div className="agentline">{agentDots(c)}</div>
      {!isHub && c.status !== "active" && <div><ClusterStatusPill status={c.status} /></div>}
    </Link>
  );
}

export default function Overview({ data, error }: { data: OverviewData | null; error: string | null }) {
  const canDeploy = useCan("engineer");
  if (!data) return error ? <ErrorBox error={error} /> : <Loading />;
  const k = data.kpis;
  const regions = new Set(data.clusters.map((c) => c.region).filter(Boolean)).size;
  return (
    <>
      <PageHead crumbs={["Home", "Fleet overview"]} title="Fleet overview"
        subtitle={`${k.clusters} clusters${regions ? ` in ${regions} region${regions === 1 ? "" : "s"}` : ""} · refreshes every 30 s`}>
        <Link className="btn secondary" to="/clusters?add=1">Add cluster</Link>
        {canDeploy && <Link className="btn primary" to="/deployments/new">Deploy model</Link>}
      </PageHead>
      <ErrorBox error={error} />
      <div className="kpis">
        <div className="kpi"><label>Clusters</label><strong>{k.clusters}</strong>
          <small>{k.clusters_healthy} healthy{k.clusters - k.clusters_healthy > 0 ? ` · ${k.clusters - k.clusters_healthy} need attention` : ""}</small></div>
        <div className="kpi"><label>GPUs</label><strong>{k.gpus_total}</strong>
          <small>{k.gpu_nodes_not_ready ? `${k.gpu_nodes_not_ready} node(s) not schedulable` : "all nodes schedulable"}</small></div>
        <div className="kpi"><label>GPUs in use</label><strong>{k.gpus_total ? Math.round((k.gpus_used / k.gpus_total) * 100) : 0}%</strong>
          <small>{k.gpus_used} of {k.gpus_total} allocated</small></div>
        <div className="kpi"><label>Endpoints</label><strong>{k.endpoints}</strong><small>{k.endpoints_ready} ready</small></div>
        <div className="kpi"><label>Deployments</label><strong>{k.deployments}</strong><small>made in Tensorhelm</small></div>
      </div>
      <div className="cards">{data.clusters.map((c) => <ClusterCard key={c.name} c={c} />)}</div>
      <div className="grid2">
        <section className="panel">
          <div className="panel-h"><h2>Model endpoints</h2><span>all clusters · not ready first</span></div>
          {data.endpoints.length === 0 ? (
            <Empty>No model endpoints yet. <Link to="/deployments/new">Deploy a model</Link></Empty>
          ) : (
            <div className="table-wrap"><table>
              <thead><tr><th>Endpoint</th><th>Cluster</th><th>Namespace</th><th>GPUs</th><th>Replicas</th><th>Status</th></tr></thead>
              <tbody>{data.endpoints.slice(0, 12).map((e) => (
                <tr key={`${e.cluster}/${e.namespace}/${e.name}`}>
                  <td><b>{e.name}</b></td><td>{e.cluster}</td><td className="mono">{e.namespace}</td>
                  <td>{e.gpus || "—"}</td><td>{e.min_replicas ?? 1}–{e.max_replicas ?? e.min_replicas ?? 1}</td>
                  <td>{e.ready ? <Pill tone="ok">Ready</Pill> : <Pill tone="warn">Not ready</Pill>}</td>
                </tr>))}
              </tbody>
            </table></div>
          )}
        </section>
        <section className="panel">
          <div className="panel-h"><h2>Alerts</h2><span>{data.alerts.length} active</span></div>
          {data.alerts.length === 0 ? <Empty>Nothing needs attention.</Empty> :
            data.alerts.slice(0, 8).map((a, i) => (
              <div key={i} className={`alert ${a.severity}`}><b>{a.title}</b><span>{a.detail}</span></div>
            ))}
        </section>
      </div>
    </>
  );
}
