import json

from tensorhelm.main import refresh_once

from .conftest import gpu_node

BOOT = {"Authorization": "Bearer boot-secret"}


async def test_health_and_me(make_client):
    _, client = await make_client()
    assert (await client.get("/api/v1/health/live")).json() == {"status": "ok"}
    me = (await client.get("/api/v1/me")).json()
    assert me["role"] == "admin" and me["delivery_mode"] == "direct"


async def test_hub_inventory_and_overview(make_client, kube):
    app, client = await make_client()
    await refresh_once(app)
    overview = (await client.get("/api/v1/overview")).json()
    assert overview["kpis"]["gpus_total"] == 12
    assert overview["kpis"]["gpus_used"] == 3
    hub = overview["clusters"][0]
    assert hub["source"] == "hub" and hub["type"] == "gpu" and hub["status"] == "active"
    gpus = (await client.get("/api/v1/gpus")).json()
    assert {g["name"] for g in gpus} == {"gpu-1", "gpu-2"}


async def test_acm_managed_clusters_are_discovered(make_client, kube):
    kube.managed = [
        {
            "metadata": {"name": "local-cluster", "labels": {"tensorhelm.io/role": "serving"}},
            "status": {"conditions": [{"type": "ManagedClusterConditionAvailable", "status": "True"}]},
        },
        {
            "metadata": {"name": "prod-west", "labels": {"region": "eu"}},
            "status": {"conditions": [{"type": "ManagedClusterConditionAvailable", "status": "True"}]},
        },
    ]
    app, client = await make_client()
    await refresh_once(app)
    clusters = {c["name"]: c for c in (await client.get("/api/v1/clusters")).json()}
    assert clusters["local-cluster"]["source"] == "hub"
    assert clusters["local-cluster"]["labels"]["tensorhelm.io/role"] == "serving"
    assert clusters["prod-west"]["status"] == "managed"

    kube.managed = kube.managed[:1]
    await refresh_once(app)
    assert "prod-west" not in {c["name"] for c in (await client.get("/api/v1/clusters")).json()}


async def test_agent_registration_approval_and_report(make_client):
    _, client = await make_client()
    bad = await client.post(
        "/api/v1/agent/register", headers={"Authorization": "Bearer nope"}, json={"cluster_name": "edge-1"}
    )
    assert bad.status_code == 401

    reg = await client.post(
        "/api/v1/agent/register",
        headers=BOOT,
        json={"cluster_name": "edge-1", "facts": {"api_url": "https://api.edge-1:6443"}},
    )
    assert reg.status_code == 201
    token = reg.json()["token"]
    again = await client.post("/api/v1/agent/register", headers=BOOT, json={"cluster_name": "edge-1"})
    assert again.status_code == 409

    cluster = (await client.get("/api/v1/clusters/edge-1")).json()
    assert cluster["status"] == "pending"

    report = {
        "cluster_name": "edge-1",
        "agent_version": "0.1.0",
        "labels": {"region": "eu"},
        "facts": {"node_count": 3},
        "nodes": [],
        "endpoints": [],
        "runtimes": [],
    }
    wrong = await client.post("/api/v1/agent/report", headers={"Authorization": "Bearer tha_wrong"}, json=report)
    assert wrong.status_code == 401
    ok = await client.post("/api/v1/agent/report", headers={"Authorization": f"Bearer {token}"}, json=report)
    assert ok.json() == {"approved": False}

    assert (await client.post("/api/v1/clusters/edge-1/approve")).status_code == 200
    cluster = (await client.get("/api/v1/clusters/edge-1")).json()
    assert cluster["status"] == "active" and cluster["type"] == "cpu"

    audit = (await client.get("/api/v1/audit")).json()
    assert [a["action"] for a in audit][:2] == ["cluster.approved", "cluster.registered"]


async def test_registration_disabled_without_bootstrap_token(make_client):
    _, client = await make_client(agent_bootstrap_token="")
    response = await client.post("/api/v1/agent/register", headers=BOOT, json={"cluster_name": "edge-1"})
    assert response.status_code == 401


async def test_plan_and_direct_deploy_to_hub(make_client, kube):
    app, client = await make_client()
    await refresh_once(app)
    plan = await client.post(
        "/api/v1/deployments/plan", json={"model_id": "meta-llama/Llama-3.1-70B-Instruct", "precision": "fp8"}
    )
    [candidate] = plan.json()["candidates"]
    assert candidate["result"] == "fits" and candidate["tensor_parallel"] == 2

    created = await client.post(
        "/api/v1/deployments",
        json={
            "model_id": "meta-llama/Llama-3.1-70B-Instruct",
            "precision": "fp8",
            "name": "contracts-rag",
            "team": "legal",
            "cluster": "local-cluster",
            "max_model_len": 16384,
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "applied" and body["gpus_per_replica"] == 2
    assert [o["kind"] for o in kube.applied] == ["Namespace", "ServingRuntime", "InferenceService"]
    assert kube.applied[2]["metadata"]["namespace"] == "team-legal"

    duplicate = await client.post(
        "/api/v1/deployments",
        json={
            "model_id": "meta-llama/Llama-3.1-70B-Instruct",
            "precision": "fp8",
            "name": "contracts-rag",
            "team": "legal",
            "cluster": "local-cluster",
        },
    )
    assert duplicate.status_code == 409

    assert (await client.delete(f"/api/v1/deployments/{body['id']}")).status_code == 204
    assert ("InferenceService", "team-legal", "contracts-rag") in kube.deleted


async def test_deploy_refuses_when_model_does_not_fit(make_client, kube):
    kube.nodes = [gpu_node("small", product="NVIDIA-L4", count=1, memory_mib=23034)]
    kube.pods = []
    app, client = await make_client()
    await refresh_once(app)
    response = await client.post(
        "/api/v1/deployments",
        json={
            "model_id": "meta-llama/Llama-3.1-70B-Instruct",
            "name": "too-big",
            "team": "legal",
            "cluster": "local-cluster",
        },
    )
    assert response.status_code == 409
    assert "does not fit" in response.json()["detail"]


async def test_gitops_deploy_to_remote_cluster(make_client, gitlab_calls):
    app, client = await make_client(
        delivery_mode="gitops",
        gitlab_url="https://gitlab.example.com",
        gitlab_project="platform/tensorhelm-gitops",
        gitlab_token="glpat-x",
    )
    token = (await client.post("/api/v1/agent/register", headers=BOOT, json={"cluster_name": "prod-east"})).json()[
        "token"
    ]
    await client.post("/api/v1/clusters/prod-east/approve")
    from tensorhelm.inventory import parse_gpu_node, parse_runtime_template

    from .conftest import VLLM_TEMPLATE

    await client.post(
        "/api/v1/agent/report",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "cluster_name": "prod-east",
            "labels": {},
            "facts": {},
            "nodes": [parse_gpu_node(gpu_node("h100-a"), 0)],
            "endpoints": [],
            "runtimes": [parse_runtime_template(VLLM_TEMPLATE)],
        },
    )

    created = await client.post(
        "/api/v1/deployments",
        json={
            "model_id": "ibm-granite/granite-3.3-8b-instruct",
            "name": "support-copilot",
            "team": "support",
            "cluster": "prod-east",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "committed"
    assert created.json()["git_commit"] == "4f2a91c0ffee"
    commit = json.loads(gitlab_calls[0].content)
    paths = sorted(a["file_path"] for a in commit["actions"])
    assert paths == [
        "deployments/prod-east/support/namespace.yaml",
        "deployments/prod-east/support/support-copilot/inferenceservice.yaml",
        "deployments/prod-east/support/support-copilot/servingruntime.yaml",
    ]


async def test_remote_deploy_without_gitops_is_refused(make_client):
    _, client = await make_client()
    await client.post("/api/v1/agent/register", headers=BOOT, json={"cluster_name": "prod-east"})
    response = await client.post(
        "/api/v1/deployments",
        json={
            "model_id": "ibm-granite/granite-3.3-8b-instruct",
            "name": "x",
            "team": "support",
            "cluster": "prod-east",
        },
    )
    assert response.status_code == 409
    assert "GitOps" in response.json()["detail"]


async def test_proxy_auth_requires_shared_secret_and_maps_groups(make_client, kube):
    kube.groups = [{"metadata": {"name": "tensorhelm-engineers"}, "users": ["maria"]}]
    _, client = await make_client(auth_mode="proxy", proxy_shared_secret="s3cret")
    assert (await client.get("/api/v1/me", headers={"X-Forwarded-User": "maria"})).status_code == 401
    headers = {"X-Forwarded-User": "maria", "X-Tensorhelm-Proxy-Secret": "s3cret"}
    me = (await client.get("/api/v1/me", headers=headers)).json()
    assert me["name"] == "maria" and me["role"] == "engineer"
    viewer = {"X-Forwarded-User": "sam", "X-Tensorhelm-Proxy-Secret": "s3cret"}
    response = await client.post("/api/v1/clusters/anything/approve", headers=viewer)
    assert response.status_code == 403


async def test_catalog_curated_and_unreachable_source(make_client):
    _, client = await make_client()
    curated = (await client.get("/api/v1/catalog/models", params={"q": "granite"})).json()
    assert [m["id"] for m in curated["models"]] == ["ibm-granite/granite-3.3-8b-instruct"]
    hf = (await client.get("/api/v1/catalog/models", params={"source": "huggingface", "q": "llama"})).json()
    assert hf["models"] == [] and "Hugging Face" in hf["warning"]


async def test_agent_registers_stores_token_and_reports(make_client, kube, monkeypatch):
    from tensorhelm.agent import TOKEN_SECRET, AgentConfig, report_once

    _, client = await make_client()
    monkeypatch.setenv("TH_AGENT_HUB_URL", "http://test")
    monkeypatch.setenv("TH_AGENT_CLUSTER_NAME", "spoke-a")
    monkeypatch.setenv("TH_AGENT_BOOTSTRAP_TOKEN", "boot-secret")
    monkeypatch.setenv("TH_AGENT_LABELS", "region=eu,tensorhelm.io/role=serving")
    cfg = AgentConfig()

    token = await report_once(kube, cfg, client, None)
    assert kube.secrets[("tensorhelm-agent", TOKEN_SECRET, "token")] == token
    await client.post("/api/v1/clusters/spoke-a/approve")
    assert await report_once(kube, cfg, client, token) == token

    cluster = (await client.get("/api/v1/clusters/spoke-a")).json()
    assert cluster["status"] == "active"
    assert cluster["gpu_total"] == 12
    assert cluster["labels"]["region"] == "eu"
    assert cluster["runtimes"][0]["name"] == "vllm-cuda-runtime"
