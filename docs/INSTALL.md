# Install and upgrade Tensorhelm

`scripts/install.sh` installs the hub into the OpenShift cluster you are logged in to. It is safe to re-run:
secrets are kept, images are rebuilt and the deployment is updated.

## 1. Before you start

| Need | Why | Check |
| --- | --- | --- |
| OpenShift 4.14+ and cluster-admin | The hub creates ClusterRoles and reads nodes cluster-wide | `oc whoami` and `oc auth can-i create clusterroles` |
| Default StorageClass, 20 GiB | PostgreSQL data | `oc get storageclass` |
| Build pods can reach `registry.access.redhat.com`, `pypi.org` and `registry.npmjs.org` | Images are built inside the cluster | For disconnected clusters, see section 6 |
| Nodes can pull `registry.redhat.io` | PostgreSQL image (the global pull secret normally covers this) | `oc get secret pull-secret -n openshift-config` |
| Red Hat OpenShift AI with KServe | Model serving. Tensorhelm uses the vLLM runtime OpenShift AI ships. | `oc get datasciencecluster` |
| NVIDIA GPU Operator and Node Feature Discovery | GPU detection and scheduling | `oc get clusterpolicies.nvidia.com` |
| A real OpenShift user (not `kube:admin`) | Portal roles come from OpenShift groups | `oc whoami` |
| Optional: RHACM | Automatic discovery of managed clusters, and multi-cluster GitOps | `oc get crd managedclusters.cluster.open-cluster-management.io` |
| Optional: OpenShift GitOps and a GitLab project | GitOps delivery to remote clusters | `oc get ns openshift-gitops` |

The hub itself needs no GPUs. If the hub cluster has GPUs, it also serves models and appears in the portal
with its GPUs.

## 2. Install

Direct delivery (models deploy to the hub cluster only):

```bash
./scripts/install.sh --admin-user jane.doe
```

GitOps delivery (models deploy to any managed cluster through GitLab and Argo CD):

```bash
export GITLAB_TOKEN=glpat-xxxxxxxx         # scope: api; role: Developer or higher on the project
export HF_TOKEN=hf_xxxxxxxx                # optional: needed for gated models such as Llama
./scripts/install.sh --admin-user jane.doe --delivery gitops \
  --gitlab-url https://gitlab.example.com \
  --gitlab-project platform/tensorhelm-gitops \
  --model-registry-url https://<model-registry-route>
```

Create the GitLab project first, with a `main` branch (an empty README is enough).

All options: `./scripts/install.sh --help`.

### What the script does

1. **Checks the cluster.** It looks for:
   - access as cluster-admin
   - the GPU Operator, OpenShift AI, RHACM and OpenShift GitOps
   - the default StorageClass
   - the OpenShift oauth-proxy image
2. **Prepares access.** It creates:
   - the namespace `tensorhelm`
   - the OpenShift groups `tensorhelm-admins`, `tensorhelm-engineers` and `tensorhelm-viewers`, adding `--admin-user` to admins
   - four Secrets: database password, oauth-proxy cookie and shared secret, agent bootstrap token, integration tokens
3. **Builds images.** It builds the API and portal images inside the cluster (`BuildConfig`, binary source), so
   you need no registry or container tool on your laptop.
4. **Deploys the hub.** It applies `deploy/openshift/hub-template.yaml`:

   | Object | Purpose |
   | --- | --- |
   | PostgreSQL StatefulSet | Portal database |
   | API Deployment | Hub API |
   | Portal Deployment | oauth-proxy and nginx in one pod |
   | Routes | One for the portal (re-encrypt TLS), one for agents (edge TLS, `/api/v1/agent` only) |
   | NetworkPolicies | Restrict traffic to the database, API and portal |
   | ClusterRole | Read-only cluster access for the API, plus model deployment rights |

5. **Connects Argo CD (GitOps mode).** It stores the GitLab credentials in `openshift-gitops`, then:
   - **With RHACM:** creates a Placement, a GitOpsCluster and an ApplicationSet. Every cluster labelled
     `tensorhelm.io/managed=true` syncs `deployments/<cluster>/` from Git.
   - **Without RHACM:** creates one Application for the hub cluster.

### Who can sign in

Members of the three groups can sign in; their role in the portal follows the group:

| Group | Role | Can |
| --- | --- | --- |
| `tensorhelm-viewers` | viewer | See everything |
| `tensorhelm-engineers` | engineer | Also deploy and delete models |
| `tensorhelm-admins` | admin | Also approve and remove clusters |

```bash
oc adm groups add-users tensorhelm-engineers alice bob
```

To use existing LDAP or Entra ID groups instead, set `TH_ADMIN_GROUPS` and `TH_ENGINEER_GROUPS` on the API
(comma-separated). Also add those groups to the `tensorhelm-portal-users` RoleBinding.

## 3. Verify

```bash
oc get pods -n tensorhelm
oc exec -n tensorhelm deploy/tensorhelm-api -- tensorhelm-admin list-clusters
curl -s https://$(oc get route tensorhelm-agent -n tensorhelm -o jsonpath='{.spec.host}')/api/v1/agent/report -o /dev/null -w '%{http_code}\n'   # 401 or 422 = reachable
```

Then open the portal URL the script printed, sign in, and check:

1. **Fleet overview:** the hub cluster shows its GPUs (if it has any).
2. **GPU fleet:** every GPU node is listed with its model and driver.
3. **Model catalog:** run a fit check on Granite 3.3 8B; the hub shows "Fits" if it has a GPU with 24 GB or more.
4. **Deployments:** deploy that model with team `smoke`. It becomes Ready once the weights download
   (several minutes for the first pull). Delete it afterwards.

## 4. Upgrade

```bash
git pull
./scripts/install.sh --admin-user jane.doe [same options as before]
```

Database tables are created automatically on start. Spoke agents are upgraded by re-running
`register-cluster.sh` for each cluster. It rebuilds the agent and keeps the cluster's token.

## 5. Uninstall

```bash
./scripts/uninstall.sh --yes
```

This removes the hub namespace (including the database) and its ClusterRoles. Model endpoints in team
namespaces keep running; delete them in the portal first if you want them gone.

## 6. Disconnected clusters

Build the two images on a connected machine, push them to your internal registry, and pass them in:

```bash
podman build -t registry.internal/tensorhelm/api:0.1.0 services/api
podman build -t registry.internal/tensorhelm/web:0.1.0 services/web
podman push registry.internal/tensorhelm/api:0.1.0 && podman push registry.internal/tensorhelm/web:0.1.0
./scripts/install.sh --api-image registry.internal/tensorhelm/api:0.1.0 \
  --web-image registry.internal/tensorhelm/web:0.1.0 \
  --postgres-image registry.internal/rhel9/postgresql-16:latest
```

Use `--agent-image registry.internal/tensorhelm/api:0.1.0` with `register-cluster.sh`: the agent uses the
API image. Mirror model weights into S3 or as OCI modelcar images, and deploy them with that storage URI.

## 7. Settings reference

The API reads `TH_*` environment variables. Change them with `oc set env deploy/tensorhelm-api -n tensorhelm KEY=value`,
or re-run the install script with the matching option.

| Variable | Default | Meaning |
| --- | --- | --- |
| `TH_DELIVERY_MODE` | `direct` | `direct` or `gitops` |
| `TH_GITLAB_URL`, `TH_GITLAB_PROJECT`, `TH_GITLAB_BRANCH` | | GitOps repository |
| `TH_GITOPS_PATH_PREFIX` | `deployments` | Folder for deployments in the repository |
| `TH_HUB_CLUSTER_NAME` | `local-cluster` | Hub name; matches RHACM's name for the hub |
| `TH_ACM_SYNC` | `true` | Mirror RHACM ManagedClusters |
| `TH_REFRESH_INTERVAL_S` | `30` | Hub inventory and RHACM sync interval |
| `TH_AGENT_OFFLINE_AFTER_S` | `180` | When a silent agent counts as offline |
| `TH_MODEL_REGISTRY_URL` | | OpenShift AI Model Registry REST URL |
| `TH_OPENROUTER_ENABLED` | `false` | List OpenRouter models (not deployable) |
| `TH_GPU_MEMORY_UTILIZATION` | `0.90` | vLLM `--gpu-memory-utilization`, also used by the fit check |
| `TH_VLLM_IMAGE` | | vLLM image to use when a cluster has no OpenShift AI vLLM template |
| `TH_RHOAI_APPLICATIONS_NAMESPACE` | `redhat-ods-applications` | Where OpenShift AI keeps serving runtime templates |
