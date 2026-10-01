# Adding and discovering clusters

The hub manages any number of OpenShift clusters, with or without GPUs. Each cluster connects **out** to the
hub; no management port needs to be open on the cluster.

| Piece on the remote cluster | Connects to | What it gives the portal |
| --- | --- | --- |
| RHACM klusterlet (optional) | Hub API server, port 6443 | Cluster appears automatically; policies; GitOps placement |
| Tensorhelm agent | Hub agent route, port 443 | GPU nodes, usage, model endpoints and vLLM runtimes, every 30 s |

## Four ways a cluster shows up

### 1. Already managed by RHACM: automatic

Every RHACM ManagedCluster appears on the Clusters page within 30 seconds. Its status is
**Agent not installed** until you add the agent with option 2 or 3. Without the agent, the portal knows the
cluster exists but not its GPUs, so it cannot place models there.

### 2. One script: import, label and install the agent

```bash
# Kube contexts for both clusters (names are up to you)
oc login https://api.hub.example.com:6443      && oc config rename-context "$(oc config current-context)" hub
oc login https://api.prod-east.example.com:6443 && oc config rename-context "$(oc config current-context)" prod-east
oc config use-context hub

./scripts/register-cluster.sh --name prod-east --spoke-context prod-east \
  --label region=eu --label tensorhelm.io/role=serving --approve
```

The script:

1. Checks both contexts and that you are cluster-admin on the new cluster.
2. **If the hub has RHACM:**
   - creates the ManagedCluster with your labels plus `tensorhelm.io/managed=true`
   - creates a KlusterletAddonConfig
   - applies RHACM's import manifests to the new cluster
   - skips the import if RHACM already manages the cluster, and only updates the labels
3. Builds the agent image inside the new cluster and starts the agent in `tensorhelm-agent`, with read-only
   access to the cluster.
4. Waits for the agent to register, then approves it (`--approve`). Without `--approve`, an admin approves it
   on the Clusters page.

Use `--skip-acm` to install only the agent. Use `--agent-image` to skip the build on a disconnected cluster.

### 3. Agent only, from the portal

On **Clusters → Add cluster → Agent only**, the portal shows an `oc process … | oc apply` command to run
against the new cluster. It uses `deploy/openshift/agent-template.yaml` and the hub's bootstrap token.

### 4. Discovered by RHACM

If RHACM cluster discovery is configured (it lists clusters from your console.redhat.com organisation), the
**Discovered by RHACM** tab shows clusters that exist but are not imported yet. Import one with option 2.

## Approval

A new agent registers with the shared bootstrap token and gets its own cluster token back. It stays
**Waiting for approval** and takes no placements until an admin approves it. The pending entry shows the API URL the agent
reported, so you can check it is the cluster you expect.

```bash
oc exec -n tensorhelm deploy/tensorhelm-api -- tensorhelm-admin list-clusters
oc exec -n tensorhelm deploy/tensorhelm-api -- tensorhelm-admin approve-cluster prod-east
```

A second registration under the same name is refused. To re-register, for example after rebuilding the
cluster, remove it first, then restart the agent:

```bash
oc exec -n tensorhelm deploy/tensorhelm-api -- tensorhelm-admin remove-cluster prod-east
oc --context prod-east delete secret tensorhelm-agent-credentials -n tensorhelm-agent
oc --context prod-east rollout restart deploy/tensorhelm-agent -n tensorhelm-agent
```

## Labels that drive placement

| Label | Set where | Effect |
| --- | --- | --- |
| `tensorhelm.io/managed=true` | RHACM (set by the script) | Cluster receives GitOps deployments from `deployments/<cluster>/` |
| `region=<value>` | RHACM or `--label` | Deploy form's "Region label" only offers matching clusters |
| `tensorhelm.io/role=serving` | RHACM or `--label` | Free-form; use for your own placement rules |
| `tensorhelm.io/burst=true` | RHACM or `--label` | Shown as a burst cluster |
| `tensorhelm.io/pool=<name>` | GPU **node** label | Shown on the GPU fleet page as the node's pool |

RHACM labels win over agent labels with the same key. Change labels with
`oc label managedcluster prod-east region=us --overwrite`.

## GPU or CPU-only clusters

The agent reports whatever the cluster has:
- **GPU nodes** come from NVIDIA GPU Feature Discovery labels (`nvidia.com/gpu.product`, `nvidia.com/gpu.memory`).
- **CPU-only clusters** report node and core counts. They appear as **CPU only**, and the placement step marks
  them "not eligible" for GPU models.

The hub needs no GPUs; it is marked **Hub**.

## Network and TLS

| From | To | Port |
| --- | --- | --- |
| Agent | `https://tensorhelm-agent-<ns>.<hub apps domain>/api/v1/agent/*` | 443 |
| Klusterlet | Hub API server | 6443 |
| Argo CD on the hub (push mode) | Spoke API server | 6443 |

The agent verifies the hub's router certificate. If the hub uses a self-signed or private CA:
- **Production:** give the router a certificate from your corporate CA that the spokes trust.
- **Labs only:** pass `--insecure` to `register-cluster.sh`.

To avoid any hub-to-spoke connection, run Argo CD in RHACM's pull model; the Git layout stays the same.

## Removing a cluster

```bash
./scripts/unregister-cluster.sh --name prod-east --spoke-context prod-east
oc delete managedcluster prod-east      # also detach from RHACM, if you want
```
