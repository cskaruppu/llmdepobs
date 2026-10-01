#!/usr/bin/env bash
# Removes the Tensorhelm hub from the current cluster. Model deployments in team namespaces are NOT touched.
set -euo pipefail

NAMESPACE=tensorhelm
YES=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --yes) YES=true; shift ;;
    -h|--help) echo "Usage: scripts/uninstall.sh [--namespace tensorhelm] --yes"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

if [[ "$YES" != true ]]; then
  echo "This deletes namespace $NAMESPACE, including the Tensorhelm database (clusters, deployments history, audit log)."
  echo "Model endpoints already running in team namespaces keep running."
  echo "Re-run with --yes to continue."
  exit 1
fi

oc delete namespace "$NAMESPACE" --wait=true
oc delete clusterrolebinding "tensorhelm-hub-$NAMESPACE" --ignore-not-found
oc delete clusterrole "tensorhelm-hub-$NAMESPACE" --ignore-not-found
oc delete clusterrolebinding tensorhelm-argocd --ignore-not-found
oc delete clusterrole tensorhelm-argocd --ignore-not-found
oc delete applicationset tensorhelm-deployments -n openshift-gitops --ignore-not-found 2>/dev/null || true
oc delete secret tensorhelm-gitops-repo -n openshift-gitops --ignore-not-found 2>/dev/null || true
echo "Tensorhelm hub removed. OpenShift groups tensorhelm-* were kept; delete them with: oc delete group <name>"
