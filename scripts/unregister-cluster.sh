#!/usr/bin/env bash
# Removes the Tensorhelm agent from a spoke cluster and forgets it on the hub. Does not detach it from RHACM.
set -euo pipefail

NAME=""; SPOKE_CTX=""; HUB_CTX="$(oc config current-context 2>/dev/null || true)"; HUB_NS=tensorhelm
while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --spoke-context) SPOKE_CTX="$2"; shift 2 ;;
    --hub-context) HUB_CTX="$2"; shift 2 ;;
    --hub-namespace) HUB_NS="$2"; shift 2 ;;
    -h|--help) echo "Usage: scripts/unregister-cluster.sh --name NAME --spoke-context CTX [--hub-context CTX]"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$NAME" && -n "$SPOKE_CTX" ]] || { echo "--name and --spoke-context are required" >&2; exit 2; }

oc --context "$SPOKE_CTX" delete namespace tensorhelm-agent --ignore-not-found
oc --context "$SPOKE_CTX" delete clusterrolebinding tensorhelm-agent --ignore-not-found
oc --context "$SPOKE_CTX" delete clusterrole tensorhelm-agent --ignore-not-found
oc --context "$HUB_CTX" exec -n "$HUB_NS" deploy/tensorhelm-api -- tensorhelm-admin remove-cluster "$NAME" || true
echo "Agent removed from $NAME. To detach it from RHACM as well: oc --context $HUB_CTX delete managedcluster $NAME"
