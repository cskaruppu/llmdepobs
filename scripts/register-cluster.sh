#!/usr/bin/env bash
# Adds a remote OpenShift cluster (with or without GPUs) to Tensorhelm:
#   1. imports it into RHACM on the hub (if RHACM is installed and the cluster is not managed yet) and labels it
#   2. installs the Tensorhelm agent on it, which connects out to the hub and reports GPUs and endpoints
#   3. optionally approves the new agent on the hub
# Needs kube contexts for both clusters (oc login to each; see `oc config get-contexts`).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

NAME=""
SPOKE_CTX=""
HUB_CTX="$(oc config current-context 2>/dev/null || true)"
HUB_NS=tensorhelm
HUB_URL=""
AGENT_NS=tensorhelm-agent
AGENT_IMAGE=""
LABELS=()
SKIP_ACM=false
APPROVE=false
INSECURE=false

usage() {
  cat <<'EOF'
Usage: scripts/register-cluster.sh --name NAME --spoke-context CONTEXT [options]

  --name NAME              Cluster name shown in the portal (and in RHACM). Lowercase, digits, dashes.
  --spoke-context CTX      kube context of the cluster to add
  --hub-context CTX        kube context of the hub (default: current context)
  --hub-namespace NS       Tensorhelm namespace on the hub (default: tensorhelm)
  --hub-url URL            Agent URL of the hub (default: read from the hub's tensorhelm-agent route)
  --label KEY=VALUE        Placement label, repeatable, e.g. --label region=eu --label tensorhelm.io/role=serving
  --agent-image IMAGE      Use a published agent image (default: build it inside the spoke cluster)
  --skip-acm               Do not import into RHACM; install the agent only
  --approve                Approve the agent on the hub as soon as it registers
  --insecure               Skip TLS verification of the hub route (labs with self-signed certificates only)
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --spoke-context) SPOKE_CTX="$2"; shift 2 ;;
    --hub-context) HUB_CTX="$2"; shift 2 ;;
    --hub-namespace) HUB_NS="$2"; shift 2 ;;
    --hub-url) HUB_URL="${2%/}"; shift 2 ;;
    --label) LABELS+=("$2"); shift 2 ;;
    --agent-image) AGENT_IMAGE="$2"; shift 2 ;;
    --skip-acm) SKIP_ACM=true; shift ;;
    --approve) APPROVE=true; shift ;;
    --insecure) INSECURE=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '    \033[33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
hub()   { oc --context "$HUB_CTX" "$@"; }
spoke() { oc --context "$SPOKE_CTX" "$@"; }

[[ -n "$NAME" && -n "$SPOKE_CTX" ]] || { usage; exit 2; }
[[ "$NAME" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || die "--name must be lowercase letters, digits and dashes"
for label in "${LABELS[@]+"${LABELS[@]}"}"; do
  [[ "$label" == *=* ]] || die "--label must be KEY=VALUE: $label"
done

# ---- 1. connections -------------------------------------------------------------------------------------------------
log "Checking access"
hub whoami >/dev/null 2>&1 || die "Cannot use hub context '$HUB_CTX'. Log in to the hub with oc first."
spoke whoami >/dev/null 2>&1 || die "Cannot use spoke context '$SPOKE_CTX'. Log in to the new cluster with oc first."
[[ "$(hub whoami --show-server)" != "$(spoke whoami --show-server)" ]] || die "Hub and spoke contexts point at the same cluster"
spoke auth can-i create clusterroles >/dev/null 2>&1 || die "You need cluster-admin on the spoke cluster"
hub get secret tensorhelm-agent-bootstrap -n "$HUB_NS" >/dev/null 2>&1 || die "Tensorhelm is not installed in $HUB_NS on the hub (run scripts/install.sh)"
ok "Hub:   $(hub whoami --show-server)"
ok "Spoke: $(spoke whoami --show-server)"

if [[ -z "$HUB_URL" ]]; then
  HUB_URL="https://$(hub get route tensorhelm-agent -n "$HUB_NS" -o jsonpath='{.spec.host}')"
fi
BOOTSTRAP="$(hub get secret tensorhelm-agent-bootstrap -n "$HUB_NS" -o jsonpath='{.data.token}' | base64 -d)"
ok "Agent URL: $HUB_URL"

if spoke get clusterpolicies.nvidia.com >/dev/null 2>&1 && [[ -n "$(spoke get clusterpolicies.nvidia.com -o name 2>/dev/null)" ]]; then
  ok "Spoke has the NVIDIA GPU Operator"
else
  warn "No NVIDIA GPU Operator on the spoke: it will be registered as a CPU-only cluster"
fi
[[ -n "$(spoke get datascienceclusters -o name 2>/dev/null)" ]] && ok "Spoke has OpenShift AI" \
  || warn "No OpenShift AI on the spoke: models cannot be deployed there until it is installed"

# ---- 2. RHACM import ------------------------------------------------------------------------------------------------
LABEL_ARGS=("tensorhelm.io/managed=true" "${LABELS[@]+"${LABELS[@]}"}")
if [[ "$SKIP_ACM" == false ]] && hub get crd managedclusters.cluster.open-cluster-management.io >/dev/null 2>&1; then
  log "Registering $NAME in RHACM"
  if hub get managedcluster "$NAME" >/dev/null 2>&1; then
    ok "Already managed by RHACM"
  else
    label_yaml=""
    for label in "${LABEL_ARGS[@]}"; do label_yaml+="    ${label%%=*}: \"${label#*=}\""$'\n'; done
    cat <<EOF | hub apply -f - >/dev/null
apiVersion: cluster.open-cluster-management.io/v1
kind: ManagedCluster
metadata:
  name: $NAME
  labels:
    cloud: auto-detect
    vendor: auto-detect
$label_yaml
spec:
  hubAcceptsClient: true
EOF
    for _ in $(seq 1 60); do
      hub get secret "$NAME-import" -n "$NAME" >/dev/null 2>&1 && break
      sleep 5
    done
    hub get secret "$NAME-import" -n "$NAME" >/dev/null 2>&1 || die "RHACM did not create the import secret for $NAME"
    if hub get crd klusterletaddonconfigs.agent.open-cluster-management.io >/dev/null 2>&1; then
      cat <<EOF | hub apply -f - >/dev/null
apiVersion: agent.open-cluster-management.io/v1
kind: KlusterletAddonConfig
metadata:
  name: $NAME
  namespace: $NAME
spec:
  applicationManager: {enabled: true}
  certPolicyController: {enabled: true}
  policyController: {enabled: true}
  searchCollector: {enabled: true}
EOF
    fi
    tmp="$(mktemp -d)"
    hub get secret "$NAME-import" -n "$NAME" -o jsonpath='{.data.crds\.yaml}' | base64 -d > "$tmp/crds.yaml"
    hub get secret "$NAME-import" -n "$NAME" -o jsonpath='{.data.import\.yaml}' | base64 -d > "$tmp/import.yaml"
    spoke apply -f "$tmp/crds.yaml" >/dev/null
    spoke apply -f "$tmp/import.yaml" >/dev/null
    rm -rf "$tmp"
    ok "Klusterlet installed on the spoke; RHACM shows the cluster as Ready within a few minutes"
  fi
  hub label managedcluster "$NAME" "${LABEL_ARGS[@]}" --overwrite >/dev/null
  ok "Labels set: ${LABEL_ARGS[*]}"
else
  [[ "$SKIP_ACM" == true ]] && warn "Skipping RHACM import" || warn "RHACM not installed on the hub: installing the agent only"
fi

# ---- 3. agent -------------------------------------------------------------------------------------------------------
log "Installing the Tensorhelm agent on $NAME"
spoke get namespace "$AGENT_NS" >/dev/null 2>&1 || spoke create namespace "$AGENT_NS" >/dev/null
if [[ -z "$AGENT_IMAGE" ]]; then
  oc process -f "$ROOT/deploy/openshift/build-template.yaml" -p NAME=tensorhelm-agent | spoke apply -n "$AGENT_NS" -f - >/dev/null
  src="$(mktemp -d)"
  tar -C "$ROOT/services/api" --exclude=.venv --exclude=__pycache__ --exclude='*.db' --exclude=.pytest_cache \
      --exclude=.ruff_cache --exclude=tests -cf - . | tar -C "$src" -xf -
  log "Building the agent image inside $NAME"
  spoke start-build tensorhelm-agent -n "$AGENT_NS" --from-dir "$src" --follow --wait >/dev/null \
    || die "Agent build failed: oc --context $SPOKE_CTX logs -n $AGENT_NS bc/tensorhelm-agent"
  rm -rf "$src"
  AGENT_IMAGE="image-registry.openshift-image-registry.svc:5000/$AGENT_NS/tensorhelm-agent:latest"
  ok "Agent image built"
fi
agent_labels="$(IFS=,; echo "${LABELS[*]+"${LABELS[*]}"}")"
oc process -f "$ROOT/deploy/openshift/agent-template.yaml" \
  -p NAMESPACE="$AGENT_NS" -p CLUSTER_NAME="$NAME" -p HUB_URL="$HUB_URL" -p BOOTSTRAP_TOKEN="$BOOTSTRAP" \
  -p AGENT_IMAGE="$AGENT_IMAGE" -p AGENT_LABELS="$agent_labels" -p INSECURE_SKIP_VERIFY="$INSECURE" \
  | spoke apply -f - >/dev/null
spoke rollout restart deployment/tensorhelm-agent -n "$AGENT_NS" >/dev/null
spoke rollout status deployment/tensorhelm-agent -n "$AGENT_NS" --timeout=5m >/dev/null \
  || die "Agent did not start: oc --context $SPOKE_CTX logs -n $AGENT_NS deploy/tensorhelm-agent"
ok "Agent running"

# ---- 4. approval ----------------------------------------------------------------------------------------------------
admin() { hub exec -n "$HUB_NS" deploy/tensorhelm-api -- tensorhelm-admin "$@"; }
log "Waiting for the agent to register with the hub"
registered=false
for _ in $(seq 1 24); do
  if admin list-clusters 2>/dev/null | awk -v n="$NAME" '$1 == n && ($3 == "pending" || $3 == "active" || $3 == "offline")' | grep -q .; then
    registered=true; break
  fi
  sleep 5
done
if [[ "$registered" != true ]]; then
  warn "The agent has not registered yet. Check its log: oc --context $SPOKE_CTX logs -n $AGENT_NS deploy/tensorhelm-agent"
  warn "Common causes: the spoke cannot reach $HUB_URL, or the hub certificate is not trusted (try --insecure in a lab)."
  exit 1
fi
ok "$NAME registered"
if [[ "$APPROVE" == true ]]; then
  admin approve-cluster "$NAME" >/dev/null
  ok "$NAME approved"
  echo
  echo "Done. $NAME appears on the Clusters page with its GPUs within 30 seconds."
else
  echo
  echo "Done. Approve $NAME on the portal's Clusters page, or run:"
  echo "  oc --context $HUB_CTX exec -n $HUB_NS deploy/tensorhelm-api -- tensorhelm-admin approve-cluster $NAME"
fi
