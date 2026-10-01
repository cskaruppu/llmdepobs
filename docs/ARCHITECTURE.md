# Architecture and security

## Components

```
                         ┌──────────────────────────── hub cluster ────────────────────────────┐
 browser ── Route ──────▶│ tensorhelm-web pod                                                   │
 (OpenShift login)       │   oauth-proxy :8443  ──▶  nginx :8080 (portal, /api proxy + secret)   │
                         │                                     │                                │
 spoke agents ── Route ─▶│ tensorhelm-api :8000  ◀─────────────┘     PostgreSQL (StatefulSet)    │
 (/api/v1/agent only)    │   • inventories the hub every 30 s (nodes, pods, InferenceServices)   │
                         │   • mirrors RHACM ManagedClusters                                      │
                         │   • fit check, placement, rendering, delivery                          │
                         │   • direct apply (hub) or GitLab commit ──▶ GitLab ──▶ Argo CD          │
                         └───────────────────────────────────────────────────────────────────────┘
                                         ▲ outbound HTTPS only
                         ┌─────── spoke cluster (GPU or CPU) ───────┐
                         │ tensorhelm-agent (read-only)              │
                         │ OpenShift AI · KServe · vLLM · GPU Operator│
                         │ RHACM klusterlet · Argo CD target          │
                         └──────────────────────────────────────────┘
```

| Component | Code | Image |
| --- | --- | --- |
| API | `services/api/tensorhelm` (FastAPI, SQLAlchemy async, kubernetes-asyncio) | `services/api/Containerfile` |
| Agent | `services/api/tensorhelm/agent.py`. Same package and image as the API; runs `tensorhelm-agent`. | same |
| Portal | `services/web` (React 19, TypeScript, Vite); nginx config in `services/web/nginx.conf` | `services/web/Containerfile` |
| Admin CLI | `services/api/tensorhelm/cli.py` (`tensorhelm-admin`) | same as API |

## How a deployment is made

1. The portal posts the request to `POST /api/v1/deployments/plan`: model, precision, context, concurrency,
   replicas, preferred GPU, required labels.
2. **Fit check** (`fit.py`). The API reads the model's shape from the catalog or Hugging Face `config.json`:
   parameters, layers, KV heads and head size. It then estimates:
   - weights at the chosen precision
   - plus a KV cache for every concurrent request at full context
   - plus 2 GB per GPU of runtime overhead

   It picks the smallest tensor-parallel size (1, 2, 4 or 8) that fits within `gpu-memory-utilization` of
   each GPU. The tensor-parallel size must divide the attention heads.
3. **Placement** (`placement.py`) evaluates every cluster:
   - the cluster's status
   - its labels
   - its GPU products
   - free GPUs per node, since tensor parallelism stays inside one node

   It ranks "fits" before "fits with changes" before "blocked" before "not eligible", then by fewest GPUs per
   replica.
4. **Rendering** (`render.py`) produces three manifests, which validate against the KServe CRD schemas:
   - a Namespace
   - a ServingRuntime copied from the cluster's OpenShift AI vLLM template
   - an InferenceService in RawDeployment mode

   It also sets the vLLM arguments: `--tensor-parallel-size`, `--max-model-len`, `--gpu-memory-utilization`,
   FP8 options, and prefix caching.
5. **Delivery:**
   - **Direct mode:** the API applies the objects to the hub.
   - **GitOps mode:** the API makes one GitLab commit, and Argo CD (an RHACM ApplicationSet, or a single
     Application) syncs `deployments/<cluster>/`.
6. **Live state:** the agent's next report (or the hub's own inventory) brings the InferenceService
   status back to the portal.

## Security model

| Concern | Control |
| --- | --- |
| User authentication | OpenShift OAuth via oauth-proxy. Only users who can `get` the `tensorhelm-web` Service may sign in (RoleBinding to three groups). |
| Trusting the user header | nginx adds a per-install shared secret header. The API rejects user requests without it (constant-time compare). The API Service only accepts traffic from the portal pods and the router (NetworkPolicy). |
| Authorisation | Roles come from OpenShift groups (viewer, engineer, admin), checked on every API call. |
| Agent authentication | A bootstrap token registers a cluster and returns a per-cluster token. The hub stores only its SHA-256. A second registration under the same name is refused. New clusters need admin approval before they take placements. |
| Agent permissions on spokes | Read-only: nodes, pods, InferenceServices, templates, DataScienceClusters, cluster version. Write access only to its own credentials Secret. No inbound traffic (NetworkPolicy). |
| Hub permissions | Read nodes and pods. Create namespaces and KServe objects (direct mode). Read RHACM ManagedClusters and OpenShift groups. No access to Secrets outside its namespace. |
| Secrets | Created by the install script and never stored in templates or Git. Integration tokens live in `tensorhelm-integrations`. |
| Containers | UBI 9 base images, non-root, `allowPrivilegeEscalation: false`, all capabilities dropped. Runs under the `restricted-v2` SCC. |
| Audit | Every registration, approval, removal and deployment is written to the audit log with the actor. |
| Input safety | Names validated as DNS labels; extra vLLM arguments restricted to `--flag[=value]`; manifests built as data, never by string templating. |

## API

All endpoints are under `/api/v1`. Interactive docs are served by the API at `/docs` (inside the cluster:
`oc port-forward -n tensorhelm deploy/tensorhelm-api 8000`).

| Method and path | Role | Purpose |
| --- | --- | --- |
| `GET /me` | any | Current user, role, delivery mode |
| `GET /overview` | any | KPIs, clusters, endpoints, alerts |
| `GET /clusters`, `GET /clusters/{name}` | any | Cluster views |
| `POST /clusters/{name}/approve`, `DELETE /clusters/{name}` | admin | Approve or forget an agent |
| `GET /clusters/discovered`, `GET /clusters/connect-info` | admin | RHACM discovered clusters; agent install commands |
| `GET /gpus` | any | All GPU nodes |
| `GET /catalog/sources`, `GET /catalog/models` | any | Catalog |
| `POST /deployments/plan` | any | Fit check and placement |
| `GET/POST /deployments`, `GET/DELETE /deployments/{id}` | engineer to write | Deployments |
| `GET /audit` | any | Audit log |
| `POST /agent/register`, `POST /agent/report` | agent tokens | Spoke agents |
| `GET /health/live`, `GET /health/ready` | none | Probes |

## Known limits of this version

- The API runs as one replica (the background inventory loop is not leader-elected yet).
- Tables are created on start-up; schema migrations (Alembic) come with the first schema change.
- The fit check is a planning estimate; vLLM shares KV cache, so real usage is usually lower.
- Deployments to CPU-only clusters (OpenVINO, embeddings) are not offered yet; CPU clusters are inventoried
  and shown.
