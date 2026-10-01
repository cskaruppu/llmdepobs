export type Role = "viewer" | "engineer" | "admin";

export interface Me {
  name: string;
  role: Role;
  groups: string[];
  hub: string;
  delivery_mode: "direct" | "gitops";
  version: string;
}

export interface GpuNode {
  name: string;
  gpu_product: string;
  gpu_memory_mib: number;
  gpu_capacity: number;
  gpu_allocatable: number;
  gpu_used: number;
  mig_strategy: string | null;
  driver_version: string | null;
  cuda_version: string | null;
  pool: string | null;
  ready: boolean;
  schedulable: boolean;
  cluster?: string;
  cluster_status?: string;
}

export interface Endpoint {
  name: string;
  namespace: string;
  ready: boolean;
  url: string | null;
  runtime: string | null;
  storage_uri: string | null;
  gpus: number;
  min_replicas: number | null;
  max_replicas: number | null;
  managed: boolean;
  team: string | null;
  cluster?: string;
}

export type ClusterStatus = "active" | "pending" | "offline" | "managed" | "unreachable";

export interface Cluster {
  name: string;
  source: "hub" | "acm" | "agent";
  status: ClusterStatus;
  type: "hub" | "gpu" | "cpu" | "burst" | "unknown";
  approved: boolean;
  labels: Record<string, string>;
  region: string | null;
  facts: Record<string, string | number | null>;
  gpu_total: number;
  gpu_used: number;
  gpus: { product: string; count: number; memory_gb: number | null }[];
  nodes: GpuNode[];
  endpoints: Endpoint[];
  runtimes: { name: string; display_name: string; image: string | null; formats: string[] }[];
  agent: { installed: boolean; version: string | null; last_seen: string | null; seconds_ago: number | null };
  acm: { managed: boolean; available: boolean | null; joined: boolean | null; openshift_version: string | null; api_url: string | null };
}

export interface Alert {
  severity: "critical" | "warning" | "info";
  title: string;
  detail: string;
}

export interface Overview {
  kpis: {
    clusters: number;
    clusters_healthy: number;
    gpus_total: number;
    gpus_used: number;
    gpu_nodes_not_ready: number;
    endpoints: number;
    endpoints_ready: number;
    deployments: number;
  };
  clusters: Cluster[];
  endpoints: Endpoint[];
  alerts: Alert[];
}

export interface Model {
  id: string;
  name: string;
  provider?: string | null;
  source: string;
  deployable: boolean;
  external?: boolean;
  gated?: boolean;
  license?: string | null;
  task?: string | null;
  context?: number | null;
  params_b?: number;
  downloads?: number;
  storage_uri?: string | null;
  description?: string | null;
}

export interface Estimate {
  weights_gb: number;
  kv_cache_gb: number | null;
  total_gb: number;
  gpu_memory_gb: number;
  tensor_parallel: number | null;
  fits: boolean;
}

export interface Candidate {
  cluster: string;
  type: string;
  region: string | null;
  result: "fits" | "fits_with_changes" | "blocked" | "not_eligible";
  reason: string;
  gpu_product: string | null;
  tensor_parallel: number | null;
  gpus_free: number;
  replicas_possible: number;
  estimate: Estimate | null;
  recommended: boolean;
}

export interface Plan {
  model_id: string;
  shape: { params_b: number; layers: number | null; kv_heads: number | null; head_dim: number | null };
  candidates: Candidate[];
}

export interface Deployment {
  id: string;
  name: string;
  team: string;
  namespace: string;
  cluster: string;
  model_id: string;
  storage_uri: string;
  precision: string;
  gpus_per_replica: number;
  gpu_product: string | null;
  min_replicas: number;
  max_replicas: number;
  max_model_len: number;
  delivery: "direct" | "gitops";
  status: string;
  message: string;
  git_commit: string | null;
  git_paths: string[];
  created_by: string;
  created_at: string;
  live: Endpoint | null;
  manifests?: Record<string, string>;
}

export interface AuditEvent {
  id: number;
  at: string;
  actor: string;
  action: string;
  target: string;
  detail: Record<string, unknown>;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const data = await response.json();
      if (typeof data.detail === "string") message = data.detail;
      else if (Array.isArray(data.detail)) message = data.detail.map((d: { msg: string }) => d.msg).join("; ");
    } catch {
      /* keep the status text */
    }
    throw new ApiError(response.status, message);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  del: (path: string) => request<void>("DELETE", path),
};
