import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, type Cluster } from "../api";
import {
  Bar, ClusterStatusPill, CopyBlock, Empty, ErrorBox, Loading, Modal, PageHead, Pill, TypeBadge, ago, useApi, useCan,
} from "../components";

interface ConnectInfo { agent_url: string; registration_enabled: boolean; hub: string; script: string; manual: string }
interface Discovered { available: boolean; clusters: { name: string; api_url: string; openshift_version: string; cloud_provider: string; type: string }[] }

function Dot({ ok, title }: { ok: boolean | null; title: string }) {
  return <span className={`dot ${ok === null ? "off" : ok ? "ok" : "bad"}`} title={title} />;
}

function AgentDot({ c }: { c: Cluster }) {
  const tone = c.status === "active" ? "ok" : c.status === "pending" ? "warn" : "bad";
  return <span className={`dot ${tone}`} title={`Agent: ${c.status}`} />;
}

function AddClusterDialog({ onClose }: { onClose: () => void }) {
  const [tab, setTab] = useState<"script" | "manual" | "discovered">("script");
  const info = useApi<ConnectInfo>("/clusters/connect-info");
  const discovered = useApi<Discovered>(tab === "discovered" ? "/clusters/discovered" : null);
  return (
    <Modal title="Add a cluster" onClose={onClose}>
      <p className="muted" style={{ margin: 0 }}>
        A cluster joins in two parts: RHACM manages it (policies, GitOps), and the Tensorhelm agent reports its GPUs and
        endpoints. Both connect out to the hub; nothing on the new cluster needs to accept inbound traffic.
      </p>
      <div className="chips">
        <button className={`chip ${tab === "script" ? "on" : ""}`} onClick={() => setTab("script")}>One script</button>
        <button className={`chip ${tab === "manual" ? "on" : ""}`} onClick={() => setTab("manual")}>Agent only</button>
        <button className={`chip ${tab === "discovered" ? "on" : ""}`} onClick={() => setTab("discovered")}>Discovered by RHACM</button>
      </div>
      <ErrorBox error={info.error} />
      {info.data && !info.data.registration_enabled && (
        <div className="error">Agent registration is turned off: the hub has no bootstrap token. Re-run install.sh to create one.</div>
      )}
      {tab === "script" && info.data && (
        <>
          <ol className="steps">
            <li>Log in to the hub and to the new cluster with <code>oc</code>; note the new cluster's kube context name.</li>
            <li>From the repository root, run the command below. It imports the cluster into RHACM (when RHACM is installed),
              labels it, and installs the agent.</li>
            <li>Come back here and approve the cluster when it shows <b>Waiting for approval</b>.</li>
          </ol>
          <CopyBlock text={`${info.data.script.replaceAll(" --", " \\\n  --")} \\\n  --label tensorhelm.io/role=serving --label region=eu`} />
        </>
      )}
      {tab === "manual" && info.data && (
        <>
          <p style={{ margin: 0 }}>Install only the agent, for a cluster RHACM already manages or one you do not want in RHACM.
            Run against the new cluster:</p>
          <CopyBlock text={info.data.manual} />
        </>
      )}
      {tab === "discovered" && (
        discovered.loading ? <Loading /> : !discovered.data?.available ? (
          <Empty>RHACM is not reachable from the hub API, so no discovered clusters can be listed.</Empty>
        ) : discovered.data.clusters.length === 0 ? (
          <Empty>RHACM has no discovered clusters waiting to be imported. Configure cluster discovery in the RHACM console
            (Infrastructure → Clusters → Discovered clusters) to list clusters from console.redhat.com.</Empty>
        ) : (
          <div className="table-wrap"><table>
            <thead><tr><th>Cluster</th><th>Type</th><th>OpenShift</th><th>Provider</th><th>API</th></tr></thead>
            <tbody>{discovered.data.clusters.map((d) => (
              <tr key={d.name}><td><b>{d.name}</b></td><td>{d.type}</td><td>{d.openshift_version}</td>
                <td>{d.cloud_provider}</td><td className="mono">{d.api_url}</td></tr>))}
            </tbody>
          </table></div>
        )
      )}
    </Modal>
  );
}

function ClusterDrawer({ c, onChanged }: { c: Cluster; onChanged: () => void }) {
  const isAdmin = useCan("admin");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await fn(); onChanged(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const labels = Object.entries(c.labels);
  return (
    <aside className="drawer">
      <section>
        <div className="panel-h"><h2>{c.name}</h2><ClusterStatusPill status={c.status} /></div>
        <div className="muted" style={{ fontSize: 12.5 }}>
          {[c.region, c.facts.platform, c.facts.openshift_version && `OpenShift ${c.facts.openshift_version}`,
            c.facts.rhoai_version && `OpenShift AI ${c.facts.rhoai_version}`].filter(Boolean).join(" · ") || "No facts reported yet"}
        </div>
        {labels.length > 0 && <div className="tags">{labels.map(([k, v]) => <span key={k} className="tag">{k}={v}</span>)}</div>}
        <ErrorBox error={error} />
        {isAdmin && c.status === "pending" && (
          <>
            <div className="notice">A new agent registered as <b>{c.name}</b>
              {c.facts.api_url ? <> from <code>{String(c.facts.api_url)}</code></> : null}. Approve it only if you expected it.</div>
            <div className="actions">
              <button className="btn primary" disabled={busy} onClick={() => act(() => api.post(`/clusters/${c.name}/approve`))}>Approve cluster</button>
              <button className="btn danger" disabled={busy} onClick={() => act(() => api.del(`/clusters/${c.name}`))}>Reject</button>
            </div>
          </>
        )}
      </section>
      <section>
        <span className="eyebrow">Connections · all outbound from the cluster</span>
        {c.source === "hub" ? (
          <div className="conn"><span className="dot ok" /><div>Inventoried by the hub API directly<small>no agent needed on the hub</small></div><span /></div>
        ) : (
          <>
            <div className="conn"><Dot ok={c.acm.managed ? !!c.acm.available : null} title="RHACM" />
              <div>RHACM klusterlet → hub<small>{c.acm.managed ? (c.acm.available ? "available" : "not available") : "not managed by RHACM"}</small></div>
              <span className="tag">{c.acm.managed ? "RHACM" : "—"}</span></div>
            <div className="conn">{c.agent.installed ? <AgentDot c={c} /> : <Dot ok={null} title="Agent" />}
              <div>Tensorhelm agent → hub API<small>{c.agent.installed ? `last report ${ago(c.agent.seconds_ago)}` : "not installed"}</small></div>
              <span className="tag">{c.agent.version ?? "—"}</span></div>
          </>
        )}
      </section>
      <section>
        <span className="eyebrow">Capacity</span>
        <div className="row"><span>GPUs in use</span><b>{c.gpu_used} / {c.gpu_total}</b></div>
        {c.gpu_total > 0 && <Bar value={c.gpu_used} max={c.gpu_total} />}
        {c.gpus.map((g) => <div key={g.product} className="row"><span>{g.product}</span><b>{g.count}{g.memory_gb ? ` × ${g.memory_gb} GB` : ""}</b></div>)}
        <div className="row"><span>Nodes · CPU cores</span><b>{c.facts.node_count ?? "—"} · {c.facts.cpu_allocatable ?? "—"}</b></div>
        <div className="row"><span>Model endpoints</span><b>{c.endpoints.length}</b></div>
        <div className="row"><span>vLLM runtimes</span><b>{c.runtimes.filter((r) => r.name.includes("vllm")).length || "none"}</b></div>
      </section>
      {isAdmin && c.source !== "hub" && c.status !== "pending" && c.agent.installed && (
        <section>
          <span className="eyebrow">Agent</span>
          <p className="muted" style={{ margin: 0, fontSize: 12 }}>Removing forgets this agent. It can register again with the bootstrap token.</p>
          <div><button className="btn danger small" disabled={busy} onClick={() => act(() => api.del(`/clusters/${c.name}`))}>Remove agent registration</button></div>
        </section>
      )}
    </aside>
  );
}

export default function Clusters() {
  const [params, setParams] = useSearchParams();
  const { data, error, loading, reload } = useApi<Cluster[]>("/clusters", 20000);
  const isAdmin = useCan("admin");
  const selectedName = params.get("name") ?? data?.[0]?.name;
  const selected = data?.find((c) => c.name === selectedName);
  const pending = data?.filter((c) => c.status === "pending").length ?? 0;

  return (
    <>
      <PageHead crumbs={["Home", "Operate", "Clusters"]} title="Clusters and agents"
        subtitle="Every managed cluster connects out to the hub. RHACM-managed clusters appear automatically; agents add GPU and endpoint detail.">
        {isAdmin && <button className="btn primary" onClick={() => setParams({ ...Object.fromEntries(params), add: "1" })}>Add cluster</button>}
      </PageHead>
      {pending > 0 && <div className="notice">{pending} cluster(s) waiting for approval.</div>}
      <ErrorBox error={error} />
      {loading && !data ? <Loading /> : (
        <div className="split">
          <section className="panel" style={{ padding: "4px 6px" }}>
            <div className="table-wrap"><table>
              <thead><tr><th>Cluster</th><th>Type</th><th>OpenShift · AI</th><th>RHACM</th><th>Agent</th><th>GPUs</th><th>Status</th></tr></thead>
              <tbody>{(data ?? []).map((c) => (
                <tr key={c.name} className={`clickable ${c.name === selectedName ? "selected" : ""}`}
                  onClick={() => setParams({ name: c.name })}>
                  <td><b>{c.name}</b>{c.region && <div className="muted" style={{ fontSize: 12 }}>{c.region}</div>}</td>
                  <td><TypeBadge type={c.type} /></td>
                  <td className="mono nowrap">{c.facts.openshift_version ?? c.acm.openshift_version ?? "—"} · {c.facts.rhoai_version ?? "—"}</td>
                  <td>{c.source === "hub" ? <span className="muted">hub</span> : <Dot ok={c.acm.managed ? !!c.acm.available : null} title="RHACM" />}</td>
                  <td className="nowrap">{c.source === "hub" ? <span className="muted">built in</span> :
                    c.agent.installed ? <><AgentDot c={c} /> <span className="mono">{c.agent.version}</span></> :
                    <span className="muted">not installed</span>}</td>
                  <td className="nowrap">{c.gpu_total ? `${c.gpu_used} / ${c.gpu_total}` : "—"}</td>
                  <td><ClusterStatusPill status={c.status} /></td>
                </tr>))}
              </tbody>
            </table></div>
            {data?.length === 1 && (
              <Empty>Only the hub is registered. Use <b>Add cluster</b> to connect GPU or CPU clusters.</Empty>
            )}
          </section>
          {selected ? <ClusterDrawer c={selected} onChanged={reload} /> : <Pill tone="neutral">Select a cluster</Pill>}
        </div>
      )}
      {params.get("add") && isAdmin && (
        <AddClusterDialog onClose={() => { params.delete("add"); setParams(params); }} />
      )}
    </>
  );
}
