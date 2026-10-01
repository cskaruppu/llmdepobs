import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type Deployment, type Model, type Plan } from "../api";
import { ErrorBox, PageHead, useApi, useCan, useMe } from "../components";
import { CandidateTable } from "./placement";

const NAME_RE = /^[a-z0-9]([-a-z0-9]{0,38}[a-z0-9])?$/;

export default function NewDeployment() {
  const [params] = useSearchParams();
  const me = useMe();
  const canDeploy = useCan("engineer");
  const curated = useApi<{ models: Model[] }>("/catalog/models?source=curated");

  const [modelId, setModelId] = useState(params.get("model") ?? "ibm-granite/granite-3.3-8b-instruct");
  const [name, setName] = useState(() => (params.get("model") ?? "granite-3.3-8b-instruct").split("/").pop()!.toLowerCase().replace(/[^a-z0-9-]/g, "-").slice(0, 40).replace(/-+$/, ""));
  const [team, setTeam] = useState("");
  const [precision, setPrecision] = useState(params.get("precision") ?? "bf16");
  const [context, setContext] = useState(Number(params.get("context") ?? 8192));
  const [users, setUsers] = useState(Number(params.get("users") ?? 16));
  const [minReplicas, setMinReplicas] = useState(1);
  const [maxReplicas, setMaxReplicas] = useState(1);
  const [gpuProduct, setGpuProduct] = useState("");
  const [region, setRegion] = useState("");
  const [storageUri, setStorageUri] = useState("");

  const [plan, setPlan] = useState<Plan | null>(null);
  const [cluster, setCluster] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Deployment | null>(null);

  const required_labels = useMemo(() => (region ? { region } : {}), [region]);
  const sizing = { model_id: modelId, precision, max_model_len: context, concurrency: users, min_replicas: minReplicas,
    gpu_product: gpuProduct || null, required_labels };
  const nameError = name && !NAME_RE.test(name) ? "Lowercase letters, digits and dashes, up to 40 characters" : null;
  const teamError = team && !NAME_RE.test(team) ? "Lowercase letters, digits and dashes, up to 40 characters" : null;
  const step = result ? 4 : cluster ? 3 : plan ? 2 : 1;

  const checkPlacement = async () => {
    setBusy(true); setError(null); setCluster(null);
    try {
      const p = await api.post<Plan>("/deployments/plan", sizing);
      setPlan(p);
      setCluster(p.candidates.find((c) => c.recommended)?.cluster ?? null);
    } catch (e) { setError((e as Error).message); setPlan(null); } finally { setBusy(false); }
  };

  const deploy = async () => {
    if (!cluster) return;
    setBusy(true); setError(null);
    try {
      setResult(await api.post<Deployment>("/deployments", {
        ...sizing, name, team, cluster, max_replicas: Math.max(maxReplicas, minReplicas), storage_uri: storageUri || null,
      }));
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };

  const chosen = plan?.candidates.find((c) => c.cluster === cluster);
  const namespace = team ? `team-${team}` : "team-<team>";

  if (!canDeploy) return <div className="notice">Deploying models needs the engineer or admin role.</div>;

  return (
    <>
      <PageHead crumbs={["Home", "Deployments", "New deployment"]} title={`New deployment${name ? ` · ${name}` : ""}`}
        subtitle={me?.delivery_mode === "gitops" ? "Manifests are committed to GitLab and applied by Argo CD on the chosen cluster."
          : "Manifests are applied directly to the hub cluster. Enable GitOps delivery to deploy to other clusters."} />
      <div className="stepper">
        {[["Model and sizing", "what to run"], ["Placement", "where it runs"], ["Review", "confirm"], ["Delivered", "applied or committed"]]
          .map(([title, sub], i) => (
            <div key={title} className={step > i + 1 ? "done" : step === i + 1 ? "current" : ""}>
              <i>{step > i + 1 ? "✓" : i + 1}</i><span>{title}<small>{sub}</small></span>
            </div>))}
      </div>

      {result ? (
        <section className="panel">
          <div className="success">
            {result.delivery === "gitops"
              ? <>Committed <code>{result.git_commit?.slice(0, 8)}</code> to GitLab. Argo CD applies it on {result.cluster} at its next sync.</>
              : <>Applied to {result.cluster}. The endpoint becomes ready once the model has downloaded and vLLM has started.</>}
          </div>
          <div className="actions">
            <Link className="btn primary" to="/deployments">Go to deployments</Link>
            <button className="btn" onClick={() => { setResult(null); setPlan(null); setCluster(null); }}>Deploy another</button>
          </div>
        </section>
      ) : (
        <div className="split">
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <section className="panel">
              <div className="panel-h"><h2>Model and sizing</h2></div>
              <div className="form-grid">
                <label className="field">Model
                  <input list="curated-models" value={modelId} onChange={(e) => { setModelId(e.target.value); setPlan(null); }} />
                  <datalist id="curated-models">{(curated.data?.models ?? []).map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}</datalist>
                  <small>Hugging Face ID or a catalog entry</small></label>
                <label className="field">Deployment name
                  <input value={name} onChange={(e) => setName(e.target.value)} />
                  {nameError ? <small style={{ color: "var(--bad)" }}>{nameError}</small> : <small>Also the served model name</small>}</label>
                <label className="field">Team
                  <input value={team} placeholder="legal" onChange={(e) => setTeam(e.target.value)} />
                  {teamError ? <small style={{ color: "var(--bad)" }}>{teamError}</small> : <small>Namespace: {namespace}</small>}</label>
                <label className="field">Precision
                  <select value={precision} onChange={(e) => { setPrecision(e.target.value); setPlan(null); }}>
                    <option value="bf16">BF16</option><option value="fp8">FP8</option><option value="int4">INT4 (pre-quantised)</option>
                  </select></label>
                <label className="field">Max context (tokens)
                  <input type="number" min={512} step={1024} value={context} onChange={(e) => { setContext(Number(e.target.value)); setPlan(null); }} /></label>
                <label className="field">Concurrent requests
                  <input type="number" min={1} value={users} onChange={(e) => { setUsers(Number(e.target.value)); setPlan(null); }} /></label>
                <label className="field">Replicas (min)
                  <input type="number" min={1} value={minReplicas} onChange={(e) => { setMinReplicas(Number(e.target.value)); setPlan(null); }} /></label>
                <label className="field">Replicas (max)
                  <input type="number" min={1} value={maxReplicas} onChange={(e) => setMaxReplicas(Number(e.target.value))} /></label>
                <label className="field">Preferred GPU
                  <input value={gpuProduct} placeholder="any, or e.g. H100" onChange={(e) => { setGpuProduct(e.target.value); setPlan(null); }} /></label>
                <label className="field">Region label
                  <input value={region} placeholder="any, or e.g. eu" onChange={(e) => { setRegion(e.target.value); setPlan(null); }} />
                  <small>Only clusters labelled region=…</small></label>
                <label className="field">Storage URI
                  <input value={storageUri} placeholder={`hf://${modelId}`} onChange={(e) => setStorageUri(e.target.value)} />
                  <small>hf://, oci:// (modelcar), s3:// or pvc://</small></label>
              </div>
              <div className="actions">
                <button className="btn secondary" disabled={busy || !modelId} onClick={checkPlacement}>{busy && !plan ? "Checking…" : "Check placement"}</button>
              </div>
            </section>
            <ErrorBox error={error} />
            {plan && (
              <section className="panel">
                <div className="panel-h"><h2>Candidate clusters</h2><span>labels, live GPU capacity and memory fit</span></div>
                <CandidateTable candidates={plan.candidates} selected={cluster} onSelect={setCluster} />
              </section>
            )}
          </div>
          <aside className="summary">
            <h2>Summary</h2>
            <dl>
              <dt>Model</dt><dd>{modelId}</dd>
              <dt>Cluster</dt><dd>{cluster ?? "check placement first"}</dd>
              <dt>Per replica</dt><dd>{chosen?.tensor_parallel ? `${chosen.tensor_parallel}× ${chosen.gpu_product}` : "—"}</dd>
              <dt>Memory</dt><dd>{chosen?.estimate ? `~${chosen.estimate.total_gb} GB (weights ${chosen.estimate.weights_gb} GB)` : "—"}</dd>
              <dt>Replicas</dt><dd>{minReplicas} → {Math.max(maxReplicas, minReplicas)}</dd>
              <dt>Namespace</dt><dd>{namespace}</dd>
              <dt>Delivery</dt><dd>{me?.delivery_mode === "gitops" ? "GitLab commit, applied by Argo CD" : "Direct apply to hub"}</dd>
              {me?.delivery_mode === "gitops" && cluster && team && (
                <><dt>Git path</dt><dd><code>deployments/{cluster}/{team}/{name}/</code></dd></>
              )}
            </dl>
            <button className="btn primary" disabled={busy || !cluster || !name || !team || !!nameError || !!teamError}
              onClick={deploy} style={{ justifyContent: "center" }}>
              {busy && plan ? "Deploying…" : "Deploy"}
            </button>
            {!team && <small style={{ color: "var(--navy-dim)" }}>Enter a team to continue.</small>}
          </aside>
        </div>
      )}
    </>
  );
}
