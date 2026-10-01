#!/usr/bin/env bash
# Installs (or upgrades) the Tensorhelm hub on the OpenShift cluster you are logged in to with `oc`.
# Re-running it is safe: secrets are kept, images are rebuilt and the deployment is updated.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

NAMESPACE=tensorhelm
HUB_NAME=local-cluster
DELIVERY=direct
GITLAB_URL=""
GITLAB_PROJECT=""
GITLAB_BRANCH=main
GITLAB_TOKEN="${GITLAB_TOKEN:-}"
HF_TOKEN="${HF_TOKEN:-}"
MODEL_REGISTRY_URL=""
MODEL_REGISTRY_TOKEN="${MODEL_REGISTRY_TOKEN:-}"
OPENROUTER=false
API_IMAGE=""
WEB_IMAGE=""
OAUTH_PROXY_IMAGE=""
POSTGRES_IMAGE="registry.redhat.io/rhel9/postgresql-16:latest"
STORAGE_CLASS=""
ADMIN_USER=""
PORTAL_HOST=""
AGENT_HOST=""
VLLM_IMAGE=""
CONFIGURE_ARGOCD=true

usage() {
  cat <<'EOF'
Usage: scripts/install.sh [options]

Installs the Tensorhelm hub (portal, API, PostgreSQL) into the current OpenShift cluster.

Common options:
  --namespace NAME            Namespace for the hub (default: tensorhelm)
  --hub-name NAME             Name of this cluster in the portal (default: local-cluster, as in RHACM)
  --admin-user USER           OpenShift user to make a Tensorhelm admin (default: the user you are logged in as)

Model delivery:
  --delivery direct|gitops    direct: apply to this cluster only (default). gitops: commit to GitLab and let
                              Argo CD apply on any managed cluster.
  --gitlab-url URL            e.g. https://gitlab.example.com
  --gitlab-project PATH       e.g. platform/tensorhelm-gitops
  --gitlab-branch NAME        default: main
                              The GitLab token is read from the GITLAB_TOKEN environment variable
                              (scope: api; role: Developer or higher on the project).
  --skip-argocd               Do not configure OpenShift GitOps (Argo CD) for the GitOps repository

Model sources:
  --model-registry-url URL    OpenShift AI Model Registry REST endpoint (token from MODEL_REGISTRY_TOKEN)
  --enable-openrouter         List OpenRouter hosted models in the catalog
                              The Hugging Face token is read from HF_TOKEN (needed for gated models)

Images and platform:
  --api-image IMAGE           Use a pre-built API image instead of building in the cluster
  --web-image IMAGE           Use a pre-built portal image instead of building in the cluster
  --oauth-proxy-image IMAGE   Override the OpenShift oauth-proxy image (detected automatically)
  --postgres-image IMAGE      default: registry.redhat.io/rhel9/postgresql-16:latest
  --storage-class NAME        StorageClass for the database (default: the cluster default)
  --portal-host HOST          Route host for the portal (default: tensorhelm-<ns>.<apps domain>)
  --agent-host HOST           Route host for spoke agents (default: tensorhelm-agent-<ns>.<apps domain>)
  --vllm-image IMAGE          Fallback vLLM image if OpenShift AI does not provide a vLLM runtime template
  -h, --help                  Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --hub-name) HUB_NAME="$2"; shift 2 ;;
    --admin-user) ADMIN_USER="$2"; shift 2 ;;
    --delivery) DELIVERY="$2"; shift 2 ;;
    --gitlab-url) GITLAB_URL="${2%/}"; shift 2 ;;
    --gitlab-project) GITLAB_PROJECT="$2"; shift 2 ;;
    --gitlab-branch) GITLAB_BRANCH="$2"; shift 2 ;;
    --skip-argocd) CONFIGURE_ARGOCD=false; shift ;;
    --model-registry-url) MODEL_REGISTRY_URL="$2"; shift 2 ;;
    --enable-openrouter) OPENROUTER=true; shift ;;
    --api-image) API_IMAGE="$2"; shift 2 ;;
    --web-image) WEB_IMAGE="$2"; shift 2 ;;
    --oauth-proxy-image) OAUTH_PROXY_IMAGE="$2"; shift 2 ;;
    --postgres-image) POSTGRES_IMAGE="$2"; shift 2 ;;
    --storage-class) STORAGE_CLASS="$2"; shift 2 ;;
    --portal-host) PORTAL_HOST="$2"; shift 2 ;;
    --agent-host) AGENT_HOST="$2"; shift 2 ;;
    --vllm-image) VLLM_IMAGE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '    \033[33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
rand() { if command -v openssl >/dev/null 2>&1; then openssl rand -hex "$1"; else head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; fi; }
has_crd() { oc get crd "$1" >/dev/null 2>&1; }
secret_value() { oc get secret "$1" -n "$NAMESPACE" -o "jsonpath={.data.$2}" 2>/dev/null | base64 -d 2>/dev/null || true; }

stage_dir() {
  # Copy a source directory without local build output, so binary builds upload only what they need.
  local dst
  dst="$(mktemp -d)"
  tar -C "$1" --exclude=node_modules --exclude=dist --exclude=.venv --exclude=__pycache__ \
      --exclude='*.db' --exclude=.pytest_cache --exclude=.ruff_cache -cf - . | tar -C "$dst" -xf -
  echo "$dst"
}

build_image() {
  local name="$1" src="$2" dir
  oc process -f "$ROOT/deploy/openshift/build-template.yaml" -p NAME="$name" | oc apply -n "$NAMESPACE" -f - >/dev/null
  dir="$(stage_dir "$src")"
  log "Building $name in the cluster (this takes a few minutes the first time)"
  oc start-build "$name" -n "$NAMESPACE" --from-dir "$dir" --follow --wait >/dev/null \
    || die "Build of $name failed. See: oc logs -n $NAMESPACE bc/$name"
  rm -rf "$dir"
  ok "$name built"
}

# ---- 1. preflight -----------------------------------------------------------------------------------------------
log "Checking prerequisites"
command -v oc >/dev/null 2>&1 || die "The oc CLI is required: https://docs.openshift.com/container-platform/latest/cli_reference/openshift_cli/getting-started-cli.html"
oc whoami >/dev/null 2>&1 || die "Log in first: oc login <api-url>"
oc auth can-i create clusterroles >/dev/null 2>&1 || die "You need cluster-admin to install Tensorhelm (it creates ClusterRoles)"
ok "Logged in to $(oc whoami --show-server) as $(oc whoami)"

case "$DELIVERY" in
  direct) ;;
  gitops)
    [[ -n "$GITLAB_URL" && -n "$GITLAB_PROJECT" ]] || die "--delivery gitops needs --gitlab-url and --gitlab-project"
    [[ -n "$GITLAB_TOKEN" || -n "$(secret_value tensorhelm-integrations gitlab-token)" ]] \
      || die "--delivery gitops needs a GitLab token in the GITLAB_TOKEN environment variable" ;;
  *) die "--delivery must be direct or gitops" ;;
esac

if oc get clusterpolicies.nvidia.com >/dev/null 2>&1 && [[ -n "$(oc get clusterpolicies.nvidia.com -o name 2>/dev/null)" ]]; then
  ok "NVIDIA GPU Operator found"
else
  warn "NVIDIA GPU Operator ClusterPolicy not found; this cluster will show no GPUs (fine for a CPU-only hub)"
fi
if [[ -n "$(oc get datascienceclusters -o name 2>/dev/null)" ]]; then
  ok "Red Hat OpenShift AI found"
  mode="$(oc get datascienceclusters -o jsonpath='{.items[0].spec.components.kserve.defaultDeploymentMode}' 2>/dev/null || true)"
  [[ "$mode" == "RawDeployment" ]] || warn "KServe default deployment mode is '${mode:-Serverless}'. Tensorhelm deploys in RawDeployment mode per model; that works either way."
else
  warn "OpenShift AI DataScienceCluster not found; models can only be deployed after OpenShift AI is installed"
fi
ACM=false
if has_crd managedclusters.cluster.open-cluster-management.io; then ACM=true; ok "RHACM found: managed clusters will appear automatically"; else warn "RHACM not found: add remote clusters with the agent only"; fi
GITOPS=false
if oc get ns openshift-gitops >/dev/null 2>&1; then GITOPS=true; ok "OpenShift GitOps found"; fi

APPS_DOMAIN="$(oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}')"
PORTAL_HOST="${PORTAL_HOST:-tensorhelm-${NAMESPACE}.${APPS_DOMAIN}}"
AGENT_HOST="${AGENT_HOST:-tensorhelm-agent-${NAMESPACE}.${APPS_DOMAIN}}"

if [[ -z "$STORAGE_CLASS" ]]; then
  STORAGE_CLASS="$(oc get storageclass -o jsonpath='{range .items[?(@.metadata.annotations.storageclass\.kubernetes\.io/is-default-class=="true")]}{.metadata.name}{"\n"}{end}' | head -n1)"
  [[ -n "$STORAGE_CLASS" ]] || die "No default StorageClass. Pass --storage-class <name>."
fi
ok "Database storage: $STORAGE_CLASS"

if [[ -z "$OAUTH_PROXY_IMAGE" ]]; then
  OAUTH_PROXY_IMAGE="$(oc get istag oauth-proxy:v4.4 -n openshift -o jsonpath='{.image.dockerImageReference}' 2>/dev/null || true)"
  [[ -n "$OAUTH_PROXY_IMAGE" ]] || OAUTH_PROXY_IMAGE="$(oc adm release info --image-for=oauth-proxy 2>/dev/null || true)"
  [[ -n "$OAUTH_PROXY_IMAGE" ]] || die "Could not find the OpenShift oauth-proxy image. Pass --oauth-proxy-image."
fi
ok "oauth-proxy image: $OAUTH_PROXY_IMAGE"

# ---- 2. namespace, groups and secrets ----------------------------------------------------------------------------
log "Preparing namespace $NAMESPACE"
oc get namespace "$NAMESPACE" >/dev/null 2>&1 || oc create namespace "$NAMESPACE" >/dev/null
oc label namespace "$NAMESPACE" app.kubernetes.io/part-of=tensorhelm --overwrite >/dev/null
ok "Namespace ready"

for group in tensorhelm-admins tensorhelm-engineers tensorhelm-viewers; do
  oc get group "$group" >/dev/null 2>&1 || oc adm groups new "$group" >/dev/null
done
ADMIN_USER="${ADMIN_USER:-$(oc whoami)}"
if [[ "$ADMIN_USER" == kube:admin || "$ADMIN_USER" == system:* ]]; then
  warn "$ADMIN_USER cannot sign in to the portal through OAuth groups. Re-run with --admin-user <your OpenShift user>,"
  warn "or later: oc adm groups add-users tensorhelm-admins <user>"
else
  oc adm groups add-users tensorhelm-admins "$ADMIN_USER" >/dev/null
  ok "$ADMIN_USER is a Tensorhelm admin"
fi

create_secret() {
  local name="$1"; shift
  if oc get secret "$name" -n "$NAMESPACE" >/dev/null 2>&1; then return 0; fi
  oc create secret generic "$name" -n "$NAMESPACE" "$@" >/dev/null
  oc label secret "$name" -n "$NAMESPACE" app.kubernetes.io/part-of=tensorhelm >/dev/null
  ok "Created secret $name"
}
create_secret tensorhelm-db --from-literal=user=tensorhelm --from-literal=password="$(rand 24)"
SHARED="$(rand 24)"
create_secret tensorhelm-proxy \
  --from-literal=session-secret="$(rand 16)" \
  --from-literal=shared-secret="$SHARED" \
  --from-literal=proxy-secret.conf="proxy_set_header X-Tensorhelm-Proxy-Secret \"$SHARED\";"
create_secret tensorhelm-agent-bootstrap --from-literal=token="thb_$(rand 24)"

# Integration tokens: keep stored values unless a new one is given.
GITLAB_TOKEN="${GITLAB_TOKEN:-$(secret_value tensorhelm-integrations gitlab-token)}"
HF_TOKEN="${HF_TOKEN:-$(secret_value tensorhelm-integrations huggingface-token)}"
MODEL_REGISTRY_TOKEN="${MODEL_REGISTRY_TOKEN:-$(secret_value tensorhelm-integrations model-registry-token)}"
oc create secret generic tensorhelm-integrations -n "$NAMESPACE" \
  --from-literal=gitlab-token="$GITLAB_TOKEN" \
  --from-literal=huggingface-token="$HF_TOKEN" \
  --from-literal=model-registry-token="$MODEL_REGISTRY_TOKEN" \
  --dry-run=client -o yaml | oc apply -f - >/dev/null
ok "Integration tokens stored"

# ---- 3. images ---------------------------------------------------------------------------------------------------
INTERNAL="image-registry.openshift-image-registry.svc:5000/$NAMESPACE"
BUILT=false
if [[ -z "$API_IMAGE" ]]; then build_image tensorhelm-api "$ROOT/services/api"; API_IMAGE="$INTERNAL/tensorhelm-api:latest"; BUILT=true; fi
if [[ -z "$WEB_IMAGE" ]]; then build_image tensorhelm-web "$ROOT/services/web"; WEB_IMAGE="$INTERNAL/tensorhelm-web:latest"; BUILT=true; fi

# ---- 4. hub ------------------------------------------------------------------------------------------------------
log "Deploying the hub"
oc process -f "$ROOT/deploy/openshift/hub-template.yaml" \
  -p NAMESPACE="$NAMESPACE" -p PORTAL_HOST="$PORTAL_HOST" -p AGENT_HOST="$AGENT_HOST" \
  -p API_IMAGE="$API_IMAGE" -p WEB_IMAGE="$WEB_IMAGE" -p OAUTH_PROXY_IMAGE="$OAUTH_PROXY_IMAGE" \
  -p POSTGRES_IMAGE="$POSTGRES_IMAGE" -p STORAGE_CLASS="$STORAGE_CLASS" -p HUB_CLUSTER_NAME="$HUB_NAME" \
  -p DELIVERY_MODE="$DELIVERY" -p GITLAB_URL="$GITLAB_URL" -p GITLAB_PROJECT="$GITLAB_PROJECT" \
  -p GITLAB_BRANCH="$GITLAB_BRANCH" -p MODEL_REGISTRY_URL="$MODEL_REGISTRY_URL" \
  -p OPENROUTER_ENABLED="$OPENROUTER" -p VLLM_IMAGE="$VLLM_IMAGE" \
  | oc apply -n "$NAMESPACE" -f - >/dev/null
if [[ "$BUILT" == true ]]; then
  oc rollout restart deployment/tensorhelm-api deployment/tensorhelm-web -n "$NAMESPACE" >/dev/null
fi
oc rollout status statefulset/tensorhelm-db -n "$NAMESPACE" --timeout=5m >/dev/null || die "Database did not start: oc logs -n $NAMESPACE statefulset/tensorhelm-db"
ok "Database running"
oc rollout status deployment/tensorhelm-api -n "$NAMESPACE" --timeout=5m >/dev/null || die "API did not start: oc logs -n $NAMESPACE deploy/tensorhelm-api"
ok "API running"
oc rollout status deployment/tensorhelm-web -n "$NAMESPACE" --timeout=5m >/dev/null || die "Portal did not start: oc logs -n $NAMESPACE deploy/tensorhelm-web -c oauth-proxy"
ok "Portal running"

if [[ "$ACM" == true ]]; then
  oc label managedcluster "$HUB_NAME" tensorhelm.io/managed=true --overwrite >/dev/null 2>&1 \
    && ok "Labelled RHACM cluster $HUB_NAME with tensorhelm.io/managed=true" || true
fi

# ---- 5. GitOps -----------------------------------------------------------------------------------------------------
if [[ "$DELIVERY" == gitops && "$CONFIGURE_ARGOCD" == true ]]; then
  if [[ "$GITOPS" != true ]]; then
    warn "OpenShift GitOps is not installed; install it, then re-run to connect Argo CD to the GitOps repository"
  else
    log "Connecting Argo CD to the GitOps repository"
    REPO_URL="$GITLAB_URL/$GITLAB_PROJECT.git"
    oc create secret generic tensorhelm-gitops-repo -n openshift-gitops \
      --from-literal=type=git --from-literal=url="$REPO_URL" \
      --from-literal=username=tensorhelm --from-literal=password="$GITLAB_TOKEN" \
      --dry-run=client -o yaml | oc apply -f - >/dev/null
    oc label secret tensorhelm-gitops-repo -n openshift-gitops argocd.argoproj.io/secret-type=repository --overwrite >/dev/null
    # Let the default Argo CD instance create team namespaces and model deployments on this cluster.
    cat <<EOF | oc apply -f - >/dev/null
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: tensorhelm-argocd
  labels: {app.kubernetes.io/part-of: tensorhelm}
rules:
  - apiGroups: [""]
    resources: [namespaces]
    verbs: [get, list, watch, create, update, patch]
  - apiGroups: [serving.kserve.io]
    resources: [inferenceservices, servingruntimes]
    verbs: [get, list, watch, create, update, patch, delete]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: tensorhelm-argocd
  labels: {app.kubernetes.io/part-of: tensorhelm}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: tensorhelm-argocd}
subjects:
  - {kind: ServiceAccount, name: openshift-gitops-argocd-application-controller, namespace: openshift-gitops}
EOF
    if [[ "$ACM" == true ]]; then
      oc process -f "$ROOT/deploy/gitops/argocd-acm-template.yaml" -p REPO_URL="$REPO_URL" -p BRANCH="$GITLAB_BRANCH" \
        | oc apply -f - >/dev/null
      ok "ApplicationSet syncs deployments/<cluster>/ to every RHACM cluster labelled tensorhelm.io/managed=true"
    else
      oc process -f "$ROOT/deploy/gitops/argocd-single-template.yaml" -p REPO_URL="$REPO_URL" -p BRANCH="$GITLAB_BRANCH" \
        -p HUB_CLUSTER_NAME="$HUB_NAME" | oc apply -f - >/dev/null
      ok "Argo CD Application syncs deployments/$HUB_NAME/ to this cluster"
    fi
  fi
fi

# ---- done ----------------------------------------------------------------------------------------------------------
cat <<EOF

Tensorhelm is running.

  Portal:      https://$PORTAL_HOST
  Agent URL:   https://$AGENT_HOST   (spoke agents connect here)
  Delivery:    $DELIVERY

Next steps
  1. Open the portal and sign in with your OpenShift account (members of tensorhelm-admins,
     tensorhelm-engineers or tensorhelm-viewers can sign in).
  2. Add people:   oc adm groups add-users tensorhelm-engineers <user>
  3. Add a remote cluster (GPU or CPU):
       scripts/register-cluster.sh --name <cluster> --spoke-context <kube-context> --approve
  4. Check status: oc exec -n $NAMESPACE deploy/tensorhelm-api -- tensorhelm-admin list-clusters
EOF
