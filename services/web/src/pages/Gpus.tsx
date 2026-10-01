import { useMemo, useState } from "react";
import type { GpuNode } from "../api";
import { Bar, Empty, ErrorBox, Loading, PageHead, Pill, useApi } from "../components";

export default function Gpus() {
  const { data, error, loading } = useApi<GpuNode[]>("/gpus", 30000);
  const [cluster, setCluster] = useState("all");
  const clusters = useMemo(() => Array.from(new Set((data ?? []).map((n) => n.cluster!))), [data]);
  const rows = (data ?? []).filter((n) => cluster === "all" || n.cluster === cluster);
  const total = rows.reduce((s, n) => s + n.gpu_allocatable, 0);
  const used = rows.reduce((s, n) => s + n.gpu_used, 0);

  return (
    <>
      <PageHead crumbs={["Home", "Operate", "GPU fleet"]} title="GPU fleet"
        subtitle="Detected from NVIDIA GPU Operator and Node Feature Discovery labels on every cluster. No manual entry." />
      <div className="chips">
        <button className={`chip ${cluster === "all" ? "on" : ""}`} onClick={() => setCluster("all")}>All clusters</button>
        {clusters.map((c) => <button key={c} className={`chip ${cluster === c ? "on" : ""}`} onClick={() => setCluster(c)}>{c}</button>)}
      </div>
      <ErrorBox error={error} />
      <div className="kpis">
        <div className="kpi"><label>GPU nodes</label><strong>{rows.length}</strong><small>{rows.filter((n) => !n.ready || !n.schedulable).length} not schedulable</small></div>
        <div className="kpi"><label>GPUs</label><strong>{total}</strong><small>allocatable</small></div>
        <div className="kpi"><label>In use</label><strong>{used}</strong><small>{total ? Math.round((used / total) * 100) : 0}% allocated to pods</small></div>
        <div className="kpi"><label>Free</label><strong>{total - used}</strong><small>available for new replicas</small></div>
      </div>
      <section className="panel">
        {loading && !data ? <Loading /> : rows.length === 0 ? (
          <Empty>No GPU nodes found. Check that the NVIDIA GPU Operator is running and nodes carry the
            <code> nvidia.com/gpu.present=true</code> label.</Empty>
        ) : (
          <div className="table-wrap"><table>
            <thead><tr><th>Node</th><th>Cluster</th><th>GPU</th><th>Memory</th><th>In use</th><th></th><th>MIG</th><th>Driver · CUDA</th><th>Pool</th><th>State</th></tr></thead>
            <tbody>{rows.map((n) => (
              <tr key={`${n.cluster}/${n.name}`}>
                <td className="mono">{n.name}</td><td>{n.cluster}</td><td>{n.gpu_product}</td>
                <td className="nowrap">{n.gpu_memory_mib ? `${Math.round(n.gpu_memory_mib / 1024)} GiB` : "—"}</td>
                <td className="nowrap">{n.gpu_used} / {n.gpu_allocatable}</td>
                <td style={{ minWidth: 80 }}><Bar value={n.gpu_used} max={n.gpu_allocatable} /></td>
                <td>{n.mig_strategy ? <span className="tag">{n.mig_strategy}</span> : "—"}</td>
                <td className="mono nowrap">{n.driver_version ?? "—"} · {n.cuda_version ?? "—"}</td>
                <td>{n.pool ? <span className="tag">{n.pool}</span> : <span className="muted">—</span>}</td>
                <td>{!n.ready ? <Pill tone="bad">Not ready</Pill> : !n.schedulable ? <Pill tone="warn">Cordoned</Pill> : <Pill tone="ok">Ready</Pill>}</td>
              </tr>))}
            </tbody>
          </table></div>
        )}
      </section>
    </>
  );
}
