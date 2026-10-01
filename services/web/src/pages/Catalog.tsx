import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Model, type Plan } from "../api";
import { Empty, ErrorBox, Loading, PageHead, Pill, useApi, useCan } from "../components";
import { CandidateTable } from "./placement";

interface Source { id: string; name: string; enabled: boolean }

function FitCheck({ model }: { model: Model }) {
  const [precision, setPrecision] = useState("bf16");
  const [context, setContext] = useState(8192);
  const [users, setUsers] = useState(16);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const canDeploy = useCan("engineer");

  useEffect(() => { setPlan(null); setError(null); }, [model.id]);
  const run = async () => {
    setBusy(true); setError(null);
    try {
      setPlan(await api.post<Plan>("/deployments/plan", { model_id: model.id, precision, max_model_len: context, concurrency: users }));
    } catch (e) { setError((e as Error).message); setPlan(null); } finally { setBusy(false); }
  };
  const query = new URLSearchParams({ model: model.id, precision, context: String(context), users: String(users) });

  return (
    <section className="panel">
      <div className="panel-h"><h2>Fit check · {model.name}</h2><span>will it run on the GPUs we own?</span></div>
      <div className="form-grid">
        <label className="field">Precision
          <select value={precision} onChange={(e) => setPrecision(e.target.value)}>
            <option value="bf16">BF16 (full)</option><option value="fp8">FP8 (half the memory)</option>
            <option value="int4">INT4 (needs a pre-quantised model)</option>
          </select></label>
        <label className="field">Max context (tokens)
          <input type="number" min={512} step={1024} value={context} onChange={(e) => setContext(Number(e.target.value))} /></label>
        <label className="field">Concurrent requests
          <input type="number" min={1} value={users} onChange={(e) => setUsers(Number(e.target.value))} /></label>
      </div>
      <div className="actions">
        <button className="btn secondary" onClick={run} disabled={busy}>{busy ? "Checking…" : "Check all clusters"}</button>
        {canDeploy && model.deployable && <Link className="btn primary" to={`/deployments/new?${query}`}>Deploy this model</Link>}
      </div>
      <ErrorBox error={error} />
      {plan && (
        <>
          <div className="muted" style={{ fontSize: 12 }}>
            {plan.shape.params_b}B parameters · estimate covers weights, KV cache for {users} requests at the full
            context, and runtime overhead.
          </div>
          <CandidateTable candidates={plan.candidates} />
        </>
      )}
    </section>
  );
}

export default function Catalog() {
  const sources = useApi<Source[]>("/catalog/sources");
  const [source, setSource] = useState("curated");
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState("");
  const [selected, setSelected] = useState<Model | null>(null);
  const models = useApi<{ models: Model[]; warning: string | null }>(
    `/catalog/models?source=${source}&q=${encodeURIComponent(submitted)}`,
  );

  return (
    <>
      <PageHead crumbs={["Home", "Operate", "Model catalog"]} title="Model catalog"
        subtitle="One catalog across the OpenShift AI Model Registry, Hugging Face and hosted APIs, with a fit check against your GPUs." />
      <div className="chips">
        {(sources.data ?? []).map((s) => (
          <button key={s.id} className={`chip ${source === s.id ? "on" : ""}`} disabled={!s.enabled}
            title={s.enabled ? undefined : "Not configured on the hub"} onClick={() => { setSource(s.id); setSelected(null); }}>
            {s.name}
          </button>))}
      </div>
      <form className="search" onSubmit={(e) => { e.preventDefault(); setSubmitted(query); }}>
        <input id="catalog-search" placeholder={source === "huggingface" ? "Search Hugging Face, e.g. granite instruct" : "Filter models"}
          value={query} onChange={(e) => setQuery(e.target.value)} />
        <button className="btn" type="submit">Search</button>
      </form>
      {models.data?.warning && <div className="error">{models.data.warning}</div>}
      <ErrorBox error={models.error} />
      {selected && <FitCheck model={selected} />}
      {models.loading ? <Loading /> : (models.data?.models.length ?? 0) === 0 ? <Empty>No models match.</Empty> : (
        <div className="mcards">
          {models.data!.models.map((m) => (
            <article key={m.id} className={`mcard ${selected?.id === m.id ? "selected" : ""}`}>
              <div className="panel-h" style={{ alignItems: "flex-start" }}>
                <div><h3>{m.name}</h3><div className="src">{m.id}</div></div>
                {m.external ? <Pill tone="neutral">External API</Pill> : m.gated ? <Pill tone="warn">Gated</Pill> :
                  m.source === "curated" || m.source === "registry" ? <Pill tone="ok">Approved</Pill> : null}
              </div>
              <div className="tags">
                {m.params_b && <span className="tag">{m.params_b}B</span>}
                {m.context && <span className="tag">{Math.round(m.context / 1024)}k ctx</span>}
                {m.license && <span className="tag">{m.license}</span>}
                {m.task && <span className="tag">{m.task}</span>}
                {m.downloads !== undefined && <span className="tag">{m.downloads.toLocaleString()} downloads</span>}
              </div>
              {m.description && <div className="muted" style={{ fontSize: 12.5 }}>{m.description}</div>}
              <div className="actions" style={{ marginTop: "auto" }}>
                {m.deployable ? (
                  <button className="btn small secondary" onClick={() => setSelected(m)}>Fit check</button>
                ) : (
                  <span className="muted" style={{ fontSize: 12 }}>Hosted API; routed through the gateway, not deployed on your GPUs.</span>
                )}
              </div>
            </article>
          ))}
        </div>
      )}
    </>
  );
}
