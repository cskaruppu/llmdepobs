# Operating Tensorhelm

Day-to-day tasks for platform admins. For the full target operating model, see the
[Deployment and Operations Guide](https://claude.ai/code/artifact/64644667-1a2f-4bdc-aa6f-7ac81482c151).

## Deploy a model

1. **Pick and check the model.** On **Model catalog**, pick a model and run **Fit check** with the precision,
   context length and number of concurrent requests you expect. Each cluster shows one of:
   - **Fits**
   - **Fits with changes** (for example, a different GPU type)
   - **Blocked** (not enough free GPUs)
   - **Not eligible**, with the reason
2. **Fill in the deployment.** Choose **Deploy this model**, enter a deployment name and a team, and choose
   **Check placement**. The recommended cluster uses the fewest GPUs per replica.
3. **Deploy.**
   - **Direct mode:** the objects are applied to the hub cluster at once.
   - **GitOps mode:** the portal commits three files to GitLab:
     - `deployments/<cluster>/<team>/namespace.yaml`
     - `deployments/<cluster>/<team>/<name>/servingruntime.yaml`
     - `deployments/<cluster>/<team>/<name>/inferenceservice.yaml`

     Argo CD applies them at its next sync (about three minutes; sync sooner from the Argo CD console).
4. **Wait for it to start.** **Deployments** shows *Starting*, then *Ready* once vLLM has loaded the weights.
   A first pull of a 70B model can take 15 to 30 minutes, depending on bandwidth. Then call it:

```bash
URL=$(oc get inferenceservice contracts-rag -n team-legal -o jsonpath='{.status.url}')
curl -s "$URL/v1/chat/completions" -H 'Content-Type: application/json' \
  -d '{"model":"contracts-rag","messages":[{"role":"user","content":"Say OK"}],"max_tokens":5}'
```

**Gated models** (for example Llama) need a Hugging Face token in the model's namespace. Create a secret
there, or mirror the model into S3 or an OCI modelcar image and set the **Storage URI**.

## Daily checks

| Check | Where |
| --- | --- |
| Alerts: offline agents, clusters waiting for approval, GPU nodes not ready or cordoned, endpoints not ready | Fleet overview → Alerts |
| Clusters list and agent versions | `oc exec -n tensorhelm deploy/tensorhelm-api -- tensorhelm-admin list-clusters` |
| GitOps sync state (GitOps mode) | `oc get applications -n openshift-gitops` |
| Hub pods | `oc get pods -n tensorhelm` |

## Users and roles

```bash
oc adm groups add-users tensorhelm-viewers  sam
oc adm groups add-users tensorhelm-engineers alice
oc adm groups add-users tensorhelm-admins   jane.doe
```

Group changes take effect within a minute; the API caches group membership for 60 seconds.

## Back up the database

The hub database holds:
- cluster registrations and agent token hashes
- deployment history
- the audit log

In GitOps mode, the deployments themselves are also in Git. Example nightly dump to a PVC:

```bash
oc exec -n tensorhelm statefulset/tensorhelm-db -- bash -c 'pg_dump -Fc tensorhelm' > tensorhelm-$(date +%F).dump
# restore into an empty database:
oc exec -i -n tensorhelm statefulset/tensorhelm-db -- pg_restore -d tensorhelm --clean < tensorhelm-2026-10-01.dump
```

For production, use an operator-managed PostgreSQL with continuous backups (Crunchy Postgres for Kubernetes
or EDB). Point the API at it with the `TH_DATABASE_URL` environment variable.

## Rotate secrets

| Secret | How |
| --- | --- |
| Agent bootstrap token | `oc delete secret tensorhelm-agent-bootstrap -n tensorhelm`, re-run `install.sh`, re-run `register-cluster.sh` for clusters you will add later. Registered agents keep working: they use their own cluster tokens. |
| A cluster's agent token | Remove the cluster (`tensorhelm-admin remove-cluster <name>`), delete `tensorhelm-agent-credentials` on the spoke, restart the agent, approve again. |
| GitLab, Hugging Face or Model Registry token | `GITLAB_TOKEN=... HF_TOKEN=... ./scripts/install.sh [same options]` |
| Database password | Change it in PostgreSQL, then in the `tensorhelm-db` Secret, then restart the API. |

## Troubleshooting

| Symptom | Check | Fix |
| --- | --- | --- |
| Portal says "Sign in through the Tensorhelm portal" | You reached the API directly, not through the portal route | Use the portal URL |
| Signed in but everything is read-only | `oc get group tensorhelm-engineers -o yaml` | Add your user to the right group; wait 60 s |
| Login loops or "403 Forbidden" from the proxy | User is in none of the three groups | Add the user to a Tensorhelm group |
| Hub shows no GPUs | `oc get nodes -l nvidia.com/gpu.present=true` | GPU Operator not running, or nodes not labelled yet |
| Cluster stuck on "Agent not installed" | It is in RHACM but has no agent | Run `register-cluster.sh` for it |
| Agent never registers | `oc --context <spoke> logs -n tensorhelm-agent deploy/tensorhelm-agent` | The spoke cannot reach the agent route, or does not trust the hub certificate (see CLUSTERS.md) |
| "Agent offline" alert | Agent log on that cluster | The agent restarts itself; check egress to the hub |
| Deploy says "No vLLM serving runtime found" | `oc get templates -n redhat-ods-applications \| grep -i vllm` on that cluster | Enable KServe in the DataScienceCluster, or set `TH_VLLM_IMAGE` |
| Deploy says "Deploying to a remote cluster needs GitOps delivery" | Direct mode only serves the hub | Re-install with `--delivery gitops` |
| Endpoint stays "Starting" | `oc get pods -n team-<team>`, then `oc logs` of the predictor pod | Model download running (wait), gated model without token, or not enough GPU memory (choose FP8 or a smaller context) |
| GitOps deployment never appears | `oc get applicationset,application -n openshift-gitops` | Cluster missing the `tensorhelm.io/managed=true` label, or the repository secret is wrong |
